"""Adversarial tests for the spatial layout engine.

The engine's output is a product contract — a stored sha256 that a caller compares across
re-conversions — so these tests are written against the two things that can break it:

* **the pure functions** (:func:`~dpc.canvas.mu`, :func:`~dpc.canvas.cell_width`), where a
  single wrong code point silently shifts one column of every canvas in the corpus; and
* **the properties the algorithm claims by construction** — frozen-seed banding does not
  chain, the coverage gate refuses a block its lines cannot reconstruct, placement loses no
  text, and equal geometry gives equal bytes regardless of the order the objects arrived in.

Where a test asserts a NEGATIVE ("this is not single-linkage clustering") it implements the
wrong algorithm alongside the right one and shows they disagree, because a test that only
exercises the implementation cannot tell you the implementation is the one you meant.
"""
from __future__ import annotations

import math
import random
import unicodedata

from dpc.canvas import (
    CANVAS_SEG_CHARS,
    CANVAS_SEG_ROWS,
    GUTTER_CELLS,
    MAX_ATOMS_PER_PAGE,
    MAX_ROW_GAP,
    MIN_GUTTER_EM,
    REASON_RANK,
    TAB_SNAP,
    Atom,
    Frame,
    _frame_of,
    _measure_adv,
    _off,
    build_bands,
    build_frames,
    build_regions,
    cell_width,
    find_gutters,
    is_rtl,
    mark_separators,
    mu,
    page_em,
    page_layout,
    page_skew_ok,
    render_canvas,
    segment,
    snap_tabs,
)
from dpc.models import KeyValue, LayoutView, Mark, PageInfo, Quad, TextBlock, TextLine, Zone

# ---------------------------------------------------------------------------------------
# Fixture builders. Coordinates are inches on a US-Letter page, so mu() is milli-inches and
# the numbers in the assertions are the same ones §6 of the spec works through.
# ---------------------------------------------------------------------------------------

EM_IN = 0.139          # 10 pt
LEAD_IN = 0.200        # single-spaced 10 pt
LEFT = (0.72, 4.00)
RIGHT = (4.45, 7.78)


def quad(x0: float, y0: float, x1: float, y1: float) -> Quad:
    """An axis-aligned quad, clockwise from top-left — the shape every adapter emits."""
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def line_block(text: str, x0: float, y0: float, x1: float, page: int = 1) -> TextBlock:
    """A one-line block: ``block.text`` is exactly its single line, so coverage passes."""
    box = quad(x0, y0, x1, y0 + EM_IN)
    return TextBlock(text=text, page=page, bbox=box, lines=[TextLine(text=text, bbox=box)])


def two_column_view(rows: int = 12) -> LayoutView:
    """A clean two-column page: ``rows`` bands, each with a left and a right line."""
    blocks: list[TextBlock] = []
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"left line number {i:02d}", LEFT[0], y, LEFT[1]))
        blocks.append(line_block(f"right line number {i:02d}", RIGHT[0], y, RIGHT[1]))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def crowded_two_column_view(rows: int = 10) -> LayoutView:
    """Two columns with TWO atoms in EVERY frame of EVERY band: a label and its value.

    ``two_column_view`` has exactly ONE atom per frame per band, so step 9's cursor rule
    (``placed = max(true_col, cursor + 1)``) never fires on it and every property that rule can
    break — the declared width, the gutter, the x-inversion — is vacuously true there. This
    fixture is the smallest realistic layout on which the rule actually runs: the label ends
    exactly where the value's ``true_col`` begins, so ~half the atoms are nudged right by one
    cell. A test about placement that runs only on ``two_column_view`` proves nothing.
    """
    blocks: list[TextBlock] = []
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"label {i:02d}", 0.72, y, 1.60))
        blocks.append(line_block(f"left value {i:02d}", 1.60, y, 3.10))
        blocks.append(line_block(f"key {i:02d}", 4.45, y, 5.16))
        blocks.append(line_block(f"right value {i:02d}", 5.16, y, 7.00))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def frame_overflow_view(rows: int = 10) -> LayoutView:
    """The sharp case: a ONE-cell atom sharing its neighbour's offset, in every frame.

    The marker writes one cell at the frame's offset 0 and leaves ``cursor`` at 1; the value's
    own ``true_col`` is also offset 0, so the cursor rule places it at offset 2 — a drift of
    exactly ``MAX_DRIFT``, which the drift test alone waves through — and it then ends two
    cells past a frame that step 7 budgeted for offset 0. Pre-fix this overflowed the declared
    width on EVERY row and cut the visible gutter from 4 cells to 2.
    """
    blocks: list[TextBlock] = []
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block("*", 0.72, y, 0.80))
        blocks.append(line_block(f"left column value {i:02d}", 0.74, y, 3.10))
        blocks.append(line_block("*", 4.45, y, 4.53))
        blocks.append(line_block(f"right column value {i:02d}", 4.47, y, 7.00))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


#: Every two-column fixture on which a placement property must hold. The first has one atom per
#: frame per band (the cursor rule never fires), the second has two (it fires on half of them),
#: the third makes it fire in the one configuration that used to overflow.
PLACEMENT_VIEWS = {
    "two_column": two_column_view,
    "crowded": crowded_two_column_view,
    "frame_overflow": frame_overflow_view,
}


def single_column_view(rows: int = 12) -> LayoutView:
    """The same page with one full-measure-minus-a-bit column: there is no corridor."""
    blocks = [
        line_block(f"single column line number {i:02d}", 0.72, 2.0 + i * LEAD_IN, 7.00)
        for i in range(rows)
    ]
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def _atom(y0: int, y1: int, ix: int, x0: int = 0, x1: int = 1000) -> Atom:
    return Atom(
        kind="line", text="x" * 8, x0=x0, y0=y0, x1=x1, y1=y1,
        skew_num=0, skew_den=1, source_ix=ix, sub_ix=0, block_ix=ix,
        multiline=False, tag="",
    )


def single_linkage_bands(atoms: list[Atom]) -> int:
    """pdfplumber's ``cluster_list``, deliberately: compare each atom to the PREVIOUS one.

    This is the algorithm the spec says must NOT be implemented. It is here so the
    anti-chaining test can show a disagreement rather than merely assert a number.
    """
    ordered = sorted(atoms, key=lambda a: a.sort_key)
    clusters = 1
    prev = ordered[0]
    for atom in ordered[1:]:
        overlap = max(0, min(atom.y1, prev.y1) - max(atom.y0, prev.y0))
        shorter = min(atom.y1 - atom.y0, prev.y1 - prev.y0)
        if overlap * 2 < shorter:
            clusters += 1
        prev = atom
    return clusters


# ---------------------------------------------------------------------------------------
# mu() — the one place a float is allowed to exist
# ---------------------------------------------------------------------------------------


def test_mu_is_half_up_not_bankers():
    """The exact .0005 boundaries where ``round()`` and half-up disagree.

    ``round(0.5) == 0`` and ``round(2.5) == 2`` under banker's rounding; half-up gives 1 and 3.
    A quad coordinate that lands on such a boundary would otherwise flip a whole column of a
    canvas on a 1-ULP change upstream.
    """
    assert mu(0.0005) == 1
    assert round(0.0005 * 1000.0) == 0        # the trap, stated
    assert mu(0.0025) == 3
    assert round(0.0025 * 1000.0) == 2        # the trap, again
    assert mu(0.0015) == 2
    assert mu(0.0) == 0
    assert mu(-0.0005) == 0                   # floor(-0.5 + 0.5)
    assert mu(2.0005) == 2001


def test_mu_matches_its_own_definition_over_a_sweep():
    """mu is exactly ``floor(v * 1000 + 0.5)`` — no shortcut, no ``round``, no ``int()``."""
    for i in range(-500, 5000):
        value = i / 997.0
        assert mu(value) == math.floor(value * 1000.0 + 0.5)


def test_mu_is_monotonic():
    """Monotonicity is what lets a rectangle be built by ``min``/``max`` after quantisation."""
    prev = mu(-1001 / 333.0)
    for i in range(-1000, 1000):
        cur = mu(i / 333.0)
        assert cur >= prev
        prev = cur


# ---------------------------------------------------------------------------------------
# cell_width() — load-bearing for every column alignment
# ---------------------------------------------------------------------------------------


def test_cell_width_ascii():
    assert cell_width("") == 0
    assert cell_width("abc") == 3
    assert cell_width("[x]") == 3
    assert cell_width("Full legal name:") == 16


