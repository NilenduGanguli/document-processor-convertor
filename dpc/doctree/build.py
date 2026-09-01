"""The doctree builder — ``build_doctree(view) -> DocTree``; pure, NEVER raises.

SPEC-DOCTREE-1 §3.2. The honesty ladder, in order of trust:

1. **Provider seed** — ``view.raw["structure"]`` (harvested Azure sections/figures) gives the
   section skeleton, sibling order verbatim. Geometry then AUDITS the provider: sibling
   sections whose band intervals materially overlap on a page (``SECT_IOU``), or same-frame
   siblings whose provider order inverts band order (R19), demote that page — per page,
   never per document.
2. **Geometry synthesis** — for section-less and demoted pages, the canvas machinery
   (bands/regions/frames, reused verbatim from :mod:`dpc.canvas`) yields leaf runs in band
   order and ``flow_group``/``frame`` containers for multi-column regions; each frame's
   interior then goes through the §3.2 step-4 recursive XY-cut (:func:`_xy_cut`), which
   re-runs the same separator/gutter evidence WITHIN the frame — bounded by
   ``XCUT_MAX_DEPTH``/``XCUT_MIN_ATOMS`` — so nested sub-layouts (a panel of sub-columns
   inside one column) read column-major instead of interleaved.
3. **Flat fallback** — whatever remains (declined pages, geometry-less blocks) appends under
   ``body`` in ``(page, seq, kind, index)`` order, ``prov.source = "seq_fallback"``.

Then: furniture split (before continuity — a page-3 header must never sit between a page-2
tail and its page-3 continuation), footnote interposer demotion (R15), heading nesting
(groups, NEVER reorders), kv clustering, continuity edges (annotate only, R11), and the
final pre-order id assignment (I1). Any internal failure or invariant violation degrades to
the flat tree with ``passes`` saying so — a tree builder that can raise is a converter that
can 500 on a document it would otherwise have converted correctly-but-flatly.

Determinism: every sort key is total and ends in an intrinsic index the artifact itself
stores (``block_ix``/``table_ix``/…); no dict-iteration-order dependence; no floats past
``canvas.mu`` / ``geom.rect_scale``; no wall clock anywhere.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from dpc.canvas import (
    MAX_ATOMS_PER_PAGE,
    REASON_RANK,
    Atom,
    Frame,
    Region,
    _assign_kvs,
    _kv_rect,
    atoms_for_page,
    build_bands,
    build_frames,
    build_regions,
    find_gutters,
    is_rtl,
    mark_separators,
    mu,
    page_em,
    page_skew_ok,
)
from dpc.doctree import continuity
from dpc.doctree.harvest import FigureRef, ProviderStructure, SectionRef, from_raw
from dpc.doctree.metrics import block_metrics, height_class, page_case_profile
from dpc.doctree.models import (
    _PROVIDER_ROLE_RE,
    BUILDER_VERSION,
    PATH_TOKENS,
    Counters,
    DocTree,
    FlowEdge,
    Metrics,
    Node,
    NodeKind,
    PageDims,
    Passes,
    Prov,
    ProvSource,
    Report,
    validate_tree,
    view_sha256,
)
from dpc.geom import rect_scale
from dpc.models import LayoutView, TextBlock, Zone

#: §3.4 ``SECT_IOU_MAX`` = 1/3, as the integer test ``3 * inter > min(len_a, len_b)``.
#: Captions/pull-quotes legitimately interleave a little; beyond a third of the smaller
#: sibling, provider hierarchy and page geometry materially disagree. A guess until measured
#: — the demotion counter makes the rate visible from day one (risk P1-a).
SECT_IOU_NUM = 3

#: §3.4: Azure gives title + flat sectionHeading; inferring deeper than 4 levels over-fits
#: scan noise.
MAX_LEVELS = 4

#: §3.4 ``KV_GAP`` = 2·em vertical — matches the visual row pitch of dense form panels.
KV_GAP_EM = 2

#: Order-tie margin, scale-free: two same-parent siblings whose band-order margin is under
#: half the page em (``2 * margin < em``) were ordered by a coin toss the LLM pass should get
#: to review. Half an em is under one line of leading — below it, "above" is not a fact about
#: the page, only about our tiebreak. Guess until measured, like ``SECT_IOU``.
ORDER_TIE_EM = (1, 2)

#: §3.4 ``XCUT_MAX_DEPTH`` = 6: 2^6 = 64 cells exceeds any real KYC page. The recursion
#: (§3.2 step 4) descends one level per accepted vertical cut inside a frame.
XCUT_MAX_DEPTH = 6

#: §3.4 ``XCUT_MIN_ATOMS`` = 3: a cut side with fewer than 3 atoms is noise-chasing, and a
#: frame with fewer than ``2 * XCUT_MIN_ATOMS`` atoms cannot yield two acceptable sides.
XCUT_MIN_ATOMS = 3

#: Furniture ordering (§3.2 step 6): header, page number, footer — top-of-page first.
_FURN_RANK = {"pageHeader": 0, "pageNumber": 1, "pageFooter": 2}

#: Fallback splice rank (§3.2 step 11): blocks first, then tables, marks, key-values —
#: nominal ``(page, seq, block_ix)`` order; R18's byte-equality test (Phase 2) is the gate
#: that may adjust it, because the 2.0 file is the spec, not the tree.
_FALLBACK_RANK = {"block": 0, "table": 1, "mark": 2, "kv": 3}

#: Leaf kinds that may sit between two flow-linked paragraphs without breaking adjacency
#: (R15's interposed pair).
_INTERPOSERS = frozenset(
    {NodeKind.figure, NodeKind.caption, NodeKind.mark, NodeKind.kv_group,
     NodeKind.kv_pair, NodeKind.footnote}
)

#: Height-class rank for heading levels: bigger type outranks smaller (§3.2 step 8).
_HCLASS_RANK = {"display": 0, "large": 1, "body": 2, "small": 3}


# ---------------------------------------------------------------------------------------
# Internal mutable node + per-page skeleton
# ---------------------------------------------------------------------------------------
@dataclass(slots=True)
class _BN:
    """Builder-internal mutable node; frozen into a pydantic ``Node`` at finalize."""

    kind: NodeKind
    page: int = 1
    children: list[_BN] = field(default_factory=list)
    bbox: tuple[int, int, int, int] | None = None
    level: int | None = None
    block_ixs: list[int] = field(default_factory=list)
    table_ix: int | None = None
    kv_ix: int | None = None
    mark_ix: int | None = None
    figure_id: str | None = None
    metrics: Metrics | None = None
    source: ProvSource = ProvSource.geometry
    provider_ref: str | None = None
    provider_role: str | None = None
    band_ix: int | None = None
    frame_ix: int | None = None
    region_ix: int | None = None
    #: Transient: column width in mu for continuity; never serialized.
    width_mu: int = 0
    em: int = 0
    #: Transient: the COLUMN's x-extent (frame extent in spatial regions, content extent in
    #: linear ones). Region ordinals split vertically WITHIN one column at separator bands,
    #: so column identity for the R15 interposed gate and the step-5 audit is this extent,
    #: never ``region_ix``. Never serialized.
    col_x0: int = 0
    col_x1: int = 0


@dataclass(slots=True)
class _Skel:
    """One page's geometry skeleton — the canvas machinery's output kept whole.

    Unlike ``canvas.page_layout`` this keeps the region list even when NO region went
    spatial: the tree wants band order for single-column pages too, where the emitter only
    wants to know "render linearly".
    """

    page: int
    em: int = 0
    declined: str = ""
    regions: list[Region] = field(default_factory=list)
    band_ord: dict[int, int] = field(default_factory=dict)  # id(Band) -> page band ordinal
    n_bands: int = 0
    content_x0: int = 0
    content_x1: int = 0
    rtl_majority: bool = False
    floating_blocks: list[int] = field(default_factory=list)
    floating_kvs: list[int] = field(default_factory=list)


def _page_skel(view: LayoutView, page: int) -> _Skel:
    """Steps 1 of §3.2 for one page — same gates as ``canvas.page_layout``, regions kept."""
    try:
        atoms, floating_blocks, floating_kvs = atoms_for_page(view, page)
        em = page_em(atoms)
        info = next((p for p in view.pages if p.page == page), None)
        skel = _Skel(page=page, em=em, floating_blocks=floating_blocks,
                     floating_kvs=floating_kvs)
        if not atoms or em <= 0 or (
            info is not None and (info.width <= 0 or info.height <= 0)
        ):
            skel.declined = "no-geometry"
            return skel
        if len(atoms) > MAX_ATOMS_PER_PAGE:
            skel.declined = "too-dense"
            return skel
        if not page_skew_ok(atoms):
            skel.declined = "skew"
            return skel
        skel.content_x0 = min(a.x0 for a in atoms)
        skel.content_x1 = max(a.x1 for a in atoms)
        bands = mark_separators(build_bands(atoms, em), skel.content_x0, skel.content_x1)
        skel.band_ord = {id(band): ix for ix, band in enumerate(bands)}
        skel.n_bands = len(bands)
        skel.regions = build_regions(bands, em, skel.content_x0, skel.content_x1, view.blocks)
        # §3.4 RTL_MAJ: strict majority of the atoms that HAVE a reading direction — line
        # atoms. Marks/tables carry no text, so counting them in the denominator would let a
        # mark-heavy RTL form page miss the majority (a broken measurement instrument).
        n_lines = sum(1 for a in atoms if a.kind == "line")
        rtl = sum(1 for a in atoms if a.kind == "line" and is_rtl(a.text))
        skel.rtl_majority = 2 * rtl > n_lines
        _assign_kvs(skel.regions, view, page)
        return skel
    except Exception:  # noqa: BLE001 - never-raises contract; reason, never message (PII).
        return _Skel(page=page, declined="error")


def _pages_of(view: LayoutView) -> list[int]:
    pages = {p.page for p in view.pages}
    pages.update(b.page for b in view.blocks)
    pages.update(t.page for t in view.tables)
    pages.update(k.page for k in view.key_values)
    pages.update(m.page for m in view.marks)
    return sorted(p for p in pages if p >= 1)


def _mu_quad(quad: Sequence[float] | None) -> tuple[int, int, int, int] | None:
    """A quad's mu rect through the ONE shared rounding (:func:`dpc.geom.rect_scale`, R13)."""
    if quad is None:
        return None
    return rect_scale(list(quad), 1000)


