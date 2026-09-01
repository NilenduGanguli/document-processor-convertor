"""The deterministic verifier — V1..V9 in order (SPEC-DOCTREE-1 §4.5), ``av1``.

Models propose; THIS decides. Every op gets a verdict, every rejection names its rule, and
re-running this module over the recorded samples of a stored artifact reproduces the verdict
list byte-for-byte — the determinism claim, precisely: every stored byte is either produced
deterministically or recorded verbatim as the input that produced it.

R10 is enforced structurally: the geometric-plausibility question of V6 is answered by
``dpc.doctree.continuity.continuation_score`` — imported, never restated. This module owns
NO scoring formula, and a test reads this file's source to prove it stays that way.

Rule order is verdict order: the FIRST failing rule names the verdict. V9 discards whole
degenerate samples before voting; V8 is the vote itself; V1–V7 run per candidate op against
the EVOLVING simulated tree state, in the canonical application order
``(page, op_rank, node_ordinal, ref_ordinal)`` — so a later op is judged in the world the
earlier accepted ops created, exactly as ``apply_patch`` will replay them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dpc.arrange.features import NodeFeature, _page_ems
from dpc.arrange.ops import MUTATING_OPS, OpName, ParsedSample, RawOp, Reason
from dpc.arrange.payload import Window
from dpc.doctree.continuity import (
    CONT_CONFIRM_MIN,
    continuation_score,
    features_from_metrics,
)
from dpc.doctree.models import DocTree, NodeKind
from dpc.models import LayoutView

#: §4.5: the verifier's version, stamped into every artifact. Every constant below is
#: versioned under it — a retune is a visible version bump, never a silent behaviour change.
VERIFIER_VERSION = "av1"

#: R9: running headers/footers occupy ~0.75-1 in of an 11 in page (68-91 permille); 90 covers
#: the range with OCR jitter. A furniture block reparented into body must sit OUTSIDE both
#: margin bands — inside them, "furniture" was almost certainly the right call.
FURNITURE_MARGIN_PM = 90

#: V9: a sample proposing more than this many MUTATING ops in one window (25% of the 48-node
#: cap) is a bug report about the heuristics, not a reviewable edit list — discarded whole.
RUNAWAY_MAX = 12

#: V8: edit-level self-consistency — accept at >= 2 of the k=3 samples.
MAJORITY_MIN = 2

#: V6: two same-frame nodes separated vertically by more than this many ems are CLEARLY
#: stacked; their order is a fact about the page, not a tie, and a move inverting it is
#: rejected. Two ems is about two lines of leading — under it, ties are arguable.
INVERSION_GAP_EM = 2

#: Canonical application order's op rank (§4.5). Advisory ops sort after mutating ones so
#: the verdict list keeps one total order; they are never "applied" at all.
OP_RANK: dict[OpName, int] = {
    OpName.reparent: 0,
    OpName.move_before: 1,
    OpName.move_after: 2,
    OpName.merge_flow: 3,
    OpName.split: 4,
    OpName.flag_break: 5,
}

#: R15's interposer kinds — leaves that may sit between two flow-linked paragraphs without
#: breaking adjacency. Mirrors the builder's gate (the values are spec §3.3's, not private
#: builder state, so they are restated rather than imported from a builder internal).
_INTERPOSERS = frozenset({
    NodeKind.figure, NodeKind.caption, NodeKind.mark, NodeKind.kv_group,
    NodeKind.kv_pair, NodeKind.footnote,
})

#: The tree's fixed skeleton — never a valid op target.
_ANCHORED = frozenset({NodeKind.document, NodeKind.body, NodeKind.furniture})

#: §5.3's fence triggers: a flow_group whose subtree holds any of these kinds renders as a
#: canvas fence, and "fence-rendered regions are opaque to ops (verifier V4 boundary)".
#: The values are spec §5.3's, mirrored from ``dpc.treemd._FENCE_TRIGGERS`` rather than
#: imported: treemd is Phase 2's module and this pass must verify without it on disk.
_FENCE_KINDS = frozenset({
    NodeKind.kv_group, NodeKind.kv_pair, NodeKind.mark, NodeKind.table,
})


@dataclass(frozen=True, slots=True)
class AcceptedOp:
    """One accepted mutating op with its canonical application-order key."""

    key: tuple[int, int, int, int]
    op: dict[str, Any]


@dataclass(slots=True)
class VerifiedWindow:
    """One window's verification output — everything the artifact records."""

    verdicts: list[dict[str, Any]]
    accepted: list[AcceptedOp]
    review: list[dict[str, Any]]
    #: V9 per-sample discard reasons, index-aligned with the input samples; ``None`` for a
    #: sample that voted (or was already discarded upstream at parse time).
    sample_discards: list[str | None]


