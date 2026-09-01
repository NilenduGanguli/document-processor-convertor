"""The SAFE per-node feature projection the arrange LLM sees (SPEC-DOCTREE-1 §4.2).

Everything here is an int, a bool, ``None``, or a CLOSED string type: every ``str`` field on
the payload models is enum- or pattern-constrained, so an unconstrained string cannot exist
in a request payload by type construction — the §8.6 ``test_payload_model_closed`` test
introspects the JSON schema and fails on any field this module would let through.

The adversarial rules this module implements (each justified in the spec's §4.2 table):

- **R7 edge anchoring** — only the alignment-anchored edge of a node is sent (``anchor_edge``
  + ``anchor_pm`` on a 1% grid). The free edge is NEVER sent: for right-aligned text,
  ``x0 = x1 - width`` made a 1%-grid left edge a working ~1-character length oracle against
  an attacker who holds the blank template. Extent is only ever ``w_class``.
- **Bucketed extents** — ``char_count_class`` is log-4 (within-bucket uncertainty >= x4),
  ``line_count_class`` is three buckets, ``w_class`` is coarse width fractions. Exact counts
  are a length oracle on a known template; buckets are not.
- **Honest nulls** — ``starts_lowercase`` stays ``None`` when the metrics gate voided it
  (all-caps page, non-bicameral script); a gate the model applies is a gate sometimes not
  applied, so the void travels as ``null``, never as a fabricated ``False``.

No float leaves this module: permille grids are integer arithmetic over mu geometry, exactly
like the rest of the pipeline (no 1-ULP threshold flips in anything that feeds stored bytes).
"""
from __future__ import annotations

import enum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from dpc.doctree.continuity import GATE_TAIL
from dpc.doctree.metrics import height_class
from dpc.doctree.models import (
    _PATH_RE,
    Alignment,
    DigitRatioClass,
    DocTree,
    Metrics,
    Node,
    NodeKind,
    ScriptClass,
    walk_body,
)
from dpc.models import LayoutView

#: Window-local node id grammar — assigned by US, pure window position, zero content.
NId = Annotated[str, StringConstraints(pattern=r"^n[0-9]{1,2}$")]

#: Structural path grammar — the same closed grammar the stored tree uses (I5).
PathStr = Annotated[str, StringConstraints(pattern=_PATH_RE)]


class AnchorEdge(enum.StrEnum):
    """Which edge of the node's box is template-fixed (R7) — the ONLY edge ever sent."""

    left = "left"
    right = "right"
    center = "center"


class WClass(enum.StrEnum):
    """Width as a coarse fraction of the page width (§4.2): extent, never a length oracle."""

    lt_1_16 = "lt_1_16"
    lt_1_8 = "lt_1_8"
    lt_1_4 = "lt_1_4"
    lt_1_2 = "lt_1_2"
    lt_3_4 = "lt_3_4"
    ge_3_4 = "ge_3_4"


class CharCountClass(enum.StrEnum):
    """Log-4 character-count buckets: xs<8, s<32, m<128, l<512, xl (§4.2)."""

    xs = "xs"
    s = "s"
    m = "m"
    l = "l"  # the spec's bucket name; a one-char enum value, not a variable.
    xl = "xl"


class LineCountClass(enum.StrEnum):
    """Three buckets only: exact address line-counts are a quasi-identifier (§4.2)."""

    one = "1"
    two_three = "2_3"
    four_plus = "4plus"


class HeightClass(enum.StrEnum):
    """Per-page relative typography (matches ``metrics.height_class`` output verbatim)."""

    small = "small"
    body = "body"
    large = "large"
    display = "display"


class NodeFeature(BaseModel):
    """One node as the model sees it — §4.2's complete SAFE set, nothing else.

    Every ``str``-typed field is a closed enum or pattern-constrained; the model has no
    unconstrained string field, so adding one fails ``test_payload_model_closed`` rather
    than code review (§4.2 FORBIDDEN item 6).
    """

    model_config = {"extra": "forbid"}

    id: NId
    path: PathStr
    kind: NodeKind
    page: int
    band: int | None = None
    frame: int | None = None
    #: R7: the template-fixed edge and its 1%-grid permille position. The free edge is
    #: never sent; ``None`` position when the node has no geometry.
    anchor_edge: AnchorEdge = AnchorEdge.left
    anchor_pm: int | None = Field(default=None, ge=0, le=1000)
    #: Vertical position on the same 1% grid (template-fixed; vertical EXTENT is
    #: ``line_count_class``).
    y0_pm: int | None = Field(default=None, ge=0, le=1000)
    w_class: WClass = WClass.lt_1_16
    char_count_class: CharCountClass = CharCountClass.xs
    line_count_class: LineCountClass = LineCountClass.one
    ends_terminal_punct: bool = False
    ends_hyphen: bool = False
    starts_lowercase: bool | None = None
    height_class: HeightClass = HeightClass.body
    script_class: ScriptClass = ScriptClass.none
    digit_ratio_class: DigitRatioClass = DigitRatioClass.none
    alignment_class: Alignment = Alignment.unknown
    at_frame_top: bool = False
    at_frame_bottom: bool = False
    #: The builder's own confessed uncertainty (an ``order_ties`` participant).
    uncertain: bool = False
    #: Carry-over from the previous window; ops may not target it (V2, R6 excepted).
    context: bool = False