# ---------------------------------------------------------------------------------------
# Leaf constructors
# ---------------------------------------------------------------------------------------
#: The model's single-token role grammar, compiled once. A role that fails it is DROPPED at
#: the leaf constructor, never handed to pydantic: a pattern-failing role would otherwise
#: raise ``ValidationError`` whose message embeds the offending value verbatim — an exception
#: detail carrying payload text, and one that fires again inside the flat fallback.
_ROLE_RE = re.compile(_PROVIDER_ROLE_RE)


def _safe_role(role: str | None) -> str | None:
    """A provider role survives only when it is a single ``_PROVIDER_ROLE_RE`` token.

    Anything else — whitespace, punctuation, a sentence-shaped value — is dropped, not raised
    on, mirroring :func:`_safe_doc_id` (I5/PII: build_doctree must never raise, and no
    exception path may carry document text).
    """
    if role is not None and _ROLE_RE.fullmatch(role):
        return role
    return None


def _block_leaf(
    bix: int,
    block: TextBlock,
    metrics: Metrics,
    source: ProvSource,
    *,
    provider_ref: str | None = None,
) -> _BN:
    """A text leaf for one block; kind by role/zone (R14 — unknown roles land as paragraph)."""
    if block.zone is Zone.title or block.zone is Zone.heading:
        kind = NodeKind.heading
    elif block.role == "footnote":
        kind = NodeKind.footnote
    else:
        kind = NodeKind.paragraph
    known = block.role in (None, "title", "sectionHeading", "footnote")
    return _BN(
        kind=kind,
        page=block.page,
        bbox=_mu_quad(block.bbox),
        block_ixs=[bix],
        metrics=metrics,
        source=source,
        provider_ref=provider_ref,
        provider_role=None if known else _safe_role(block.role),
    )


def _leaf_sort_rect(bn: _BN) -> tuple[int, int]:
    return (bn.bbox[1], bn.bbox[0]) if bn.bbox else (0, 0)


# ---------------------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------------------
def build_doctree(view: LayoutView) -> DocTree:
    """§3.2 — the whole pipeline; pure function of the view, never raises.

    Args:
        view: The provider-neutral read of the document (the same object the emitter sees).

    Returns:
        A valid ``DocTree``. Internal errors or invariant violations return the minimal flat
        tree with ``passes`` recording what happened — degraded output, never an exception.
        Should even the flat rung fail, the unconditional last resort is the three-node
        empty-body tree from :func:`_minimal_tree` — this function CANNOT raise, and no
        degraded path ever records more than an exception's type name (PII).
    """
    try:
        tree = _build(view)
        check = validate_tree(tree, view)
        if check.ok:
            return tree
        note = f"invariant_failed({check.violations[0]})"
    except Exception as exc:  # noqa: BLE001 - never-raises; class name only, no text (PII).
        note = f"error({type(exc).__name__})"
    try:
        return _flat_tree(view, geometry_note=note)
    except Exception as exc:  # noqa: BLE001 - the flat rung failed too; type name only.
        return _minimal_tree(view, geometry_note=f"error({type(exc).__name__})")


class _Builder:
    """One build's working state — pages, skeletons, metrics, claims."""

    def __init__(self, view: LayoutView) -> None:
        self.view = view
        self.pages = _pages_of(view)
        self.skels = {p: _page_skel(view, p) for p in self.pages}
        self.metrics = self._all_metrics()
        self.claimed_blocks: set[int] = set()
        self.claimed_tables: set[int] = set()
        self.claimed_kvs: set[int] = set()
        self.claimed_marks: set[int] = set()
        self.table_nodes: dict[int, _BN] = {}
        self.passes = Passes()
        self.demoted_pages: list[int] = []
        self.footnotes_moved = 0

    def _all_metrics(self) -> dict[int, Metrics]:
        out: dict[int, Metrics] = {}
        by_page: dict[int, list[tuple[int, TextBlock]]] = {}
        for bix, block in enumerate(self.view.blocks):
            by_page.setdefault(block.page, []).append((bix, block))
        for page in sorted(by_page):
            skel = self.skels.get(page)
            em = skel.em if skel else 0
            cx0 = skel.content_x0 if skel and not skel.declined else None
            cx1 = skel.content_x1 if skel and not skel.declined else None
            voided = page_case_profile(
                [b for _, b in by_page[page] if b.zone is not Zone.table]
            )
            for bix, block in by_page[page]:
                out[bix] = block_metrics(
                    block, em, case_voided=voided, content_x0=cx0, content_x1=cx1
                )
        return out


def _build(view: LayoutView) -> DocTree:
    st = _Builder(view)
    structure = from_raw(view.raw.get("structure")) if "structure" in view.raw else None

    fig_ids = _figure_ids(structure)
    caption_reserved = _caption_blocks(structure, view)

    provider_items: list[_BN] = []
    if structure is not None:
        provider_items = _seed_provider(st, structure, fig_ids, caption_reserved)
        demoted = _audit_sections(st, provider_items)
        if demoted:
            st.demoted_pages = sorted(demoted)
            provider_items = _demote_pages(st, provider_items, demoted)

    provider_pages = _provider_pages(provider_items)

    geometry_items: dict[int, list[_BN]] = {}
    for page in st.pages:
        skel = st.skels[page]
        if skel.declined or page in provider_pages:
            continue
        items = _page_geometry_items(st, page)
        if items:
            geometry_items[page] = items

    kv_items = _kv_group_items(st)
    fallback_items = _fallback_items(st)
    _claim_table_zone_blocks(st, fallback_items)

    body = _BN(kind=NodeKind.body, page=st.pages[0] if st.pages else 1)
    body.children = _assemble_body(provider_items, geometry_items, kv_items, fallback_items)
    body.children = _nest_headings(st, body.children)
    _demote_footnotes(st, body)

    furniture = _furniture_root(st)
    doc = _BN(kind=NodeKind.document, page=body.page)
    doc.children = [body, furniture]

    edges, candidates = _continuity_edges(st, body)
    ties = _order_ties(st, body)

    # Figures live only on the provider rung: count the nodes that SURVIVED demotion, not
    # the ids that were minted — a demoted page's figure is removed and never rebuilt, and
    # the manifest must not claim otherwise.
    n_figures = sum(
        1 for item in provider_items for node in _walk_bn(item)
        if node.kind is NodeKind.figure
    )
    _fill_passes(st, structure, bool(fig_ids), n_figures, edges, candidates)
    return _finalize(st, doc, body, furniture, edges, ties)


