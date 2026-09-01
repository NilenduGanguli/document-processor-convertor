"""Windowing + canonical request payloads (SPEC-DOCTREE-1 §4.3, §10).

One request per window. The serialized window payload is canonical JSON (sorted keys, ASCII,
compact separators — the same canonical form as every stored artifact in this repo), and its
``payload_sha256`` is stamped into the arrangement artifact: the disclosable, replayable
proof of exactly what left us. In MULTIMODAL mode (§10, owner-authorized) a window may also
carry its page's PNG — the image travels on the request and its sha256 travels in the
artifact, but the bytes themselves are NEVER stored in any artifact.

Windowing rules (§4.3): per-page, at most ``arrange_max_window`` (default 48) nodes per
window — LayoutGPT-class ordering degrades past a few dozen objects, and 48 nodes is roughly
1.5K tokens; splits prefer band boundaries (band-atomic: a band never straddles a split when
a boundary exists to cut at); each window after the first carries the previous window's last
4 nodes as ``context:true`` carry-overs, which is what lets a cross-page continuation span a
window seam at all (R6).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from dpc.arrange.features import NId, NodeFeature, build_features
from dpc.doctree.models import DocTree, NodeKind, walk_body
from dpc.models import LayoutView

#: Payload schema id — versioned like every other stored/disclosed shape.
PAYLOAD_SCHEMA = "dpc-arrange-window/1"

#: §4.3: frozen prompt template version, stamped into the artifact. Bumping the template is
#: a visible version bump, never a silent prompt drift.
PROMPT_TEMPLATE_VERSION = "ap1"

#: The frozen system prompt (§4.3). Task, one-line feature glossary, the op grammar, the
#: prefer-no-ops instruction, and the context-node rules including the R6 merge_flow
#: exception. Nothing else is ever concatenated into a request — template + payload is the
#: whole outgoing body, which is what the transport-level n-gram tripwire captures.
PROMPT_TEMPLATE = (
    "You review the READING ORDER of one page window of a document layout tree. "
    "You see structural features only, never text: ids (n0..), tree paths, kinds, page/band/"
    "frame coordinates, the alignment-anchored edge position (anchor_pm, permille), coarse "
    "width/char/line buckets, punctuation booleans, relative height class, script and digit "
    "classes, frame-top/bottom flags. `order` is the current heuristic order; "
    "`succ_uncertain` lists pairs whose order was a coin toss.\n"
    "Respond with STRICT JSON only: {\"schema\": \"dpc-arrange-ops/1\", \"ops\": [...]}. "
    "Allowed ops: move_before, move_after, reparent (ref must be a heading/section), "
    "merge_flow (node continues into ref), split (advisory), flag_break (advisory, "
    "confidence_pm 0..1000). Each op names node/ref by window id and one reason from: "
    "COLUMN_CONTINUATION, PAGE_CONTINUATION, INTERRUPTED_FLOW, ORDER_INVERSION, "
    "SIDEBAR_DEFERRED, FURNITURE_MISPLACED, CAPTION_DETACHED, TABLE_FRAGMENT, "
    "LIST_CONTINUATION, HEADING_SCOPE, OTHER_STRUCTURAL.\n"
    "Prefer NO ops when the order is plausible. Nodes marked context:true are carry-overs "
    "from the previous window: never target them, with ONE exception — a context node may "
    "be the SOURCE of merge_flow into an in-window ref when the source sits at a frame "
    "bottom and the ref at the next page's frame top (cross-page continuation)."
)

#: §4.3: nodes per window cap (settings ``arrange_max_window`` overrides).
ARRANGE_MAX_WINDOW = 48

#: §4.3: context carry-over nodes per window seam.
CONTEXT_CARRY = 4

#: The ``NId`` grammar (``n0``..``n99``) caps window-local ids at this many; context
#: carry-overs share the id space, so a window may hold at most ``100 - CONTEXT_CARRY``
#: OWN nodes no matter what ``arrange_max_window`` is configured to — an unclamped cap
#: would make the payload model refuse (``n100`` fails the pattern) and cost the whole
#: pass a doc-wide ValidationError artifact.
_NID_SPACE = 100


class WindowPayload(BaseModel):
    """The complete request payload for one window — closed types only (§4.2)."""

    model_config = {"populate_by_name": True, "extra": "forbid"}

    schema_: Annotated[
        str, StringConstraints(pattern=r"^dpc-arrange-window/[0-9]+$")
    ] = Field(default=PAYLOAD_SCHEMA, alias="schema")
    page: int
    nodes: list[NodeFeature] = Field(default_factory=list)
    #: The heuristic order, in-window (context carry-overs included, in sequence).
    order: list[NId] = Field(default_factory=list)
    #: Coin-toss pairs from ``tree.report.order_ties`` with both ends in this window.
    succ_uncertain: list[tuple[NId, NId]] = Field(default_factory=list)


@dataclass(slots=True)
class Window:
    """One window: the payload plus the private bookkeeping the verifier needs.

    Only ``payload_bytes`` (and, in multimodal mode, ``image_png``) ever leave the process;
    ``id_map`` is ours — the model emits ``n{k}``, the verifier resolves to tree ids and
    stores canonical paths (R3).
    """

    window_ix: int
    page: int
    #: ``(lowest, highest)`` tree node id among the window's OWN (non-context) nodes.
    node_span: tuple[int, int]
    payload: WindowPayload
    payload_bytes: bytes
    payload_sha256: str
    #: ``n{k}`` -> tree node id, context carry-overs included.
    id_map: dict[str, int] = field(default_factory=dict)
    #: The ``n{k}`` ids that are context carry-overs (V2's subjects).
    context_ids: frozenset[str] = frozenset()
    #: §10: the window page's PNG, attached to the REQUEST only — never to any artifact.
    image_png: bytes | None = None
    #: sha256 of ``image_png`` — the auditable record of the disclosure, without a copy.
    image_sha256: str | None = None


def payload_bytes_of(payload: WindowPayload) -> bytes:
    """Canonical bytes for one window payload — what ``payload_sha256`` hashes.

    Same canonical form as :func:`dpc.doctree.models.dump_tree`: ``exclude_none`` +
    ``sort_keys`` + ``ensure_ascii`` + compact separators, so the disclosed-bytes proof is
    writer-independent.
    """
    dumped = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(
        dumped, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def _page_runs(tree: DocTree) -> list[tuple[int, list[int]]]:
    """Contiguous per-page runs of the body pre-order (the body root itself excluded).

    Furniture LEAVES join their page's last run, after its body nodes: they are excluded
    from flow but must be addressable, because V4's furniture-reparent affordance (R9) is
    the model's only way to flag a mis-zoned block back into the body.
    """
    runs: list[tuple[int, list[int]]] = []
    for node in walk_body(tree):
        if node.kind is NodeKind.body:
            continue
        if runs and runs[-1][0] == node.page:
            runs[-1][1].append(node.id)
        else:
            runs.append((node.page, [node.id]))
    if 0 <= tree.furniture < len(tree.nodes):
        last_for_page = {run_page: run for run_page, run in runs}
        for leaf_id in tree.nodes[tree.furniture].children:
            if not (0 <= leaf_id < len(tree.nodes)):
                continue
            leaf_page = tree.nodes[leaf_id].page
            run = last_for_page.get(leaf_page)
            if run is None:
                runs.append((leaf_page, [leaf_id]))
                last_for_page[leaf_page] = runs[-1][1]
            else:
                run.append(leaf_id)
    return runs


def _split_band_atomic(
    ids: list[int], tree: DocTree, max_window: int
) -> list[list[int]]:
    """Split one run into chunks of <= max_window, cutting at band boundaries when one
    exists inside the over-full prefix (band-atomic, §4.3); a single giant band is cut hard
    at the cap rather than refused."""
    chunks: list[list[int]] = []
    start = 0
    while len(ids) - start > max_window:
        cut = start + max_window
        bands = [tree.nodes[i].prov.band_ix for i in ids[start:cut + 1]]
        boundary = None
        for offset in range(max_window, 0, -1):
            if bands[offset] != bands[offset - 1]:
                boundary = start + offset
                break
        cut = boundary if boundary is not None and boundary > start else cut
        chunks.append(ids[start:cut])
        start = cut
    chunks.append(ids[start:])
    return chunks


def make_windows(
    tree: DocTree,
    view: LayoutView,
    *,
    page_images: dict[int, bytes] | None = None,
    max_window: int = ARRANGE_MAX_WINDOW,
) -> list[Window]:
    """§4.3: per-page windows over the body pre-order, with context carry-overs.

    Args:
        tree: The heuristic tree.
        view: The layout view (per-page em only; no text read).
        page_images: §10 multimodal mode — PNG bytes per page. When a window's page has an
            image it is attached to the window (``image_png`` + ``image_sha256``); the
            structural payload bytes are IDENTICAL with or without it, so one recorded
            fixture set replays both modes.
        max_window: Node cap per window (``arrange_max_window``).

    Returns:
        Windows in document order. Empty when the body has no nodes.
    """
    feats = build_features(tree, view)
    tie_pairs = [(a, b) for a, b, _ in tree.report.order_ties]

    # Clamp to the NId grammar's ceiling: own nodes + CONTEXT_CARRY carry-overs must all
    # fit in n0..n99, whatever the configured cap says.
    max_window = min(max(1, max_window), _NID_SPACE - CONTEXT_CARRY)

    chunks: list[tuple[int, list[int]]] = []
    for page, run in _page_runs(tree):
        for chunk in _split_band_atomic(run, tree, max_window):
            if chunk:
                chunks.append((page, chunk))

    windows: list[Window] = []
    prev_tail: list[int] = []
    for window_ix, (page, own_ids) in enumerate(chunks):
        context_ids = [i for i in prev_tail if i in feats]
        nodes: list[NodeFeature] = []
        id_map: dict[str, int] = {}
        ctx_nids: set[str] = set()
        for k, node_id in enumerate([*context_ids, *own_ids]):
            nid = f"n{k}"
            is_ctx = k < len(context_ids)
            nodes.append(feats[node_id].model_copy(update={"id": nid, "context": is_ctx}))
            id_map[nid] = node_id
            if is_ctx:
                ctx_nids.add(nid)
        by_tree_id = {tree_id: nid for nid, tree_id in id_map.items()}
        payload = WindowPayload(
            page=page,
            nodes=nodes,
            order=[n.id for n in nodes],
            succ_uncertain=[
                (by_tree_id[a], by_tree_id[b])
                for a, b in tie_pairs
                if a in by_tree_id and b in by_tree_id
            ],
        )
        raw = payload_bytes_of(payload)
        image = (page_images or {}).get(page)
        windows.append(Window(
            window_ix=window_ix,
            page=page,
            node_span=(min(own_ids), max(own_ids)),
            payload=payload,
            payload_bytes=raw,
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            id_map=id_map,
            context_ids=frozenset(ctx_nids),
            image_png=image,
            image_sha256=hashlib.sha256(image).hexdigest() if image is not None else None,
        ))
        prev_tail = own_ids[-CONTEXT_CARRY:]
    return windows


__all__ = [
    "ARRANGE_MAX_WINDOW",
    "CONTEXT_CARRY",
    "PAYLOAD_SCHEMA",
    "PROMPT_TEMPLATE",
    "PROMPT_TEMPLATE_VERSION",
    "Window",
    "WindowPayload",
    "make_windows",
    "payload_bytes_of",
]
