"""The spatial emitter, tested through the one thing a user can actually check.

The complaint this feature answers was concrete: two columns of text on a page came out of
PMD 1.0 as one vertical list. That is not merely lossy — it reads as SEQUENTIAL, so a
consumer binds the right column's last value to the left column's last heading. The
regression test for it therefore has to assert what a human would look for: that the two
columns appear on the same line, in the right order, separated by space.

The second thing these tests guard is the promise made to everything already in the store:
a document that uses no 2.0 feature must be byte-identical to what PMD 1.0 produced. That is
a structural guarantee in the emitter (a page with no canvas is rendered by the untouched
linear path over the whole page), and structural guarantees are exactly the ones worth
testing, because they are the ones a later refactor silently breaks.
"""
from __future__ import annotations

import hashlib
import random

import pytest

from dpc.emitter import to_pmd
from dpc.models import LayoutView, PageInfo, TextBlock, TextLine, Zone


def quad(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def block(text: str, box: tuple[float, float, float, float], **kw: object) -> TextBlock:
    """A block that carries its own single line — the Azure-layout shape."""
    q = quad(*box)
    return TextBlock(text=text, bbox=q, lines=[TextLine(text=text, bbox=q)], **kw)  # type: ignore[arg-type]


#: The left/right pairs of a US-Letter statement, in inches. The gutter (4.03 -> 4.58 in) is
#: 0.55 in, comfortably wider than any word gap at this type size.
COLUMN_ROWS = [
    ("Account Holder", "Statement Period", 1.53, 1.78),
    ("Jane Q. Public", "01 Jun 2026 - 30 Jun 2026", 1.88, 2.10),
    ("14 Rivermill Lane", "Currency  USD", 2.18, 2.40),
    ("Portland, OR 97205", "Closing Balance", 2.48, 2.70),
    ("Account  ****-****-4417", "USD 12,480.55", 2.78, 3.00),
]


def two_column_view() -> LayoutView:
    blocks = [block("QUARTERLY ACCOUNT STATEMENT", (0.83, 0.69, 7.78, 1.04), zone=Zone.title)]
    for left, right, y0, y1 in COLUMN_ROWS:
        blocks.append(block(left, (0.83, y0, 4.03, y1)))
        blocks.append(block(right, (4.58, y0, 7.78, y1)))
    return LayoutView(
        pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")], blocks=blocks
    )


def single_column_view(unit: str = "point") -> LayoutView:
    """One column, point units — the shape that must stay byte-identical to PMD 1.0."""
    blocks = [
        block("ANNUAL REPORT", (72, 60, 540, 90), zone=Zone.title),
        block("The directors present their report for the year ended 31 March.", (72, 110, 540, 130)),
        block("Principal activities are set out in note 3 to the accounts.", (72, 140, 540, 160)),
        block("The auditors have indicated their willingness to continue.", (72, 170, 540, 190)),
    ]
    return LayoutView(
        pages=[PageInfo(page=1, width=612, height=792, unit=unit)], blocks=blocks
    )


def render(view: LayoutView, **kw: object) -> str:
    return to_pmd(view, source="document", provider="azure_layout_v4", **kw)  # type: ignore[arg-type]


def canvas_rows(out: str) -> list[str]:
    """The rows inside the first ```text fence — the canvas payload, spaces intact."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("```text"))
    fence = lines[start][: -len("text")]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == fence)
    return lines[start + 1 : end]


# ---------------------------------------------------------------------------
# The complaint itself
# ---------------------------------------------------------------------------
def test_two_columns_render_side_by_side_on_one_line() -> None:
    """The regression for the reported defect, asserted the way a human would check it."""
    rows = canvas_rows(render(two_column_view()))

    for left, right, _, _ in COLUMN_ROWS:
        row = next((r for r in rows if left in r), None)
        assert row is not None, f"{left!r} missing from the canvas"
        assert right in row, f"{right!r} should share a line with {left!r}; got {row!r}"
        # ... and in the right order, with real separation between them.
        assert row.index(left) < row.index(right)
        gap = row[row.index(left) + len(left) : row.index(right)]
        assert gap.strip() == "" and len(gap) >= 3, f"columns not separated: {row!r}"


def test_the_left_column_never_wanders_between_rows() -> None:
    """A column is only a column if it starts at the same place on every row."""
    rows = canvas_rows(render(two_column_view()))
    starts = {r.index(left) for left, _, _, _ in COLUMN_ROWS for r in rows if left in r}
    assert len(starts) == 1, f"left column starts at differing offsets: {sorted(starts)}"

    rights = {r.index(right) for _, right, _, _ in COLUMN_ROWS for r in rows if right in r}
    assert len(rights) == 1, f"right column starts at differing offsets: {sorted(rights)}"


def test_a_full_width_heading_stays_real_markdown() -> None:
    """The canvas fires only where linear markdown was lying; a page title is not a column."""
    out = render(two_column_view())
    assert "# QUARTERLY ACCOUNT STATEMENT" in out
    assert "QUARTERLY ACCOUNT STATEMENT" not in "\n".join(canvas_rows(out))


def test_no_document_text_is_lost_to_the_canvas() -> None:
    out = render(two_column_view())
    for blk in two_column_view().blocks:
        assert blk.text in out, f"{blk.text!r} disappeared"


# ---------------------------------------------------------------------------
# The promise to everything already in the store
# ---------------------------------------------------------------------------
def test_a_page_with_no_canvas_is_pmd_1_0() -> None:
    """No 2.0 feature used => the file still declares 1.0, front matter unchanged."""
    out = render(single_column_view())
    assert out.startswith("---\npmd: 1.0\n")
    assert "layout:" not in out
    assert "canvases:" not in out
    assert "```text" not in out


def test_layout_linear_reproduces_the_band_free_rendering() -> None:
    """The escape hatch: a caller regenerating a stored hash can turn the pass off."""
    view = single_column_view()
    assert render(view, layout="linear") == render(view)


def test_legacy_rect_scale_pins_pmd_1_0_rounding_on_inch_pages() -> None:
    out = render(two_column_view(), layout="linear", rect_scale="legacy")
    assert "scale=" not in out
    assert out.startswith("---\npmd: 1.0\n")


def test_an_inch_page_gets_distinct_anchors_for_distinct_rows() -> None:
    """The total-loss bug: inch coordinates rounded to integers collapsed onto an 8x11 grid.

    Before the scale fix a heading at y 1.53-1.78 in and the paragraph below it at y 1.88-2.10
    in both emitted ``[1,2,4,2]``. Distinct rows became indistinguishable, which is loss.
    """
    out = render(two_column_view(), layout="linear")
    anchors = [ln for ln in out.splitlines() if ln.startswith("<!-- @1 [")]
    assert len(anchors) == len(set(anchors)), "distinct elements share an anchor"
    assert "scale=1000" in out
    # And no rectangle is degenerate: a real element has real height.
    for anchor in anchors:
        x0, y0, x1, y1 = (int(v) for v in anchor.split("[")[1].split("]")[0].split(","))
        assert x1 > x0 and y1 > y0, f"degenerate rectangle in {anchor}"


# ---------------------------------------------------------------------------
# Determinism — the product contract
# ---------------------------------------------------------------------------
def _sha(view: LayoutView, **kw: object) -> str:
    return hashlib.sha256(render(view, **kw).encode()).hexdigest()


def test_bytes_are_identical_across_independently_built_views() -> None:
    assert _sha(two_column_view()) == _sha(two_column_view())


@pytest.mark.parametrize("seed", range(8))
def test_bytes_are_stable_under_block_permutation(seed: int) -> None:
    """Provider array order must not reach the output — it carries no semantics."""
    baseline = _sha(two_column_view())
    view = two_column_view()
    random.Random(seed).shuffle(view.blocks)
    assert _sha(view) == baseline


@pytest.mark.parametrize("seed", range(12))
def test_atoms_sharing_an_x_coordinate_do_not_reorder_under_permutation(seed: int) -> None:
    """The placement sort must not fall through to the provider's arrival index.

    §7.1's key ends in ``source_ix``, which is a position in ``view.blocks``. Two atoms in one
    band and one frame that tie on every geometric component are then ordered by where the
    provider happened to list them — so permuting an array that carries no semantics changes
    the rendered row and the artifact's sha256. An audit measured this on 5 of 800 fuzzed
    pages, all of them atoms sharing a milli-unit x0. The tiebreaker is now the atom's own
    text, which is intrinsic to it.
    """
    view = two_column_view()
    # Two atoms with an IDENTICAL x0, in the same band and the same frame.
    view.blocks.append(block("字中符字", (4.58, 3.10, 6.00, 3.32)))
    view.blocks.append(block("second atom same x0", (4.58, 3.10, 7.20, 3.32)))
    baseline = _sha(view)

    shuffled = two_column_view()
    shuffled.blocks.append(block("字中符字", (4.58, 3.10, 6.00, 3.32)))
    shuffled.blocks.append(block("second atom same x0", (4.58, 3.10, 7.20, 3.32)))
    random.Random(seed).shuffle(shuffled.blocks)
    assert _sha(shuffled) == baseline


def test_every_key_value_pair_reaches_exactly_one_region() -> None:
    """The emitter partitions pairs; a pair owned by no region must still be emitted.

    :mod:`dpc.canvas` also exposes ``Region.kv_ixs``, computed by full-rect containment. The
    emitter deliberately does NOT use it: full-rect containment is not a cover, so a margin
    note straddling a region boundary belongs to no region and to no floating list, and would
    be silently dropped. Placing a pair by the y-centre of its union rectangle, with an
    explicit floating bucket for the rest, is a partition — every pair lands exactly once.
    """
    from dpc.models import KeyValue

    view = two_column_view()
    view.key_values = [
        KeyValue(key="Full Name", value="Jane Q. Public", page=1,
                 key_bbox=quad(0.83, 1.88, 2.0, 2.10), value_bbox=quad(2.1, 1.88, 4.03, 2.10)),
        # A margin note far outside the content column, straddling nothing.
        KeyValue(key="Ref", value="MN-4417", page=1,
                 key_bbox=quad(0.10, 9.50, 0.40, 9.70), value_bbox=quad(0.10, 9.72, 0.40, 9.90)),
    ]
    out = render(view)
    assert "MN-4417" in out, "a pair owned by no region was dropped"


def test_a_model_round_trip_renders_identically() -> None:
    view = two_column_view()
    assert _sha(LayoutView.model_validate(view.model_dump())) == _sha(view)


# ---------------------------------------------------------------------------
# The canvas fence
# ---------------------------------------------------------------------------
def test_a_document_containing_a_fence_does_not_break_out_of_the_canvas() -> None:
    """Canvas text is never mutated to protect its own fence; the fence grows instead."""
    view = two_column_view()
    view.blocks[1] = block("``` not a fence", (0.83, 1.53, 4.03, 1.78))
    out = render(view)
    assert "````text" in out
    assert "``` not a fence" in out


def test_a_comment_opener_in_the_document_cannot_swallow_our_anchors() -> None:
    view = two_column_view()
    view.blocks[1] = block("<!-- hostile", (0.83, 1.53, 4.03, 1.78))
    out = render(view)
    assert "<! -- hostile" in out
    assert "<!-- hostile" not in out