# ---------------------------------------------------------------------------------------
# Rung 1 — provider seed + geometry audit
# ---------------------------------------------------------------------------------------
def _figure_ids(
    structure: ProviderStructure | None,
) -> dict[int, tuple[str, int, tuple[int, int, int, int] | None]]:
    """``figure_ix -> (figure_id, page, mu_rect)`` — ``fig-{page}-{n}`` per R20.

    ``n`` is 1-based per page by ``(y0, x0, provider_ix)``: intrinsic geometry first, the
    figure's own index (which the artifact stores) as the total tiebreak. Azure's
    undocumented id never participates.
    """
    if structure is None:
        return {}
    per_page: dict[int, list[tuple[int, int, int, FigureRef]]] = {}
    for fig in structure.figures:
        rect = _mu_quad(fig.bbox)
        y0, x0 = (rect[1], rect[0]) if rect else (0, 0)
        per_page.setdefault(fig.page, []).append((y0, x0, fig.figure_ix, fig))
    out: dict[int, tuple[str, int, tuple[int, int, int, int] | None]] = {}
    for page in sorted(per_page):
        ordered = sorted(per_page[page], key=lambda t: (t[0], t[1], t[2]))
        for n, (_, _, _, fig) in enumerate(ordered, start=1):
            out[fig.figure_ix] = (f"fig-{page}-{n}", page, _mu_quad(fig.bbox))
    return out


def _caption_blocks(structure: ProviderStructure | None, view: LayoutView) -> dict[int, int]:
    """``block_ix -> figure_ix`` for caption paragraphs — the ONE deliberate reversal of
    first-claim (§3.2 step 9): a caption's strongest home is its figure, so its block is
    reserved before sections get to claim it."""
    if structure is None:
        return {}
    out: dict[int, int] = {}
    for fig in structure.figures:
        bix = _block_of_paragraph(fig.caption_paragraph_ix, view)
        if bix is not None and bix not in out:
            out[bix] = fig.figure_ix
    return out


def _block_of_paragraph(paragraph_ix: int | None, view: LayoutView) -> int | None:
    """Provider paragraph index -> ``view.blocks`` index.

    Today this is a bounds-checked identity: ``adapters._map_blocks`` maps ``paragraphs[]``
    in order, skipping only empty-text paragraphs — rare enough that the identity holds on
    the recorded corpus. When it does not, the ref lands on a neighbouring block and the
    audit/flat machinery bounds the damage; a stored offset map in ``raw["structure"]`` is
    the Phase-0 follow-up if the corpus shows real skew.
    """
    if paragraph_ix is None or not (0 <= paragraph_ix < len(view.blocks)):
        return None
    return paragraph_ix


def _figure_node(
    st: _Builder,
    fig: FigureRef,
    fig_ids: dict[int, tuple[str, int, tuple[int, int, int, int] | None]],
    caption_reserved: dict[int, int],
) -> _BN:
    figure_id, page, rect = fig_ids[fig.figure_ix]
    node = _BN(
        kind=NodeKind.figure,
        page=page,
        bbox=rect,
        figure_id=figure_id,
        source=ProvSource.azure_section,
        provider_ref=f"/figures/{fig.figure_ix}",
    )
    caption_bix = next(
        (b for b, f in caption_reserved.items() if f == fig.figure_ix), None
    )
    if caption_bix is not None and caption_bix not in st.claimed_blocks:
        st.claimed_blocks.add(caption_bix)
        block = st.view.blocks[caption_bix]
        node.children.append(_BN(
            kind=NodeKind.caption,
            page=block.page,
            bbox=_mu_quad(block.bbox),
            block_ixs=[caption_bix],
            metrics=st.metrics[caption_bix],
            source=ProvSource.azure_section,
        ))
    return node


def _seed_provider(
    st: _Builder,
    structure: ProviderStructure,
    fig_ids: dict[int, tuple[str, int, tuple[int, int, int, int] | None]],
    caption_reserved: dict[int, int],
) -> list[_BN]:
    """§3.2 step 3: the root section's elements become body items, sibling order verbatim."""
    items = _section_elements(st, structure.root, structure, fig_ids, caption_reserved, 1)
    referenced = _referenced_figures(structure.root)
    loose = [f for f in structure.figures if f.figure_ix not in referenced]
    for _, _, _, fig in sorted(
        ((fig_ids[f.figure_ix][1], *_rect_yx(fig_ids[f.figure_ix][2]), f) for f in loose),
        key=lambda t: (t[0], t[1], t[2], t[3].figure_ix),
    ):
        items.append(_figure_node(st, fig, fig_ids, caption_reserved))
    return items


def _rect_yx(rect: tuple[int, int, int, int] | None) -> tuple[int, int]:
    return (rect[1], rect[0]) if rect else (0, 0)


def _referenced_figures(section: SectionRef) -> set[int]:
    out: set[int] = set()
    stack = [section]
    while stack:
        node = stack.pop()
        out.update(ix for kind, ix in node.elements if kind == "figure")
        stack.extend(node.children)
    return out


def _section_elements(
    st: _Builder,
    section: SectionRef,
    structure: ProviderStructure,
    fig_ids: dict[int, tuple[str, int, tuple[int, int, int, int] | None]],
    caption_reserved: dict[int, int],
    depth: int,
) -> list[_BN]:
    """One section's claims as builder nodes, provider array order verbatim."""
    out: list[_BN] = []
    child_iter = iter(section.children)
    figures = {f.figure_ix: f for f in structure.figures}
    for kind, ix in section.elements:
        if kind == "section":
            child = next(child_iter, None)
            if child is None:
                continue
            node = _BN(
                kind=NodeKind.section,
                level=min(depth, MAX_LEVELS),
                source=ProvSource.azure_section,
                provider_ref=f"/sections/{child.section_ix}",
            )
            node.children = _section_elements(
                st, child, structure, fig_ids, caption_reserved, depth + 1
            )
            if node.children:
                node.page = min(c.page for c in node.children)
                out.append(node)
            continue
        if kind == "paragraph":
            bix = _block_of_paragraph(ix, st.view)
            if bix is None or bix in st.claimed_blocks or bix in caption_reserved:
                continue
            block = st.view.blocks[bix]
            if block.zone is Zone.furniture or block.zone is Zone.table:
                continue  # furniture pass / table claim own these (steps 6 and 9).
            st.claimed_blocks.add(bix)
            leaf = _block_leaf(
                bix, block, st.metrics[bix], ProvSource.azure_section,
                provider_ref=f"/paragraphs/{ix}",
            )
            if leaf.kind is NodeKind.heading:
                leaf.level = min(depth, MAX_LEVELS)
            _attach_geometry(st, leaf, bix)
            out.append(leaf)
            continue
        if kind == "table":
            if not (0 <= ix < len(st.view.tables)) or ix in st.claimed_tables:
                continue
            st.claimed_tables.add(ix)
            table = st.view.tables[ix]
            node = _BN(
                kind=NodeKind.table, page=table.page, bbox=_mu_quad(table.bbox),
                table_ix=ix, source=ProvSource.azure_section,
                provider_ref=f"/tables/{ix}",
            )
            st.table_nodes[ix] = node
            out.append(node)
            continue
        if kind == "figure" and ix in fig_ids:
            out.append(_figure_node(st, figures[ix], fig_ids, caption_reserved))
    return out


def _attach_geometry(st: _Builder, leaf: _BN, bix: int) -> None:
    """Stamp band/frame/region coordinates (and column width) from the page skeleton."""
    block = st.view.blocks[bix]
    skel = st.skels.get(block.page)
    if skel is None or skel.declined:
        return
    leaf.em = skel.em
    for rix, region in enumerate(skel.regions):
        for band in region.bands:
            for atom in band.atoms:
                if atom.kind == "line" and atom.block_ix == bix:
                    leaf.region_ix = rix
                    leaf.band_ix = skel.band_ord.get(id(band))
                    if region.kind == "spatial" and region.frames:
                        fix = _frame_ix_of(region, atom)
                        leaf.frame_ix = fix
                        frame = region.frames[fix]
                        leaf.width_mu = frame.x1 - frame.x0
                        leaf.col_x0, leaf.col_x1 = frame.x0, frame.x1
                    else:
                        leaf.width_mu = skel.content_x1 - skel.content_x0
                        leaf.col_x0 = skel.content_x0
                        leaf.col_x1 = skel.content_x1
                    return


def _frame_ix_in(frames: Sequence[Frame], atom: Atom) -> int:
    """``canvas._frame_of``'s centre rule, restated on the public ``Frame`` fields."""
    centre = (atom.x0 + atom.x1) // 2
    for ix, frame in enumerate(frames):
        if centre <= frame.x1:
            return ix
    return len(frames) - 1


def _frame_ix_of(region: Region, atom: Atom) -> int:
    return _frame_ix_in(region.frames, atom)