def test_cell_width_cjk_is_two_per_glyph():
    """poppler counts one column per code point here and under-pads everything to the right."""
    assert cell_width("日本語") == 6          # W
    assert cell_width("ＡＢ") == 4                 # F (fullwidth Latin)
    assert cell_width("a日b") == 4


def test_cell_width_combining_and_format_are_zero():
    """A combining mark occupies no cell of its own; nor does a format character."""
    assert cell_width("e\u0301") == 1        # e + COMBINING ACUTE
    assert cell_width("\u200d") == 0         # ZERO WIDTH JOINER, category Cf
    assert cell_width("\u200b") == 0         # ZERO WIDTH SPACE, category Cf
    assert cell_width("a\u0301\u0301b") == 2  # two stacked marks, still two cells
    assert cell_width("\u0e31") == 0         # THAI MAI HAN AKAT, category Mn


def test_cell_width_emoji_is_wide():
    assert cell_width("\U0001f600") == 2
    assert cell_width("ok \U0001f600") == 5


def test_cell_width_ambiguous_counts_one():
    """Ambiguous (A) is 1, matching every wcwidth narrow default. U+2611 is why marks are ASCII."""
    assert cell_width("Ω") == 1                       # GREEK CAPITAL OMEGA, EAW A
    assert cell_width("±") == 1                       # PLUS-MINUS, EAW A


def test_cell_width_is_additive():
    """A pure, additive function of the string — the property every padding calculation uses."""
    parts = ["abc", "日本", "é", "\U0001f600", ""]
    assert cell_width("".join(parts)) == sum(cell_width(p) for p in parts)


def test_unicode_version_is_the_one_pinned_in_front_matter():
    """Documents the coupling §7.3 makes visible: cell_width is a function of the UCD too."""
    assert unicodedata.unidata_version


def test_is_rtl():
    """A STRICT majority of the STRONG characters. Digits and punctuation do not vote."""
    assert is_rtl("الرياض") is True                 # Arabic, all AL
    assert is_rtl("Riyadh") is False
    assert is_rtl("1234") is False                  # no strong characters at all
    assert is_rtl("") is False
    assert is_rtl("-17.5%") is False                # numerals and punctuation are neutral
    assert is_rtl("שלום abc") is True               # 4 R vs 3 L
    assert is_rtl("של abcd") is False               # 2 R vs 4 L
    assert is_rtl("ab شب") is False                 # 2 R vs 2 L is not a STRICT majority


# ---------------------------------------------------------------------------------------
# Step 3 — the frozen seed. THE key anti-regression property.
# ---------------------------------------------------------------------------------------


def test_forty_drifting_body_lines_yield_forty_bands():
    """Forty body lines at ordinary leading, tops drifting by a hair, must stay forty bands."""
    atoms = []
    for i in range(40):
        y0 = mu(2.0 + i * LEAD_IN + i * 0.0004)     # cumulative sub-quantum drift
        atoms.append(_atom(y0, y0 + mu(EM_IN), i))
    bands = build_bands(atoms, mu(EM_IN))
    assert len(bands) == 40
    assert all(len(b.atoms) == 1 for b in bands)


def test_frozen_seed_refuses_to_chain_where_single_linkage_does_not():
    """The anti-chaining property, shown as a DISAGREEMENT with the algorithm it replaces.

    Leading 0.05 in under a 0.139 in line height: consecutive boxes overlap 64%, so
    pdfplumber's previous-element comparison chains all forty lines into ONE cluster — a step
    function of a continuous input. The frozen seed cannot creep, because every atom is tested
    against the seed's own interval, which never moves.
    """
    atoms = []
    for i in range(40):
        y0 = mu(2.0 + i * 0.05)
        atoms.append(_atom(y0, y0 + mu(EM_IN), i))

    assert single_linkage_bands(atoms) == 1, "the fixture must actually chain under the wrong algorithm"

    bands = build_bands(atoms, mu(EM_IN))
    assert len(bands) > 1
    # Seed [y0, y0+139]; atom i joins iff 139 - 50*i >= 69 (half the shorter height), so each
    # seed takes itself and one neighbour: 20 bands, bounded and independent of page length.
    assert len(bands) == 20
    assert max(b.y1 - b.y0 for b in bands) < mu(2.0)   # no band swallowed the page


def test_band_extent_grows_but_the_test_interval_does_not():
    """The reported extent is the union; the seed interval is frozen. Both are observable."""
    seed = _atom(1000, 1139, 0)
    joiner = _atom(1060, 1220, 1)        # overlaps the seed by 79 of its own 160 -> joins
    stray = _atom(1180, 1330, 2)         # overlaps the SEED by 0 -> must not join
    bands = build_bands([seed, joiner, stray], 139)
    assert len(bands) == 2
    assert (bands[0].seed_y0, bands[0].seed_y1) == (1000, 1139)
    assert (bands[0].y0, bands[0].y1) == (1000, 1220)
    assert [a.source_ix for a in bands[0].atoms] == [0, 1]
    assert [a.source_ix for a in bands[1].atoms] == [2]


def test_exact_duplicates_within_a_band_are_dropped():
    """The fake-bold / drop-shadow case: same milli-unit rect AND same text. First one wins."""
    # Same rect, same text, different source index — the shadow copy a renderer paints twice.
    a = _atom(1000, 1139, 0)
    b = _atom(1000, 1139, 1)
    bands = build_bands([a, b], 139)
    assert len(bands) == 1
    assert len(bands[0].atoms) == 1
    assert bands[0].atoms[0].source_ix == 0


def test_a_suppressed_duplicate_donates_its_tag_to_the_survivor():
    """A duplicate duplicates the TEXT; it does not duplicate the TAG.

    A shadow copy that carries ``Zone.heading`` while the survivor is a plain body line used to
    take the heading with it: the dropped block appears in no region's ``block_ixs`` and in no
    ``floating_blocks``, so its legend entry vanished with no reason recorded anywhere. Text
    loss is impossible by definition here — the twin's text IS the survivor's — which is
    exactly why nothing in the information-loss suite could ever have caught it.
    """
    plain = _atom(1000, 1139, 0)
    tagged = replace_atom_tag(_atom(1000, 1139, 1), "heading")
    bands = build_bands([plain, tagged], 139)
    assert len(bands[0].atoms) == 1, "the duplicate was not suppressed"
    assert bands[0].atoms[0].source_ix == 0, "the wrong copy survived"
    assert bands[0].atoms[0].tag == "heading", "the dropped twin took its tag with it"

    # And it never OVERWRITES a tag the survivor already has: first non-empty in sort order.
    first = replace_atom_tag(_atom(1000, 1139, 0), "title")
    second = replace_atom_tag(_atom(1000, 1139, 1), "heading")
    kept = build_bands([first, second], 139)[0].atoms
    assert len(kept) == 1
    assert kept[0].tag == "title"


def replace_atom_tag(atom: Atom, tag: str) -> Atom:
    """The same atom with a different legend tag — ``Atom`` is frozen, so this is a rebuild."""
    return Atom(
        kind=atom.kind, text=atom.text, x0=atom.x0, y0=atom.y0, x1=atom.x1, y1=atom.y1,
        skew_num=atom.skew_num, skew_den=atom.skew_den, source_ix=atom.source_ix,
        sub_ix=atom.sub_ix, block_ix=atom.block_ix, multiline=atom.multiline, tag=tag,
    )


def test_band_order_is_ascending_seed_y0():
    """Bands are emitted in seed order. There is no second sort, so this must hold by sweep."""
    atoms = [_atom(mu(5.0 - i * 0.3), mu(5.0 - i * 0.3) + mu(EM_IN), i) for i in range(10)]
    bands = build_bands(atoms, mu(EM_IN))
    assert [b.seed_y0 for b in bands] == sorted(b.seed_y0 for b in bands)


# ---------------------------------------------------------------------------------------
# Step 2 — em and the gates
# ---------------------------------------------------------------------------------------


def test_page_em_is_exact_lower_quartile_index_selection():
    """``sorted(h)[(n-1)//4]`` — never an interpolated percentile."""
    heights = [12000, 18000, 18000, 30000, 30000, 30000, 40000, 60000]
    atoms = [_atom(0, h, i) for i, h in enumerate(heights)]
    assert page_em(atoms) == 18000               # sorted[(8-1)//4] == sorted[1]
    assert page_em([]) == 0
    assert page_em([_atom(0, 500, 0)]) == 500    # sorted[0]