class _Sim:
    """The simulated tree state V3/V6 re-check against — parent/children id arrays only."""

    def __init__(self, tree: DocTree) -> None:
        self.kinds = [n.kind for n in tree.nodes]
        self.parents: list[int | None] = [n.parent for n in tree.nodes]
        self.children: list[list[int]] = [list(n.children) for n in tree.nodes]

    def is_ancestor(self, ancestor: int, node: int) -> bool:
        current = self.parents[node]
        while current is not None:
            if current == ancestor:
                return True
            current = self.parents[current]
        return False

    def boundary_root(self, node: int, *, include_self: bool) -> int | None:
        """Topmost table/figure on the ancestor chain — V4's opaque-subtree boundary."""
        found: int | None = None
        current: int | None = node if include_self else self.parents[node]
        while current is not None:
            if self.kinds[current] in (NodeKind.table, NodeKind.figure):
                found = current
            current = self.parents[current]
        return found

    def fence_root(self, node: int, *, include_self: bool) -> int | None:
        """Topmost fence-rendering flow_group on the ancestor chain — §5.3's opacity
        boundary, evaluated against the EVOLVING simulated subtree (an earlier accepted op
        may have made a prose group fence-triggering)."""
        found: int | None = None
        current: int | None = node if include_self else self.parents[node]
        while current is not None:
            if (
                self.kinds[current] is NodeKind.flow_group
                and self._triggers_fence(current)
            ):
                found = current
            current = self.parents[current]
        return found

    def _triggers_fence(self, group: int) -> bool:
        stack = list(self.children[group])
        while stack:
            nid = stack.pop()
            if self.kinds[nid] in _FENCE_KINDS:
                return True
            stack.extend(self.children[nid])
        return False

    def in_furniture(self, node: int, furniture_id: int) -> bool:
        return node == furniture_id or self.is_ancestor(furniture_id, node)

    def _detach(self, node: int) -> None:
        parent = self.parents[node]
        if parent is not None:
            self.children[parent].remove(node)
            self.parents[node] = None

    def apply(self, op: OpName, node: int, ref: int) -> None:
        """Apply one ACCEPTED op — mirrors ``patch.apply_patch`` semantics exactly."""
        if op is OpName.reparent:
            self._detach(node)
            self.children[ref].append(node)
            self.parents[node] = ref
            return
        if op is OpName.merge_flow:
            self._detach(ref)
            parent = self.parents[node]
            assert parent is not None  # V3 checked before apply
            at = self.children[parent].index(node)
            self.children[parent].insert(at + 1, ref)
            self.parents[ref] = parent
            return
        parent = self.parents[ref]
        assert parent is not None  # V3 checked before apply
        self._detach(node)
        at = self.children[parent].index(ref)
        self.children[parent].insert(at if op is OpName.move_before else at + 1, node)
        self.parents[node] = parent


@dataclass(slots=True)
class _Ctx:
    """Per-window verification context, built once."""

    tree: DocTree
    window: Window
    feats: dict[str, NodeFeature]
    ems: dict[int, int]
    sim: _Sim


