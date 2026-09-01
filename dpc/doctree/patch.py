"""``apply_patch`` — the ONE implementation (SPEC-DOCTREE-1 R12).

``arrange`` (variant derivation) and ``treemd`` import this; nobody else re-implements it.
The upstream caller checks ``sha256_tree`` BEFORE calling (a mismatch is a 409 at the API
boundary — the patch was written against different bytes); this module's own contract is the
other half: an op that no longer applies raises :class:`PatchInvalid` and the ENTIRE patch is
abandoned — never partial application, because a partially applied patch with no error is
unauditable.

Semantics (§4.4, R11): ``move_before``/``move_after`` reposition a node (and its subtree)
among the ref's siblings; ``reparent`` makes the node the last child of a heading/section;
``merge_flow`` records a flow-join AND makes ``ref`` the immediate successor of ``node`` —
in this derived variant only, never in the stored heuristic artifact. After application the
variant is re-derived deterministically: pre-order ids, paths, parent/child refs and bbox
unions are all recomputed, and surviving flow edges are remapped to the new ids.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dpc.doctree.models import (
    PATH_TOKENS,
    DocTree,
    FlowEdge,
    Node,
    NodeKind,
)

#: Ops this implementation applies. ``split`` is ADVISORY_ONLY in v1 and ``flag_break`` is a
#: pure advisory — neither reaches ``apply_patch`` (the verifier routes them to the review
#: queue), so their presence here would be a lie about what the variant contains.
_MUTATING_OPS = frozenset({"move_before", "move_after", "reparent", "merge_flow"})

#: The full closed op vocabulary (§4.4). ``PatchInvalid`` messages may name an op ONLY when
#: it is in this set: the ``op`` field of an unknown op is unvalidated caller input, and
#: interpolating it verbatim would let an exception message carry arbitrary text.
_KNOWN_OPS = _MUTATING_OPS | frozenset({"split", "flag_break"})

#: Kinds a ``merge_flow`` endpoint may have (verifier V4's gate, re-checked here because
#: apply_patch is also reachable from ``treemd`` replay, not only from the verifier).
_FLOW_KINDS = frozenset({NodeKind.paragraph, NodeKind.list_item})

#: Kinds that may never be moved — the tree's fixed skeleton.
_ANCHORED = frozenset({NodeKind.document, NodeKind.body, NodeKind.furniture})


class PatchInvalid(ValueError):
    """An op no longer applies. The message carries op index, op name and rule id ONLY —
    paths are content-free, but even they are omitted to keep messages trivially safe."""

    def __init__(self, op_ix: int, op: str, rule: str) -> None:
        super().__init__(f"op[{op_ix}] {op}: {rule}")
        self.op_ix = op_ix
        self.op = op
        self.rule = rule


@dataclass(slots=True)
class _MN:
    """Mutable mirror of one node for the application pass."""

    node: Node
    children: list[_MN] = field(default_factory=list)
    parent: _MN | None = None


def apply_patch(
    tree: DocTree, ops: Sequence[Mapping[str, Any]]
) -> tuple[DocTree, frozenset[tuple[int, int]]]:
    """Apply verified, path-addressed ops to a tree — all of them, or none (R12).

    Args:
        tree: The heuristic tree the ops were verified against. The caller has already
            checked ``sha256_tree``; paths here are resolved against THIS tree.
        ops: Ops in canonical application order (§4.5), each a mapping with ``op``, ``node``
            and (except advisory ops, which must not be passed) ``ref`` path strings.

    Returns:
        ``(patched_tree, flow_joins)`` — a NEW ``DocTree`` with pre-order ids re-assigned,
        and the ``merge_flow`` joins as ``(src_id, dst_id)`` pairs in the new id space.

    Raises:
        PatchInvalid: An op does not apply (unknown path, structural impossibility). The
            tree argument is never mutated; no partial result exists.
    """
    mirrors = [_MN(node=node) for node in tree.nodes]
    for mirror in mirrors:
        for child_id in mirror.node.children:
            child = mirrors[child_id]
            child.parent = mirror
            mirror.children.append(child)
    by_path = {mirror.node.path: mirror for mirror in mirrors}

    joins: list[tuple[_MN, _MN]] = []
    for op_ix, op in enumerate(ops):
        name = str(op.get("op", ""))
        if name not in _MUTATING_OPS:
            # Name the op only from the closed vocabulary; anything else is unvalidated
            # input and must not reach the message (or the stored ``op`` attribute).
            raise PatchInvalid(op_ix, name if name in _KNOWN_OPS else "?", "unknown_op")
        node = by_path.get(str(op.get("node", "")))
        ref = by_path.get(str(op.get("ref", "")))
        if node is None or ref is None:
            raise PatchInvalid(op_ix, name, "unknown_path")
        _apply_one(op_ix, name, node, ref, joins)

    root = mirrors[0] if mirrors else None
    if root is None:
        return tree.model_copy(deep=True), frozenset()
    new_tree = _rederive(tree, root)
    new_ids = {id(m): ix for ix, m in enumerate(_preorder(root))}
    flow_joins = frozenset((new_ids[id(a)], new_ids[id(b)]) for a, b in joins)
    return new_tree, flow_joins


def _apply_one(
    op_ix: int, name: str, node: _MN, ref: _MN, joins: list[tuple[_MN, _MN]]
) -> None:
    """One op against the evolving state; every refusal is a named PatchInvalid rule."""
    if node.node.kind in _ANCHORED:
        raise PatchInvalid(op_ix, name, "anchored_node")
    if node is ref or _is_ancestor(node, ref):
        raise PatchInvalid(op_ix, name, "self_or_descendant_ref")

    if name == "reparent":
        if ref.node.kind not in (NodeKind.heading, NodeKind.section):
            raise PatchInvalid(op_ix, name, "reparent_target_kind")
        _detach(op_ix, name, node)
        ref.children.append(node)
        node.parent = ref
        return

    if name == "merge_flow":
        if node.node.kind not in _FLOW_KINDS or ref.node.kind not in _FLOW_KINDS:
            raise PatchInvalid(op_ix, name, "merge_flow_kind")
        if ref.node.kind is not node.node.kind:
            raise PatchInvalid(op_ix, name, "merge_flow_kind")
        # merge_flow moves REF (not node), so the dangerous ancestry runs the other way:
        # detaching an ancestor ref and reinserting it inside its own now-detached subtree
        # would orphan a cycle the re-derivation cannot reach.
        if _is_ancestor(ref, node):
            raise PatchInvalid(op_ix, name, "self_or_descendant_ref")
        # The join is recorded AND ref becomes the immediate successor (R11 — variant only).
        _detach(op_ix, name, ref)
        parent = node.parent
        if parent is None:
            raise PatchInvalid(op_ix, name, "orphan_node")
        parent.children.insert(parent.children.index(node) + 1, ref)
        ref.parent = parent
        joins.append((node, ref))
        return

    # move_before / move_after: node joins ref's siblings.
    parent = ref.parent
    if parent is None:
        raise PatchInvalid(op_ix, name, "ref_is_root")
    _detach(op_ix, name, node)
    at = parent.children.index(ref)
    parent.children.insert(at if name == "move_before" else at + 1, node)
    node.parent = parent


def _detach(op_ix: int, name: str, node: _MN) -> None:
    parent = node.parent
    if parent is None:
        raise PatchInvalid(op_ix, name, "orphan_node")
    parent.children.remove(node)
    node.parent = None


def _is_ancestor(candidate: _MN, node: _MN) -> bool:
    current = node.parent
    while current is not None:
        if current is candidate:
            return True
        current = current.parent
    return False


def _preorder(root: _MN) -> list[_MN]:
    out: list[_MN] = []
    stack = [root]
    while stack:
        mirror = stack.pop()
        out.append(mirror)
        stack.extend(reversed(mirror.children))
    return out


def _rederive(tree: DocTree, root: _MN) -> DocTree:
    """Deterministic re-derivation: pre-order ids, paths, parents, bbox unions, edge remap."""
    order = _preorder(root)
    new_ids = {id(m): ix for ix, m in enumerate(order)}

    # Paths: recomputed from the new sibling structure (per-kind 1-based counters).
    paths: dict[int, str] = {id(root): "//doc"}
    stack = [root]
    while stack:
        mirror = stack.pop()
        counts: dict[NodeKind, int] = {}
        for child in mirror.children:
            counts[child.node.kind] = counts.get(child.node.kind, 0) + 1
            token = PATH_TOKENS[child.node.kind]
            paths[id(child)] = f"{paths[id(mirror)]}/{token}[{counts[child.node.kind]}]"
            stack.append(child)

    # Bboxes/pages: recomputed bottom-up — a moved subtree changes every former and current
    # ancestor's union. A container's stored bbox is folded into its new union because the
    # intrinsic part (a figure's image rect) is not stored separately from the union; after a
    # move-out this over-approximates, never under-approximates, and leaf rects — the ones
    # anchors are minted from — stay exact.
    boxes: dict[int, tuple[int, int, int, int] | None] = {}
    pages: dict[int, int] = {}
    for mirror in reversed(order):
        rects = [
            box for box in (boxes[id(c)] for c in mirror.children) if box is not None
        ]
        if mirror.node.bbox is not None:
            rects.append(mirror.node.bbox)
        boxes[id(mirror)] = (
            (
                min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[2] for r in rects), max(r[3] for r in rects),
            )
            if rects else None
        )
        pages[id(mirror)] = min(
            [mirror.node.page, *(pages[id(c)] for c in mirror.children)]
        )

    nodes: list[Node] = []
    for ix, mirror in enumerate(order):
        nodes.append(mirror.node.model_copy(update={
            "id": ix,
            "path": paths[id(mirror)],
            "parent": new_ids[id(mirror.parent)] if mirror.parent is not None else None,
            "children": [new_ids[id(c)] for c in mirror.children],
            "bbox": boxes[id(mirror)],
            "page": pages[id(mirror)],
        }))

    # Flow edges: remap by old id -> new id; an edge whose endpoints a move has inverted no
    # longer satisfies I4 (src < dst) and is dropped — the annotation described the OLD
    # adjacency, and keeping a stale hint would be worse than losing it.
    old_to_new = {mirror.node.id: new_ids[id(mirror)] for mirror in order}
    flow: list[FlowEdge] = []
    for edge in tree.flow:
        src = old_to_new.get(edge.src)
        dst = old_to_new.get(edge.dst)
        if src is not None and dst is not None and src < dst:
            flow.append(edge.model_copy(update={"src": src, "dst": dst}))

    body_id = next((n.id for n in nodes if n.kind is NodeKind.body), tree.body)
    furniture_id = next(
        (n.id for n in nodes if n.kind is NodeKind.furniture), tree.furniture
    )
    counters = tree.counters.model_copy(update={"nodes": len(nodes), "edges": len(flow)})
    return tree.model_copy(update={
        "nodes": nodes,
        "flow": flow,
        "body": body_id,
        "furniture": furniture_id,
        "counters": counters,
    })


__all__ = ["PatchInvalid", "apply_patch"]