def test_skew_gate_uses_the_measured_median_not_the_page_angle():
    flat = [
        Atom(kind="line", text="body text here", x0=0, y0=i * 200, x1=1000, y1=i * 200 + 139,
             skew_num=1, skew_den=1000, source_ix=i, sub_ix=0, block_ix=i,
             multiline=False, tag="")
        for i in range(10)
    ]
    assert page_skew_ok(flat) is True
    skewed = [
        Atom(kind="line", text="body text here", x0=0, y0=i * 200, x1=1000, y1=i * 200 + 139,
             skew_num=52, skew_den=1000, source_ix=i, sub_ix=0, block_ix=i,
             multiline=False, tag="")
        for i in range(10)
    ]
    assert page_skew_ok(skewed) is False      # 50*52 > 1000
    assert page_skew_ok([]) is True


def test_skewed_page_declines_to_linear_only():
    view = two_column_view()
    for block in view.blocks:
        assert block.bbox is not None
        x0, y0, x1, _y0b, _x1b, y1 = (
            block.bbox[0], block.bbox[1], block.bbox[2], block.bbox[3], block.bbox[4],
            block.bbox[5],
        )
        tilt = (x1 - x0) * 0.06          # tan ~ 0.06, three times MAX_SKEW
        block.bbox = [x0, y0, x1, y0 + tilt, x1, y1 + tilt, x0, y1]
        block.lines[0].bbox = block.bbox
    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "skew"


def test_page_with_no_geometry_declines():
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
        blocks=[TextBlock(text="a paragraph with no polygon at all", page=1)],
    )
    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "no-geometry"
    # A declined page carries no floating lists: step 11 sends the WHOLE page through the
    # linear path, where PMD 1.0's own "no geometry -> end of page, no anchor" rule applies.
    assert out.floating_blocks == []


def test_floating_members_are_reported_on_a_page_that_does_produce_a_canvas():
    """A block with no geometry at all must be named, so the emitter can append it honestly."""
    view = two_column_view()
    view.blocks.append(TextBlock(text="a footnote the provider gave no polygon for", page=1))
    out = page_layout(view, 1)
    assert out.canvases == 1
    assert out.floating_blocks == [len(view.blocks) - 1]


def test_dense_page_declines_in_bounded_time():
    blocks = [
        line_block(f"row {i}", 0.72, 0.10 + (i % 400) * 0.02, 2.00)
        for i in range(MAX_ATOMS_PER_PAGE + 1)
    ]
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "too-dense"


# ---------------------------------------------------------------------------------------
# Steps 4-6 — separators and gutters
# ---------------------------------------------------------------------------------------


def test_two_column_region_produces_a_gutter():
    view = two_column_view()
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    bands = mark_separators(build_bands(atoms, em), x0, x1)
    gutters = find_gutters(bands, em, x0, x1)
    assert len(gutters) >= 1
    gx0, gx1 = gutters[0]
    assert mu(LEFT[1]) <= gx0 < gx1 <= mu(RIGHT[0])


def test_single_column_page_produces_no_gutter():
    view = single_column_view()
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    bands = mark_separators(build_bands(atoms, em), x0, x1)
    assert find_gutters(bands, em, x0, x1) == []


def test_edge_whitespace_is_a_margin_not_a_corridor():
    """A clear run touching bucket 0 or the last bucket is a margin, and must be rejected."""
    view = single_column_view()
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    # Widen the content extent artificially so a large clear run exists on the right edge.
    bands = mark_separators(build_bands(atoms, em), mu(0.72), mu(8.00))
    assert find_gutters(bands, em, mu(0.72), mu(8.00)) == []


def test_too_few_bands_yields_no_gutters():
    """``MIN_GUTTER_ROWS`` is an absolute floor of evidence: three rows are not a column."""
    view = two_column_view(rows=3)
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    bands = mark_separators(build_bands(atoms, em), x0, x1)
    assert find_gutters(bands, em, x0, x1) == []


def test_full_measure_line_is_a_separator_and_a_wide_hull_is_not():
    """Both halves of the name, including the half the old version never constructed.

    §4.2 step 4(b) excludes MULTILINE atoms, and the spec says that clause is what stops every
    justified paragraph from shattering its own region: a wrapped paragraph's hull routinely
    spans the full measure. Deleting ``not atom.multiline`` from ``mark_separators`` used to
    pass the whole suite, because no test built a wide hull.
    """
    view = two_column_view()
    view.blocks.append(line_block("A FULL MEASURE DOCUMENT HEADING", 0.72, 1.40, 7.78))
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    bands = mark_separators(build_bands(atoms, em), x0, x1)
    seps = [b for b in bands if b.separator]
    assert len(seps) == 1
    assert seps[0].y0 == mu(1.40)

    # The other half: a FULL-MEASURE MULTILINE hull — a justified paragraph — is NOT a divider.
    wide = two_column_view()
    wide.blocks.append(TextBlock(
        text="a justified paragraph whose hull spans the whole measure and four visual rows",
        page=1, bbox=quad(0.72, 1.20, 7.78, 1.20 + 4 * LEAD_IN), lines=[],
    ))
    atoms, _, _ = _atoms(wide)
    hulls = [a for a in atoms if a.multiline]
    assert len(hulls) == 1, "the fixture did not produce a multiline hull"
    assert (hulls[0].x1 - hulls[0].x0) * 10 >= (7780 - 720) * 9, "the hull is not full measure"
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    bands = mark_separators(build_bands(atoms, em), x0, x1)
    assert not [b for b in bands if b.separator], (
        "a full-measure multiline hull was treated as a page divider"
    )


def _two_columns_with_gap(gap_in: float, rows: int = 8) -> LayoutView:
    """Two columns whose corridor is exactly ``gap_in`` inches wide."""
    blocks = []
    left_x1 = 3.90
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"left column line {i:02d}", 0.72, y, left_x1))
        blocks.append(
            line_block(f"right column line {i:02d}", left_x1 + gap_in, y, 7.40)
        )
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def _gutters_of(view: LayoutView):
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)
    return find_gutters(mark_separators(build_bands(atoms, em), x0, x1), em, x0, x1)


def test_a_corridor_narrower_than_MIN_GUTTER_EM_is_not_a_gutter():
    """§4.3: 1.5 em is poppler's ``maxWordSpacing``, the LARGEST gap that still sits inside one
    line. A narrower run could be sitting inside somebody's sentence, so it cannot be a column
    boundary. Dropping the floor to 0.1 em passed the whole suite.
    """
    assert MIN_GUTTER_EM == (3, 2)
    em = 139                                     # the fixtures' 10 pt em, in milli-inches
    narrow = 0.9 * em / 1000                     # 0.9 em: a wide word gap, not a corridor
    wide = 2.5 * em / 1000                       # 2.5 em: unambiguously a corridor
    assert _gutters_of(_two_columns_with_gap(narrow)) == [], (
        "a sub-1.5-em word gap was accepted as a column boundary"
    )
    found = _gutters_of(_two_columns_with_gap(wide))
    assert len(found) == 1, "a 2.5 em corridor was not found"
    assert (found[0][1] - found[0][0]) * MIN_GUTTER_EM[1] >= em * MIN_GUTTER_EM[0]


def test_vertical_gaps_are_clamped_to_MAX_ROW_GAP():
    """poppler's ``d = clamp((base_next - base) / fontSize, 1, 5)``: a half-empty page must not
    become forty blank lines. Raising the clamp to 500 passed the whole suite."""
    assert MAX_ROW_GAP == 5
    blocks = []
    for i in range(8):
        # A 4-inch chasm between band 3 and band 4 — ~29 em, far past the clamp.
        y = 2.0 + i * LEAD_IN + (4.0 if i >= 4 else 0.0)
        blocks.append(line_block(f"left column line {i:02d}", 0.72, y, LEFT[1]))
        blocks.append(line_block(f"right column line {i:02d}", RIGHT[0], y, RIGHT[1]))
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    _out, region = _spatial(view)
    runs, run = [], 0
    for row in region.rows:
        if row == "":
            run += 1
            continue
        if run:
            runs.append(run)
        run = 0
    assert runs, "the fixture produced no blank rows at all"
    assert max(runs) == MAX_ROW_GAP, f"blank-row runs were {runs}"


