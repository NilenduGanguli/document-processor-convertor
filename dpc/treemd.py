"""DocTree + LayoutView -> PMD 3.0, the tree-flattened markdown (SPEC-DOCTREE-1 §5).

PMD 3.0 is a NEW artifact stored beside the byte-untouched PMD 2.0 — two claims, two files,
two hashes (§5.1). Where 2.0's order is the provider's stream with a y-splice, 3.0's order is
the pre-order of the doctree's ``body`` — the tree IS the reading-order claim, and this module
only walks it. The design constraints, in the order they were traded against each other:

1. **Pure function of ``(tree, view, args)`` (R2).** The tree carries zero document strings
   (invariant I5); every character of output text is joined HERE by resolving ``block_ixs``/
   ``table_ix``/``kv_ix``/``mark_ix`` against the view. The tree pins ``view_sha256`` and this
   module refuses on mismatch — a tree flattened against the wrong view would emit the wrong
   document's text under the right document's paths, which is worse than no output.
2. **Leaves render EXACTLY as the emitter renders them.** ``_block_md``/``_table_md``/
   ``_clean``/``_anchor`` are imported from :mod:`dpc.emitter` — private helpers included,
   deliberately (see the import comment) — so a flat tree flattens element-for-element equal
   to the 2.0 linear rendering (§8.5) by sharing code, not by parallel reimplementation.
3. **Anchors stay 2.0-parseable.** The one change is an APPENDED `` path=<node-path>`` clause
   (§5.2): every existing anchor parser keeps working on 3.0 bytes, and the path is the
   artifact-to-artifact audit link back to the stored ``doctree.json``.
4. **Nothing raises out of ``flatten``.** Failures return a typed error in the
   :class:`FlattenReport` (``TreeInvalid:*`` / ``error:<Name>``) — fail closed on the NEW
   artifact, never on the conversion (and never with document text in the error).

Rendering rules (§5.2/§5.3):

- Page markers appear at the first body visit of each page — a flow annotation in 3.0, not a
  partition; per-element anchors are ground truth.
- ``continues`` flow edges (and caller-supplied ``flow_joins`` from an accepted patch) render
  the pair without an intervening blank line. Dehyphenation ships OFF (§5.2) until measured.
- A ``flow_group`` linearizes (frames in visit order, plain markdown) iff its subtree holds
  only prose kinds; a form panel (kv/mark/table present) renders as the existing canvas fence
  by re-running :func:`dpc.canvas.page_layout` and selecting the region recorded in
  ``prov.region_ix`` — possible precisely because the view is an input (R2), so no canvas rows
  are embedded in the tree and no drift can exist.
- A ``kv_pair`` whose key AND value are both visible on a covering canvas fence is suppressed
  and counted (``kv_in_canvas``) — the same ``_norm`` containment test the 2.0 emitter uses.
- The ``furniture`` root renders once after the body behind a ``<!-- furniture -->`` marker,
  children in stored (page, pre-order) order — demoted, not lost: anchors keep true page/rect.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from dpc.canvas import PageLayout, Region, page_layout, segment
from dpc.doctree.models import (
    DocTree,
    Node,
    NodeKind,
    ProvSource,
    tree_sha256,
    validate_tree,
    view_sha256,
)

# The emitter helpers below are PRIVATE by name but are imported on purpose: §5.2 requires
# per-kind rendering to reuse the emitter's functions VERBATIM, and importing them is the only
# arrangement in which the 2.0 and 3.0 bytes cannot drift apart (the same reasoning that
# promoted ``rect_scale`` to dpc/geom.py — R13). The coupling is one-way (emitter never
# imports treemd) and every name is re-tested here through the flat-tree ≡ 2.0 equivalence
# test, so a rename in the emitter fails loudly in this module's suite, not silently in bytes.
from dpc.emitter import (
    GENERATOR,
    _anchor,
    _block_md,
    _canvas_anchor,
    _clean,
    _clean_row,
    _fence,
    _kv_rect,
    _legend_lines,
    _mu_to_scale,
    _norm,
    _page_marker,
    _table_md,
    page_scale,
)
from dpc.geom import rect_scale
from dpc.models import LayoutView, Zone

#: The 3.0 format version — a new artifact generation, never an edit of 2.0's "pmd:" line.
PMD_VERSION_TREE = "3.0"

#: §5.3: a flow_group renders as a canvas fence iff any of these kinds appears in its subtree
#: (a form panel); otherwise it linearizes frame-by-frame (multi-column prose).
_FENCE_TRIGGERS = frozenset(
    {NodeKind.kv_group, NodeKind.kv_pair, NodeKind.mark, NodeKind.table}
)

#: Container kinds that render nothing themselves — their children carry the content.
_CONTAINERS = frozenset(
    {NodeKind.body, NodeKind.section, NodeKind.frame, NodeKind.kv_group, NodeKind.list_group}
)


@dataclass(frozen=True, slots=True)
class FlattenReport:
    """What one flatten emitted — the Phase-2 contract's typed result.

    ``error`` is ``None`` on success; on refusal it names the reason
    (``TreeInvalid:view_sha_mismatch``, ``TreeInvalid:I3:blocks``, ``error:<ExcName>``) and
    the markdown string is empty. Codes only, never document text (PII rule).
    """

    elements_emitted: int = 0
    figures: int = 0
    kv_in_canvas: int = 0
    furniture_nodes: int = 0
    #: Furniture leaves NOT re-emitted because every block of theirs is already painted as
    #: an atom of an emitted canvas fence (§8.3 exactly-once — same accounting as
    #: ``kv_in_canvas``, but by atom membership, exactly the 2.0 emitter's behaviour).
    furniture_in_canvas: int = 0
    pages_visited: int = 0
    error: str | None = None


def flatten(
    tree: DocTree,
    view: LayoutView,
    *,
    doc_id: str = "",
    source: str = "",
    provider: str = "",
    generated: str = "",
    extra: dict[str, Any] | None = None,
    decided_by: str = "heuristics",
    flow_joins: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[str, FlattenReport]:
    """DocTree + LayoutView -> PMD 3.0. Pure function of its arguments (R2); NEVER raises.

    Refuses — ``("", report)`` with ``report.error`` set, never an exception — when the view's
    sha does not match ``tree.view_sha256`` or when the tree fails I1–I5 against this view
    (I3 is the element census, so a stale tree fails closed here). Text joins happen in this
    function via the tree's stored indices; the tree itself carries no text (R1/I5).

    Args:
        tree: The stored doctree (heuristic, or a patched variant).
        view: The ``LayoutView`` the tree indexes into; must hash to ``tree.view_sha256``.
        doc_id: Caller's identifier; falls back to ``tree.doc_id`` when empty. Also the
            ``conversion_id`` of figure placeholder URIs.
        source: Front-matter ``source`` — same vocabulary as :func:`dpc.emitter.to_pmd`.
        provider: Front-matter ``provider``, e.g. ``azure_layout_v4``.
        generated: ISO timestamp, caller-supplied so this stays a pure function (2.0 rule).
        extra: Additional front-matter fields (e.g. ``sha256_input``), emitted sorted.
        decided_by: ``"heuristics"`` or ``"heuristics+patch@{sha8}"`` — the audit spine.
        flow_joins: Accepted ``merge_flow`` pairs ``(src_id, dst_id)`` from ``apply_patch``;
            rendered exactly like the tree's own ``continues`` edges (R11: adjacency in the
            patched variant is expressed by the caller having already repositioned ``dst``).

    Returns:
        ``(markdown, report)``. On refusal the markdown is ``""`` and ``report.error`` names
        the reason.
    """
    try:
        return _flatten(
            tree, view, doc_id=doc_id, source=source, provider=provider,
            generated=generated, extra=extra, decided_by=decided_by, flow_joins=flow_joins,
        )
    except Exception as exc:  # noqa: BLE001 - never-raises contract; class name only (PII).
        return "", FlattenReport(error=f"error:{type(exc).__name__}")


# ---------------------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------------------
class _Emit:
    """One flatten's mutable emission state (lines, counters, page markers, flow joins)."""

    def __init__(
        self,
        view: LayoutView,
        succ: dict[int, int],
        canvas_text: dict[tuple[int, int], frozenset[str]],
        canvas_blocks: dict[tuple[int, int], frozenset[int]],
        layouts: dict[int, PageLayout],
    ) -> None:
        self.view = view
        self.succ = succ
        self.canvas_text = canvas_text
        self.canvas_blocks = canvas_blocks
        self.layouts = layouts
        self.info = {p.page: p for p in view.pages}
        self.lines: list[str] = []
        self.marked: set[int] = set()
        self.last_leaf: int | None = None
        self.elements = 0
        self.figures = 0
        self.kv_in_canvas = 0
        self.furniture_nodes = 0
        self.furniture_in_canvas = 0
        self.canvases = 0

    def scale(self, page: int) -> int:
        pi = self.info.get(page)
        return page_scale(pi.unit if pi else "", "auto")

    def element(self, nid: int, page: int, anchor: str | None, md: str) -> None:
        """One rendered element: page marker on first visit, flow-join blank suppression,
        anchor line, markdown, trailing blank — the same shape as the 2.0 body list."""
        if page not in self.marked:
            self.marked.add(page)
            self.lines.append(_page_marker(page, self.info.get(page), self.scale(page)))
            self.lines.append("")
        if (
            self.last_leaf is not None
            and self.succ.get(self.last_leaf) == nid
            and self.lines and self.lines[-1] == ""
        ):
            # A continues-pair renders without an intervening blank line (§5.2). Rendering
            # adjacency only: the texts are never joined and never dehyphenated in v1.
            self.lines.pop()
        if anchor:
            self.lines.append(anchor)
        self.lines.append(md)
        self.lines.append("")
        self.last_leaf = nid
        self.elements += 1