def _walk_bn(root: _BN) -> Iterator[_BN]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _audit_sections(st: _Builder, items: list[_BN]) -> set[int]:
    """§3.2 step 5 + R19: geometry audits the provider, per page.

    (a) sibling sections whose band intervals overlap beyond ``SECT_IOU`` on a page — but
    only when the sections SHARE a column: two side-by-side column sections legitimately
    overlap band-for-band, and demoting them would degrade the provider rung on exactly
    the two-column documents sections exist for (R19's own "logical flow != page order is
    the feature" principle, applied to overlap as well as order);
    (b) same-column siblings whose provider order inverts band order (R19).

    Column identity is the leaf column x-extent (:func:`_col_overlap`), never ``region_ix``
    — separator bands split regions vertically WITHIN one column.
    """
    demoted: set[int] = set()
    parents: list[list[_BN]] = [items]
    for item in items:
        parents.extend(
            node.children for node in _walk_bn(item) if node.kind is NodeKind.section
        )
    for siblings in parents:
        sections = [n for n in siblings if n.kind is NodeKind.section]
        for a_ix in range(len(sections)):
            for b_ix in range(a_ix + 1, len(sections)):
                demoted.update(_conflicting_pages(st, sections[a_ix], sections[b_ix]))
    return demoted


def _section_page_stats(
    st: _Builder, section: _BN
) -> dict[int, tuple[int, int, set[tuple[int, int]]]]:
    """Per page: (min_band, max_band, {column x-extents}) over the subtree's leaves."""
    out: dict[int, tuple[int, int, set[tuple[int, int]]]] = {}
    for node in _walk_bn(section):
        if node.band_ix is None or not node.block_ixs:
            continue
        lo, hi, cols = out.get(node.page, (node.band_ix, node.band_ix, set()))
        cols.add((node.col_x0, node.col_x1))
        out[node.page] = (min(lo, node.band_ix), max(hi, node.band_ix), cols)
    return out


def _col_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when two column x-extents are the SAME column: their intersection covers a
    strict majority of each. Integer, scale-free, total; zero-width extents (no geometry)
    never overlap — geometry cannot audit what it did not see."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    return 2 * inter > a[1] - a[0] and 2 * inter > b[1] - b[0]


def _share_column(a_cols: set[tuple[int, int]], b_cols: set[tuple[int, int]]) -> bool:
    return any(_col_overlap(a, b) for a in a_cols for b in b_cols)


def _conflicting_pages(st: _Builder, first: _BN, second: _BN) -> set[int]:
    stats_a = _section_page_stats(st, first)
    stats_b = _section_page_stats(st, second)
    out: set[int] = set()
    for page in sorted(set(stats_a) & set(stats_b)):
        a_lo, a_hi, a_cols = stats_a[page]
        b_lo, b_hi, b_cols = stats_b[page]
        shared = _share_column(a_cols, b_cols)
        if not shared:
            # Disjoint columns: parallel section pairs (two-column layouts) overlap in band
            # interval by construction, and cross-column inversion is the feature (R19).
            continue
        inter = min(a_hi, b_hi) - max(a_lo, b_lo) + 1
        shorter = min(a_hi - a_lo + 1, b_hi - b_lo + 1)
        if inter > 0 and SECT_IOU_NUM * inter > shorter:
            out.add(page)
            continue
        # R19: provider order says first-then-second; same-column band order disagreeing is
        # a provider error, cross-column inversion is the feature (logical flow != page order).
        if a_lo > b_lo:
            out.add(page)
    return out


def _demote_pages(st: _Builder, items: list[_BN], demoted: set[int]) -> list[_BN]:
    """Rescind every provider claim on the demoted pages; prune emptied containers."""
    def keep(node: _BN) -> _BN | None:
        node.children = [c for c in (keep(child) for child in node.children) if c]
        is_claiming_leaf = bool(node.block_ixs) or node.table_ix is not None
        if (is_claiming_leaf or node.kind is NodeKind.figure) and node.page in demoted:
            for bix in node.block_ixs:
                st.claimed_blocks.discard(bix)
            if node.table_ix is not None:
                st.claimed_tables.discard(node.table_ix)
                st.table_nodes.pop(node.table_ix, None)
            # A demoted figure keeps nothing; its caption block is freed with it.
            for child in node.children:
                for bix in child.block_ixs:
                    st.claimed_blocks.discard(bix)
            return None
        if node.kind is NodeKind.section and not node.children:
            return None
        return node

    return [n for n in (keep(item) for item in items) if n]


def _provider_pages(items: list[_BN]) -> set[int]:
    out: set[int] = set()
    for item in items:
        for node in _walk_bn(item):
            if node.block_ixs or node.table_ix is not None:
                out.add(node.page)
    return out


# ---------------------------------------------------------------------------------------
# Rung 2 — geometry synthesis
# ---------------------------------------------------------------------------------------
def _element_order(region: Region, band_ord: dict[int, int]) -> list[tuple[int, Atom]]:
    """First-occurrence element order over a region: ``(page_band, Atom)`` per element.

    Bands in region order, atoms in the canvas's own total sort; the first atom of each
    element (block/table/mark) is the element's position.
    """
    seen: set[tuple[str, int]] = set()
    out: list[tuple[int, Atom]] = []
    for band in region.bands:
        for atom in sorted(band.atoms, key=lambda a: a.sort_key):
            key = (atom.kind, atom.source_ix)
            if key in seen:
                continue
            seen.add(key)
            out.append((band_ord.get(id(band), 0), atom))
    return out


def _atom_leaf(st: _Builder, page: int, band_ix: int, atom: Atom) -> _BN | None:
    """One atom's element as a leaf, honouring prior claims; None when already claimed."""
    skel = st.skels[page]
    if atom.kind == "line":
        bix = atom.source_ix
        block = st.view.blocks[bix]
        if bix in st.claimed_blocks or block.zone is Zone.furniture:
            return None
        st.claimed_blocks.add(bix)
        leaf = _block_leaf(bix, block, st.metrics[bix], ProvSource.geometry)
        leaf.band_ix = band_ix
        leaf.em = skel.em
        return leaf
    if atom.kind == "table":
        tix = atom.source_ix
        if tix in st.claimed_tables:
            return None
        st.claimed_tables.add(tix)
        table = st.view.tables[tix]
        node = _BN(kind=NodeKind.table, page=page, bbox=_mu_quad(table.bbox),
                   table_ix=tix, band_ix=band_ix)
        st.table_nodes[tix] = node
        return node
    if atom.kind == "mark":
        mix = atom.source_ix
        if mix in st.claimed_marks:
            return None
        st.claimed_marks.add(mix)
        mark = st.view.marks[mix]
        return _BN(kind=NodeKind.mark, page=page, bbox=_mu_quad(mark.bbox),
                   mark_ix=mix, band_ix=band_ix)
    return None


def _page_geometry_items(st: _Builder, page: int) -> list[_BN]:
    """§3.2 step 4: one page's items in band order; multi-frame regions as flow_groups.

    Each frame's interior goes through :func:`_xy_cut` — the recursive cut driver over the
    canvas's own evidence (separator bands horizontally, ``find_gutters`` corridors
    vertically). When no cut fires, a frame's children are exactly the flat
    ``(band_ix, y0, x0, block_ix)``-ordered leaf run.
    """
    skel = st.skels[page]
    items: list[_BN] = []
    for rix, region in enumerate(skel.regions):
        ordered = _element_order(region, skel.band_ord)
        if region.kind == "spatial" and len(region.frames) >= 2:
            # Every element is homed in the frame of its FIRST atom (band-major order) —
            # the same rule the flat builder used — and the recursion sees ALL of the home
            # frame's atoms, so gutter occupancy and the straddle gate see full geometry.
            home: dict[tuple[str, int], int] = {}
            for _, atom in ordered:
                home[(atom.kind, atom.source_ix)] = _frame_ix_of(region, atom)
            by_frame: dict[int, list[tuple[int, Atom]]] = {}
            for band in region.bands:
                for atom in sorted(band.atoms, key=lambda a: a.sort_key):
                    hfix = home.get((atom.kind, atom.source_ix))
                    if hfix is None:
                        continue
                    by_frame.setdefault(hfix, []).append(
                        (skel.band_ord.get(id(band), 0), atom)
                    )
            group = _BN(kind=NodeKind.flow_group, page=page, region_ix=rix)
            # Frame visit order: frame_ix ascending, reversed on majority-RTL pages —
            # the reader's column order, reusing the canvas's own directional evidence.
            order = sorted(by_frame)
            if skel.rtl_majority:
                order = list(reversed(order))
            for fix in order:
                frame = region.frames[fix]
                frame_bn = _BN(kind=NodeKind.frame, page=page, region_ix=rix,
                               frame_ix=fix)
                frame_bn.children = _xy_cut(
                    st, page, rix, fix, by_frame[fix], frame.x0, frame.x1, 1
                )
                if frame_bn.children:
                    group.children.append(frame_bn)
            if group.children:
                items.append(group)
            continue
        for band_ix, atom in ordered:
            leaf = _atom_leaf(st, page, band_ix, atom)
            if leaf is None:
                continue
            leaf.region_ix = rix
            leaf.width_mu = skel.content_x1 - skel.content_x0
            leaf.col_x0 = skel.content_x0
            leaf.col_x1 = skel.content_x1
            items.append(leaf)
    return items