def test_frame_of_resolves_a_gutter_centre_to_the_frame_on_its_right():
    """The tie-break is written down and pinned, because the docstring used to say the opposite.

    Nothing in §4.2 step 7 fixes the gutter-centre case, so what matters is that the rule is a
    pure function of the geometry and that the comment describing it is TRUE. The loop is "the
    first frame whose ``x1`` is at or beyond the centre", which for a centre inside a gutter is
    the frame on the RIGHT — an earlier docstring claimed "the frame on its left".
    """
    frames = [
        Frame(x0=720, x1=4080, adv=70, cells=48, col_start=0),
        Frame(x0=4430, x1=7780, adv=70, cells=48, col_start=51),
    ]
    in_gutter = Atom(
        kind="line", text="x", x0=4200, y0=0, x1=4300, y1=100, skew_num=0, skew_den=1,
        source_ix=0, sub_ix=0, block_ix=0, multiline=False, tag="",
    )
    assert 4080 < (in_gutter.x0 + in_gutter.x1) // 2 < 4430, "the centre is not in the gutter"
    assert _frame_of(frames, in_gutter) == 1

    inside_left = replace_atom_x(in_gutter, 1000, 2000)
    assert _frame_of(frames, inside_left) == 0
    past_right = replace_atom_x(in_gutter, 9000, 9500)
    assert _frame_of(frames, past_right) == len(frames) - 1


def replace_atom_x(atom: Atom, x0: int, x1: int) -> Atom:
    """The same atom moved horizontally — ``Atom`` is frozen, so this is a rebuild."""
    return Atom(
        kind=atom.kind, text=atom.text, x0=x0, y0=atom.y0, x1=x1, y1=atom.y1,
        skew_num=atom.skew_num, skew_den=atom.skew_den, source_ix=atom.source_ix,
        sub_ix=atom.sub_ix, block_ix=atom.block_ix, multiline=atom.multiline, tag=atom.tag,
    )


def test_measure_adv_is_the_LOWER_median_never_the_upper():
    """§7.1's ``(n - 1) // 2``. With an even sample the two medians differ, and nothing pinned
    which one the code takes — an upper-median mutant survived the whole suite."""
    # Four qualifying atoms with advances 100, 200, 300, 400: lower median 200, upper 300.
    atoms = [
        Atom(kind="line", text="abcd", x0=0, y0=0, x1=400 * (i + 1), y1=100,
             skew_num=0, skew_den=1, source_ix=i, sub_ix=0, block_ix=i,
             multiline=False, tag="")
        for i in range(4)
    ]
    assert [(a.x1 - a.x0) // cell_width(a.text) for a in atoms] == [100, 200, 300, 400]
    assert _measure_adv(atoms, em=1000) == 200


def test_measure_adv_ignores_lines_shorter_than_MIN_MEASURE_CELLS():
    """poppler refuses a gap measured from a line with one word; so does this."""
    short = Atom(kind="line", text="ab", x0=0, y0=0, x1=9000, y1=100, skew_num=0, skew_den=1,
                 source_ix=0, sub_ix=0, block_ix=0, multiline=False, tag="")
    long_ = Atom(kind="line", text="abcdef", x0=0, y0=0, x1=600, y1=100, skew_num=0, skew_den=1,
                 source_ix=1, sub_ix=0, block_ix=1, multiline=False, tag="")
    assert _measure_adv([short, long_], em=1000) == 100      # only ``long_`` qualifies
    assert _measure_adv([short], em=1000) == 500             # fallback: em // 2


# ---------------------------------------------------------------------------------------
# Step 5 — THE COVERAGE GATE
# ---------------------------------------------------------------------------------------


def test_coverage_gate_rejects_a_block_its_lines_cannot_reconstruct():
    """A block whose lines do not rebuild ``block.text`` sends its whole region to linear.

    This is the guarantee that makes the information-loss test pass by construction: a block is
    rendered either whole (linear) or as its lines (canvas), never half of each.
    """
    view = two_column_view()
    victim = view.blocks[0]
    victim.text = victim.text + " AND A CLAUSE THE LINE STREAM NEVER SAW"
    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "coverage"


def test_coverage_gate_accepts_a_multi_line_block_that_does_reconstruct():
    """Two lines whose concatenation normalises to ``block.text`` must NOT be rejected."""
    view = two_column_view()
    lines = [
        TextLine(text="first half of the clause",
                 bbox=quad(LEFT[0], 2.0, LEFT[1], 2.0 + EM_IN)),
        TextLine(text="second half of the clause",
                 bbox=quad(LEFT[0], 2.2, LEFT[1], 2.2 + EM_IN)),
    ]
    view.blocks[0] = TextBlock(
        text="first half of the clause   second half of the clause",   # whitespace differs
        page=1, bbox=quad(LEFT[0], 2.0, LEFT[1], 2.2 + EM_IN), lines=lines,
    )
    del view.blocks[2]      # the left line of row 1, whose slot the second line now occupies
    out = page_layout(view, 1)
    assert out.reason == ""
    assert out.canvases == 1


def test_a_block_whose_lines_have_no_polygons_is_still_a_hull():
    """Regression: the fallback to ``block.bbox`` is reached both when a block has NO lines and
    when it has lines the provider gave no polygon for. Both rectangles are paragraph hulls, so
    both must be measured for ``multiline`` — treating the second as a single row would put five
    visual rows of text onto one canvas row."""
    view = two_column_view()
    view.blocks[0] = TextBlock(
        text="a wrapped paragraph the line stream could not place",
        page=1, bbox=quad(LEFT[0], 2.0, LEFT[1], 2.0 + 5 * EM_IN),
        lines=[TextLine(text="a wrapped paragraph"), TextLine(text="the line stream")],
    )
    atoms, _, _ = _atoms(view)
    hulls = [a for a in atoms if a.source_ix == 0 and a.kind == "line"]
    assert len(hulls) == 1, "a bbox-less line must not become an atom"
    assert hulls[0].multiline is True
    assert page_layout(view, 1).reason == "multiline"


def test_multiline_hull_sends_its_region_to_linear():
    """A hull spanning five visual rows cannot go on one canvas row; refusing is the honest
    answer and degrades to exactly PMD 1.0."""
    view = two_column_view()
    view.blocks[0] = TextBlock(
        text="a wrapped paragraph whose hull spans five visual rows",
        page=1, bbox=quad(LEFT[0], 2.0, LEFT[1], 2.0 + 5 * EM_IN),
    )
    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "multiline"


# ---------------------------------------------------------------------------------------
# Steps 7-9 — frames, tab stops, placement
# ---------------------------------------------------------------------------------------


def _atoms(view: LayoutView, page: int = 1):
    from dpc.canvas import atoms_for_page
    return atoms_for_page(view, page)


def _spatial(view: LayoutView):
    out = page_layout(view, 1)
    assert out.canvases == 1, f"expected one canvas, got reason={out.reason!r}"
    return out, out.spatial[0]


def test_two_column_page_produces_two_frames_with_measured_advances():
    _out, region = _spatial(two_column_view())
    assert len(region.frames) == 2
    assert region.frames[0].col_start == 0
    assert region.frames[1].col_start == region.frames[0].cells + GUTTER_CELLS
    assert all(f.adv >= 1 for f in region.frames)


def test_placement_never_loses_text():
    """Every atom's text must appear in the rendered rows. The coarse net for a lost column."""
    view = two_column_view()
    view.marks.append(Mark(state="selected", page=1, bbox=quad(4.45, 2.0, 4.60, 2.0 + EM_IN)))
    _out, region = _spatial(view)
    blob = "\n".join(region.rows)
    for atom in region.atoms:
        if atom.text:
            assert atom.text in blob, f"lost atom kind={atom.kind} ix={atom.source_ix}"


def test_no_row_exceeds_the_canvas_width():
    """Intra-frame overflow is impossible — on a fixture where the cursor rule actually runs.

    ``limit`` is the DECLARED grid width, ``frames[-1].col_start + frames[-1].cells``, which is
    what §5.3's ``canvas <cols>x<rows>`` publishes. Running this on ``two_column_view`` alone
    proved nothing: one atom per frame per band means ``placed == true_col`` always, and step
    7's ``cells_j`` term then makes the assertion true by arithmetic. On ``frame_overflow``
    every row exceeded the limit before the frame's own end became a row-break condition.
    """
    for name, build in PLACEMENT_VIEWS.items():
        _out, region = _spatial(build())
        limit = region.frames[-1].col_start + region.frames[-1].cells
        for i, row in enumerate(region.rows):
            assert cell_width(row) <= limit, (
                f"{name}: row {i} is {cell_width(row)} cells, declared width is {limit}"
            )


def test_no_atom_is_placed_past_its_own_frames_last_column():
    """The per-ATOM form of the same guarantee, which the per-ROW form can only sample.

    §5.3 promises exact x-inversion: ``x(col) = left_j + (col - col_start_j) * adv_j``. A
    placement whose ``col1`` is past ``col_start_j + cells_j`` inverts to an x outside the
    frame it belongs to, so the promise is broken for that atom even if some other atom's
    absence keeps the ROW under the declared width.
    """
    # The CJK fixture joins this one: its right frame has enough slack that a left frame
    # overrunning its own end by two cells still leaves the ROW inside the declared width, so
    # only the per-atom form can see it.
    views = {**PLACEMENT_VIEWS, "cjk": cjk_two_column_view}
    for name, build in views.items():
        view = build()
        for block in view.blocks:          # tag everything so every atom gets a placement
            block.zone = Zone.heading
        _out, region = _spatial(view)
        starts = [f.col_start for f in region.frames]
        for p in region.placements:
            fix = max(i for i, s in enumerate(starts) if s <= p.col0)
            frame = region.frames[fix]
            assert p.col0 >= frame.col_start, f"{name}: {p.key} starts before its frame"
            assert p.col1 < frame.col_start + frame.cells, (
                f"{name}: {p.key} ends at col {p.col1}, frame {fix} ends at "
                f"{frame.col_start + frame.cells - 1}"
            )


def test_columns_do_not_interleave():
    """Left-column text stays left of the gutter column and right-column text stays right."""
    _out, region = _spatial(two_column_view())
    split = region.frames[1].col_start
    for row in region.rows:
        if not row.strip():
            continue
        assert "left line" in row[:split]
        assert "right line" in row[split:]


def test_gutter_between_frames_is_at_least_three_spaces():
    """§9.3's gutter property, measured GENERICALLY and on fixtures where it can fail.

    The gap is measured at the frame boundary — trailing spaces of everything left of
    ``col_start_1``, plus leading spaces of everything right of it — rather than between two
    known substrings, so it holds however many atoms a frame contains. The old version keyed on
    ``"left line"``/``"right line"``, which only exist in the one-atom-per-frame fixture whose
    cursor rule never fires; on ``frame_overflow`` the real gap was 2, below ``GUTTER_CELLS``.
    """
    for name, build in PLACEMENT_VIEWS.items():
        _out, region = _spatial(build())
        split = region.frames[1].col_start
        for i, row in enumerate(region.rows):
            left, right = row[:split], row[split:]
            if not left.strip() or not right.strip():
                continue
            gap = (len(left) - len(left.rstrip())) + (len(right) - len(right.lstrip()))
            assert gap >= GUTTER_CELLS, f"{name}: row {i} has a {gap}-cell gutter: {row!r}"


def indented_view(rows: int = 8) -> LayoutView:
    """A full-measure heading over a two-column region whose LEFT column is itself indented.

    The heading fixes ``content_x0`` at 0.72 in while the left column starts at 1.50 in, so
    every canvas row must carry real leading spaces — and the heading must land in its own
    linear region rather than inside the canvas.
    """
    blocks = [line_block("A FULL MEASURE DOCUMENT HEADING ACROSS THE PAGE", 0.72, 1.40, 7.78)]
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"indented left {i:02d}", 1.50, y, 4.00))
        blocks.append(line_block(f"right column {i:02d}", RIGHT[0], y, RIGHT[1]))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def test_rows_are_rstripped_but_leading_spaces_survive():
    """Leading spaces are the payload; trailing spaces are variance."""
    out = page_layout(indented_view(), 1)
    assert out.canvases == 1
    region = out.spatial[0]
    indents = {len(r) - len(r.lstrip(" ")) for r in region.rows}
    assert indents == {5}, f"the 0.78 in indent was lost or jittered: {sorted(indents)}"
    for row in region.rows:
        assert row == row.rstrip()