# ---------------------------------------------------------------------------------------
# Integer bucketing helpers — every threshold a cross-multiplied integer test
# ---------------------------------------------------------------------------------------
def _w_class(width_mu: int, page_w_mu: int) -> WClass:
    """Width-fraction bucket via integer cross-multiplication (no division, no floats)."""
    if page_w_mu <= 0 or width_mu <= 0:
        return WClass.lt_1_16
    if 16 * width_mu < page_w_mu:
        return WClass.lt_1_16
    if 8 * width_mu < page_w_mu:
        return WClass.lt_1_8
    if 4 * width_mu < page_w_mu:
        return WClass.lt_1_4
    if 2 * width_mu < page_w_mu:
        return WClass.lt_1_2
    if 4 * width_mu < 3 * page_w_mu:
        return WClass.lt_3_4
    return WClass.ge_3_4


def _char_class(count: int) -> CharCountClass:
    if count < 8:
        return CharCountClass.xs
    if count < 32:
        return CharCountClass.s
    if count < 128:
        return CharCountClass.m
    if count < 512:
        return CharCountClass.l
    return CharCountClass.xl


def _line_class(count: int) -> LineCountClass:
    if count <= 1:
        return LineCountClass.one
    if count <= 3:
        return LineCountClass.two_three
    return LineCountClass.four_plus


def _grid_pm(position_mu: int, extent_mu: int) -> int | None:
    """Permille of ``position`` within ``extent``, snapped to the 1% grid (R7).

    Integer round-half-up to the nearest percent, then x10 to permille — so the value is
    always one of 0, 10, 20 … 1000 and sub-percent position never leaves the process.
    """
    if extent_mu <= 0:
        return None
    clamped = min(max(position_mu, 0), extent_mu)
    percent = (200 * clamped + extent_mu) // (2 * extent_mu)
    return min(percent, 100) * 10