def _ordinal(ctx: _Ctx, nid: str) -> int:
    """Total sort ordinal for a window id: the tree id when known; unknown ids sort after
    every real node by their numeric part — an intrinsic property of the id, never an
    arrival index (the placement-sort lesson)."""
    tree_id = ctx.window.id_map.get(nid)
    if tree_id is not None:
        return tree_id
    digits = nid[1:]
    return 10**9 + (int(digits) if digits.isdigit() else 10**8)


def _page_of(ctx: _Ctx, nid: str) -> int:
    tree_id = ctx.window.id_map.get(nid)
    return ctx.tree.nodes[tree_id].page if tree_id is not None else 0


def _addr(ctx: _Ctx, nid: str) -> str:
    """Resolve a window id to its canonical path (R3); an unknown id stays as the verbatim
    ``n{k}`` token — content-free either way, and the verdict names WHY it is unknown."""
    tree_id = ctx.window.id_map.get(nid)
    return ctx.tree.nodes[tree_id].path if tree_id is not None else nid


def _op_record(ctx: _Ctx, op: RawOp) -> dict[str, Any]:
    record: dict[str, Any] = {
        "op": op.op.value,
        "node": _addr(ctx, op.node),
        "reason": op.reason.value,
    }
    if op.ref is not None:
        record["ref"] = _addr(ctx, op.ref)
    if op.confidence_pm is not None:
        record["confidence_pm"] = op.confidence_pm
    return record


# ---------------------------------------------------------------------------------------
# The rules. Each returns a REJECT verdict name or None; first non-None names the verdict.
# ---------------------------------------------------------------------------------------
def _cross_page_gate(ctx: _Ctx, op: RawOp) -> bool:
    """V5's one legitimate cross-page relation: frame-bottom into next page's frame-top."""
    node_f = ctx.feats.get(op.node)
    ref_f = ctx.feats.get(op.ref or "")
    return (
        node_f is not None
        and ref_f is not None
        and ref_f.page == node_f.page + 1
        and node_f.at_frame_bottom
        and ref_f.at_frame_top
    )


def _v1_unknown_id(ctx: _Ctx, op: RawOp) -> str | None:
    if op.node not in ctx.window.id_map:
        return "REJECT_UNKNOWN_ID"
    if op.ref is not None and op.ref not in ctx.window.id_map:
        return "REJECT_UNKNOWN_ID"
    return None


def _v2_context(ctx: _Ctx, op: RawOp) -> str | None:
    ctx_ids = ctx.window.context_ids
    if op.node in ctx_ids:
        # R6: a context node MAY be the SOURCE of merge_flow into an in-window,
        # non-context ref, when the cross-page geometric gate holds.
        allowed = (
            op.op is OpName.merge_flow
            and op.ref is not None
            and op.ref not in ctx_ids
            and _cross_page_gate(ctx, op)
        )
        if not allowed:
            return "REJECT_CONTEXT_TARGET"
    if op.ref in ctx_ids and op.op is not OpName.merge_flow:
        # Context nodes may otherwise appear only as ref of merge_flow.
        return "REJECT_CONTEXT_TARGET"
    return None


def _v3_orphan(ctx: _Ctx, op: RawOp) -> str | None:
    if op.op not in MUTATING_OPS:
        return None
    node = ctx.window.id_map[op.node]
    ref = ctx.window.id_map[op.ref or ""]
    sim = ctx.sim
    if sim.kinds[node] in _ANCHORED or sim.kinds[ref] in _ANCHORED:
        return "REJECT_ORPHAN"
    if node == ref or sim.is_ancestor(node, ref):
        return "REJECT_ORPHAN"
    if sim.parents[node] is None:
        return "REJECT_ORPHAN"
    if op.op is OpName.reparent:
        if sim.kinds[ref] not in (NodeKind.heading, NodeKind.section):
            return "REJECT_ORPHAN"
    elif sim.parents[ref] is None:
        return "REJECT_ORPHAN"
    return None