def test_separator_band_is_its_own_linear_region():
    """A full-measure heading divides the page; it never joins the canvas below it."""
    out = page_layout(indented_view(), 1)
    assert [r.kind for r in out.regions] == ["linear", "spatial"]
    assert out.regions[0].block_ixs == [0]
    assert out.regions[0].bands[0].separator is True
    assert 0 not in out.regions[1].block_ixs


def test_right_column_starts_at_its_frame_on_every_row():
    _out, region = _spatial(two_column_view())
    starts = {row.index("right line") for row in region.rows if "right line" in row}
    assert len(starts) == 1, f"right column jitters across {sorted(starts)}"
    assert starts.pop() >= region.frames[1].col_start


def jittered_values_view() -> LayoutView:
    """Labels on the left, a numeric column on the right whose LEFT edge wobbles.

    The wobble is 0.12 in against a measured advance of ~0.115 in, i.e. almost exactly one
    cell — the +/-1-cell jitter of a right-aligned numeric column, which is the single most
    eyeball-salient defect of any padded renderer and the reason step 7b exists.
    """
    blocks = []
    for i in range(8):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"label for row {i:02d}", 0.72, y, 2.60))
        blocks.append(line_block(f"{i}23,456.00", 4.45 + (i % 2) * 0.12, y, 5.60))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def _value_columns(view: LayoutView, *, tab_snap: bool) -> set[int]:
    out = page_layout(view, 1, tab_snap=tab_snap)
    assert out.canvases == 1, f"expected one canvas, got reason={out.reason!r}"
    return {r.index("23,456.00") for r in out.spatial[0].rows if "23,456.00" in r}


def test_tab_snapping_collapses_one_cell_jitter():
    """Shown as a DIFFERENCE: without snapping the column really does land two cells apart."""
    view = jittered_values_view()
    assert len(_value_columns(view, tab_snap=False)) == 2, "the fixture must actually jitter"
    assert len(_value_columns(view, tab_snap=True)) == 1


def test_tab_snapping_is_a_no_op_on_an_already_aligned_page():
    """Snapping must not move anything that was already flush — it is a fix, not a re-layout."""
    view = two_column_view()
    on = page_layout(view, 1, tab_snap=True)
    off = page_layout(view, 1, tab_snap=False)
    assert on.spatial[0].rows == off.spatial[0].rows


def test_tab_snap_is_ONE_cell_so_a_genuine_indent_survives():
    """§4.3: "Two would let a genuine one-character indent collapse into its neighbour's stop."

    A ``TAB_SNAP`` of 4 passed the whole suite, because nothing asserted the NEGATIVE half:
    that a deliberate indent two or three cells from the body's stop is left alone.
    """
    assert TAB_SNAP == 1
    blocks = []
    for i in range(8):
        y = 2.0 + i * LEAD_IN
        # Six body lines flush left, two indented by ~3 cells — a genuine list indent.
        indent = 0.35 if i in (3, 4) else 0.0
        blocks.append(line_block(f"left body line {i:02d}", 0.72 + indent, y, 2.60))
        blocks.append(line_block(f"right column {i:02d}", RIGHT[0], y, RIGHT[1]))
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    _out, region = _spatial(view)
    snap = snap_tabs(region.frames, region.bands)
    frame = region.frames[0]
    offs = sorted({
        snap.get(a.key, _off(a, frame))
        for band in region.bands for a in band.atoms
        if a.text.startswith("left body")
    })
    assert len(offs) == 2, f"the indent collapsed into the body's stop: {offs}"
    assert offs[0] == 0 and offs[1] >= 2


def test_snap_tabs_keys_are_unique_across_kinds():
    """Regression: ``(source_ix, sub_ix)`` alone collides between a block and a mark."""
    view = two_column_view()
    view.marks.append(Mark(state="unselected", page=1,
                           bbox=quad(4.45, 2.0, 4.60, 2.0 + EM_IN)))
    _out, region = _spatial(view)
    snap = snap_tabs(region.frames, region.bands)
    assert all(len(k) == 3 for k in snap)
    assert len({k for k in snap if k[0] == "mark"}) >= 1