def _cut_leaves(
    st: _Builder,
    page: int,
    rix: int,
    fix: int,
    pairs: list[tuple[int, Atom]],
    x0: int,
    x1: int,
) -> list[_BN]:
    """A flat leaf run for one cut cell: claim in band-major atom order (first atom of an
    element wins — later atoms are already-claimed no-ops), then read top-to-bottom in the
    §3.2 step-4 ``(band_ix, y0, x0, block_ix)`` order — all intrinsic."""
    leaves: list[_BN] = []
    for band_ix, atom in pairs:
        leaf = _atom_leaf(st, page, band_ix, atom)
        if leaf is None:
            continue
        leaf.region_ix = rix
        leaf.frame_ix = fix
        leaf.width_mu = x1 - x0
        leaf.col_x0, leaf.col_x1 = x0, x1
        leaves.append(leaf)
    leaves.sort(key=_frame_leaf_key)
    return leaves


def _xy_cut(
    st: _Builder,
    page: int,
    rix: int,
    fix: int,
    pairs: list[tuple[int, Atom]],
    x0: int,
    x1: int,
    depth: int,
) -> list[_BN]:
    """§3.2 step 4's recursion driver: alternate X (separator bands) and Y (gutter) cuts
    WITHIN one frame, bounded by ``XCUT_MAX_DEPTH``/``XCUT_MIN_ATOMS``.

    The canvas machinery is the projection profile — ``build_bands`` + ``mark_separators``
    supply the horizontal cuts (re-measured against the FRAME's width, so a nested panel's
    full-frame divider counts) and ``find_gutters``/``build_frames`` the vertical ones. Only
    this driver is new. A cut is accepted only when it yields >= 2 occupied sides, every
    occupied side holds >= ``XCUT_MIN_ATOMS`` atoms, and no element's atoms straddle a side
    (the coverage-gate principle: a corridor that cuts through a block is not real). When no
    segment of this frame accepts a cut, the output is byte-identical to the flat run —
    recursion changes nothing unless genuine nested structure exists.

    Args:
        st: The build state (claims).
        page: The page number.
        rix: The owning canvas region ordinal (provenance).
        fix: The owning TOP-LEVEL canvas frame ordinal (provenance — ``prov.frame_ix`` is
            a canvas coordinate, and the canvas knows nothing of nested sub-frames).
        pairs: ``(page_band_ordinal, atom)`` for every atom homed in this cell, band-major.
        x0: Cell left edge, mu.
        x1: Cell right edge, mu.
        depth: Current recursion depth, 1-based.

    Returns:
        The cell's children: leaf runs and nested ``flow_group``/``frame`` containers.
    """
    skel = st.skels[page]
    em = skel.em
    if (
        depth > XCUT_MAX_DEPTH
        or len(pairs) < 2 * XCUT_MIN_ATOMS
        or em <= 0
        or x1 <= x0
    ):
        return _cut_leaves(st, page, rix, fix, pairs, x0, x1)
    band_of = {atom.key: band_ix for band_ix, atom in pairs}
    bands = mark_separators(build_bands([a for _, a in pairs], em), x0, x1)
    segments: list[tuple[bool, list[Any]]] = []  # (is_candidate_run, bands)
    run: list[Any] = []
    for band in bands:
        if band.separator:
            if run:
                segments.append((True, run))
                run = []
            segments.append((False, [band]))
        else:
            run.append(band)
    if run:
        segments.append((True, run))

    plans: list[tuple[list[tuple[int, Atom]], Any]] = []
    any_cut = False
    for is_run, seg in segments:
        seg_pairs = [
            (band_of[atom.key], atom)
            for band in seg
            for atom in sorted(band.atoms, key=lambda a: a.sort_key)
        ]
        cut = _plan_cut(seg, seg_pairs, em, x0, x1) if is_run else None
        any_cut = any_cut or cut is not None
        plans.append((seg_pairs, cut))
    if not any_cut:
        return _cut_leaves(st, page, rix, fix, pairs, x0, x1)

    out: list[_BN] = []
    for seg_pairs, cut in plans:
        if cut is None:
            out.extend(_cut_leaves(st, page, rix, fix, seg_pairs, x0, x1))
            continue
        frames, cells = cut
        group = _BN(kind=NodeKind.flow_group, page=page, region_ix=rix)
        order = sorted(cells)
        if skel.rtl_majority:
            order = list(reversed(order))
        for sub_ix in order:
            sub = frames[sub_ix]
            frame_bn = _BN(kind=NodeKind.frame, page=page, region_ix=rix, frame_ix=fix)
            frame_bn.children = _xy_cut(
                st, page, rix, fix, cells[sub_ix], sub.x0, sub.x1, depth + 1
            )
            if frame_bn.children:
                group.children.append(frame_bn)
        if group.children:
            out.append(group)
    return out


def _plan_cut(
    seg: list[Any],
    seg_pairs: list[tuple[int, Atom]],
    em: int,
    x0: int,
    x1: int,
) -> tuple[list[Frame], dict[int, list[tuple[int, Atom]]]] | None:
    """One segment's vertical-cut plan, or None when the cut fails a gate."""
    if len(seg_pairs) < 2 * XCUT_MIN_ATOMS:
        return None
    if any(atom.multiline for _, atom in seg_pairs):
        return None  # the canvas's own linear gate: a hull cannot be columnised.
    gutters = find_gutters(seg, em, x0, x1)
    if not gutters:
        return None
    frames = build_frames(gutters, x0, x1, seg, em)
    cells: dict[int, list[tuple[int, Atom]]] = {}
    elem_side: dict[tuple[str, int], int] = {}
    for band_ix, atom in seg_pairs:
        key = (atom.kind, atom.source_ix)
        side = elem_side.get(key)
        if side is None:
            side = _frame_ix_in(frames, atom)
            elem_side[key] = side
        elif _frame_ix_in(frames, atom) != side:
            return None  # an element straddles the corridor: the gutter is not real for it.
        cells.setdefault(side, []).append((band_ix, atom))
    if len(cells) < 2 or any(len(v) < XCUT_MIN_ATOMS for v in cells.values()):
        return None
    return frames, cells


def _frame_leaf_key(bn: _BN) -> tuple[int, int, int, int]:
    y0, x0 = _leaf_sort_rect(bn)
    if bn.block_ixs:
        ref = bn.block_ixs[0]
    elif bn.table_ix is not None:
        ref = bn.table_ix
    elif bn.mark_ix is not None:
        ref = bn.mark_ix
    else:
        ref = 0
    return (bn.band_ix if bn.band_ix is not None else 0, y0, x0, ref)


# ---------------------------------------------------------------------------------------
# KV groups (step 10)
# ---------------------------------------------------------------------------------------
def _kv_group_items(st: _Builder) -> dict[int, list[_BN]]:
    """Per page: kv_group nodes, clustered within region by vertical gap <= ``KV_GAP``."""
    out: dict[int, list[_BN]] = {}
    for page in st.pages:
        skel = st.skels[page]
        if skel.declined:
            continue
        groups: list[_BN] = []
        for rix, region in enumerate(skel.regions):
            pending = [
                kix for kix in region.kv_ixs if kix not in st.claimed_kvs
            ]
            rects = {kix: _kv_rect(st.view.key_values[kix]) for kix in pending}
            pending = [k for k in pending if rects[k] is not None]
            # (y0, x0, kv_ix): geometry first, the pair's own stored index as tiebreak.
            pending.sort(key=lambda k: (rects[k][1], rects[k][0], k))  # type: ignore[index]
            cluster: list[int] = []
            prev_y1 = 0
            for kix in pending:
                rect = rects[kix]
                assert rect is not None
                if cluster and rect[1] - prev_y1 > KV_GAP_EM * skel.em:
                    groups.append(_kv_group(st, page, rix, cluster, rects))
                    cluster = []
                cluster.append(kix)
                prev_y1 = max(prev_y1, rect[3]) if len(cluster) > 1 else rect[3]
            if cluster:
                groups.append(_kv_group(st, page, rix, cluster, rects))
        if groups:
            out[page] = groups
    return out


def _kv_group(
    st: _Builder,
    page: int,
    region_ix: int,
    kv_ixs: list[int],
    rects: dict[int, tuple[int, int, int, int] | None],
) -> _BN:
    group = _BN(kind=NodeKind.kv_group, page=page, region_ix=region_ix)
    def pair_key(kix: int) -> tuple[int, int, int, int]:
        kv = st.view.key_values[kix]
        key_rect = _mu_quad(kv.key_bbox) or rects[kix] or (0, 0, 0, 0)
        return (page, key_rect[1], key_rect[0], kix)
    for kix in sorted(kv_ixs, key=pair_key):
        st.claimed_kvs.add(kix)
        group.children.append(_BN(
            kind=NodeKind.kv_pair, page=page, bbox=rects[kix], kv_ix=kix,
            region_ix=region_ix,
        ))
    return group


