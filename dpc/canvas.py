"""LayoutView geometry -> horizontal bands, column frames, and canvas rows.

Everything here is an integer in milli-units of the page's own unit, so one set of thresholds
serves an inch-unit PDF, a pixel-unit 300-DPI scan and a 72-dpi image with no unit switch
anywhere. The conversion happens once, in :func:`mu`, and never again.

This module is **geometry only**. It emits no markdown and imports nothing from
``dpc.emitter``; the dependency runs one way, so the layout engine can be reasoned about (and
fuzzed, and property-tested) without a renderer in the loop.

The pipeline is the eleven steps of SPEC-PMD-2 §4.2, in order:

    atoms -> gates -> bands -> separators -> candidate regions -> gutters -> frames
          -> tab stops -> canvas rows -> segments

Two properties are load-bearing and are worth stating where they can be seen:

1. **A band is a frozen-seed sweep, not single-linkage clustering.** The seed's test interval
   never grows, so a page whose line tops drift by a hair cannot chain into one band. That is
   the anti-creep property; ``build_bands`` is where it lives.
2. **No floating-point arithmetic survives :func:`mu`.** Every threshold below is an exact
   rational applied as an integer multiply-and-compare, every column index is integer floor
   division, and every ceiling is ``-(-a // b)``. Determinism here is a construction, not a
   convention: the stored sha256 of the emitted document is a product contract.
"""
from __future__ import annotations

import logging
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Literal

from dpc.models import KeyValue, LayoutView, Quad, TextBlock, Zone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------
# Constants — SPEC-PMD-2 §4.3. Every one is sourced; the sourcing comment is the reason the
# number is defensible, so it travels with the number rather than living in a design doc that
# nobody opens while reading the code.
# ---------------------------------------------------------------------------------------

#: Profile resolution: four occupancy buckets per em. Fine enough to resolve a 1.5 em gutter
#: to a sixth of its own width, coarse enough that the profile stays ~250 buckets on Letter.
BUCKETS_PER_EM = 4

#: 1.5 em, as an exact rational. poppler's ``maxWordSpacing = 1.5 em`` is the LARGEST gap that
#: can still sit inside one line, so a gutter floor there is provably wider than any intra-line
#: word gap: a gutter can never bisect a line.
MIN_GUTTER_EM = (3, 2)

#: poppler ``TextOutputDev.cc:3181,3228`` — ``if (n > 0 && n <= 3)`` absorbs left/right
#: stragglers into the neighbouring block. Four is the first count poppler treats as a genuine
#: column. Inverted verbatim.
MIN_GUTTER_ROWS = 4

#: The same straggler cap applied to the corridor instead of the block: up to three bands may
#: cross a corridor and it is still a corridor; four make it a body. ABSOLUTE, deliberately —
#: a proportional budget (``K // 10``) is a threshold that is a function of corpus size rather
#: than of the thing measured, and it makes the same layout answer differently depending on how
#: many rows it happens to have.
MAX_BLOCKING_BANDS = 3

#: 0.90 of the content width. A line at 90% leaves <=10%; on a 6.5-in measure that is 0.65 in
#: ~ 4.6 em at 10 pt — about five characters, which is not a column.
FULLWIDTH_FRAC = (9, 10)

#: pdfminer.six ``LAParams.line_overlap = 0.5``, which is exactly this quantity: below half,
#: the shorter box is more outside the seed row than inside it.
BAND_OVERLAP = (1, 2)

#: 1.60 em. One line's polygon is ~1.0-1.3 em tall; two single-spaced lines are >=2.0 em. 1.6
#: is the midpoint; anything in (1.35, 1.9) classifies real data identically.
SINGLE_ROW_EM = (8, 5)

#: poppler refuses to trust a measured gap unless a line has more than one word
#: (``TextLine::coalesce``). A 1-3 character line's polygon hugs two or three glyphs and its
#: implied advance is not representative.
MIN_MEASURE_CELLS = 4

#: em / 2 — the standard average-character-width figure for proportional type. Courier's
#: advance is 0.60 em and pdfplumber's ``DEFAULT_X_DENSITY = 7.25 pt`` at 12 pt is also
#: 0.60 em; 0.5 is the conservative (denser) end, which errs toward MORE cells, never overlap,
#: because ``cells_j`` takes the max with the required extent.
FALLBACK_ADV = (1, 2)

#: poppler's column assignment, verbatim: ``col2 = blk1->col + blk1->nColumns + 3``. One space
#: reads as a word gap, two as a sentence gap; three is the shortest run that reads
#: unambiguously as a column separator in a monospace face.
GUTTER_CELLS = 3

#: One cell. Two would let a genuine one-character indent collapse into its neighbour's stop.
TAB_SNAP = 1

#: poppler ``TextPage::dump``: ``d = clamp((base_next - base) / fontSize, 1, 5)``. Its
#: rationale — a half-empty page must not become forty blank lines — applies unchanged.
MAX_ROW_GAP = 5

#: Strictly less than ``GUTTER_CELLS = 3``, so an atom nudged right to clear its neighbour can
#: never be pushed into the next frame's territory. A causal bound, not a tuned one.
MAX_DRIFT = 2

#: tan(1.15 deg). Over a 6.5-in content width a 1.15 deg skew drifts the baseline 0.13 in ~ one
#: line height — precisely the skew at which band membership becomes wrong by one row across
#: the page. Derived from the failure, not from a percentile.
MAX_SKEW = (1, 50)

#: 64 atoms on one visual row at >=3 cells each is >=250 cells of content. Past that the row is
#: noise, not a layout. Also bounds the intra-band placement loop.
MAX_ATOMS_PER_BAND = 64

#: Azure returns ~60-120 lines for a dense A4 page. 4000 is 30x, i.e. pathological.
MAX_ATOMS_PER_PAGE = 4000

#: ~350 tokens at the ~4 chars/token English ratio: fits inside the smallest chunk window in
#: common use (512) WITH room for the anchor and the legend. Sizing to the largest tolerable
#: chunk window is the wrong optimisation — it orphans the second half of every cut canvas.
CANVAS_SEG_ROWS = 20
CANVAS_SEG_CHARS = 1400

#: Beyond twelve tagged atoms in a 20-row segment the legend costs more lines than the canvas
#: it describes; the segment falls back to a ``has=`` tag list.
CANVAS_LEGEND_MAX = 12

Kind = Literal["line", "mark", "table"]

#: Sort rank for the atom order's third key. Fixed here rather than derived from the Literal so
#: a reordering of the Literal cannot silently reorder output.
KIND_RANK: dict[str, int] = {"line": 0, "mark": 1, "table": 2}

#: ``(kind, source_ix, sub_ix)``. SPEC §4.4 writes the tab-snap map's key as
#: ``(source_ix, sub_ix)``, but that is NOT unique across kinds — a line atom of block 3 and a
#: mark atom of index 3 collide, and the collision would silently place a checkbox at a
#: paragraph's tab stop. §7.1 states the total key as ``(kind, source_ix, sub_ix)``, so the map
#: uses that. The map is opaque to the emitter (it is produced and consumed inside this
#: module's own call graph), so widening the key costs nothing downstream.
AtomKey = tuple[str, int, int]

#: Reasons a page or a candidate region declined the spatial path. These are the buckets of the
#: corpus sweep's regression histogram (§9.6), so they are a closed vocabulary: a rise in
#: ``no-geometry`` means routing broke, a rise in ``coverage`` means the span join broke, a rise
#: in ``multiline`` means lines stopped being attached.
Reason = Literal[
    "", "no-geometry", "skew", "too-dense", "no-gutter", "multiline", "coverage", "error"
]

#: Diagnostic priority for collapsing several declining regions into ONE page-level reason.
#: Lower wins. Taking the first decline in band order instead would let a ``no-gutter`` region
#: at the top of a page hide a ``coverage`` region below it — and §9.6 says in as many words
#: that "a rise in ``coverage`` means the span join broke", so the bucket the sweep exists to
#: watch is precisely the one first-in-band-order can bury. Ranked by how much each reason
#: tells you: ``coverage`` is a defect in the pipeline, ``multiline`` is a defect in the
#: provider's segmentation, ``no-gutter`` is the ordinary answer for a one-column page and
#: carries almost no signal. Total and fixed, so the choice is still deterministic.
#: ``error`` outranks everything: it is the one reason that means the layout engine itself
#: failed, and §9.6's six-bucket vocabulary has no home for it, so it must not be swallowed.
REASON_RANK: dict[str, int] = {
    "error": 0,
    "coverage": 1,
    "multiline": 2,
    "too-dense": 3,
    "skew": 4,
    "no-geometry": 5,
    "no-gutter": 6,
}


# ---------------------------------------------------------------------------------------
# Units and text metrics
# ---------------------------------------------------------------------------------------