def cjk_two_column_view(rows: int = 8) -> LayoutView:
    """A CJK left column with THREE atoms per frame per band — a bullet, a label, a value.

    Every glyph is two cells, so a code-point width count under-pads everything to its right by
    about half. The extra atoms are what make the cursor rule run: with one atom per frame the
    placement is always ``true_col`` and the overflow this test names cannot occur, which is
    why the old one-atom-per-frame version passed for a reason unrelated to its name. The
    one-cell bullet shares the label's offset, so the label is nudged to the frame's ``MAX_DRIFT``
    limit and the value behind it used to be pushed past the frame's last legal column.
    """
    blocks = []
    for i in range(rows):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block("*", 0.72, y, 0.78))
        blocks.append(line_block("氏名住所", 0.74, y, 1.60))
        blocks.append(line_block("生年月日国籍", 1.62, y, 2.80))
        blocks.append(line_block(f"key {i:02d}", RIGHT[0], y, 5.20))
        blocks.append(line_block(f"right column {i:02d}", 5.20, y, RIGHT[1]))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def test_cjk_column_does_not_overflow_its_frame():
    """Every CJK glyph is two cells. poppler counts one and under-pads everything right of it."""
    out = page_layout(cjk_two_column_view(), 1)
    assert out.canvases == 1
    region = out.spatial[0]
    # Three atoms in the CJK frame of every band, so the cursor rule really does run here.
    assert all(
        sum(1 for a in b.atoms if a.x0 < region.frames[1].x0) == 3 for b in region.bands
    )
    limit = region.frames[-1].col_start + region.frames[-1].cells
    for i, row in enumerate(region.rows):
        assert cell_width(row) <= limit, f"row {i} is {cell_width(row)} cells, limit {limit}"
    # The right frame must start at ONE column on every row: that is the property CJK breaks
    # when width is counted in code points. Measured in CELLS, not characters.
    starts = {cell_width(r[:r.index("key ")]) for r in region.rows if "key " in r}
    assert len(starts) == 1, f"the right frame starts at {sorted(starts)}"
    assert starts == {region.frames[1].col_start}
    # And the CJK frame does not eat the gutter to get there. The per-ROW width check above
    # cannot see this: the right frame has slack, so a left frame that overruns its own end by
    # two cells still leaves the whole row inside the declared width. Measured in CELLS.
    for row in region.rows:
        if "key " not in row:
            continue
        head = row[:row.index("key ")]
        assert len(head) - len(head.rstrip()) >= GUTTER_CELLS, (
            f"the CJK frame overran into the gutter: {row!r}"
        )


def test_rtl_line_is_anchored_by_its_right_edge_and_carries_no_bidi_controls():
    """Azure returns logical order; this module keeps it there and moves only the anchor."""
    arabic = ["الرياض", "الدمام", "الخبرا", "القصيم", "الاحسا", "جدة", "مكة", "تبوك"]
    blocks = []
    for i, word in enumerate(arabic):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"city record {i:02d}", 0.72, y, LEFT[1]))
        # Right-aligned: every value ENDS at the frame's right edge, so x0 varies with length.
        blocks.append(
            line_block(word, RIGHT[1] - 0.12 * cell_width(word), y, RIGHT[1])
        )
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    out = page_layout(view, 1, tab_snap=False)
    assert out.canvases == 1
    rows = out.spatial[0].rows
    ends = {cell_width(row.rstrip()) for row in rows}
    assert len(ends) == 1, f"RTL values do not share a right edge: {sorted(ends)}"

    # Step 7b must PRESERVE that exactly — not merely keep it within TAB_SNAP. The old
    # assertion (``max - min <= TAB_SNAP``) accepted a right edge that snapping had ragged.
    snapped = page_layout(view, 1, tab_snap=True).spatial[0].rows
    assert {cell_width(r.rstrip()) for r in snapped} == ends

    for row in rows + snapped:
        # No U+202A..U+202E anywhere: logical order, no bidi control characters, ever.
        assert not any("\u202a" <= ch <= "\u202e" for ch in row)


def ragged_rtl_view() -> LayoutView:
    """A right-aligned RTL column of MIXED-LENGTH values, so the START columns are ragged.

    Every value ends at the same page x, so ``_off``'s ``(x1 - frame.x0) // adv`` gives them
    one shared end column and as many distinct start columns as there are lengths. That is
    exactly the shape on which snapping start columns does damage.
    """
    pool = "\u0627\u0644\u0628\u062a\u062b\u062c\u062d\u062e\u062f"
    blocks = []
    for i, width in enumerate((9, 6, 6, 5, 4, 4, 3, 3)):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"city record {i:02d}", 0.72, y, 4.00))
        blocks.append(line_block(pool[:width], 7.78 - 0.115 * width, y, 7.78))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def test_tab_snapping_does_not_scatter_rtl_right_edges():
    """Step 7b snaps RTL atoms by their RIGHT edge, in a stop set of their own.

    \u00a74.2 step 9 anchors an RTL line by its visual end, and \u00a79.3's
    ``test_rtl_placed_by_right_edge`` asserts the shared right edge that follows. Step 7b as
    written snaps START columns; for an RTL line the start is ``end - width``, so ONE stop set
    over start columns moves each value's right edge by an amount that depends on its LENGTH \u2014
    an exactly-aligned edge becomes ragged and snapping invents right edges no atom ever had.

    The property: snapping may only ever REMOVE distinct right edges from an RTL column, never
    add one. Pre-fix this fixture went from 3 distinct right edges to 4, the new one being a
    column no unsnapped atom occupied.
    """
    view = ragged_rtl_view()
    plain = page_layout(view, 1, tab_snap=False).spatial[0].rows
    snapped = page_layout(view, 1, tab_snap=True).spatial[0].rows
    before = {cell_width(r.rstrip()) for r in plain if r.strip()}
    after = {cell_width(r.rstrip()) for r in snapped if r.strip()}
    assert after <= before, f"snapping invented right edges {sorted(after - before)}"


def start_only_snap_tabs(frames, bands):
    """Step 7b read LITERALLY: ONE stop set per frame, over START columns, both directions.

    This is the algorithm the RTL clause says must not be applied to RTL atoms. It is here so
    the test below can show a DISAGREEMENT rather than assert a number — the same shape as
    ``single_linkage_bands`` above.
    """
    out: dict = {}
    for fix, frame in enumerate(frames):
        counts: dict[int, int] = {}
        offs = []
        for band in bands:
            for atom in band.atoms:
                if atom.kind not in ("line", "mark") or _frame_of(frames, atom) != fix:
                    continue
                off = _off(atom, frame)
                counts[off] = counts.get(off, 0) + 1
                offs.append((atom.key, off))
        accepted: list[int] = []
        for off in sorted(counts, key=lambda o: (-counts[o], o)):
            if any(abs(off - s) <= TAB_SNAP for s in accepted):
                continue
            accepted.append(off)
        accepted.sort()
        for key, off in offs:
            near = [s for s in accepted if abs(off - s) <= TAB_SNAP]
            if near:
                out[key] = min(near, key=lambda s: (abs(off - s), s))
    return out


def _rtl_end_columns(region, snap) -> set[int]:
    """The set of columns at which the region's RTL atoms END, under a given snap map."""
    ends = set()
    for band in region.bands:
        for atom in band.atoms:
            if not is_rtl(atom.text):
                continue
            frame = region.frames[_frame_of(region.frames, atom)]
            off = snap.get(atom.key, _off(atom, frame))
            ends.add(frame.col_start + off + cell_width(atom.text))
    return ends


def test_start_column_snapping_would_invent_rtl_right_edges_and_end_snapping_does_not():
    """The disagreement, stated as a disagreement.

    An RTL line is anchored by its visual end, so its right edge is where its position lives.
    Snapping over START columns moves that edge by an amount that depends on the value's
    LENGTH, which can put a value's right edge in a column no value occupied. Snapping over END
    columns cannot: every accepted stop is an end column some atom already had, so the snapped
    set is a subset of the unsnapped one.
    """
    _out, region = _spatial(ragged_rtl_view())
    baseline = _rtl_end_columns(region, {})
    assert len(baseline) > 1, "the fixture's RTL right edges are already all identical"

    by_end = _rtl_end_columns(region, snap_tabs(region.frames, region.bands))
    assert by_end <= baseline, f"end-snapping invented {sorted(by_end - baseline)}"

    by_start = _rtl_end_columns(region, start_only_snap_tabs(region.frames, region.bands))
    assert by_start - baseline, (
        "start-column snapping was expected to invent a right edge on this fixture; "
        "the fixture no longer demonstrates the defect"
    )