# ---------------------------------------------------------------------------------------
# Rung 3 — flat fallback (step 11) + table-zone claims
# ---------------------------------------------------------------------------------------
def _fallback_items(st: _Builder) -> list[_BN]:
    """Everything still unclaimed, in the order that flattens to today's 2.0 output —
    nominal ``(page, seq, kind, index)``; R18's Phase-2 byte-equality test is the gate that
    may adjust the splice, because the 2.0 file is the spec, not the tree."""
    entries: list[tuple[int, int, int, int, _BN]] = []
    view = st.view
    for bix, block in enumerate(view.blocks):
        if bix in st.claimed_blocks or block.zone in (Zone.furniture, Zone.table):
            continue
        st.claimed_blocks.add(bix)
        leaf = _block_leaf(bix, block, st.metrics[bix], ProvSource.seq_fallback)
        entries.append((block.page, _seq(block.seq), _FALLBACK_RANK["block"], bix, leaf))
    for tix, table in enumerate(view.tables):
        if tix in st.claimed_tables:
            continue
        st.claimed_tables.add(tix)
        node = _BN(kind=NodeKind.table, page=table.page, bbox=_mu_quad(table.bbox),
                   table_ix=tix, source=ProvSource.seq_fallback)
        st.table_nodes[tix] = node
        entries.append((table.page, _seq(table.seq), _FALLBACK_RANK["table"], tix, node))
    for mix, mark in enumerate(view.marks):
        if mix in st.claimed_marks:
            continue
        st.claimed_marks.add(mix)
        node = _BN(kind=NodeKind.mark, page=mark.page, bbox=_mu_quad(mark.bbox),
                   mark_ix=mix, source=ProvSource.seq_fallback)
        entries.append((mark.page, _seq(None), _FALLBACK_RANK["mark"], mix, node))
    for kix, kv in enumerate(view.key_values):
        if kix in st.claimed_kvs:
            continue
        st.claimed_kvs.add(kix)
        node = _BN(kind=NodeKind.kv_pair, page=kv.page, bbox=_kv_rect(kv), kv_ix=kix,
                   source=ProvSource.seq_fallback)
        entries.append((kv.page, _seq(None), _FALLBACK_RANK["kv"], kix, node))
    entries.sort(key=lambda e: e[:4])
    return [e[4] for e in entries]


def _seq(value: int | None) -> int:
    return value if value is not None else 10**9


def _claim_table_zone_blocks(st: _Builder, fallback: list[_BN]) -> None:
    """Claim ``Zone.table`` blocks under their table node (I3 stays total without
    manufacturing paragraph nodes for text the table already carries as cells)."""
    tables_by_page: dict[int, list[int]] = {}
    for tix, table in enumerate(st.view.tables):
        tables_by_page.setdefault(table.page, []).append(tix)
    extras: list[tuple[int, int, int, int, _BN]] = []
    for bix, block in enumerate(st.view.blocks):
        if bix in st.claimed_blocks or block.zone is not Zone.table:
            continue
        st.claimed_blocks.add(bix)
        candidates = tables_by_page.get(block.page, [])
        owner = _containing_table(st, block, candidates)
        if owner is not None and owner in st.table_nodes:
            st.table_nodes[owner].block_ixs.append(bix)
            continue
        # No table on the page owns it: the honest home is a fallback paragraph.
        leaf = _block_leaf(bix, block, st.metrics[bix], ProvSource.seq_fallback)
        extras.append((block.page, _seq(block.seq), _FALLBACK_RANK["block"], bix, leaf))
    for entry in sorted(extras, key=lambda e: e[:4]):
        fallback.append(entry[4])
    for node in st.table_nodes.values():
        node.block_ixs.sort()


def _containing_table(st: _Builder, block: TextBlock, candidates: list[int]) -> int | None:
    rect = _mu_quad(block.bbox)
    if rect is not None:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        for tix in candidates:
            trect = _mu_quad(st.view.tables[tix].bbox)
            if trect and trect[0] <= cx <= trect[2] and trect[1] <= cy <= trect[3]:
                return tix
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------------------
# Assembly + structural passes (steps 6-8)
# ---------------------------------------------------------------------------------------
def _assemble_body(
    provider_items: list[_BN],
    geometry_items: dict[int, list[_BN]],
    kv_items: dict[int, list[_BN]],
    fallback_items: list[_BN],
) -> list[_BN]:
    """Merge the three rungs into one body-children list, ordered by first page touched.

    Provider items are ONE root's children and §3.2 step 3 makes their sibling order
    verbatim — including across pages, where "logical flow != page order" is exactly the
    case R19 protects. They therefore stay one contiguous block in provider array order,
    anchored at the earliest page any of them touches; a per-item page sort here would
    silently re-order the provider's own siblings. The other tiers merge by page around
    that block: geometry items (tier 1, band order), kv groups (tier 2), fallback (tier 3,
    splice order). The tier+arrival pair is total because each tier's list is already in
    its own deterministic order.
    """
    merged: list[tuple[int, int, int, _BN]] = []
    if provider_items:
        anchor = min(
            min(n.page for n in _walk_bn(item)) for item in provider_items
        )
        for ix, item in enumerate(provider_items):
            merged.append((anchor, 0, ix, item))
    for page in sorted(geometry_items):
        for ix, item in enumerate(geometry_items[page]):
            merged.append((page, 1, ix, item))
    for page in sorted(kv_items):
        for ix, item in enumerate(kv_items[page]):
            merged.append((page, 2, ix, item))
    for ix, item in enumerate(fallback_items):
        merged.append((item.page, 3, ix, item))
    merged.sort(key=lambda e: e[:3])
    return [e[3] for e in merged]


def _heading_levels(st: _Builder, items: list[_BN]) -> dict[int, int]:
    """Body-level heading block -> level (§3.2 step 8): title 1; sectionHeading by
    descending rank of distinct height classes, shifted by one when a title exists."""
    has_title = any(
        st.view.blocks[bn.block_ixs[0]].zone is Zone.title
        for bn in items if bn.kind is NodeKind.heading and bn.block_ixs
    )
    classes: set[int] = set()
    for bn in items:
        if bn.kind is not NodeKind.heading or not bn.block_ixs:
            continue
        if st.view.blocks[bn.block_ixs[0]].zone is Zone.title:
            continue
        metrics = bn.metrics or Metrics()
        classes.add(_HCLASS_RANK[height_class(metrics.height_mu, bn.em)])
    rank_to_level = {
        rank: ix + 1 for ix, rank in enumerate(sorted(classes))
    }
    shift = 1 if has_title else 0
    out: dict[int, int] = {}
    for bn in items:
        if bn.kind is not NodeKind.heading or not bn.block_ixs:
            continue
        bix = bn.block_ixs[0]
        if st.view.blocks[bix].zone is Zone.title:
            out[bix] = 1
        else:
            metrics = bn.metrics or Metrics()
            rank = _HCLASS_RANK[height_class(metrics.height_mu, bn.em)]
            out[bix] = min(rank_to_level.get(rank, 1) + shift, MAX_LEVELS)
    return out


def _nest_headings(st: _Builder, items: list[_BN]) -> list[_BN]:
    """Step 8 — nesting GROUPS, never reorders: a wrong level distorts outline depth only.

    Only geometry/fallback headings at body level open sections; provider items (already
    sectioned by the provider) nest under whatever section is open when they appear, which
    preserves their relative order exactly.
    """
    levels = _heading_levels(
        st, [bn for bn in items if bn.source is not ProvSource.azure_section]
    )
    out: list[_BN] = []
    stack: list[tuple[int, _BN]] = []
    for item in items:
        opens = (
            item.kind is NodeKind.heading
            and item.source is not ProvSource.azure_section
            and bool(item.block_ixs)
            and item.block_ixs[0] in levels
        )
        if opens:
            level = levels[item.block_ixs[0]]
            item.level = level
            while stack and stack[-1][0] >= level:
                stack.pop()
            section = _BN(
                kind=NodeKind.section, page=item.page, level=level, source=item.source,
            )
            section.children.append(item)
            if stack:
                stack[-1][1].children.append(section)
            else:
                out.append(section)
            stack.append((level, section))
            continue
        if stack:
            stack[-1][1].children.append(item)
        else:
            out.append(item)
    return out