def _path_anchor(page: int, rect: tuple[int, int, int, int] | None,
                 tag: str, path: str) -> str | None:
    """The 2.0 anchor with the one appended `` path=`` clause (§5.2). No geometry, no anchor
    — 2.0's rule survives verbatim, so a geometry-less element stays honestly unanchored."""
    base = _anchor(page, rect, tag)
    if base is None:
        return None
    return base[:-4] + f" path={path} -->"


def _mu_rect_scaled(
    rect: tuple[int, int, int, int] | None, scale: int
) -> tuple[int, int, int, int] | None:
    """A tree-stored mu rect in the page's anchor scale (figures — no view quad to rescale)."""
    if rect is None:
        return None
    x0, y0, x1, y1 = (_mu_to_scale(v, scale) for v in rect)
    return (x0, y0, x1, y1)


def _succ_map(tree: DocTree, flow_joins: frozenset[tuple[int, int]]) -> dict[int, int]:
    """``src -> dst`` over the tree's continues edges plus the caller's accepted joins.

    Built from a SORTED union so a (theoretical) duplicate src resolves the same way in every
    process — no set-iteration order in a decision path.
    """
    pairs = {(e.src, e.dst) for e in tree.flow if e.kind == "continues"}
    pairs.update(flow_joins)
    out: dict[int, int] = {}
    for src, dst in sorted(pairs):
        out.setdefault(src, dst)
    return out