def test_an_over_long_atom_breaks_the_row_instead_of_shifting_its_neighbours():
    """Bounded damage AND preserved columns: the row breaks and the cursor resets to the
    overflowing atom's OWN frame start, not to column 0 (poppler) and not never (pdfplumber)."""
    blocks = []
    for i in range(8):
        y = 2.0 + i * LEAD_IN
        blocks.append(line_block(f"ordinary left line {i:02d}", 0.72, y, LEFT[1]))
        blocks.append(line_block(f"ordinary right line {i:02d}", RIGHT[0], y, RIGHT[1]))
    # One band gains a left atom whose TEXT is far wider than its own rectangle implies.
    fat = "X" * 60
    blocks.append(line_block(fat, 0.72, 2.0, 1.00))
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    out = page_layout(view, 1)
    assert out.canvases == 1
    region = out.spatial[0]
    assert region.band_rows[0][1] > region.band_rows[0][0], "the row did not break"
    blob = "\n".join(region.rows)
    assert fat in blob
    assert "ordinary left line 00" in blob
    assert "ordinary right line 00" in blob
    # Every later band is untouched: the damage is one extra row, not a shifted page.
    assert all(lo == hi for lo, hi in region.band_rows[1:])


# ---------------------------------------------------------------------------------------
# Step 10 — segments
# ---------------------------------------------------------------------------------------


def test_segments_respect_the_row_and_char_caps():
    _out, region = _spatial(two_column_view(rows=60))
    segs = segment(region, region.rows)
    assert len(segs) > 1
    for seg in segs:
        # An oversized segment is one holding exactly ONE band that alone breaks a cap — that
        # band may well be MANY rows (it broke), and a legitimately capped segment may be one
        # row, so ``len(seg.rows) == 1`` classifies the wrong set and skipped the cap
        # assertion for the wrong segments.
        band_count = sum(
            1 for lo, hi in region.band_rows if seg.row_lo <= hi and lo <= seg.row_hi
        )
        if band_count > 1:
            assert len(seg.rows) <= CANVAS_SEG_ROWS
            assert sum(len(r) for r in seg.rows) + len(seg.rows) <= CANVAS_SEG_CHARS
        assert seg.index >= 1
        assert seg.total == len(segs)
        assert seg.frames == region.frames        # columns line up across segments
        assert len(seg.row_ys) == len(seg.rows)


def test_segment_cols_is_the_declared_frame_width_not_the_widest_row():
    """§5.3/§6.1: ``cols`` is a property of the FRAME TABLE, so every row merely FITS in it.

    §6.1 emits ``canvas 99x11`` and ``canvas 99x10`` for the same
    ``frames=720:4080:48:70|4430:7780:48:70`` — 48 + 3 + 48 = 99 — while the widest row in
    those payloads is 92 and 76 cells, and states it outright: "Every canvas row below … is
    <= 99 cells". Defining ``cols`` as the widest RENDERED row makes §9.3's
    ``cell_width(row) <= cols`` a tautology that can never fail, which is what hid the
    frame-overflow defect this module's placement tests are supposed to catch.
    """
    for name, build in PLACEMENT_VIEWS.items():
        _out, region = _spatial(build(rows=60))
        declared = region.frames[-1].col_start + region.frames[-1].cells
        segs = segment(region, region.rows)
        assert len(segs) > 1, name
        for seg in segs:
            assert seg.cols == declared, f"{name}: seg {seg.index} declares {seg.cols}"
            for row in seg.rows:
                assert cell_width(row) <= seg.cols, f"{name}: {row!r} exceeds {seg.cols}"
        # Constant across the region's segments — the whole point of it being frame-derived.
        assert len({s.cols for s in segs}) == 1, name
    # And it is genuinely NOT the max-row definition: on this fixture the widest row is
    # narrower than the declared grid, so the two definitions disagree.
    _out, region = _spatial(two_column_view(rows=60))
    segs = segment(region, region.rows)
    assert any(max(cell_width(r) for r in s.rows) < s.cols for s in segs)


def test_segment_legend_rows_are_segment_local():
    """§6.1's second segment carries ``rows=11..20`` and a legend at ``cell=[0,3,24,3]``.

    Row 3 is the SEGMENT-local index of an atom sitting at region row 14, so the legend's
    ``r0``/``r1`` index the fence the reader is holding, not the region. Emitting the
    region-relative row puts every legend entry after the first segment ``row_lo`` rows off.
    """
    view = two_column_view(rows=60)
    for block in view.blocks:
        block.zone = Zone.heading
    _out, region = _spatial(view)
    segs = segment(region, region.rows)
    assert len(segs) > 1
    assert any(s.row_lo > 0 for s in segs), "no segment starts past region row 0"
    for seg in segs:
        for p in seg.legend:
            assert 0 <= p.row < len(seg.rows), (
                f"seg {seg.index} rows={seg.row_lo}..{seg.row_hi} has a legend row {p.row}"
            )
            # The legend's own text must be ON the row it points at.
            assert seg.rows[p.row][p.col0:p.col0 + 1].strip(), (
                f"seg {seg.index}: nothing at row {p.row} col {p.col0}"
            )
    # ``region.placements`` stays region-relative — segmenting must not mutate the region.
    assert [p.row for p in region.placements] == sorted(p.row for p in region.placements)
    assert max(p.row for p in region.placements) == len(region.rows) - 1


def test_segments_cover_every_row_of_the_region_in_order():
    _out, region = _spatial(two_column_view(rows=60))
    segs = segment(region, region.rows)
    rebuilt: list[str] = []
    for seg in segs:
        rebuilt.extend(seg.rows)
    # Segments drop only the blank gap rows that would lead a segment; no content row is lost.
    assert [r for r in rebuilt if r] == [r for r in region.rows if r]
    assert [s.row_lo for s in segs] == sorted(s.row_lo for s in segs)


def test_segment_rect_is_the_hull_of_its_own_bands():
    _out, region = _spatial(two_column_view(rows=60))
    for seg in segment(region, region.rows):
        assert seg.rect[0] <= seg.rect[2]
        assert seg.rect[1] <= seg.rect[3]
        assert seg.rect[1] >= region.rect[1]
        assert seg.rect[3] <= region.rect[3]


def test_legend_carries_tagged_atoms_only():
    view = two_column_view()
    view.blocks[0].zone = Zone.heading
    _out, region = _spatial(view)
    segs = segment(region, region.rows)
    tagged = [p for seg in segs for p in seg.legend]
    assert [p.tag for p in tagged] == ["heading"]
    assert tagged[0].col0 == 0


# ---------------------------------------------------------------------------------------
# Determinism — §7
# ---------------------------------------------------------------------------------------


def _fingerprint(view: LayoutView) -> str:
    out = page_layout(view, 1)
    parts = [f"reason={out.reason} em={out.em} canvases={out.canvases}"]
    for region in out.regions:
        parts.append(f"region kind={region.kind} reason={region.reason} rect={region.rect}")
        parts += [f"frame {f.x0}:{f.x1}:{f.cells}:{f.adv}:{f.col_start}" for f in region.frames]
        parts += region.rows
        for seg in segment(region, region.rows):
            parts.append(f"seg {seg.index}/{seg.total} {seg.row_lo}..{seg.row_hi} {seg.rect}")
            parts += [f"legend {p.tag} {p.rect} {p.row},{p.col0},{p.col1}" for p in seg.legend]
    return "\n".join(parts)


def test_independently_constructed_views_render_identically():
    """Two views built from scratch, sharing no object, must produce the same bytes."""
    assert _fingerprint(two_column_view()) == _fingerprint(two_column_view())


def test_model_round_trip_renders_identically():
    view = two_column_view()
    clone = LayoutView.model_validate(view.model_dump())
    assert _fingerprint(view) == _fingerprint(clone)


def test_input_permutation_invariance():
    """Permuting ``view.blocks`` remaps ``source_ix`` — a legitimate tiebreaker — so this test
    proves the sort keys are TOTAL, not that indices are irrelevant. Where no two atoms tie on
    geometry, the rendered bytes cannot depend on arrival order.
    """
    baseline = _fingerprint(two_column_view())
    for seed in range(25):
        view = two_column_view()
        rng = random.Random(seed)
        rng.shuffle(view.blocks)
        assert _fingerprint(view) == baseline, f"order dependence at seed {seed}"