def _anchor(
    node: Node, metrics: Metrics | None, page_w_mu: int
) -> tuple[AnchorEdge, int | None]:
    """R7: the alignment-anchored edge only. Right-aligned boxes anchor their RIGHT edge
    (the template-fixed one); centered boxes their centre; everything else the left edge —
    ``justified``/``unknown`` included, because for those the left edge is the printed
    field's fixed edge and the right edge would re-open the width oracle."""
    if node.bbox is None:
        return AnchorEdge.left, None
    alignment = metrics.alignment if metrics is not None else Alignment.unknown
    if alignment is Alignment.right:
        return AnchorEdge.right, _grid_pm(node.bbox[2], page_w_mu)
    if alignment is Alignment.center:
        return AnchorEdge.center, _grid_pm((node.bbox[0] + node.bbox[2]) // 2, page_w_mu)
    return AnchorEdge.left, _grid_pm(node.bbox[0], page_w_mu)


# ---------------------------------------------------------------------------------------
# build_features
# ---------------------------------------------------------------------------------------
def _page_dims(tree: DocTree) -> dict[int, tuple[int, int]]:
    return {p.page: (p.width_mu, p.height_mu) for p in tree.pages}


def _page_ems(view: LayoutView, pages: list[int]) -> dict[int, int]:
    """Per-page em via the canvas's own measurement — 0 (honest unknown) on any failure."""
    from dpc.canvas import atoms_for_page, page_em  # local: canvas is heavy, feature-only

    out: dict[int, int] = {}
    for page in pages:
        try:
            atoms, _, _ = atoms_for_page(view, page)
            out[page] = page_em(atoms)
        except Exception:  # noqa: BLE001 - a page the canvas cannot read has em 0, not a raise.
            out[page] = 0
    return out


def _frame_band_ranges(
    nodes: list[Node],
) -> dict[tuple[int, int | None, int | None], tuple[int, int]]:
    """Band extents per COLUMN key ``(page, region_ix, frame_ix)`` and per page
    ``(page, None, None)``.

    The column key is registered only for nodes with a real ``frame_ix``: on a
    single-column page the canvas gives every band its own linear region, so a
    region-scoped range would collapse to one band and make every paragraph
    "frame-top AND frame-bottom" — the page-level range is the honest denominator
    there (and is exactly what the cross-page gate of §3.3 uses)."""
    out: dict[tuple[int, int | None, int | None], tuple[int, int]] = {}
    for node in nodes:
        band = node.prov.band_ix
        if band is None:
            continue
        keys: list[tuple[int, int | None, int | None]] = [(node.page, None, None)]
        if node.prov.frame_ix is not None:
            keys.append((node.page, node.prov.region_ix, node.prov.frame_ix))
        for key in keys:
            lo, hi = out.get(key, (band, band))
            out[key] = (min(lo, band), max(hi, band))
    return out


def build_features(tree: DocTree, view: LayoutView) -> dict[int, NodeFeature]:
    """§4.2's projection for every body node — keyed by TREE node id.

    Window-local ``n{k}`` ids and the ``context`` flag are assigned by
    :func:`dpc.arrange.payload.make_windows`; features here carry the placeholder ``id="n0"``
    and ``context=False``, replaced per window via ``model_copy``.

    Args:
        tree: The heuristic tree (source of structure, metrics, geometry).
        view: The layout view the tree indexes into — used ONLY for the per-page em (the
            height-class denominator); no text is read here.

    Returns:
        ``{node_id: NodeFeature}`` for every node under ``body`` (except the ``body`` root)
        plus the furniture root's LEAF children — furniture leaves are windowed too, because
        V4's one furniture affordance (``reparent`` with ``FURNITURE_MISPLACED``, R9) is the
        model's only way to rescue a mis-zoned block, and an unaddressable node cannot be
        rescued.
    """
    dims = _page_dims(tree)
    body_nodes = [n for n in walk_body(tree) if n.kind is not NodeKind.body]
    if 0 <= tree.furniture < len(tree.nodes):
        body_nodes.extend(
            tree.nodes[i] for i in tree.nodes[tree.furniture].children
            if 0 <= i < len(tree.nodes)
        )
    ems = _page_ems(view, sorted({n.page for n in body_nodes}))
    ranges = _frame_band_ranges(body_nodes)
    uncertain_ids = {i for tie in tree.report.order_ties for i in tie[:2]}

    out: dict[int, NodeFeature] = {}
    for node in body_nodes:
        page_w, page_h = dims.get(node.page, (0, 0))
        metrics = node.metrics
        if node.children:
            # CONTAINERS carry structure, never geometry buckets. A container's bbox is the
            # UNION of its children, and a union edge is content-dependent: the two-identities
            # fixture proved a frame of right-aligned values anchors its left edge at the
            # LONGEST value's free edge — R7's length oracle re-opened one level up. Kind,
            # page, band/frame and path say everything ordering needs about a container.
            edge, anchor_pm, width = AnchorEdge.left, None, 0
        else:
            edge, anchor_pm = _anchor(node, metrics, page_w)
            width = (node.bbox[2] - node.bbox[0]) if node.bbox is not None else 0

        at_top = at_bottom = False
        band = node.prov.band_ix
        if band is not None:
            key: tuple[int, int | None, int | None] = (
                (node.page, node.prov.region_ix, node.prov.frame_ix)
                if node.prov.frame_ix is not None else (node.page, None, None)
            )
            lo, hi = ranges.get(key) or ranges.get((node.page, None, None)) or (band, band)
            at_top = band <= lo + (GATE_TAIL - 1)
            at_bottom = band >= hi - (GATE_TAIL - 1)

        out[node.id] = NodeFeature(
            id="n0",
            path=node.path,
            kind=node.kind,
            page=node.page,
            band=band,
            frame=node.prov.frame_ix,
            anchor_edge=edge,
            anchor_pm=anchor_pm,
            y0_pm=(
                _grid_pm(node.bbox[1], page_h)
                if node.bbox is not None and not node.children else None
            ),
            w_class=_w_class(width, page_w),
            char_count_class=_char_class(metrics.char_count if metrics else 0),
            line_count_class=_line_class(metrics.line_count if metrics else 0),
            ends_terminal_punct=bool(metrics.ends_terminal_punct) if metrics else False,
            ends_hyphen=bool(metrics.ends_hyphen) if metrics else False,
            starts_lowercase=metrics.starts_lowercase if metrics else None,
            height_class=HeightClass(
                height_class(metrics.height_mu if metrics else 0, ems.get(node.page, 0))
            ),
            script_class=metrics.script_class if metrics else ScriptClass.none,
            digit_ratio_class=metrics.digit_ratio_class if metrics else DigitRatioClass.none,
            alignment_class=metrics.alignment if metrics else Alignment.unknown,
            at_frame_top=at_top,
            at_frame_bottom=at_bottom,
            uncertain=node.id in uncertain_ids,
            context=False,
        )
    return out


__all__ = [
    "AnchorEdge",
    "CharCountClass",
    "HeightClass",
    "LineCountClass",
    "NId",
    "NodeFeature",
    "PathStr",
    "WClass",
    "build_features",
]