def _v4_type(ctx: _Ctx, op: RawOp) -> str | None:
    if op.op not in MUTATING_OPS:
        return None
    node = ctx.window.id_map[op.node]
    ref = ctx.window.id_map[op.ref or ""]
    sim = ctx.sim

    if op.op is OpName.merge_flow:
        flow_kinds = (NodeKind.paragraph, NodeKind.list_item)
        if sim.kinds[node] not in flow_kinds or sim.kinds[node] is not sim.kinds[ref]:
            return "REJECT_TYPE"

    # Moves may not cross a table/figure subtree boundary: the node's opaque root
    # (ancestors only — moving a whole figure is legal) must be the destination's.
    dest = sim.parents[ref] if op.op in (OpName.move_before, OpName.move_after) else ref
    src_boundary = sim.boundary_root(node, include_self=False)
    dst_boundary = (
        sim.boundary_root(dest, include_self=True) if dest is not None else None
    )
    if src_boundary != dst_boundary:
        return "REJECT_TYPE"

    # §5.3: fence-rendered regions are opaque to ops (the V4 boundary the spec names).
    # The fence paints only the region's own atoms, so a node moved INTO a fenced
    # flow_group would be silently swallowed (its text vanishes from the derived variant)
    # and one moved OUT would be duplicated. Same shape as the table/figure rule: the
    # node's fenced root (ancestors only — moving a whole fenced panel is legal) must be
    # the destination's, judged on the evolving simulated state.
    src_fence = sim.fence_root(node, include_self=False)
    dst_fence = (
        sim.fence_root(dest, include_self=True) if dest is not None else None
    )
    if src_fence != dst_fence:
        return "REJECT_TYPE"

    if op.op is OpName.reparent and sim.in_furniture(node, ctx.tree.furniture):
        # R9: pulling furniture into body demands the named reason AND a rect clear of both
        # margin bands — inside them, the furniture call was almost certainly right.
        if op.reason is not Reason.FURNITURE_MISPLACED:
            return "REJECT_TYPE"
        rect = ctx.tree.nodes[node].bbox
        dims = {p.page: p.height_mu for p in ctx.tree.pages}
        height = dims.get(ctx.tree.nodes[node].page, 0)
        if rect is None or height <= 0:
            return "REJECT_TYPE"
        margin = FURNITURE_MARGIN_PM * height // 1000
        if rect[1] < margin or rect[3] > height - margin:
            return "REJECT_TYPE"
    return None


def _v5_page_cross(ctx: _Ctx, op: RawOp) -> str | None:
    if op.ref is None:
        return None
    node_page = _page_of(ctx, op.node)
    ref_page = _page_of(ctx, op.ref)
    if node_page == ref_page:
        return None
    if op.op is OpName.merge_flow and _cross_page_gate(ctx, op):
        return None
    return "REJECT_PAGE_CROSS"


def _v6_geometry(ctx: _Ctx, op: RawOp) -> str | None:
    if op.op not in MUTATING_OPS:
        return None
    node_id = ctx.window.id_map[op.node]
    ref_id = ctx.window.id_map[op.ref or ""]
    node = ctx.tree.nodes[node_id]
    ref = ctx.tree.nodes[ref_id]
    if node.bbox is None or ref.bbox is None:
        return "REJECT_GEOMETRY"  # geometry-null nodes fail V6 for all mutating ops.

    if op.op is OpName.merge_flow:
        if node.metrics is None or ref.metrics is None:
            return "REJECT_GEOMETRY"
        em = ctx.ems.get(node.page, 0)
        src = features_from_metrics(
            node.metrics, width_mu=node.bbox[2] - node.bbox[0], em=em
        )
        dst = features_from_metrics(
            ref.metrics, width_mu=ref.bbox[2] - ref.bbox[0],
            em=ctx.ems.get(ref.page, em),
        )
        adjacency = _adjacent(ctx, op, node_id, ref_id)
        # R10: the ONE rubric, imported — confirm-plausible, never create-impossible.
        if continuation_score(src, dst, adjacency) < CONT_CONFIRM_MIN:
            return "REJECT_GEOMETRY"
        return None

    if op.op in (OpName.move_before, OpName.move_after):
        return _v6_inversion(ctx, op, node_id, ref_id)
    return None