def _fence_plan(
    tree: DocTree, view: LayoutView
) -> tuple[
    dict[int, tuple[int, int]],
    dict[tuple[int, int], frozenset[str]],
    dict[tuple[int, int], frozenset[int]],
    dict[int, PageLayout],
]:
    """Which flow_groups fence-render, and each fenced region's on-canvas text/atom sets.

    Returns ``(fenced, canvas_text, canvas_blocks, layouts)``:
    ``fenced[flow_group_id] = (page, region_ix)``, ``canvas_text[(page, region_ix)]`` = the
    ``_norm`` texts of the region's atoms — the input to the kv suppression rule —
    ``canvas_blocks[(page, region_ix)]`` = the block indices painted as line atoms (the
    furniture suppression rule's input), and the per-page layouts (computed once, reused by
    the fence renderer). Computed BEFORE the walk so suppression cannot depend on emission
    order. A flow_group whose recorded region cannot be recovered as a spatial region (a
    defensive case the builder should never produce) is simply not in ``fenced`` and
    linearizes — degraded looks, zero text loss.

    §5.3 defense-in-depth (fence opacity): a group whose subtree holds a content leaf the
    region's atoms do NOT paint — an immigrant paragraph from a patched tree that slipped
    past the verifier's V4 boundary, or any kv/figure leaf (never atoms) — also refuses to
    fence and linearizes. A fence that swallows an unpainted leaf's text is silent
    information loss; linearizing is only degraded looks.
    """
    fenced: dict[int, tuple[int, int]] = {}
    canvas_text: dict[tuple[int, int], frozenset[str]] = {}
    canvas_blocks: dict[tuple[int, int], frozenset[int]] = {}
    layouts: dict[int, PageLayout] = {}
    for node in _walk(tree, tree.body):
        if node.kind is not NodeKind.flow_group:
            continue
        if not _subtree_triggers_fence(tree, node):
            continue
        rix = node.prov.region_ix
        if rix is None:
            continue
        layout = layouts.get(node.page)
        if layout is None:
            layout = page_layout(view, node.page)
            layouts[node.page] = layout
        if not (0 <= rix < len(layout.regions)):
            continue
        region = layout.regions[rix]
        if region.kind != "spatial" or not region.rows:
            continue
        if not _subtree_painted_by(tree, node, region):
            continue
        fenced[node.id] = (node.page, rix)
        canvas_text[(node.page, rix)] = frozenset(_norm(a.text) for a in region.atoms)
        canvas_blocks[(node.page, rix)] = frozenset(region.block_ixs)
    return fenced, canvas_text, canvas_blocks, layouts