def _demote_footnotes(st: _Builder, body: _BN) -> None:
    """Step 7 (R15): footnote leaves move to the tail of their innermost section, ordered
    ``(page, y0, x0, block_ix)`` — body content rendered at section end, never furniture."""
    moved: dict[int, list[_BN]] = {}  # id(target) -> footnotes
    targets: dict[int, _BN] = {}

    def sweep(node: _BN, innermost: _BN) -> None:
        kept: list[_BN] = []
        for child in node.children:
            here = child if child.kind is NodeKind.section else innermost
            if child.kind is NodeKind.footnote:
                moved.setdefault(id(innermost), []).append(child)
                targets[id(innermost)] = innermost
                st.footnotes_moved += 1
                continue
            sweep(child, here)
            kept.append(child)
        node.children = kept

    sweep(body, body)
    # Append per target; iteration order over ``moved`` is insertion order = document order,
    # which is deterministic (dict preserves insertion order by language guarantee).
    for key, notes in moved.items():
        notes.sort(key=lambda bn: (bn.page, *_leaf_sort_rect(bn),
                                   bn.block_ixs[0] if bn.block_ixs else 0))
        targets[key].children.extend(notes)


def _furniture_root(st: _Builder) -> _BN:
    """Step 6: every ``Zone.furniture`` block, ordered (page, role_rank, y0, x0, block_ix)."""
    root = _BN(kind=NodeKind.furniture)
    entries: list[tuple[int, int, int, int, int]] = []
    for bix, block in enumerate(st.view.blocks):
        if block.zone is not Zone.furniture or bix in st.claimed_blocks:
            continue
        st.claimed_blocks.add(bix)
        rect = _mu_quad(block.bbox)
        y0, x0 = (rect[1], rect[0]) if rect else (0, 0)
        entries.append((block.page, _FURN_RANK.get(block.role or "", 3), y0, x0, bix))
    for page, _, _, _, bix in sorted(entries):
        block = st.view.blocks[bix]
        leaf = _BN(
            kind=NodeKind.paragraph, page=page, bbox=_mu_quad(block.bbox),
            block_ixs=[bix], metrics=st.metrics[bix],
            provider_role=_safe_role(block.role),
        )
        root.children.append(leaf)
    if root.children:
        root.page = root.children[0].page
    return root


# ---------------------------------------------------------------------------------------
# Continuity (step 12) + order ties
# ---------------------------------------------------------------------------------------
def _body_leaves(body: _BN) -> list[_BN]:
    return [bn for bn in _walk_bn(body) if not bn.children and bn.kind is not NodeKind.body]


def _continuity_edges(
    st: _Builder, body: _BN
) -> tuple[list[tuple[_BN, _BN, int, list[Any]]], int]:
    """§3.3's candidate gates over the finished pre-order; edges annotate, never reorder."""
    leaves = _body_leaves(body)
    frame_bands: dict[tuple[int, int | None, int | None], tuple[int, int]] = {}
    page_bands: dict[int, tuple[int, int]] = {}
    for leaf in leaves:
        if leaf.band_ix is None:
            continue
        fkey = (leaf.page, leaf.region_ix, leaf.frame_ix)
        lo, hi = frame_bands.get(fkey, (leaf.band_ix, leaf.band_ix))
        frame_bands[fkey] = (min(lo, leaf.band_ix), max(hi, leaf.band_ix))
        plo, phi = page_bands.get(leaf.page, (leaf.band_ix, leaf.band_ix))
        page_bands[leaf.page] = (min(plo, leaf.band_ix), max(phi, leaf.band_ix))

    edges: list[tuple[_BN, _BN, int, list[Any]]] = []
    candidates = 0
    tail = continuity.GATE_TAIL
    prev: _BN | None = None
    between: list[_BN] = []
    for leaf in leaves:
        if leaf.kind is not NodeKind.paragraph:
            between.append(leaf)
            continue
        if prev is not None:
            adjacency = _pair_adjacent(prev, leaf, between, frame_bands, page_bands, tail)
            same_script = (
                prev.metrics is not None
                and leaf.metrics is not None
                and prev.metrics.script_class == leaf.metrics.script_class
            )
            if adjacency and same_script:
                candidates += 1
                src = continuity.features_from_metrics(
                    prev.metrics, width_mu=prev.width_mu, em=prev.em  # type: ignore[arg-type]
                )
                dst = continuity.features_from_metrics(
                    leaf.metrics, width_mu=leaf.width_mu, em=leaf.em  # type: ignore[arg-type]
                )
                score, evidence = continuity.score_with_evidence(src, dst, True)
                if score >= continuity.CONT_EDGE_MIN:
                    edges.append((prev, leaf, score, list(evidence)))
        prev = leaf
        between = []
    return edges, candidates


def _pair_adjacent(
    src: _BN,
    dst: _BN,
    between: list[_BN],
    frame_bands: dict[tuple[int, int | None, int | None], tuple[int, int]],
    page_bands: dict[int, tuple[int, int]],
    tail: int,
) -> bool:
    """The two candidate gates: frame-edge pair (a) and interposed pair (b) — §3.3, R15."""
    src_col = (src.page, src.region_ix, src.frame_ix)
    dst_col = (dst.page, dst.region_ix, dst.frame_ix)
    if between:
        # (b) interposed: same column, only non-paragraph leaves between (R15). Column
        # identity is the x-extent, NOT region_ix: mark/table separators split regions
        # exactly AT the interposer, so the pair the gate exists for always lands in
        # different regions of the same column.
        return (
            src.page == dst.page
            and _col_overlap((src.col_x0, src.col_x1), (dst.col_x0, dst.col_x1))
            and all(bn.kind in _INTERPOSERS for bn in between)
        )
    if src.band_ix is None or dst.band_ix is None:
        return False
    if dst.page == src.page + 1:
        # (a, cross-page): src within the bottom GATE_TAIL bands of its page, dst within
        # the top GATE_TAIL of the next.
        _, src_hi = page_bands.get(src.page, (0, 0))
        dst_lo, _ = page_bands.get(dst.page, (0, 0))
        return src.band_ix >= src_hi - (tail - 1) and dst.band_ix <= dst_lo + (tail - 1)
    if src.page == dst.page and src_col != dst_col and src.frame_ix is not None:
        # (a, frame-edge): src last-of-column within GATE_TAIL, dst first-of-next likewise.
        _, src_hi = frame_bands.get(src_col, (0, 0))
        dst_lo, _ = frame_bands.get(dst_col, (0, 0))
        return src.band_ix >= src_hi - (tail - 1) and dst.band_ix <= dst_lo + (tail - 1)
    return False


def _order_ties(st: _Builder, body: _BN) -> list[tuple[_BN, _BN, int]]:
    """Coin-toss sibling orderings: same parent, same page, band-order margin under half an
    em (``ORDER_TIE_EM``) — the builder confessing where its order is a guess, so the LLM
    pass knows where a second opinion is worth asking for."""
    out: list[tuple[_BN, _BN, int]] = []
    stack = [body]
    while stack:
        node = stack.pop()
        for first, second in zip(node.children, node.children[1:]):
            stack.append(first)
            if (
                first.bbox is not None and second.bbox is not None
                and first.page == second.page
                and not first.children and not second.children
                and first.em > 0
            ):
                margin = abs(second.bbox[1] - first.bbox[1])
                if margin * ORDER_TIE_EM[1] < first.em * ORDER_TIE_EM[0]:
                    out.append((first, second, margin))
        if node.children:
            stack.append(node.children[-1])
    return out


# ---------------------------------------------------------------------------------------
# Passes + finalize
# ---------------------------------------------------------------------------------------
def _fill_passes(
    st: _Builder,
    structure: ProviderStructure | None,
    figures_present: bool,
    n_figures: int,
    edges: list[tuple[_BN, _BN, int, list[Any]]],
    candidates: int,
) -> None:
    if structure is None:
        st.passes.provider_sections = "absent"
    elif st.demoted_pages:
        pages = ",".join(str(p) for p in st.demoted_pages)
        st.passes.provider_sections = f"conflict_demoted(pages=[{pages}])"
    else:
        count = sum(1 for _ in _walk_sections(structure.root))
        st.passes.provider_sections = f"used({count})"
    st.passes.provider_figures = f"used({n_figures})" if figures_present else "absent"

    declined = [(p, s.declined) for p, s in sorted(st.skels.items()) if s.declined]
    ran = [p for p, s in sorted(st.skels.items()) if not s.declined]
    if not st.pages:
        st.passes.geometry = "absent"
    elif not declined:
        st.passes.geometry = "ran"
    elif not ran and all(r == "no-geometry" for _, r in declined):
        st.passes.geometry = "absent"
    else:
        reason = min(declined, key=lambda d: (REASON_RANK.get(d[1], 99), d[0]))[1]
        pages = ",".join(str(p) for p, _ in declined)
        st.passes.geometry = f"declined({reason.replace('-', '_')}, pages=[{pages}])"

    st.passes.interposer = f"ran(footnotes={st.footnotes_moved})"
    st.passes.continuity = f"ran(edges={len(edges)}, candidates={candidates})"