def mu(value: float) -> int:
    """A page coordinate as an integer in milli-units of the page's own unit.

    ``math.floor(value * 1000.0 + 0.5)``. Half-up, explicitly: Python's ``round()`` is
    banker's rounding, and a 0.5 boundary that flips on a 1-ULP change in a polygon is the
    single most-cited determinism trap in the prior art (pdfplumber's ``round(x_dist)``).

    For inch pages this is milli-inches (0.072 pt of resolution); for pixel pages,
    milli-pixels. Azure's documented examples carry four decimal places and nothing guarantees
    more, so nothing below this quantum is signal.

    AFTER THIS CALL THE PIPELINE PERFORMS NO FLOATING-POINT ARITHMETIC.

    Args:
        value: A coordinate in the page's own unit.

    Returns:
        The coordinate in milli-units, rounded half-up.
    """
    return math.floor(value * 1000.0 + 0.5)


def cell_width(text: str) -> int:
    """Display cells the text occupies in a monospace canvas.

    0 for combining marks and format characters (``unicodedata.combining(ch)`` non-zero, or
    category in Mn/Me/Cf); 2 for East-Asian-Width W and F; 1 otherwise. Ambiguous (A) counts as
    1, matching every ``wcwidth`` narrow default and what a Western monospace face renders.

    poppler is measurably WRONG here: ``col[]`` counts one column per code point under UTF-8
    (``TextOutputDev.cc:1265-1273``), so every CJK block under-counts its own width by about
    half and everything to its right is under-padded; its legacy byte-counting branch gets
    Shift-JIS right by accident. Azure returns CJK and this is a KYC corpus, so this is not
    hypothetical.

    A pure function of the string given a Unicode version. That version is pinned into the
    front matter (``unicode:``) whenever a canvas exists, so a Python upgrade that moves the
    East-Asian-Width tables shows up as a visible field change rather than a mystery hash
    drift.

    Args:
        text: The string to measure.

    Returns:
        Total display cells, an integer >= 0.
    """
    total = 0
    for char in text:
        if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def is_rtl(text: str) -> bool:
    """True when a strict majority of the string's strong-directional characters are R or AL.

    Used only to decide the ANCHOR POINT of a line, never to reorder it. Azure returns line
    ``content`` in logical order and this module keeps it there: poppler emits visual order
    bracketed with ``U+202B``/``U+202A``/``U+202C``, which makes ``len(str)`` differ from
    display width and means any consumer that normalises the string changes the rendering.
    Zero transformation is the determinism-safe choice; the one thing that must be handled is
    that an RTL line's ``x0`` is its visual END, so it is placed by its right edge.

    Args:
        text: The line's content.

    Returns:
        True when the line reads right-to-left.
    """
    rtl = ltr = 0
    for char in text:
        bidi = unicodedata.bidirectional(char)
        if bidi in ("R", "AL"):
            rtl += 1
        elif bidi == "L":
            ltr += 1
    return rtl > ltr and rtl > 0


def _norm_ws(text: str) -> str:
    """Collapse every Unicode whitespace run to one space and strip.

    ``str.split()`` with no argument splits on Unicode whitespace, which is exactly the
    normalisation the coverage gate (§4.2 step 5.4) is specified against.
    """
    return " ".join(text.split())


# ---------------------------------------------------------------------------------------
# Step 1 — atoms
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Atom:
    """One placeable thing. All coordinates in milli-units, origin page top-left."""

    kind: Kind
    text: str
    x0: int
    y0: int
    x1: int
    y1: int
    #: Top-edge dy and dx of the atom's OWN quad, in milli-units (``skew_den >= 1``).
    #: ``PageInfo.angle`` is never read: Microsoft does not document whether polygons are in
    #: the raw or a deskew-corrected frame, so the polygon's own edge is the only trustworthy
    #: signal about how tilted this atom is.
    skew_num: int
    skew_den: int
    #: Index into ``view.blocks`` / ``view.marks`` / ``view.tables`` per ``kind``.
    source_ix: int
    #: Line index within its block; 0 for every other kind.
    sub_ix: int
    #: Owning block, for ``line`` atoms. Equal to ``source_ix`` by construction (a line atom's
    #: source list IS ``view.blocks``); carried separately because the emitter reads ownership
    #: and provenance for different reasons and conflating them invites a silent aliasing bug.
    block_ix: int | None
    #: Taller than ``SINGLE_ROW_EM``, i.e. this atom's own rectangle spans more than one visual
    #: row. A property of THIS rectangle, never inherited from a parent.
    multiline: bool
    #: Legend tag: PMD 1.0 §5.2's vocabulary (``title``, ``heading``, ``furniture[:role]``, the
    #: verbatim provider role, ``mark``). A body line with no role has ``""``.
    tag: str

    @property
    def key(self) -> AtomKey:
        """The atom's total identity — see :data:`AtomKey`."""
        return (self.kind, self.source_ix, self.sub_ix)

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        """§7.1's band-input order: ``(y0, x0, KIND_RANK, source_ix, sub_ix)``. Total."""
        return (self.y0, self.x0, KIND_RANK[self.kind], self.source_ix, self.sub_ix)


def _quad_rect(quad: Quad | None) -> tuple[int, int, int, int] | None:
    """A quad's axis-aligned rectangle in milli-units, or None when there is no geometry."""
    if not quad or len(quad) < 8:
        return None
    xs = [mu(v) for v in quad[0:8:2]]
    ys = [mu(v) for v in quad[1:8:2]]
    return (min(xs), min(ys), max(xs), max(ys))


def _quad_skew(quad: Quad | None) -> tuple[int, int]:
    """The quad's own top-edge ``(dy, dx)`` in milli-units, ``dx`` floored at 1.

    Points are clockwise from top-left, so the top edge runs ``(q0, q1) -> (q2, q3)``.
    """
    if not quad or len(quad) < 8:
        return (0, 1)
    return (mu(quad[3]) - mu(quad[1]), max(mu(quad[2]) - mu(quad[0]), 1))


def _block_tag(block: TextBlock) -> str:
    """PMD 1.0 §5.2's anchor tag for a block, reproduced so the legend speaks one vocabulary."""
    if block.zone in (Zone.title, Zone.heading):
        return str(block.zone)
    if block.zone is Zone.furniture:
        return f"furniture:{block.role}" if block.role else "furniture"
    return block.role or ""