def _subtree_painted_by(tree: DocTree, group: Node, region: Region) -> bool:
    """True iff EVERY content leaf under ``group`` is painted by ``region``'s atoms.

    Blocks/marks/tables check by index membership (exactly what the canvas painted, not a
    text comparison); kv and figure leaves are never atoms, so their presence refuses the
    fence outright — only the linear path renders them.
    """
    blocks = set(region.block_ixs)
    marks = set(region.mark_ixs)
    tables = set(region.table_ixs)
    stack = list(group.children)
    while stack:
        node = tree.nodes[stack.pop()]
        stack.extend(node.children)
        if any(bix not in blocks for bix in node.block_ixs):
            return False
        if node.mark_ix is not None and node.mark_ix not in marks:
            return False
        if node.table_ix is not None and node.table_ix not in tables:
            return False
        if node.figure_id is not None or node.kv_ix is not None:
            return False
    return True


def _subtree_triggers_fence(tree: DocTree, group: Node) -> bool:
    stack = list(group.children)
    while stack:
        nid = stack.pop()
        node = tree.nodes[nid]
        if node.kind in _FENCE_TRIGGERS:
            return True
        stack.extend(node.children)
    return False


def _walk(tree: DocTree, root: int):
    """Pre-order over ``root``'s subtree — explicit stack (dense pages, §8.8)."""
    stack = [root]
    while stack:
        nid = stack.pop()
        node = tree.nodes[nid]
        yield node
        stack.extend(reversed(node.children))


def _heading_md(text: str, zone: Zone, depth: int) -> str:
    """Heading depth per §5.2: ``#`` reserved for ``title``; a section at depth ``d`` yields
    ``'#' * (1 + min(d, 5))`` with a floor of one enclosing section, so a flat tree's
    body-level ``sectionHeading`` reproduces 2.0's ``##`` exactly (the §8.5 gate)."""
    body = _clean(text)
    if body.startswith("#"):
        body = "\\" + body  # the emitter's escape: document text cannot deepen the heading.
    if zone is Zone.title:
        return "# " + body
    return "#" * (1 + min(max(depth, 1), 5)) + " " + body