def _adjacent(ctx: _Ctx, op: RawOp, node_id: int, ref_id: int) -> bool:
    """Adjacency for the rubric: cross-page gate, same-page frame-edge pair, or an
    interposed pair (R15) — same column, only interposer-kind leaves between in id order."""
    if _cross_page_gate(ctx, op):
        return True
    node = ctx.tree.nodes[node_id]
    ref = ctx.tree.nodes[ref_id]
    node_f = ctx.feats.get(op.node)
    ref_f = ctx.feats.get(op.ref or "")
    if node_f is None or ref_f is None or node.page != ref.page:
        return False
    node_col = (node.prov.region_ix, node.prov.frame_ix)
    ref_col = (ref.prov.region_ix, ref.prov.frame_ix)
    if node_col != ref_col:
        return node_f.at_frame_bottom and ref_f.at_frame_top
    lo, hi = sorted((node_id, ref_id))
    for between_id in range(lo + 1, hi):
        between = ctx.tree.nodes[between_id]
        if between.children:
            continue  # containers between are structure, not rendered interposition.
        if between.kind is NodeKind.paragraph or between.kind not in _INTERPOSERS:
            return False
    return True


def _v6_inversion(ctx: _Ctx, op: RawOp, node_id: int, ref_id: int) -> str | None:
    """A move that inverts a strict geometric order: same frame, gap > 2 em, neither node
    uncertain — clearly stacked same-frame text has one true order; only ties are arguable."""
    node = ctx.tree.nodes[node_id]
    ref = ctx.tree.nodes[ref_id]
    node_f = ctx.feats.get(op.node)
    ref_f = ctx.feats.get(op.ref or "")
    if node.page != ref.page:
        return None
    if (node.prov.region_ix, node.prov.frame_ix) != (ref.prov.region_ix, ref.prov.frame_ix):
        return None
    if (node_f is not None and node_f.uncertain) or (ref_f is not None and ref_f.uncertain):
        return None
    assert node.bbox is not None and ref.bbox is not None  # checked by caller
    upper_id, lower_id = (
        (node_id, ref_id) if node.bbox[1] <= ref.bbox[1] else (ref_id, node_id)
    )
    upper = ctx.tree.nodes[upper_id]
    lower = ctx.tree.nodes[lower_id]
    gap = lower.bbox[1] - upper.bbox[3]  # type: ignore[index]
    em = ctx.ems.get(node.page, 0)
    if em <= 0 or gap <= INVERSION_GAP_EM * em:
        return None
    inverts = (op.op is OpName.move_before and node_id == lower_id) or (
        op.op is OpName.move_after and node_id == upper_id
    )
    return "REJECT_GEOMETRY" if inverts else None