def _em_from_heights(heights: Sequence[int]) -> int:
    """Lower-quartile height by EXACT index selection: ``sorted(heights)[(n - 1) // 4]``.

    Never an interpolated percentile, because interpolation reintroduces a division whose
    result a 1-ULP change can move.
    """
    if not heights:
        return 0
    ordered = sorted(heights)
    return ordered[(len(ordered) - 1) // 4]


def atoms_for_page(
    view: LayoutView, page: int
) -> tuple[list[Atom], list[int], list[int]]:
    """(atoms, floating block indices, floating key/value indices) for one page.

    Step 1 of §4.2. Blocks with ``zone is Zone.table`` contribute nothing — the adapters
    re-zone paragraphs that overlap a detected table precisely so their text is not emitted
    twice, and ``tables[]`` already carries the cells.

    A block WITH lines contributes one ``line`` atom per line that has a bbox, and
    ``multiline`` is False for every one of them: a provider line is one visual row by
    construction, which is the whole reason lines are the placement unit. A block with no
    lines but a bbox contributes one hull atom whose ``multiline`` is measured from its own
    rectangle.

    Key/value pairs are NEVER atoms: their text is text the lines already carry, so placing
    them would print it twice. They are handled in step 11 by the emitter.

    The page's ``em`` is needed to classify hull atoms as multiline, and ``em`` is a function
    of the atom heights — so the heights are collected first and :func:`_em_from_heights` is
    applied to them, giving exactly the value :func:`page_em` returns for the result.

    Args:
        view: The provider-neutral read of the document.
        page: 1-based page number.

    Returns:
        ``(atoms, floating_block_ixs, floating_kv_ixs)``. Floating members have no geometry at
        all and are appended at the end of the page rather than given an invented position.
    """
    # First pass: measure. (kind, text, rect, skew, source_ix, sub_ix, tag, is_hull)
    RawAtom = tuple[str, str, tuple[int, int, int, int], tuple[int, int], int, int, str, bool]
    raw: list[RawAtom] = []
    floating_blocks: list[int] = []

    for bix, block in enumerate(view.blocks):
        if block.page != page or block.zone is Zone.table:
            continue
        tag = _block_tag(block)
        placed = False
        for lix, line in enumerate(block.lines):
            rect = _quad_rect(line.bbox)
            if rect is None:
                continue
            raw.append(("line", line.text, rect, _quad_skew(line.bbox), bix, lix, tag, False))
            placed = True
        if placed:
            continue
        rect = _quad_rect(block.bbox)
        if rect is None:
            floating_blocks.append(bix)
            continue
        # The HULL path: reached both when the block has no lines and when it has lines the
        # provider gave no polygon for. Either way this rectangle is a paragraph hull, so it
        # is the one atom kind that can span more than one visual row.
        raw.append(("line", block.text, rect, _quad_skew(block.bbox), bix, 0, tag, True))

    for mix, mark in enumerate(view.marks):
        if mark.page != page:
            continue
        rect = _quad_rect(mark.bbox)
        if rect is None:
            continue
        # ASCII, not Azure's U+2611/U+2610: those carry East-Asian-Width A (Ambiguous) and so
        # render as 1 or 2 cells depending on the reader's locale — a cell-width ambiguity
        # inside a grid whose entire purpose is cell alignment. "[x]" is 3 cells everywhere.
        text = "[x]" if mark.selected else "[ ]"
        raw.append(("mark", text, rect, _quad_skew(mark.bbox), mix, 0, "mark", False))

    for tix, table in enumerate(view.tables):
        if table.page != page:
            continue
        rect = _quad_rect(table.bbox)
        if rect is None:
            continue
        # A table atom is a SEPARATOR and nothing else; its markdown is the GFM table, which is
        # why its text is empty. Making tables separators is what satisfies "do not destroy GFM
        # tables" by construction rather than by argument.
        raw.append(("table", "", rect, _quad_skew(table.bbox), tix, 0, "", False))

    em = _em_from_heights([r[2][3] - r[2][1] for r in raw])

    atoms: list[Atom] = []
    for kind, text, rect, skew, source_ix, sub_ix, tag, hull in raw:
        x0, y0, x1, y1 = rect
        # A hull atom is the ONLY atom that can span more than one visual row: a provider line
        # is one visual row by construction, and so are a checkbox and a table hull-as-
        # separator. ``multiline`` is measured from THIS rectangle, never inherited.
        multiline = hull and (y1 - y0) * SINGLE_ROW_EM[1] > em * SINGLE_ROW_EM[0]
        atoms.append(Atom(
            kind=kind,  # type: ignore[arg-type]
            text=text, x0=x0, y0=y0, x1=x1, y1=y1,
            skew_num=skew[0], skew_den=skew[1],
            source_ix=source_ix, sub_ix=sub_ix,
            block_ix=source_ix if kind == "line" else None,
            multiline=multiline, tag=tag,
        ))

    floating_kvs = [
        kix for kix, kv in enumerate(view.key_values)
        if kv.page == page and _quad_rect(kv.key_bbox) is None
        and _quad_rect(kv.value_bbox) is None
    ]
    return atoms, floating_blocks, floating_kvs


# ---------------------------------------------------------------------------------------
# Step 2 — scale and the two gates
# ---------------------------------------------------------------------------------------


def page_em(atoms: Sequence[Atom]) -> int:
    """The page's em, in milli-units: ``sorted(heights)[(n - 1) // 4]`` — the lower quartile.

    An exact index selection, never an interpolated percentile, because interpolation
    reintroduces a division whose result a 1-ULP change can move.

    Quartile rather than median because the atom population is mixed. On a line-atom page the
    heights cluster tightly and q25 ~ median ~ the true em. On a degraded hull-atom page the
    single-line atoms are always the shorter part of the distribution, and q25 sits inside them
    whenever at least a quarter of the blocks are single-line — true on every real page
    (headings, labels, list items, captions). One rule covers both populations, so there is no
    mode switch to get wrong.

    Args:
        atoms: The page's atoms.

    Returns:
        The em in milli-units, or 0 when there are no atoms.
    """
    return _em_from_heights([a.y1 - a.y0 for a in atoms])


def page_skew_ok(atoms: Sequence[Atom]) -> bool:
    """False when the median |skew| over line atoms exceeds ``MAX_SKEW``.

    The median is selected exactly, by sorting the atoms on their own rational
    ``|skew_num| / skew_den`` — :class:`fractions.Fraction` is exact integer arithmetic, not
    floating point, so the comparison is the same on every machine. The threshold itself is
    then applied as the integer test ``50 * |dy| > dx``.

    Args:
        atoms: The page's atoms. Non-line atoms are ignored: a table's hull and a checkbox's
            square say nothing about the baseline angle of the text.

    Returns:
        True when the page is flat enough to band.
    """
    lines = [a for a in atoms if a.kind == "line"]
    if not lines:
        return True
    ordered = sorted(
        lines,
        key=lambda a: (Fraction(abs(a.skew_num), a.skew_den), a.source_ix, a.sub_ix),
    )
    med = ordered[(len(ordered) - 1) // 2]
    return abs(med.skew_num) * MAX_SKEW[1] <= med.skew_den * MAX_SKEW[0]


# ---------------------------------------------------------------------------------------
# Step 3 — bands (frozen-seed sweep)
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Band:
    """One horizontal row of the page: the atoms that share a visual line.

    ``seed_y0``/``seed_y1`` are the FROZEN test interval — the seed atom's own vertical
    extent, which never moves. ``y0``/``y1`` are the reported extent, the union of everything
    that joined. Keeping the two apart is the whole anti-chaining property; see
    :func:`build_bands`.
    """

    atoms: list[Atom]
    seed_y0: int
    seed_y1: int
    y0: int
    y1: int
    x0: int
    x1: int
    separator: bool = False

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


def build_bands(atoms: Sequence[Atom], em: int) -> list[Band]:
    """Group atoms into horizontal bands with a frozen-seed forward sweep.

    Atoms are sorted by ``(y0, x0, KIND_RANK, source_ix, sub_ix)`` — total, because
    ``(kind, source_ix, sub_ix)`` is unique per atom. The first unassigned atom seeds a band
    and FIXES the test interval at ``[seed.y0, seed.y1]``. A later atom joins iff::

        2 * max(0, min(a.y1, s.y1) - max(a.y0, s.y0)) >= min(a.y1 - a.y0, s.y1 - s.y0)

    The band's REPORTED extent grows to the union; the TEST interval never does.

    Freezing the seed is what makes this not single-linkage clustering. pdfplumber's
    ``cluster_list`` compares each element to the PREVIOUS one, so on a page whose line tops
    drift by less than the tolerance — a half-degree skew, a table with uneven leading — the
    whole page chains into one cluster. That is a step function of a continuous input and it is
    the worst determinism hazard in the prior art. A frozen seed cannot creep: forty body lines
    yield forty bands, at any leading.

    Args:
        atoms: The page's atoms.
        em: The page em in milli-units. Accepted for signature symmetry with the rest of the
            pipeline and for future use; the overlap test is scale-free (it compares an overlap
            against the shorter box's own height), so no threshold here needs it.

    Returns:
        Bands in seed order, which is ascending ``y0``. There is no second sort.
    """
    del em  # scale-free by construction; see Args.
    ordered = sorted(atoms, key=lambda a: a.sort_key)
    taken = [False] * len(ordered)
    bands: list[Band] = []

    for i, seed in enumerate(ordered):
        if taken[i]:
            continue
        taken[i] = True
        band = Band(
            atoms=[seed], seed_y0=seed.y0, seed_y1=seed.y1,
            y0=seed.y0, y1=seed.y1, x0=seed.x0, x1=seed.x1,
        )
        bands.append(band)
        # Exact duplicates — same milli-unit rect AND same text — are the fake-bold / drop-
        # shadow case poppler handles with ``minDupBreakOverlap``. First in sort order wins,
        # and the survivor's POSITION is remembered so a suppressed twin can still donate its
        # tag (see below).
        seen: dict[tuple[int, int, int, int, str], int] = {
            (seed.x0, seed.y0, seed.x1, seed.y1, seed.text): 0
        }
        for j in range(i + 1, len(ordered)):
            if taken[j]:
                continue
            if len(band.atoms) >= MAX_ATOMS_PER_BAND:
                # A full band stops accepting; overflow atoms seed the next band.
                break
            cand = ordered[j]
            overlap = max(0, min(cand.y1, band.seed_y1) - max(cand.y0, band.seed_y0))
            shorter = min(cand.y1 - cand.y0, band.seed_y1 - band.seed_y0)
            if overlap * BAND_OVERLAP[1] < shorter * BAND_OVERLAP[0]:
                continue
            taken[j] = True
            dup = (cand.x0, cand.y0, cand.x1, cand.y1, cand.text)
            if dup in seen:
                # The twin's TEXT is by definition already on the canvas, but its TAG is not:
                # if the shadow copy is the block carrying ``Zone.heading`` and the survivor is
                # a plain body line, dropping it silently deletes a legend entry — the one
                # thing a duplicate does NOT duplicate. The survivor adopts the first non-empty
                # tag in sort order; it never overwrites a tag it already has, so the merge is
                # order-independent given the (total) sort and cannot flip an existing anchor.
                held = band.atoms[seen[dup]]
                if cand.tag and not held.tag:
                    band.atoms[seen[dup]] = replace(held, tag=cand.tag)
                continue
            seen[dup] = len(band.atoms)
            band.atoms.append(cand)
            band.y0 = min(band.y0, cand.y0)
            band.y1 = max(band.y1, cand.y1)
            band.x0 = min(band.x0, cand.x0)
            band.x1 = max(band.x1, cand.x1)
    return bands


# ---------------------------------------------------------------------------------------
# Step 4 — separators
# ---------------------------------------------------------------------------------------


def mark_separators(bands: list[Band], x0: int, x1: int) -> list[Band]:
    """Flag the bands that divide the page, in place, and return the same list.

    A band is a separator iff it contains

    (a) any ``table`` atom — always, because a GFM pipe table preserves cell identity, spans in
        the ``RxC`` tag, and every consumer's ``^| `` grep, where an ASCII grid preserves none
        of it; or
    (b) any NON-MULTILINE line atom at least ``FULLWIDTH_FRAC`` of the content width.

    The multiline exclusion in (b) matters: a justified paragraph hull frequently spans the
    full measure, and if that counted as a separator every wrapped paragraph would shatter its
    own region. A full-measure hull is a block's measure, not a page divider.

    Args:
        bands: The page's bands.
        x0: Content extent left, milli-units.
        x1: Content extent right, milli-units.

    Returns:
        ``bands``, with ``separator`` set.
    """
    width = x1 - x0
    for band in bands:
        sep = False
        for atom in band.atoms:
            if atom.kind == "table":
                sep = True
                break
            if atom.kind == "line" and not atom.multiline and width > 0 and (
                (atom.x1 - atom.x0) * FULLWIDTH_FRAC[1] >= width * FULLWIDTH_FRAC[0]
            ):
                sep = True
                break
        band.separator = sep
    return bands


# ---------------------------------------------------------------------------------------
# Steps 6-7 — gutters and frames
# ---------------------------------------------------------------------------------------


def find_gutters(
    bands: Sequence[Band], em: int, x0: int, x1: int
) -> list[tuple[int, int]]:
    """Vertical corridors through a candidate region, as milli-unit ``(left, right)`` spans.

    The occupancy profile counts BANDS, not atoms: one wide element cannot veto a corridor that
    twenty other bands respect. That removes the need for a separate cross-layout pre-mask pass
    and it is non-circular — no gutter is needed to compute it.

    The blocker budget is ABSOLUTE (``MAX_BLOCKING_BANDS``) with an absolute floor of clear
    evidence (``K - occ >= MIN_GUTTER_ROWS``). A proportional budget makes a 30-row two-column
    table of contents keep its columns while the same content split across a page break loses
    them on the short half; same layout must give the same answer regardless of how many rows
    it happens to have.

    Args:
        bands: The candidate region's bands.
        em: Page em, milli-units.
        x0: Content extent left, milli-units.
        x1: Content extent right, milli-units.

    Returns:
        Left-to-right ``(gx0, gx1)`` spans. Empty when the region has no columns.
    """
    k = len(bands)
    if k < MIN_GUTTER_ROWS or x1 <= x0 or em <= 0:
        return []

    bucket = max(em // BUCKETS_PER_EM, 1)
    n_buckets = (x1 - x0) // bucket + 1
    occ = [0] * n_buckets

    for band in bands:
        hit = [False] * n_buckets
        for atom in band.atoms:
            lo = max(0, min(n_buckets - 1, (atom.x0 - x0) // bucket))
            hi = max(0, min(n_buckets - 1, (atom.x1 - x0) // bucket))
            for i in range(lo, hi + 1):
                hit[i] = True
        for i in range(n_buckets):
            if hit[i]:
                occ[i] += 1

    gutters: list[tuple[int, int]] = []
    run_start: int | None = None
    for i in range(n_buckets + 1):
        clear = (
            i < n_buckets
            and occ[i] <= MAX_BLOCKING_BANDS
            and k - occ[i] >= MIN_GUTTER_ROWS
        )
        if clear:
            if run_start is None:
                run_start = i
            continue
        if run_start is None:
            continue
        run_end = i - 1
        # (i) A run touching bucket 0 or the last bucket is edge whitespace — a MARGIN, and a
        # margin is not a corridor. (ii) 1.5 em is poppler's largest intra-line word gap, so a
        # narrower run could be sitting inside somebody's sentence.
        run_mu = (run_end - run_start + 1) * bucket
        if (
            run_start > 0
            and run_end < n_buckets - 1
            and run_mu * MIN_GUTTER_EM[1] >= em * MIN_GUTTER_EM[0]
        ):
            gutters.append((x0 + run_start * bucket, x0 + (run_end + 1) * bucket))
        run_start = None
    return gutters


@dataclass(frozen=True, slots=True)
class Frame:
    """One column of a spatial region: an x-span, its measured advance, and its cell budget."""

    #: Milli-unit bounds of the column on the page.
    x0: int
    x1: int
    #: Milli-units per display cell, MEASURED from this frame's own line atoms. pdfplumber uses
    #: one global ``x_density = 7.25 pt`` (the advance of 12 pt Courier, unrelated to the
    #: document) and its documented consequence is drift; poppler derives a block's width from
    #: its own longest line and ignores intra-block x offsets, so one long line pushes every
    #: neighbouring column right for the whole page. Here it is per frame, automatically.
    adv: int
    #: Width in cells. Defined as the max of the frame's geometric width and its own widest
    #: actual placement, which is what makes intra-frame overflow impossible BY CONSTRUCTION.
    cells: int
    #: Absolute canvas column where this frame begins.
    col_start: int


def _frame_of(frames: Sequence[Frame], atom: Atom) -> int:
    """Index of the frame owning an atom, by its x-CENTRE — §4.2 step 7.

    Centres, never edges, so an atom that slightly overhangs a gutter lands in exactly one
    frame.

    The rule is literally "the FIRST frame whose right edge is at or beyond the centre".
    Frames are left to right and disjoint, so for a centre inside a frame that frame is the
    answer. A centre that falls inside a GUTTER — possible for an atom wider than its column —
    is past the left frame's ``x1`` and before the right frame's ``x1``, so it resolves to the
    frame on its **right**; a centre past the last frame's ``x1`` resolves to the last frame.
    The spec (§4.2 step 7) fixes only the centre rule and is silent on the gutter case, so what
    matters here is that the tie-break is a pure function of the geometry and is written down
    truthfully: an earlier draft of this docstring claimed "the frame on its left", which is
    the opposite of what the loop does, and a false invariant in a comment is the kind of thing
    the next refactor trusts.
    """
    centre = (atom.x0 + atom.x1) // 2
    for i, frame in enumerate(frames):
        if centre <= frame.x1:
            return i
    return len(frames) - 1


def _measure_adv(atoms: Sequence[Atom], em: int) -> int:
    """A frame's own character advance: the lower median of ``(x1 - x0) // cell_width(text)``.

    Only line atoms of at least ``MIN_MEASURE_CELLS`` cells qualify. poppler refuses to trust a
    measured gap unless a line has more than one word for the same reason: a 1-3 character
    line's polygon hugs two or three glyphs and its implied advance is not representative.
    """
    samples: list[int] = []
    for atom in atoms:
        if atom.kind != "line":
            continue
        width = cell_width(atom.text)
        if width < MIN_MEASURE_CELLS:
            continue
        samples.append((atom.x1 - atom.x0) // width)
    if not samples:
        return max(em * FALLBACK_ADV[0] // FALLBACK_ADV[1], 1)
    samples.sort()
    return max(samples[(len(samples) - 1) // 2], 1)


def _off(atom: Atom, frame: Frame) -> int:
    """The atom's start column WITHIN its frame, before tab snapping.

    LTR lines anchor on their left edge. An RTL line's ``x0`` is its visual END, so it anchors
    on its right edge instead; the result is clamped into the frame, and step 7's
    ``cells`` term then guarantees the other end fits too.
    """
    if is_rtl(atom.text):
        colpos = (atom.x1 - frame.x0) // frame.adv
        return max(0, colpos - cell_width(atom.text))
    return max(0, (atom.x0 - frame.x0) // frame.adv)


def build_frames(
    gutters: Sequence[tuple[int, int]],
    x0: int,
    x1: int,
    bands: Sequence[Band],
    em: int,
) -> list[Frame]:
    """The ``m`` gutters cut a candidate into ``m + 1`` frames, left to right.

    Each frame measures its OWN advance, so a frame of 8 pt values and a frame of 14 pt labels
    each get their own correct density. ``cells`` is::

        cells_j = max( ceil((frame.x1 - frame.x0) / adv_j),
                       max over L of ( off(L) + cell_width(L.text) ) )

    and the second term is what makes intra-frame overflow impossible by construction: the
    frame is defined to be as wide as its own widest actual placement, so no atom can be pushed
    past its own frame's end.

    Args:
        gutters: Left-to-right ``(gx0, gx1)`` spans from :func:`find_gutters`.
        x0: Region left, milli-units.
        x1: Region right, milli-units.
        bands: The region's bands.
        em: Page em, milli-units.

    Returns:
        ``len(gutters) + 1`` frames, left to right, with ``col_start`` accumulated.
    """
    spans: list[tuple[int, int]] = []
    left = x0
    for gx0, gx1 in gutters:
        spans.append((left, gx0))
        left = gx1
    spans.append((left, x1))

    # Advance first: the frame's cell budget is expressed in cells, so it cannot be computed
    # before the size of a cell is known. Two passes over the same partition, not one pass with
    # a placeholder, because a placeholder advance would silently survive into `cells`.
    shells = [Frame(x0=sx0, x1=sx1, adv=1, cells=0, col_start=0) for sx0, sx1 in spans]
    owned: list[list[Atom]] = [[] for _ in shells]
    for band in bands:
        for atom in band.atoms:
            owned[_frame_of(shells, atom)].append(atom)

    frames: list[Frame] = []
    col_start = 0
    for i, (sx0, sx1) in enumerate(spans):
        adv = _measure_adv(owned[i], em)
        probe = Frame(x0=sx0, x1=sx1, adv=adv, cells=0, col_start=col_start)
        cells = -(-(sx1 - sx0) // adv)  # integer ceiling; never math.ceil on a float
        for atom in owned[i]:
            cells = max(cells, _off(atom, probe) + cell_width(atom.text))
        frames.append(replace(probe, cells=cells))
        col_start += cells + GUTTER_CELLS
    return frames


# ---------------------------------------------------------------------------------------
# Step 7b — tab snapping
# ---------------------------------------------------------------------------------------


def _accept_stops(counts: dict[int, int]) -> list[int]:
    """Greedy tab stops over a multiset of column positions, ascending.

    Candidates are the distinct positions sorted by ``(-count, position)`` — a total order,
    because a position appears once among the candidates. A candidate within ``TAB_SNAP`` of an
    already-accepted stop is skipped, so no two stops can compete for the same atom.
    """
    accepted: list[int] = []
    for pos in sorted(counts, key=lambda p: (-counts[p], p)):
        if any(abs(pos - s) <= TAB_SNAP for s in accepted):
            continue
        accepted.append(pos)
    accepted.sort()
    return accepted


def _nearest_stop(accepted: Sequence[int], pos: int) -> int | None:
    """The accepted stop within ``TAB_SNAP`` of ``pos``, ties to the LOWER stop; None if none.

    ``accepted`` is ascending, so the FIRST strictly-minimal distance is the lower stop.
    """
    best: int | None = None
    for stop in accepted:
        if abs(pos - stop) > TAB_SNAP:
            continue
        if best is None or abs(pos - stop) < abs(pos - best):
            best = stop
    return best


def snap_tabs(frames: list[Frame], bands: Sequence[Band]) -> dict[AtomKey, int]:
    """Atom key -> snapped column offset within its frame. Empty when tab snapping is off.

    Tesseract's tab-stop idea reduced to an integer pass. It fixes the single most
    eyeball-salient defect of any padded renderer: a numeric column that aligns on its RIGHT
    edge lands +/-1 cell apart when two values of different length round their left edges
    differently.

    Within each frame the multiset of ``off(L)`` over line and mark atoms is collected;
    candidate stops are the distinct values sorted by ``(-count, off)``; stops are accepted
    greedily, skipping any candidate within ``TAB_SNAP`` of one already accepted. Each atom
    whose ``off`` is within ``TAB_SNAP`` of an accepted stop snaps to it, ties to the LOWER
    stop.

    **RTL ATOMS SNAP BY THEIR RIGHT EDGE, IN A SEPARATE STOP SET.** ``_off`` already anchors an
    RTL line by its visual end (§4.2 step 9 is explicit about it, and §9.3's
    ``test_rtl_placed_by_right_edge`` asserts the exact shared right edge that follows). A stop
    set over START columns snaps the wrong end of those atoms: an Arabic column whose values
    differ in length has as many distinct start offsets as it has lengths, so start-snapping
    moves each value's right edge by up to ``TAB_SNAP`` in a direction that depends on its
    length — turning an exactly-aligned right edge into a ragged one. Snapping is a repair for
    integer-division jitter, and for an RTL line the jitter lives in ``(x1 - frame.x0) //
    adv``, i.e. in the right edge; that is the quantity worth clustering.

    So each frame keeps TWO independent stop sets: one over the start columns of its LTR atoms,
    one over the ``off + cell_width`` end columns of its RTL atoms. An atom votes only in the
    set for its own direction, so an LTR run can never drag an RTL right edge and vice versa.
    An RTL atom's snapped END is converted back to a start offset before it is stored, so the
    map's contract — key -> START offset within the frame — is unchanged for every caller.

    The alternative, excluding RTL atoms from snapping entirely, would also stop the damage,
    but it leaves the RTL column with the raw per-atom division jitter that snapping exists to
    remove, and it makes ``tab_snap`` mean two different things depending on the script.

    Snapping cannot create an overlap, because step 9's cursor rule still enforces at least one
    space between neighbours.

    Args:
        frames: The region's frames.
        bands: The region's bands.

    Returns:
        A map from :data:`AtomKey` to snapped START offset. Atoms with no nearby stop are
        absent, and callers read the map with ``snap.get(atom.key, _off(atom, frame))``.
    """
    out: dict[AtomKey, int] = {}
    if not frames:
        return out

    grouped: list[list[Atom]] = [[] for _ in frames]
    for band in bands:
        for atom in band.atoms:
            if atom.kind not in ("line", "mark"):
                continue
            grouped[_frame_of(frames, atom)].append(atom)

    for fix, frame in enumerate(frames):
        # (key, position, width) per direction. ``width`` is 0 for the LTR set, where the
        # position IS the offset; for the RTL set the position is the END column and the width
        # is what converts it back.
        ltr_counts: dict[int, int] = {}
        rtl_counts: dict[int, int] = {}
        ltr: list[tuple[AtomKey, int]] = []
        rtl: list[tuple[AtomKey, int, int]] = []
        for atom in grouped[fix]:
            off = _off(atom, frame)
            if is_rtl(atom.text):
                width = cell_width(atom.text)
                end = off + width
                rtl_counts[end] = rtl_counts.get(end, 0) + 1
                rtl.append((atom.key, end, width))
            else:
                ltr_counts[off] = ltr_counts.get(off, 0) + 1
                ltr.append((atom.key, off))

        if ltr_counts:
            accepted = _accept_stops(ltr_counts)
            for key, off in ltr:
                best = _nearest_stop(accepted, off)
                if best is not None:
                    out[key] = best
        if rtl_counts:
            accepted = _accept_stops(rtl_counts)
            for key, end, width in rtl:
                best = _nearest_stop(accepted, end)
                if best is not None:
                    # Back to a START offset, floored at the frame start exactly as ``_off``
                    # floors it, so a value wider than its own frame degrades identically.
                    out[key] = max(0, best - width)
    return out


def _refit_frames(
    frames: Sequence[Frame], bands: Sequence[Band], snap: dict[AtomKey, int]
) -> list[Frame]:
    """Re-widen each frame to cover its SNAPPED placements, and re-accumulate ``col_start``.

    Step 7 computes ``cells`` from raw offsets and step 7b then moves some of them by up to
    ``TAB_SNAP`` cells. An atom snapped RIGHT onto a neighbour's stop can therefore need one
    cell more than step 7 budgeted — rare, but "intra-frame overflow is impossible by
    construction" is a guarantee, and a guarantee that holds in the common case is not one.
    Widening only: no frame ever shrinks, so this cannot pull an atom out of its own column.
    """
    grouped: list[list[Atom]] = [[] for _ in frames]
    for band in bands:
        for atom in band.atoms:
            grouped[_frame_of(frames, atom)].append(atom)

    out: list[Frame] = []
    col_start = 0
    for fix, frame in enumerate(frames):
        cells = frame.cells
        for atom in grouped[fix]:
            off = snap.get(atom.key, _off(atom, frame))
            cells = max(cells, off + cell_width(atom.text))
        out.append(replace(frame, cells=cells, col_start=col_start))
        col_start += cells + GUTTER_CELLS
    return out


# ---------------------------------------------------------------------------------------
# Steps 5, 8 — candidate regions, the gates, and block ownership
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Placement:
    """Where one atom landed on the canvas, in region-relative rows and absolute columns."""

    key: AtomKey
    tag: str
    rect: tuple[int, int, int, int]
    row: int
    col0: int
    col1: int


@dataclass(slots=True)
class Region:
    """A contiguous run of bands emitted as one unit: either linear or spatial."""

    kind: Literal["linear", "spatial"]
    bands: list[Band]
    em: int
    frames: list[Frame] = field(default_factory=list)
    #: Why a candidate declined the spatial path — one of :data:`Reason`. "" when spatial.
    reason: str = ""
    #: Populated by :func:`render_canvas` for spatial regions only.
    rows: list[str] = field(default_factory=list)
    placements: list[Placement] = field(default_factory=list)
    #: ``band_rows[k] = (first_row, last_row)`` for band ``k``, region-relative and inclusive.
    #: Segments are cut on these boundaries, never mid-band.
    band_rows: list[tuple[int, int]] = field(default_factory=list)
    #: Indices into ``view.key_values`` whose union rect falls inside this region's rect,
    #: ascending. Populated by :func:`page_layout`.
    #:
    #: §4.2 step 11 needs this in two places — key/values spliced into their LINEAR region's
    #: ``_linear_elements(..., kv_ixs=…)`` (§4.4) and key/values emitted after a SPATIAL
    #: region's last segment under the ``additive`` suppression — and neither is derivable from
    #: ``block_ixs``/``table_ixs``/``mark_ixs``, because a key/value is never an atom. Without
    #: it the emitter has to call ``mu()`` and rebuild quad rectangles itself, which is exactly
    #: the geometry this module exists to keep on one side of a one-way dependency.
    kv_ixs: list[int] = field(default_factory=list)

    @property
    def atoms(self) -> list[Atom]:
        return [a for band in self.bands for a in band.atoms]

    @property
    def y0(self) -> int:
        return min(b.y0 for b in self.bands)

    @property
    def y1(self) -> int:
        return max(b.y1 for b in self.bands)

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (
            min(b.x0 for b in self.bands), self.y0,
            max(b.x1 for b in self.bands), self.y1,
        )

    @property
    def block_ixs(self) -> list[int]:
        """Owning block indices, ascending — the emitter's ``block_ixs`` for a linear region."""
        return sorted({a.source_ix for a in self.atoms if a.kind == "line"})

    @property
    def table_ixs(self) -> list[int]:
        return sorted({a.source_ix for a in self.atoms if a.kind == "table"})

    @property
    def mark_ixs(self) -> list[int]:
        return sorted({a.source_ix for a in self.atoms if a.kind == "mark"})


def _coverage_ok(bands: Sequence[Band], blocks: Sequence[TextBlock]) -> bool:
    """THE COVERAGE GATE — §4.2 step 5.4.

    For every block owning a line atom in this candidate, ALL of that block's line atoms must
    be in this candidate AND the whitespace-normalised concatenation of those line texts must
    equal the whitespace-normalised ``block.text``.

    This is what makes the information-loss test pass BY CONSTRUCTION: a block is rendered in
    exactly one place, and a block rendered on a canvas has every character of its text on that
    canvas. A block whose lines do not reconstruct it (a span-join partial failure, a provider
    oddity) falls back to linear, where ``block.text`` is emitted whole.
    """
    present: dict[int, list[Atom]] = {}
    for band in bands:
        for atom in band.atoms:
            if atom.kind == "line" and atom.block_ix is not None:
                present.setdefault(atom.block_ix, []).append(atom)

    for bix in sorted(present):
        block = blocks[bix]
        expected = {i for i, line in enumerate(block.lines) if line.bbox}
        got = {a.sub_ix for a in present[bix]}
        if expected:
            if got != expected:
                return False
            ordered = sorted(present[bix], key=lambda a: a.sub_ix)
            if _norm_ws(" ".join(a.text for a in ordered)) != _norm_ws(block.text):
                return False
        else:
            # A hull atom: the block IS its own single atom, so it reconstructs itself. It is
            # still subject to the multiline gate, which runs separately.
            if got != {0} or len(present[bix]) != 1:
                return False
    return True


def _kv_rect(kv: KeyValue) -> tuple[int, int, int, int] | None:
    """The union of a key/value's key and value rectangles, in milli-units, or None.

    ``None`` only when NEITHER box has geometry — that pair is floating and §4.2 step 11 sends
    it to the end of the page. A pair with one box is placed by that box alone rather than
    being discarded, because half a position is still a position.
    """
    rects = [
        r for r in (_quad_rect(kv.key_bbox), _quad_rect(kv.value_bbox)) if r is not None
    ]
    if not rects:
        return None
    return (
        min(r[0] for r in rects), min(r[1] for r in rects),
        max(r[2] for r in rects), max(r[3] for r in rects),
    )


def _assign_kvs(regions: Sequence[Region], view: LayoutView, page: int) -> None:
    """Populate ``Region.kv_ixs``: each on-page key/value goes to the FIRST region containing
    it, by full rectangle containment.

    First-in-band-order, not nearest, and containment rather than overlap: both are total
    functions of the geometry with no tie to break, so the assignment cannot depend on
    iteration order. A pair contained by no region is simply absent from every ``kv_ixs`` —
    step 11's "falls inside a spatial region" clause is a filter, not a partition, and
    inventing an owner for a pair that straddles a region boundary would print it under a
    canvas that does not show it.
    """
    for kix, kv in enumerate(view.key_values):
        if kv.page != page:
            continue
        rect = _kv_rect(kv)
        if rect is None:
            continue
        for region in regions:
            rx0, ry0, rx1, ry1 = region.rect
            if rx0 <= rect[0] and ry0 <= rect[1] and rect[2] <= rx1 and rect[3] <= ry1:
                region.kv_ixs.append(kix)
                break


def _own_blocks(regions: Sequence[Region]) -> None:
    """Step 8: each block is owned by the first region (in band order) holding a line atom of
    it; its line atoms in any later region are dropped from that region.

    Reachable only when a ``Table`` interleaves a paragraph's y-range. The coverage gate then
    sends the owning region to linear anyway — where ``block.text`` is emitted whole — so no
    text is lost by the drop.
    """
    owner: dict[int, int] = {}
    for rix, region in enumerate(regions):
        for band in region.bands:
            keep: list[Atom] = []
            for atom in band.atoms:
                if atom.kind != "line" or atom.block_ix is None:
                    keep.append(atom)
                    continue
                held = owner.setdefault(atom.block_ix, rix)
                if held == rix:
                    keep.append(atom)
            band.atoms = keep
        region.bands = [b for b in region.bands if b.atoms]


def build_regions(
    bands: Sequence[Band], em: int, x0: int, x1: int, blocks: Sequence[TextBlock]
) -> list[Region]:
    """Bands -> ordered regions, including the multiline gate and the coverage gate.

    Steps 5 and 8. Separator bands are singleton linear regions; maximal runs of consecutive
    non-separator bands are candidates. A candidate is linear if it holds a ``multiline`` atom
    (a hull spanning five visual rows cannot be put on one canvas row without either printing
    it five times or squashing it — refusing degrades to exactly PMD 1.0, which is the honest
    answer), if it fails the coverage gate, or if it has no gutter.

    Args:
        bands: The page's bands, in seed order, already marked by :func:`mark_separators`.
        em: Page em, milli-units.
        x0: Content extent left, milli-units.
        x1: Content extent right, milli-units.
        blocks: ``view.blocks``, for the coverage gate.

    Returns:
        Regions in band order. There is no second sort.
    """
    regions: list[Region] = []
    run: list[Band] = []
    for band in bands:
        if band.separator:
            if run:
                regions.append(Region(kind="linear", bands=run, em=em))
                run = []
            regions.append(Region(kind="linear", bands=[band], em=em))
            continue
        run.append(band)
    if run:
        regions.append(Region(kind="linear", bands=run, em=em))

    # Ownership runs BEFORE the gates: a candidate that lost atoms to an earlier region must
    # fail the coverage gate, which is exactly what the gate's "all of that block's line atoms
    # are in this candidate" clause says.
    _own_blocks(regions)
    regions = [r for r in regions if r.bands]

    for region in regions:
        if any(b.separator for b in region.bands):
            continue
        if any(a.multiline for a in region.atoms):
            region.reason = "multiline"
            continue
        if not _coverage_ok(region.bands, blocks):
            region.reason = "coverage"
            continue
        gutters = find_gutters(region.bands, em, x0, x1)
        if not gutters:
            region.reason = "no-gutter"
            continue
        frames = build_frames(gutters, x0, x1, region.bands, em)
        region.kind = "spatial"
        region.frames = frames
        region.reason = ""
    return regions


# ---------------------------------------------------------------------------------------
# Step 9 — canvas rows
# ---------------------------------------------------------------------------------------


def render_canvas(region: Region, snap: dict[AtomKey, int]) -> list[str]:
    """A spatial region as canvas rows: no fence, no trailing spaces, one row per band.

    Vertical spacing is poppler's clamp,
    ``gap_rows(k) = min(MAX_ROW_GAP, max(0, (band_k.y0 - band_{k-1}.y1) // em))``, with a floor
    of 0 rather than 1 because a row is already emitted per band.

    Horizontally, a band's atoms are placed in ``(frame_ix, true_col, x0, KIND_RANK,
    source_ix, sub_ix)`` order under the cursor rule::

        placed = max(true_col, cursor + 1) if cursor > 0 else true_col
        if placed - true_col > MAX_DRIFT or placed + width > frame.col_start + frame.cells:
            start a new row; the floor becomes the atom's OWN frame start

    THE SECOND BREAK CONDITION IS LOAD-BEARING AND IS NOT REDUNDANT WITH THE FIRST. Step 7
    budgets ``cells_j`` from each atom's OWN offset, so ``true_col + width <= col_start +
    cells`` always holds — but the cursor rule places at ``max(true_col, cursor + 1)``, which
    may sit up to ``MAX_DRIFT`` cells further right *without* tripping the drift test. Two
    atoms sharing one frame in one band are enough: a 1-cell atom at offset 0 leaves the
    cursor at 1, and a 10-cell atom whose ``true_col`` is also offset 0 then lands at offset 2
    and ends two cells past the frame. That breaks three stated guarantees at once — §5.3's
    x-inversion (``left_j + (col - col_start_j) * adv_j`` maps past the frame's right edge),
    the ``GUTTER_CELLS`` visible gap when the frame is not the last one, and the declared
    ``cols`` when it is. ``MAX_DRIFT < GUTTER_CELLS`` (§4.3) only bounds the damage to "not
    inside the NEXT frame"; it does not keep the atom inside its own. ``_refit_frames`` does
    not close the hole either — it models snapping, not the cursor. Testing the frame's own
    end directly is the only thing that preserves the guarantee, and it preserves it exactly
    rather than widening the canvas after the fact.

    The break is always productive: after it, ``cursor`` is 0 and ``placed`` is
    ``max(true_col, frame.col_start) == true_col``, and ``true_col + width <= col_start +
    cells`` by step 7's construction, so the atom fits and the loop cannot break twice for one
    atom.

    That overflow policy is poppler's break-the-line, one step better. pdfplumber's
    ``max(min(1, line_len), round(x_dist) - line_len)`` never goes backwards, so the first
    over-long token shifts every remaining word on that line right, permanently and silently —
    unbounded damage. poppler breaks the row and resets to column 0, which is bounded but
    throws away the columnar structure for the rest of the row. Resetting to the OVERFLOWING
    ATOM'S OWN FRAME START means the atom still lands in its true column and the rows above and
    below still align: bounded damage AND preserved columns, at most one extra row per
    overflowing atom.

    Every row is ``rstrip()``ed. **Leading spaces are never stripped — they are the payload.**

    Side effect, by design: ``region.rows``, ``region.placements`` and ``region.band_rows`` are
    populated. The placements are what the segment legend and the x-inversion test read, and
    recomputing them from the rows is not possible (a row is a string; an atom's identity is
    not recoverable from it).

    Args:
        region: A spatial region with frames.
        snap: The tab-stop map from :func:`snap_tabs`; empty disables snapping.

    Returns:
        The region's rows.
    """
    rows: list[str] = []
    placements: list[Placement] = []
    band_rows: list[tuple[int, int]] = []
    frames = region.frames
    em = region.em

    prev_y1: int | None = None
    for band in region.bands:
        if prev_y1 is not None:
            gap = min(MAX_ROW_GAP, max(0, (band.y0 - prev_y1) // em)) if em > 0 else 0
            rows.extend("" for _ in range(gap))
        prev_y1 = band.y1
        first_row = len(rows)

        placed_atoms: list[tuple[int, int, int, int, int, Atom, Frame]] = []
        for atom in band.atoms:
            fix = _frame_of(frames, atom)
            frame = frames[fix]
            off = snap.get(atom.key, _off(atom, frame))
            true_col = frame.col_start + off
            placed_atoms.append(
                (fix, true_col, atom.x0, KIND_RANK[atom.kind], atom.source_ix, atom, frame)
            )
        # §7.1's key is (frame_ix, true_col, x0, KIND_RANK, source_ix, sub_ix). It is total —
        # no two atoms compare equal — but `source_ix` is a PROVIDER ARRIVAL INDEX, so two
        # atoms tying on all of the geometry are ordered by where they happened to sit in
        # `view.blocks`. Permute that array and they swap, which changes the rendered row and
        # therefore the artifact's sha256. Measured: 5 of 800 fuzzed multi-column pages
        # rendered differently under a block permutation, all of them CJK/RTL atoms sharing a
        # milli-unit x0 inside one frame.
        #
        # `text` is intrinsic to the atom, so inserting it ahead of `source_ix` makes the
        # order depend only on what the atoms ARE, never on the order they arrived in. Exact
        # duplicates (same rect, same text) are already dropped by the band sweep, so the
        # positions text cannot separate are positions nothing needs to separate.
        placed_atoms.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[5].text, t[4], t[5].sub_ix))

        row = ""
        cursor = 0
        for _fix, true_col, _x0, _rank, _six, atom, frame in placed_atoms:
            width = cell_width(atom.text)
            placed = max(true_col, cursor + 1) if cursor > 0 else true_col
            # The atom's OWN frame end is a break condition in its own right; see the docstring
            # for why the drift test alone does not imply it.
            limit = frame.col_start + frame.cells
            if placed - true_col > MAX_DRIFT or placed + width > limit:
                rows.append(row.rstrip())
                row = ""
                # ``cursor`` counts cells ACTUALLY written, so a fresh row restarts it at 0 and
                # the frame start becomes the placement FLOOR. Carrying the spec's
                # ``cursor = frame.col_start`` literally into the padding term would under-pad
                # the new row by exactly ``col_start`` cells.
                cursor = 0
                placed = max(true_col, frame.col_start)
            row += " " * (placed - cursor) + atom.text
            cursor = placed + width
            if atom.tag:
                placements.append(Placement(
                    key=atom.key, tag=atom.tag,
                    rect=(atom.x0, atom.y0, atom.x1, atom.y1),
                    row=len(rows), col0=placed, col1=placed + max(width - 1, 0),
                ))
        rows.append(row.rstrip())
        band_rows.append((first_row, len(rows) - 1))

    region.rows = rows
    region.placements = placements
    region.band_rows = band_rows
    return rows


# ---------------------------------------------------------------------------------------
# Step 10 — segments
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Segment:
    """One emitted canvas: a self-contained slice of a spatial region."""

    index: int          # 1-based
    total: int
    row_lo: int         # region-relative, inclusive
    row_hi: int
    rows: list[str]
    #: The region's DECLARED grid width, ``frames[-1].col_start + frames[-1].cells`` — §5.3's
    #: ``canvas <cols>x<rows>``. A property of the frame table, so it is CONSTANT across every
    #: segment of one region: §6.1 emits ``canvas 99x11`` and ``canvas 99x10`` for the same
    #: ``frames=720:4080:48:70|4430:7780:48:70`` (48 + 3 + 48 = 99) while the widest row in
    #: those payloads is 92 and 76 cells. Defining ``cols`` as the widest RENDERED row would
    #: make two segments of one region advertise different widths for the same frame table,
    #: and would turn §9.3's ``cell_width(row) <= cols`` into a tautology that can never fail.
    cols: int
    rect: tuple[int, int, int, int]   # hull of the segment's OWN bands, milli-units
    em: int
    frames: list[Frame]
    #: The segment's tagged atoms, with ``Placement.row`` REBASED to be SEGMENT-LOCAL
    #: (``0 .. len(rows) - 1``). ``region.placements`` counts from the region's row 0; §6.1's
    #: second segment carries ``rows=11..20`` and writes its ``2. CUSTOMER DUE DILIGENCE``
    #: legend as ``cell=[0,3,24,3]`` for an atom sitting at region row 14, so the worked
    #: example is unambiguous that the legend's ``r0``/``r1`` index the fence the reader is
    #: holding. §5.3 defines ``rows=lo..hi`` as region-relative and says nothing about the
    #: legend, so §6 is the only evidence and it says segment-local. A consumer that indexes
    #: its own fence with a region-relative row lands ``row_lo`` rows off on every segment
    #: after the first.
    legend: list[Placement]
    #: Per-row band ``y0``, for ``DPC_CANVAS_ROW_Y``. Blank gap rows carry the previous band's
    #: value, so the list is always ``len(rows)`` long and monotonic.
    row_ys: list[int]

    @property
    def has_tags(self) -> list[str]:
        """Distinct legend tags, sorted — the ``has=`` fallback past ``CANVAS_LEGEND_MAX``."""
        return sorted({p.tag for p in self.legend})


def _seg_chars(rows: Sequence[str]) -> int:
    """Characters a row list costs in the emitted file, newlines included."""
    return sum(len(r) for r in rows) + len(rows)


def segment(region: Region, rows: list[str]) -> list[Segment]:
    """Cut a spatial region's rows into segments at BAND boundaries.

    Each segment is at most ``CANVAS_SEG_ROWS`` rows and ``CANVAS_SEG_CHARS`` characters. A
    single band that alone exceeds a cap becomes its own oversized segment rather than being
    cut mid-row — a canvas row cut in half is not a smaller canvas, it is a corrupt one.

    Every segment re-uses the region's frames — and therefore the region's declared ``cols``,
    which is a function of that frame table and not of the rows that happen to fall in this
    slice — so columns line up across segments, and carries its own complete anchor and
    legend: a 4000-character canvas cut by a 512-token chunker leaves the second half with no
    anchor, no frame table and no ``em`` — an orphaned block of space-padded text no consumer
    can invert.

    Args:
        region: The rendered spatial region.
        rows: ``region.rows``, passed explicitly so the function is testable in isolation.

    Returns:
        Segments in band order, numbered ``1..n``.
    """
    if not rows or not region.band_rows:
        return []

    # A chunk is one band plus the blank gap rows that precede it, so a cut always lands on a
    # band boundary and no gap row is ever orphaned onto the far side of a cut.
    chunks: list[tuple[int, int, int]] = []  # (row_lo, row_hi, band_ix)
    prev_hi = -1
    for bix, (lo, hi) in enumerate(region.band_rows):
        chunks.append((prev_hi + 1, hi, bix))
        prev_hi = hi

    groups: list[list[tuple[int, int, int]]] = []
    cur: list[tuple[int, int, int]] = []
    for chunk in chunks:
        trial = cur + [chunk]
        span = rows[trial[0][0]:trial[-1][1] + 1]
        if cur and (len(span) > CANVAS_SEG_ROWS or _seg_chars(span) > CANVAS_SEG_CHARS):
            groups.append(cur)
            cur = [chunk]
            continue
        cur = trial
    if cur:
        groups.append(cur)

    # The declared grid width, computed ONCE from the frame table so every segment of this
    # region advertises the same number (§5.3). A region with no frames cannot happen on the
    # rendered path — ``render_canvas`` needs them — but ``segment`` is called directly by
    # tests, so the empty case degrades to 0 rather than raising.
    cols = (
        region.frames[-1].col_start + region.frames[-1].cells if region.frames else 0
    )

    segments: list[Segment] = []
    for i, group in enumerate(groups):
        lo, hi = group[0][0], group[-1][1]
        # Leading blank rows are the gap BEFORE this segment's first band; they say nothing
        # once the segment is a standalone chunk, and dropping them keeps `rows=lo..hi`
        # honest about what the payload actually contains.
        while lo < hi and rows[lo] == "":
            lo += 1
        band_ixs = [c[2] for c in group]
        bands = [region.bands[b] for b in band_ixs]
        seg_rows = rows[lo:hi + 1]

        row_ys: list[int] = []
        cursor_y = bands[0].y0
        for row_ix in range(lo, hi + 1):
            for bix in band_ixs:
                blo, bhi = region.band_rows[bix]
                if blo <= row_ix <= bhi:
                    cursor_y = region.bands[bix].y0
                    break
            row_ys.append(cursor_y)

        # Rebased to segment-local rows; see ``Segment.legend``. The rebase happens on a COPY,
        # so ``region.placements`` stays region-relative for the x-inversion tests and for a
        # second call to ``segment`` on the same region.
        legend = [
            replace(p, row=p.row - lo) for p in region.placements if lo <= p.row <= hi
        ]
        # §7.1's legend order: (row, col, tag, source_ix, sub_ix). Subtracting the same ``lo``
        # from every row is order-preserving, so sorting after the rebase is the same sort.
        legend.sort(key=lambda p: (p.row, p.col0, p.tag, p.key[1], p.key[2]))
        segments.append(Segment(
            index=i + 1, total=len(groups), row_lo=lo, row_hi=hi, rows=seg_rows,
            cols=cols,
            rect=(
                min(b.x0 for b in bands), min(b.y0 for b in bands),
                max(b.x1 for b in bands), max(b.y1 for b in bands),
            ),
            em=region.em, frames=region.frames, legend=legend, row_ys=row_ys,
        ))
    return segments


# ---------------------------------------------------------------------------------------
# Steps 1-10 — the page entry point
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class PageLayout:
    """One page's spatial decomposition, or an empty one with the reason it declined."""

    page: int
    em: int
    #: Empty regions + a populated reason means "emit this page exactly as PMD 1.0 would have".
    regions: list[Region]
    reason: str
    #: Content extent, milli-units. The denominator of every width threshold on this page.
    content_x0: int
    content_x1: int
    #: Blocks / key-values / tables / marks with NO geometry at all. PMD 1.0's rule applies:
    #: they append at the end of the page, without an anchor, rather than with an invented one.
    floating_blocks: list[int] = field(default_factory=list)
    floating_kvs: list[int] = field(default_factory=list)
    floating_tables: list[int] = field(default_factory=list)
    floating_marks: list[int] = field(default_factory=list)

    @property
    def spatial(self) -> list[Region]:
        return [r for r in self.regions if r.kind == "spatial"]

    @property
    def canvases(self) -> int:
        return len(self.spatial)


def _declined(page: int, reason: str, em: int = 0) -> PageLayout:
    """An empty layout: the emitter treats this as 'render the whole page linearly'."""
    return PageLayout(
        page=page, em=em, regions=[], reason=reason, content_x0=0, content_x1=0,
    )


def page_layout(view: LayoutView, page: int, *, tab_snap: bool = True) -> PageLayout:
    """Steps 1-10 for one page.

    NEVER raises: every failure returns a ``PageLayout`` whose ``regions`` is empty and whose
    ``reason`` is populated, which the emitter treats as "emit this page exactly as PMD 1.0
    would have". A layout engine that can raise is a converter that can 500 on a document it
    would otherwise have converted correctly-but-linearly, which is strictly worse output than
    no output is better.

    A page that produced ZERO spatial regions returns no regions at all, because §4.2 step 11
    discards the decomposition in that case: every page without a canvas is byte-identical to
    PMD 1.0, structurally rather than by test.

    Args:
        view: The provider-neutral read of the document.
        page: 1-based page number.
        tab_snap: Frame-local tab-stop snapping (§4.2 step 7b). The first thing to disable if a
            fidelity regression appears.

    Returns:
        A ``PageLayout``. ``regions`` is empty iff no canvas was produced.
    """
    try:
        atoms, floating_blocks, floating_kvs = atoms_for_page(view, page)
        floating_tables = [
            i for i, t in enumerate(view.tables)
            if t.page == page and _quad_rect(t.bbox) is None
        ]
        floating_marks = [
            i for i, m in enumerate(view.marks)
            if m.page == page and _quad_rect(m.bbox) is None
        ]

        info = next((p for p in view.pages if p.page == page), None)
        em = page_em(atoms)
        if not atoms or em <= 0 or (info is not None and (info.width <= 0 or info.height <= 0)):
            return _declined(page, "no-geometry", em)
        if len(atoms) > MAX_ATOMS_PER_PAGE:
            return _declined(page, "too-dense", em)
        # The geometry gate keys on MEASURED data, never on the provider name.
        if not page_skew_ok(atoms):
            return _declined(page, "skew", em)

        content_x0 = min(a.x0 for a in atoms)
        content_x1 = max(a.x1 for a in atoms)

        bands = mark_separators(build_bands(atoms, em), content_x0, content_x1)
        regions = build_regions(bands, em, content_x0, content_x1, view.blocks)

        spatial = [r for r in regions if r.kind == "spatial"]
        if not spatial:
            # The MOST DIAGNOSTIC decline, not the topmost one; see ``REASON_RANK``. The
            # secondary key is the region's band-order index, so the choice stays total when
            # two regions decline for the same reason.
            declines = [
                (REASON_RANK.get(r.reason, len(REASON_RANK)), i, r.reason)
                for i, r in enumerate(regions) if r.reason
            ]
            return _declined(page, min(declines)[2] if declines else "no-gutter", em)

        _assign_kvs(regions, view, page)
        for region in spatial:
            snap = snap_tabs(region.frames, region.bands) if tab_snap else {}
            region.frames = _refit_frames(region.frames, region.bands, snap)
            render_canvas(region, snap)

        out = PageLayout(
            page=page, em=em, regions=regions, reason="",
            content_x0=content_x0, content_x1=content_x1,
            floating_blocks=floating_blocks, floating_kvs=floating_kvs,
            floating_tables=floating_tables, floating_marks=floating_marks,
        )
        return out
    except Exception as exc:  # noqa: BLE001 - see the NEVER-raises contract above.
        # PII: the class name and the page number only. An exception message on this path can
        # carry document text, and this is a KYC service.
        log.warning("canvas layout declined page=%d error=%s", page, type(exc).__name__)
        return _declined(page, "error")


__all__ = [
    "BAND_OVERLAP",
    "BUCKETS_PER_EM",
    "CANVAS_LEGEND_MAX",
    "CANVAS_SEG_CHARS",
    "CANVAS_SEG_ROWS",
    "FALLBACK_ADV",
    "FULLWIDTH_FRAC",
    "GUTTER_CELLS",
    "KIND_RANK",
    "MAX_ATOMS_PER_BAND",
    "MAX_ATOMS_PER_PAGE",
    "MAX_BLOCKING_BANDS",
    "MAX_DRIFT",
    "MAX_ROW_GAP",
    "MAX_SKEW",
    "MIN_GUTTER_EM",
    "MIN_GUTTER_ROWS",
    "MIN_MEASURE_CELLS",
    "REASON_RANK",
    "SINGLE_ROW_EM",
    "TAB_SNAP",
    "Atom",
    "AtomKey",
    "Band",
    "Frame",
    "Kind",
    "PageLayout",
    "Placement",
    "Reason",
    "Region",
    "Segment",
    "atoms_for_page",
    "build_bands",
    "build_frames",
    "build_regions",
    "cell_width",
    "find_gutters",
    "is_rtl",
    "mark_separators",
    "mu",
    "page_em",
    "page_layout",
    "page_skew_ok",
    "render_canvas",
    "segment",
    "snap_tabs",
]