def _flatten(
    tree: DocTree,
    view: LayoutView,
    *,
    doc_id: str,
    source: str,
    provider: str,
    generated: str,
    extra: dict[str, Any] | None,
    decided_by: str,
    flow_joins: frozenset[tuple[int, int]],
) -> tuple[str, FlattenReport]:
    if view_sha256(view) != tree.view_sha256:
        return "", FlattenReport(error="TreeInvalid:view_sha_mismatch")
    check = validate_tree(tree, view)
    if not check.ok:
        return "", FlattenReport(error=f"TreeInvalid:{check.violations[0]}")

    resolved_id = doc_id or tree.doc_id
    fenced, canvas_text, canvas_blocks, layouts = _fence_plan(tree, view)
    st = _Emit(view, _succ_map(tree, flow_joins), canvas_text, canvas_blocks, layouts)

    # (node_id, section_depth); reversed pushes keep children in stored order.
    stack: list[tuple[int, int]] = [(tree.body, 0)]
    while stack:
        nid, depth = stack.pop()
        node = tree.nodes[nid]
        kind = node.kind
        if kind is NodeKind.flow_group:
            where = fenced.get(nid)
            if where is not None:
                _emit_fence(st, tree, node, where)
                continue  # the fence covers the subtree's text; leaves are not re-emitted.
            for child in reversed(node.children):
                stack.append((child, depth))
            continue
        if kind in _CONTAINERS:
            child_depth = depth + 1 if kind is NodeKind.section else depth
            for child in reversed(node.children):
                stack.append((child, child_depth))
            continue
        for child in reversed(node.children):  # figure -> caption; table claims render none.
            stack.append((child, depth))
        _emit_leaf(st, node, depth, resolved_id)

    body_lines = st.lines
    furniture = _furniture_lines(st, tree)

    pages = sorted(
        {p.page for p in view.pages} | {b.page for b in view.blocks}
        | {t.page for t in view.tables} | {m.page for m in view.marks}
        | {k.page for k in view.key_values}
    )
    front = _front_matter(
        tree, view, n_pages=len(pages), doc_id=resolved_id, source=source,
        provider=provider, generated=generated, extra=extra, decided_by=decided_by,
        figures=st.figures, furniture_nodes=st.furniture_nodes, canvases=st.canvases,
    )
    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in front.items() if v != "" and v is not None]
    lines += ["---", ""]
    lines += body_lines
    lines += furniture
    text = "\n".join(lines).rstrip() + "\n"
    report = FlattenReport(
        elements_emitted=st.elements,
        figures=st.figures,
        kv_in_canvas=st.kv_in_canvas,
        furniture_nodes=st.furniture_nodes,
        furniture_in_canvas=st.furniture_in_canvas,
        pages_visited=len(st.marked),
    )
    return text, report


# ---------------------------------------------------------------------------------------
# Per-kind rendering — emitter functions verbatim (§5.2)
# ---------------------------------------------------------------------------------------
def _emit_leaf(st: _Emit, node: Node, depth: int, doc_id: str) -> None:
    view = st.view
    scale = st.scale(node.page)
    kind = node.kind

    if kind in (NodeKind.paragraph, NodeKind.footnote, NodeKind.list_item):
        if not node.block_ixs:
            return
        block = view.blocks[node.block_ixs[0]]
        md, tag = _block_md(block)
        if kind is NodeKind.footnote:
            tag = "footnote"  # §5.2: footnotes render at their section tail, tagged.
        if md:
            rect = rect_scale(block.bbox, scale) if block.bbox else None
            st.element(node.id, node.page, _path_anchor(node.page, rect, tag, node.path), md)
        return

    if kind is NodeKind.heading:
        if not node.block_ixs:
            return
        block = view.blocks[node.block_ixs[0]]
        md = _heading_md(block.text, block.zone, depth)
        # Gate on md truthiness exactly as the 2.0 emitter does (`_block_md` -> `if md:`),
        # never on the text: 2.0 emits an empty-text heading as a bare "## ", and the §8.5
        # flat ≡ 2.0 equivalence gate holds element for element only if 3.0 does too.
        if md:
            rect = rect_scale(block.bbox, scale) if block.bbox else None
            anchor = _path_anchor(node.page, rect, str(block.zone), node.path)
            st.element(node.id, node.page, anchor, md)
        return

    if kind is NodeKind.caption:
        if not node.block_ixs:
            return
        block = view.blocks[node.block_ixs[0]]
        text = _clean(block.text)
        if text:
            # Italic and visually subordinate, but real document text — searchable (§5.2).
            rect = rect_scale(block.bbox, scale) if block.bbox else None
            anchor = _path_anchor(node.page, rect, "caption", node.path)
            st.element(node.id, node.page, anchor, f"*{text}*")
        return

    if kind is NodeKind.table:
        if node.table_ix is None:
            return
        table = view.tables[node.table_ix]
        md = _table_md(table)
        if md:
            tag = f"table {table.row_count}x{table.col_count}"
            rect = rect_scale(table.bbox, scale) if table.bbox else None
            st.element(node.id, node.page, _path_anchor(node.page, rect, tag, node.path), md)
        # Zone.table blocks claimed by this node render nothing: their text is the cells.
        return

    if kind is NodeKind.figure:
        fid = node.figure_id
        if fid is None:
            return
        # Never a generated description — a described signature/photo is manufactured PII.
        md = f"![figure {fid}](figure://{doc_id}/{fid})"
        rect = _mu_rect_scaled(node.bbox, scale)
        st.element(node.id, node.page, _path_anchor(node.page, rect, "figure", node.path), md)
        st.figures += 1
        return

    if kind is NodeKind.kv_pair:
        if node.kv_ix is None:
            return
        kv = view.key_values[node.kv_ix]
        on_canvas = (
            st.canvas_text.get((node.page, node.prov.region_ix))
            if node.prov.region_ix is not None else None
        )
        if on_canvas is not None:
            # §5.2's one surviving splice rule: a pair whose key AND value are both visible
            # on the covering canvas fence is near-duplicate text in a retrieval index —
            # suppressed and counted, exactly the 2.0 emitter's containment test.
            covered = any(_norm(kv.key) in t for t in on_canvas) and any(
                _norm(kv.value) in t for t in on_canvas
            )
            if covered:
                st.kv_in_canvas += 1
                return
        md = f"**{_clean(kv.key)}:** {_clean(kv.value)}".rstrip()
        if md:
            rect = _kv_rect(kv, scale)
            st.element(node.id, node.page, _path_anchor(node.page, rect, "kv", node.path), md)
        return

    if kind is NodeKind.mark:
        if node.mark_ix is None:
            return
        mark = view.marks[node.mark_ix]
        box = "[x]" if mark.state == "selected" else "[ ]"
        rect = rect_scale(mark.bbox, scale) if mark.bbox else None
        st.element(node.id, node.page, _path_anchor(node.page, rect, "mark", node.path),
                   f"- {box}")
        return