# ---------------------------------------------------------------------------------------
# verify_window
# ---------------------------------------------------------------------------------------
def verify_window(
    tree: DocTree,
    view: LayoutView,
    window: Window,
    samples: list[ParsedSample],
) -> VerifiedWindow:
    """V1..V9 over one window's samples — pure and deterministic (the replay property).

    Args:
        tree: The heuristic tree the window was cut from.
        view: The layout view (per-page em for V6's thresholds; no text is read).
        window: The window, with its ``id_map`` and context set.
        samples: One :class:`ParsedSample` per model call, index = sample_ix. Samples
            discarded at parse time arrive with ``ops=None`` and never vote.

    Returns:
        A :class:`VerifiedWindow`: verdicts in canonical order, accepted mutating ops with
        their application-order keys, the review queue, and V9's per-sample discards.
    """
    pages = sorted({n.page for n in tree.nodes})
    ctx = _Ctx(
        tree=tree,
        window=window,
        feats={n.id: n for n in window.payload.nodes},
        ems=_page_ems(view, pages),
        sim=_Sim(tree),
    )

    # V9 — degenerate samples are discarded whole, before any vote is counted.
    sample_discards: list[str | None] = []
    usable: list[tuple[int, tuple[RawOp, ...]]] = []
    for sample_ix, sample in enumerate(samples):
        if sample.ops is None:
            sample_discards.append(None)  # parse-level discard, recorded by the runner.
            continue
        mutating = sum(1 for op in sample.ops if op.op in MUTATING_OPS)
        if mutating > RUNAWAY_MAX:
            sample_discards.append("runaway_ops")
            continue
        sample_discards.append(None)
        usable.append((sample_ix, sample.ops))

    # Vote aggregation over the canonical identity (op, node, ref) — V8's ballot box.
    votes: dict[tuple[str, str, str], int] = {}
    first: dict[tuple[str, str, str], RawOp] = {}
    for _, sample_ops in usable:
        seen: set[tuple[str, str, str]] = set()
        for op in sample_ops:
            identity = op.identity()
            if identity in seen:
                continue  # one sample, one vote per identity.
            seen.add(identity)
            votes[identity] = votes.get(identity, 0) + 1
            if identity not in first:
                first[identity] = op

    candidates = sorted(
        first.values(),
        key=lambda op: (
            _page_of(ctx, op.node),
            OP_RANK[op.op],
            _ordinal(ctx, op.node),
            _ordinal(ctx, op.ref) if op.ref is not None else -1,
        ),
    )

    verdicts: list[dict[str, Any]] = []
    accepted: list[AcceptedOp] = []
    review: list[dict[str, Any]] = []
    for op in candidates:
        verdict, rule = _judge(ctx, op, votes[op.identity()])
        verdicts.append({
            "op": _op_record(ctx, op),
            "votes": votes[op.identity()],
            "verdict": verdict,
            "rule": rule,
        })
        if verdict == "ACCEPTED":
            node_id = ctx.window.id_map[op.node]
            ref_id = ctx.window.id_map[op.ref or ""]
            ctx.sim.apply(op.op, node_id, ref_id)
            accepted.append(AcceptedOp(
                key=(_page_of(ctx, op.node), OP_RANK[op.op], node_id, ref_id),
                op={
                    "op": op.op.value,
                    "node": ctx.tree.nodes[node_id].path,
                    "ref": ctx.tree.nodes[ref_id].path,
                    "reason": op.reason.value,
                },
            ))
        elif verdict == "ADVISORY" and op.op is OpName.flag_break:
            review.append({
                "after": _addr(ctx, op.node),
                "confidence_pm": op.confidence_pm,
                "reason": op.reason.value,
            })
    return VerifiedWindow(
        verdicts=verdicts, accepted=accepted, review=review,
        sample_discards=sample_discards,
    )


def _judge(ctx: _Ctx, op: RawOp, op_votes: int) -> tuple[str, str | None]:
    """One candidate through V1..V8 in order; the first failure names the verdict."""
    for rule_id, check in (
        ("V1", _v1_unknown_id),
        ("V2", _v2_context),
        ("V3", _v3_orphan),
        ("V4", _v4_type),
        ("V5", _v5_page_cross),
        ("V6", _v6_geometry),
    ):
        failure = check(ctx, op)
        if failure is not None:
            return failure, rule_id
    if op.op is OpName.flag_break:
        # V7 — advisory confidence gate: below 700 permille it is recorded, never queued.
        if op.confidence_pm is None or op.confidence_pm < 700:
            return "REJECT_LOW_CONFIDENCE", "V7"
        return "ADVISORY", None
    if op.op is OpName.split:
        return "ADVISORY", None  # ADVISORY_ONLY in v1 (§4.4) — recorded, applied nowhere.
    if op_votes < MAJORITY_MIN:
        return "REJECT_NO_MAJORITY", "V8"
    return "ACCEPTED", None


__all__ = [
    "FURNITURE_MARGIN_PM",
    "INVERSION_GAP_EM",
    "MAJORITY_MIN",
    "OP_RANK",
    "RUNAWAY_MAX",
    "VERIFIER_VERSION",
    "AcceptedOp",
    "VerifiedWindow",
    "verify_window",
]