def _walk_sections(root: SectionRef) -> Iterator[SectionRef]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _finalize(
    st: _Builder,
    doc: _BN,
    body: _BN,
    furniture: _BN,
    edges: list[tuple[_BN, _BN, int, list[Any]]],
    ties: list[tuple[_BN, _BN, int]],
) -> DocTree:
    """Step 13: pre-order ids (I1), paths, bottom-up bbox unions, and the frozen model."""
    _union_geometry(doc)
    levels = {
        bn.level for bn in _walk_bn(body)
        if bn.kind is NodeKind.section and bn.level is not None
    }
    st.passes.heading_nesting = f"ran(levels={len(levels)})"

    order: list[_BN] = list(_walk_bn(doc))
    ids = {id(bn): ix for ix, bn in enumerate(order)}
    paths = _paths(doc)
    parents: dict[int, _BN] = {}
    for bn in order:
        for child in bn.children:
            parents[id(child)] = bn

    nodes: list[Node] = []
    for ix, bn in enumerate(order):
        parent_bn = parents.get(id(bn))
        nodes.append(Node(
            id=ix,
            kind=bn.kind,
            path=paths[id(bn)],
            parent=ids[id(parent_bn)] if parent_bn is not None else None,
            children=[ids[id(c)] for c in bn.children],
            page=bn.page,
            bbox=bn.bbox,
            level=bn.level,
            block_ixs=bn.block_ixs,
            table_ix=bn.table_ix,
            kv_ix=bn.kv_ix,
            mark_ix=bn.mark_ix,
            figure_id=bn.figure_id,
            metrics=bn.metrics,
            prov=Prov(
                source=bn.source,
                provider_ref=bn.provider_ref,
                provider_role=bn.provider_role,
                band_ix=bn.band_ix,
                frame_ix=bn.frame_ix,
                region_ix=bn.region_ix,
            ),
        ))

    flow = [
        FlowEdge(src=ids[id(a)], dst=ids[id(b)], score=score, evidence=evidence)
        for a, b, score, evidence in edges
        if ids[id(a)] < ids[id(b)]
    ]
    report = Report(
        order_ties=[(ids[id(a)], ids[id(b)], margin) for a, b, margin in ties],
        coverage_fallback_pages=sorted({
            n.page for n in nodes if n.prov.source is ProvSource.seq_fallback
            and (n.block_ixs or n.table_ix is not None or n.kv_ix is not None
                 or n.mark_ix is not None)
        }),
        declined_pages=[p for p, s in sorted(st.skels.items()) if s.declined],
    )
    view = st.view
    counters = Counters(
        blocks_total=len(view.blocks),
        blocks_claimed=sum(len(n.block_ixs) for n in nodes),
        tables_claimed=sum(1 for n in nodes if n.table_ix is not None),
        kvs_claimed=sum(1 for n in nodes if n.kv_ix is not None),
        marks_claimed=sum(1 for n in nodes if n.mark_ix is not None),
        nodes=len(nodes),
        edges=len(flow),
    )
    return DocTree(
        doc_id=_safe_doc_id(view.doc_id),
        view_sha256=view_sha256(view),
        builder=BUILDER_VERSION,
        pages=[
            # R16: page dims as mu ints, through the one shared rounding (``canvas.mu``).
            PageDims(page=p.page, width_mu=mu(p.width), height_mu=mu(p.height))
            for p in sorted(view.pages, key=lambda p: p.page)
        ],
        body=ids[id(body)],
        furniture=ids[id(furniture)],
        nodes=nodes,
        flow=flow,
        report=report,
        passes=st.passes,
        counters=counters,
    )


def _paths(doc: _BN) -> dict[int, str]:
    """Path per node: ``//doc`` at the root, ``/{token}[{n}]`` per child, n 1-based per kind
    among preceding siblings — pure tree position, content-free by construction."""
    out: dict[int, str] = {id(doc): "//doc"}
    stack: list[_BN] = [doc]
    while stack:
        node = stack.pop()
        counts: dict[NodeKind, int] = {}
        for child in node.children:
            counts[child.kind] = counts.get(child.kind, 0) + 1
            token = PATH_TOKENS[child.kind]
            out[id(child)] = f"{out[id(node)]}/{token}[{counts[child.kind]}]"
            stack.append(child)
    return out


def _union_geometry(doc: _BN) -> None:
    """Bottom-up bbox unions + first-page propagation (iterative post-order)."""
    post: list[_BN] = []
    stack: list[_BN] = [doc]
    while stack:
        node = stack.pop()
        post.append(node)
        stack.extend(node.children)
    for node in reversed(post):
        rects = [c.bbox for c in node.children if c.bbox is not None]
        if node.bbox is not None:
            rects.append(node.bbox)
        if rects:
            node.bbox = (
                min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[2] for r in rects), max(r[3] for r in rects),
            )
        if node.children:
            node.page = min([node.page, *(c.page for c in node.children)])


def _safe_doc_id(doc_id: str) -> str:
    """Doc ids are uuids/slugs; anything else (I5 pattern miss) is dropped, not raised on."""
    return doc_id if re.fullmatch(r"[A-Za-z0-9._:-]{0,128}", doc_id) else ""


# ---------------------------------------------------------------------------------------
# The flat fallback tree
# ---------------------------------------------------------------------------------------
def _flat_tree(view: LayoutView, *, geometry_note: str) -> DocTree:
    """The minimal valid tree — step 11 for the WHOLE document; the last rung that must
    always succeed, so it uses no geometry machinery at all."""
    try:
        metrics = {
            bix: block_metrics(block, 0, case_voided=page_case_profile(
                [b for b in view.blocks if b.page == block.page]))
            for bix, block in enumerate(view.blocks)
        }
    except Exception:  # noqa: BLE001 - metrics must never cost the fallback tree.
        metrics = {}

    st = _Builder.__new__(_Builder)  # skip __init__: no skeletons on the last rung.
    st.view = view
    st.pages = _pages_of(view)
    st.skels = {}
    st.metrics = {bix: metrics.get(bix, Metrics()) for bix in range(len(view.blocks))}
    st.claimed_blocks = set()
    st.claimed_tables = set()
    st.claimed_kvs = set()
    st.claimed_marks = set()
    st.table_nodes = {}
    st.passes = Passes(geometry=geometry_note)
    st.demoted_pages = []
    st.footnotes_moved = 0

    body = _BN(kind=NodeKind.body, page=st.pages[0] if st.pages else 1)
    fallback = _fallback_items(st)
    _claim_table_zone_blocks(st, fallback)
    body.children = fallback
    furniture = _furniture_root(st)
    doc = _BN(kind=NodeKind.document, page=body.page)
    doc.children = [body, furniture]
    st.passes.provider_sections = "absent"
    st.passes.provider_figures = "absent"
    return _finalize(st, doc, body, furniture, edges=[], ties=[])


def _minimal_tree(view: LayoutView, *, geometry_note: str) -> DocTree:
    """The unconditional last resort: three fixed nodes, zero claims, no machinery at all.

    Reached only when :func:`_flat_tree` itself raised (e.g. a view shape the fallback
    machinery cannot digest). Every view-derived value is guarded individually so this
    constructor is total — an empty-body tree that honestly fails I3 downstream beats an
    exception out of ``build_doctree``, which is a converter that can 500.
    """
    try:
        doc_id = _safe_doc_id(view.doc_id)
    except Exception:  # noqa: BLE001 - total by construction.
        doc_id = ""
    try:
        sha = view_sha256(view)
    except Exception:  # noqa: BLE001 - total by construction.
        sha = "0" * 64
    try:
        pages = [
            PageDims(page=p.page, width_mu=mu(p.width), height_mu=mu(p.height))
            for p in sorted(view.pages, key=lambda p: p.page)
        ]
    except Exception:  # noqa: BLE001 - total by construction.
        pages = []
    try:
        blocks_total = len(view.blocks)
    except Exception:  # noqa: BLE001 - total by construction.
        blocks_total = 0
    nodes = [
        Node(id=0, kind=NodeKind.document, path="//doc", parent=None, children=[1, 2]),
        Node(id=1, kind=NodeKind.body, path="//doc/body[1]", parent=0),
        Node(id=2, kind=NodeKind.furniture, path="//doc/furn[1]", parent=0),
    ]
    return DocTree(
        doc_id=doc_id,
        view_sha256=sha,
        builder=BUILDER_VERSION,
        pages=pages,
        body=1,
        furniture=2,
        nodes=nodes,
        passes=Passes(geometry=geometry_note),
        counters=Counters(blocks_total=blocks_total, nodes=3),
    )


__all__ = [
    "KV_GAP_EM",
    "MAX_LEVELS",
    "ORDER_TIE_EM",
    "SECT_IOU_NUM",
    "XCUT_MAX_DEPTH",
    "XCUT_MIN_ATOMS",
    "build_doctree",
]