def _furniture_lines(st: _Emit, tree: DocTree) -> list[str]:
    """The furniture root, once, after the body (§5.2): demoted, not lost.

    No page markers here — the section is not part of the page flow; each element's anchor
    keeps its true page and rectangle, which is the whole honesty claim.
    """
    root = tree.nodes[tree.furniture]
    out: list[str] = []
    for nid in root.children:
        node = tree.nodes[nid]
        if not node.block_ixs:
            continue
        if _furniture_painted(st, node):
            # §8.3 exactly-once: a mis-zoned furniture block that landed INSIDE a fenced
            # region's band is already painted on the emitted canvas (it is one of the
            # region's atoms). Re-emitting it here is the double the 2.0 emitter never
            # produced — suppressed and counted, the kv_in_canvas accounting's sibling.
            st.furniture_in_canvas += 1
            continue
        block = st.view.blocks[node.block_ixs[0]]
        md, tag = _block_md(block)
        if not md:
            continue
        if not out:
            out.append("<!-- furniture -->")
            out.append("")
        scale = st.scale(node.page)
        rect = rect_scale(block.bbox, scale) if block.bbox else None
        anchor = _path_anchor(node.page, rect, tag, node.path)
        if anchor:
            out.append(anchor)
        out.append(md)
        out.append("")
        st.elements += 1
        st.furniture_nodes += 1
    return out


def _furniture_painted(st: _Emit, node: Node) -> bool:
    """True iff EVERY block of this furniture leaf is a line atom of an emitted fence on
    its page — membership by block index, exactly the 2.0 emitter's painted-once rule,
    never a text comparison (two same-text stamps must not suppress each other)."""
    painted = [
        blocks
        for (page, _rix), blocks in st.canvas_blocks.items()
        if page == node.page
    ]
    return all(
        any(bix in blocks for blocks in painted) for bix in node.block_ixs
    )


def _emit_fence(
    st: _Emit, tree: DocTree, group: Node, where: tuple[int, int]
) -> None:
    """§5.3's fence branch: the region's canvas segments, exactly as the 2.0 emitter writes
    them, with the flow_group's path appended to each segment anchor. Legend lines are left
    verbatim (their identity is per-atom, not per-node — appending a node path to them would
    claim a mapping the tree does not store)."""
    page, rix = where
    scale = st.scale(page)
    region = st.layouts[page].regions[rix]
    for seg in segment(region, region.rows):
        head = _canvas_anchor(page, seg, scale)
        anchor = head[:-4] + f" path={group.path} -->"
        if page not in st.marked:
            st.marked.add(page)
            st.lines.append(_page_marker(page, st.info.get(page), scale))
            st.lines.append("")
        st.lines.append(anchor)
        st.lines += _legend_lines(page, seg, scale)
        open_fence, close_fence = _fence(seg.rows)
        st.lines.append(open_fence)
        st.lines += [_clean_row(row) for row in seg.rows]
        st.lines.append(close_fence)
        st.lines.append("")
        st.elements += 1
        st.canvases += 1
    st.last_leaf = group.id