def test_float_tripwire():
    """Perturb every coordinate by +/-1e-9 in a deterministic sign pattern: nothing may move.

    Passes trivially today because ``mu()`` quantises at 1e-3. It exists to fire on the day
    someone puts a float back into the layout path.
    """
    baseline = _fingerprint(two_column_view())
    view = two_column_view()
    sign = 1
    for block in view.blocks:
        for holder in (block, *block.lines):
            if holder.bbox is None:
                continue
            holder.bbox = [v + sign * 1e-9 for v in holder.bbox]
            sign = -sign
    assert _fingerprint(view) == baseline


def test_no_band_or_region_depends_on_set_iteration():
    """Bands are seed order and regions are band order; both must be reproducible verbatim.

    Feeding ``build_bands`` a REVERSED atom list proves nothing — it sorts on entry, so the
    test reduces to ``sorted(x) == sorted(reversed(x))``. What can actually vary is the
    iteration order of the ``set``/``dict`` objects INSIDE the pipeline (the duplicate ``seen``
    map, the tab-stop ``counts`` map, ``block_ixs``' set comprehension), and Python randomises
    string hashing per process, not per call. So this runs several independent RANDOM
    permutations and compares the full rendered bytes and every derived index list, and it
    perturbs the interned identity of the atom texts so any latent hash dependence has
    something to bite on.
    """
    view = two_column_view()
    atoms, _, _ = _atoms(view)
    em = page_em(atoms)
    x0, x1 = min(a.x0 for a in atoms), max(a.x1 for a in atoms)

    def run(order: list[Atom]) -> tuple:
        regions = build_regions(
            mark_separators(build_bands(order, em), x0, x1), em, x0, x1, view.blocks
        )
        out = []
        for r in regions:
            rows: list[str] = []
            if r.kind == "spatial":
                rows = render_canvas(r, snap_tabs(r.frames, r.bands))
            out.append((
                r.kind, r.rect, r.reason, tuple(r.block_ixs), tuple(r.table_ixs),
                tuple(r.mark_ixs), tuple(rows),
                tuple((p.key, p.tag, p.row, p.col0, p.col1) for p in r.placements),
            ))
        return tuple(out)

    baseline = run(list(atoms))
    rng = random.Random(20260901)
    for _ in range(12):
        shuffled = list(atoms)
        rng.shuffle(shuffled)
        # Fresh, non-interned copies of the text: equal by value, distinct by identity.
        shuffled = [replace_atom_tag(a, a.tag) for a in shuffled]
        assert run(shuffled) == baseline
    assert run(list(reversed(atoms))) == baseline


def test_page_reason_reports_the_most_diagnostic_decline_not_the_topmost():
    """§9.6 tracks ``coverage`` as the signal that the span join broke; it must not be buried.

    A page with a ``no-gutter`` region ABOVE a ``coverage`` region reported ``no-gutter``,
    because the page-level reason was ``declines[0]`` — first in band order. The bucket the
    corpus sweep exists to watch was exactly the one that selection could hide.
    """
    view = LayoutView(pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")])
    # Region 1 (top): a single column — no corridor, so "no-gutter".
    for i in range(8):
        view.blocks.append(
            line_block(f"single column line number {i:02d}", 0.72, 1.0 + i * LEAD_IN, 7.00)
        )
    # A full-measure heading separates the two candidates.
    view.blocks.append(line_block("A FULL MEASURE DIVIDING HEADING LINE", 0.72, 3.00, 7.78))
    # Region 2 (below): two columns, but one block's lines cannot rebuild its text.
    for i in range(8):
        y = 4.0 + i * LEAD_IN
        view.blocks.append(line_block(f"left line number {i:02d}", LEFT[0], y, LEFT[1]))
        view.blocks.append(line_block(f"right line number {i:02d}", RIGHT[0], y, RIGHT[1]))
    view.blocks[-1].text += " AND A CLAUSE THE LINE STREAM NEVER SAW"

    out = page_layout(view, 1)
    assert out.regions == []
    assert out.reason == "coverage", (
        "the topmost decline buried the diagnostic one"
    )
    assert REASON_RANK["coverage"] < REASON_RANK["no-gutter"]
    assert REASON_RANK["error"] == min(REASON_RANK.values())


def test_key_values_are_assigned_to_the_region_that_contains_them():
    """§4.2 step 11 needs a key/value's region, and a key/value is never an atom.

    Without ``Region.kv_ixs`` the emitter has to call ``mu()`` and rebuild quad rectangles
    itself — re-implementing this module's geometry on the far side of a one-way dependency.
    """
    view = two_column_view()
    inside = KeyValue(
        key="Full legal name:", value="A J Whitcombe", page=1,
        key_bbox=quad(0.80, 2.10, 1.90, 2.10 + EM_IN),
        value_bbox=quad(2.00, 2.10, 3.20, 2.10 + EM_IN),
    )
    outside = KeyValue(
        key="Filed:", value="2026", page=1,
        key_bbox=quad(0.10, 9.50, 0.60, 9.64),
        value_bbox=quad(0.70, 9.50, 1.20, 9.64),
    )
    floating = KeyValue(key="No geometry", value="at all", page=1)
    view.key_values = [inside, outside, floating]

    out = page_layout(view, 1)
    assert out.canvases == 1
    region = out.spatial[0]
    assert region.kv_ixs == [0], "the contained pair was not assigned to its region"
    assert all(1 not in r.kv_ixs for r in out.regions), "an outside pair was given an owner"
    assert out.floating_kvs == [2]
    # Every pair belongs to at most one region: this is an assignment, not a broadcast.
    seen = [kix for r in out.regions for kix in r.kv_ixs]
    assert len(seen) == len(set(seen))


def test_segment_drops_only_the_blank_rows_that_would_lead_it():
    """A segment's leading gap rows say nothing once it is a standalone chunk, and ``rows=lo..``
    must stay honest about what the payload contains. Keeping them passed the whole suite."""
    blocks = []
    for i in range(40):
        # A 3-row gap every ten bands, so some segment boundary lands on blank rows.
        y = 2.0 + i * LEAD_IN + (i // 10) * 0.60
        blocks.append(line_block(f"left line number {i:02d}", LEFT[0], y, LEFT[1]))
        blocks.append(line_block(f"right line number {i:02d}", RIGHT[0], y, RIGHT[1]))
    view = LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )
    _out, region = _spatial(view)
    assert "" in region.rows, "the fixture produced no gap rows"
    segs = segment(region, region.rows)
    assert len(segs) > 1
    for seg in segs:
        assert seg.rows[0] != "", f"seg {seg.index} leads with a blank row"
        assert seg.rows == region.rows[seg.row_lo:seg.row_hi + 1], (
            f"seg {seg.index}'s rows do not match its own rows={seg.row_lo}..{seg.row_hi}"
        )
    # Nothing but leading blanks is dropped: every non-blank row survives, in order.
    rebuilt = [r for s in segs for r in s.rows]
    assert [r for r in rebuilt if r] == [r for r in region.rows if r]


def test_render_is_idempotent():
    """Rendering twice must not accumulate rows or placements onto the region."""
    _out, region = _spatial(two_column_view())
    first = list(render_canvas(region, snap_tabs(region.frames, region.bands)))
    second = list(render_canvas(region, snap_tabs(region.frames, region.bands)))
    assert first == second
    assert len(region.placements) == len([a for a in region.atoms if a.tag])


# ---------------------------------------------------------------------------------------
# Honesty: page_layout never raises
# ---------------------------------------------------------------------------------------


def test_page_layout_never_raises_on_degenerate_input():
    degenerate = [
        LayoutView(),
        LayoutView(pages=[PageInfo(page=1, width=0.0, height=0.0, unit="inch")]),
        LayoutView(
            pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
            blocks=[TextBlock(text="", page=1, bbox=quad(1.0, 1.0, 1.0, 1.0))],
        ),
        LayoutView(
            pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
            blocks=[TextBlock(text="x", page=1, bbox=[1.0, 1.0, 2.0])],   # short quad
        ),
    ]
    for view in degenerate:
        out = page_layout(view, 1)
        assert out.regions == []
        assert out.reason


def test_build_frames_survives_a_zero_width_frame():
    """A degenerate frame must still yield a usable advance rather than dividing by zero."""
    atoms = [_atom(1000, 1139, i, x0=500, x1=500) for i in range(4)]
    bands = build_bands(atoms, 139)
    frames = build_frames([], 500, 500, bands, 139)
    assert len(frames) == 1
    assert frames[0].adv >= 1