# ---------------------------------------------------------------------------------------
# Front matter (§5.4, fixed order)
# ---------------------------------------------------------------------------------------
def _tree_source(tree: DocTree) -> str:
    """provider_sections | geometry | flat — the rung that actually PLACED content.

    A ``declined(...)`` geometry manifest alone is not a geometry claim: on an all-declined
    document every node is ``seq_fallback`` and the truthful rung is ``flat`` (§5.4 — the
    audit spine must not report a rung that placed nothing). Geometry is reported for a
    declined manifest only when some node's provenance really is ``geometry`` (the
    mixed-page case: declined on one page, ran on another)."""
    sections = tree.passes.provider_sections
    if sections.startswith(("used", "conflict_demoted")):
        return "provider_sections"
    geometry = tree.passes.geometry
    if geometry == "ran":
        return "geometry"
    if geometry.startswith("declined") and any(
        n.prov.source is ProvSource.geometry and _has_content(n) for n in tree.nodes
    ):
        return "geometry"
    return "flat"


def _has_content(node: Node) -> bool:
    """A node that actually carries document content — containers keep the model's default
    ``prov.source`` and must not count as a rung's placement claim."""
    return bool(
        node.block_ixs
        or node.table_ix is not None
        or node.kv_ix is not None
        or node.mark_ix is not None
        or node.figure_id is not None
    )


def _passes_summary(tree: DocTree) -> str:
    """``sections,geometry[,interposer,continuity]`` — the construction passes that did
    something. The LLM is never named here (R5): its status lives in its own artifact."""
    parts: list[str] = []
    if tree.passes.provider_sections.startswith(("used", "conflict_demoted")):
        parts.append("sections")
    if tree.passes.geometry == "ran" or tree.passes.geometry.startswith("declined"):
        parts.append("geometry")
    if _pass_count(tree.passes.interposer, "footnotes"):
        parts.append("interposer")
    if _pass_count(tree.passes.continuity, "edges"):
        parts.append("continuity")
    return ",".join(parts)


def _pass_count(value: str, key: str) -> int:
    """The integer after ``{key}=`` in a pass-manifest string, or 0 — grammar-tolerant."""
    marker = f"{key}="
    start = value.find(marker)
    if start < 0:
        return 0
    digits = ""
    for char in value[start + len(marker):]:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else 0


def _front_matter(
    tree: DocTree,
    view: LayoutView,
    *,
    n_pages: int,
    doc_id: str,
    source: str,
    provider: str,
    generated: str,
    extra: dict[str, Any] | None,
    decided_by: str,
    figures: int,
    furniture_nodes: int,
    canvases: int,
) -> dict[str, Any]:
    front: dict[str, Any] = {
        "pmd": PMD_VERSION_TREE,
        "generator": GENERATOR,
        "order": "tree",
        "tree_source": _tree_source(tree),
        "decided_by": decided_by,
        "sha256_tree": tree_sha256(tree),
        "doc_id": doc_id,
        "source": source,
        "provider": provider,
        "pages": n_pages,
        "blocks": len(view.blocks),
        "tables": len(view.tables),
        "marks": len(view.marks),
        "key_values": len(view.key_values),
        "chars": sum(len(b.text) for b in view.blocks),
    }
    if figures:
        front["figures"] = figures
    if furniture_nodes:
        front["furniture_nodes"] = furniture_nodes
    passes = _passes_summary(tree)
    if passes:
        front["passes"] = passes
    if canvases:
        # 2.0's rule verbatim: only a canvas's bytes depend on the East-Asian-Width tables,
        # so only a file containing one surrenders hash stability across a Python upgrade.
        front["unicode"] = unicodedata.unidata_version
    front["generated"] = generated
    for key in sorted(extra or {}):
        front[key] = (extra or {})[key]
    return front


__all__ = [
    "PMD_VERSION_TREE",
    "FlattenReport",
    "flatten",
]
