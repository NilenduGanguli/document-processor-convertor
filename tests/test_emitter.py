"""Golden tests for :func:`dpc.emitter.to_pmd` — every SPEC-PMD rule pinned to bytes.

One synthetic view exercises every element kind the emitter knows: a title, a heading (with a
leading ``#`` in its own text), furniture with a provider role, plain body, a body block with
a verbatim provider role, a 2x2 table with a pipe, a newline and a column span in its cells,
a selected and an unselected mark, a key/value pair, a ``zone=table`` block that must NOT be
emitted, and a block without a bbox that must be emitted WITHOUT an anchor.

The golden string is the normative rendering of that view (SPEC-PMD.md section by section);
the individual tests re-assert each rule so a regression names the rule it broke instead of
just diffing the golden blob.
"""
from __future__ import annotations

from dpc.emitter import to_pmd
from dpc.models import Cell, KeyValue, LayoutView, Mark, PageInfo, Table, TextBlock, Zone


def build_view() -> LayoutView:
    """A fresh view per call, so determinism is tested across instances, not on one object."""
    return LayoutView(
        doc_id="GOLD-1",
        pages=[
            PageInfo(page=1, width=612.0, height=792.0, unit="point"),
            PageInfo(page=2, width=612.0, height=792.0, unit="point"),
        ],
        blocks=[
            TextBlock(
                text="ACME ONBOARDING FORM", zone=Zone.title, page=1,
                bbox=[72, 40, 540, 40, 540, 80, 72, 80],
            ),
            # Provider order beats y-order: this furniture block sits ABOVE the title on the
            # page (y=20 < y=40) but after it in provider order, and must stay there.
            TextBlock(
                text="Page 1 of 2", zone=Zone.furniture, role="pageNumber", page=1,
                bbox=[500, 20, 560, 20, 560, 32, 500, 32],
            ),
            # Leading '#' in the document's own text — must be escaped, not deepen the level.
            TextBlock(
                text="#1 Priority Client", zone=Zone.heading, page=1,
                bbox=[72, 100, 540, 100, 540, 130, 72, 130],
            ),
            # A literal comment opener would swallow everything to the next '-->'.
            TextBlock(
                text="Comment opener <!-- must not swallow anchors.", zone=Zone.body, page=1,
                bbox=[72, 150, 540, 150, 540, 180, 72, 180],
            ),
            # Starts lower than the table (y=400 > y=300): the table splices in before this.
            TextBlock(
                text="Signature section follows.", zone=Zone.body, page=1,
                bbox=[72, 400, 540, 400, 540, 430, 72, 430],
            ),
            # Re-zoned into a detected table by the adapters — must NOT be emitted again.
            TextBlock(
                text="MUST NOT APPEAR", zone=Zone.table, page=1,
                bbox=[80, 310, 200, 310, 200, 330, 80, 330],
            ),
            # No geometry: emitted, but with NO anchor — never an invented rectangle.
            TextBlock(text="Trailing note without geometry.", zone=Zone.body, page=1, bbox=None),
            TextBlock(
                text="Second page body.", zone=Zone.body, page=2,
                bbox=[72, 60, 540, 60, 540, 90, 72, 90],
            ),
            # Body with a verbatim provider role: the role IS the anchor tag.
            TextBlock(
                text="Registered office footnote.", zone=Zone.body, role="footnote", page=2,
                bbox=[72, 700, 540, 700, 540, 720, 72, 720],
            ),
        ],
        tables=[
            Table(
                table_id="t1", page=1, row_count=2, col_count=2,
                bbox=[72, 300, 540, 300, 540, 360, 72, 360],
                cells=[
                    Cell(row=0, col=0, text="Field", is_header=True),
                    # Pipe in a cell: must be escaped or it grows a column.
                    Cell(row=0, col=1, text="Value|Notes", is_header=True),
                    # Column span with a newline: text lands top-left, newline -> <br>,
                    # the spanned-over cell stays blank.
                    Cell(row=1, col=0, col_span=2, text="Line one\nLine two"),
                ],
            ),
        ],
        marks=[
            Mark(state="selected", page=1, bbox=[72, 440, 90, 440, 90, 458, 72, 458]),
            Mark(state="unselected", page=1, bbox=[72, 470, 90, 470, 90, 488, 72, 488]),
        ],
        key_values=[
            KeyValue(
                key="Full Name", value="Jane Q. Public", page=1,
                key_bbox=[72, 500, 150, 500, 150, 520, 72, 520],
                value_bbox=[160, 500, 340, 500, 340, 520, 160, 520],
            ),
        ],
    )


def render(view: LayoutView) -> str:
    return to_pmd(
        view,
        source="azure_layout",
        provider="azure-prebuilt-layout",
        doc_id="GOLD-1",
        generated="2026-08-25T00:00:00Z",
        extra={"sha256_input": "deadbeef"},
    )


GOLDEN = """\
---
pmd: 1.0
generator: document-processor-convertor
source: azure_layout
provider: azure-prebuilt-layout
doc_id: GOLD-1
pages: 2
blocks: 9
tables: 1
marks: 2
key_values: 1
chars: 210
generated: 2026-08-25T00:00:00Z
sha256_input: deadbeef
---

<!-- page 1 size=612x792 unit=point -->

<!-- @1 [72,40,540,80] title -->
# ACME ONBOARDING FORM

<!-- @1 [500,20,560,32] furniture:pageNumber -->
Page 1 of 2

<!-- @1 [72,100,540,130] heading -->
## \\#1 Priority Client

<!-- @1 [72,150,540,180] p -->
Comment opener <! -- must not swallow anchors.

<!-- @1 [72,300,540,360] table 2x2 -->
| Field | Value\\|Notes |
| --- | --- |
| Line one<br>Line two |  |

<!-- @1 [72,400,540,430] p -->
Signature section follows.

<!-- @1 [72,440,90,458] mark -->
- [x]

<!-- @1 [72,470,90,488] mark -->
- [ ]

<!-- @1 [72,500,340,520] kv -->
**Full Name:** Jane Q. Public

Trailing note without geometry.

<!-- page 2 size=612x792 unit=point -->

<!-- @2 [72,60,540,90] p -->
Second page body.

<!-- @2 [72,700,540,720] footnote -->
Registered office footnote.
"""


# ---------------------------------------------------------------------------
# The golden file itself
# ---------------------------------------------------------------------------
def test_golden_bytes() -> None:
    assert render(build_view()) == GOLDEN


def test_byte_determinism() -> None:
    """Same view in, same bytes out — across calls AND across independent instances."""
    first = render(build_view())
    second = render(build_view())
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
    # A view round-tripped through its own serialised form renders the same bytes too.
    round_tripped = LayoutView.model_validate(build_view().model_dump())
    assert render(round_tripped) == first


def test_single_trailing_newline() -> None:
    out = render(build_view())
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------
def test_front_matter_counts_and_extras() -> None:
    out = render(build_view())
    head = out.split("---")[1]
    assert "pmd: 1.0" in head
    assert "pages: 2" in head
    assert "blocks: 9" in head          # counts every block, including the zone=table one
    assert "tables: 1" in head and "marks: 2" in head and "key_values: 1" in head
    assert "sha256_input: deadbeef" in head   # caller extras, emitted sorted
    assert "generated: 2026-08-25T00:00:00Z" in head


# ---------------------------------------------------------------------------
# Element renderings
# ---------------------------------------------------------------------------
def test_title_and_heading_levels() -> None:
    out = render(build_view())
    assert "\n# ACME ONBOARDING FORM\n" in out
    assert "<!-- @1 [72,40,540,80] title -->" in out


def test_heading_escapes_leading_hash() -> None:
    """'#1 Priority Client' must not silently become a deeper heading."""
    out = render(build_view())
    assert "## \\#1 Priority Client" in out
    assert "## #1 Priority Client" not in out


def test_furniture_role_tag() -> None:
    out = render(build_view())
    assert "<!-- @1 [500,20,560,32] furniture:pageNumber -->\nPage 1 of 2" in out


def test_verbatim_provider_role_tag() -> None:
    out = render(build_view())
    assert "<!-- @2 [72,700,540,720] footnote -->\nRegistered office footnote." in out


def test_marks_render_as_task_items() -> None:
    out = render(build_view())
    assert "<!-- @1 [72,440,90,458] mark -->\n- [x]" in out
    assert "<!-- @1 [72,470,90,488] mark -->\n- [ ]" in out


def test_key_value_unions_key_and_value_rects() -> None:
    """kv anchor spans key rect [72..150] plus value rect [160..340] as one visual unit."""
    out = render(build_view())
    assert "<!-- @1 [72,500,340,520] kv -->\n**Full Name:** Jane Q. Public" in out


def test_page_markers_carry_size_and_unit() -> None:
    out = render(build_view())
    assert "<!-- page 1 size=612x792 unit=point -->" in out
    assert "<!-- page 2 size=612x792 unit=point -->" in out


# ---------------------------------------------------------------------------
# Escaping rules
# ---------------------------------------------------------------------------
def test_comment_opener_sanitised() -> None:
    """A literal '<!--' in body text would open a comment and swallow our own anchors."""
    out = render(build_view())
    assert "Comment opener <! -- must not swallow anchors." in out
    assert "Comment opener <!--" not in out


def test_table_cell_pipe_escaped_and_newline_flattened() -> None:
    out = render(build_view())
    assert "| Field | Value\\|Notes |" in out
    assert "| Line one<br>Line two |  |" in out    # span text top-left, spanned cell blank
    assert "Value|Notes" not in out.replace("Value\\|Notes", "")


def test_table_tag_is_rows_x_cols() -> None:
    out = render(build_view())
    assert "<!-- @1 [72,300,540,360] table 2x2 -->" in out


# ---------------------------------------------------------------------------
# Structural rules
# ---------------------------------------------------------------------------
def test_zone_table_block_skipped() -> None:
    """The adapters re-zone paragraphs inside detected tables; emitting both = text twice."""
    out = render(build_view())
    assert "MUST NOT APPEAR" not in out


def test_y_splice_ordering() -> None:
    """Blocks keep provider order; the table (y=300) splices in before the block at y=400."""
    out = render(build_view())
    i_body_above = out.index("Comment opener")            # block at y=150
    i_table = out.index("| Field |")                      # table at y=300
    i_body_below = out.index("Signature section follows") # block at y=400
    assert i_body_above < i_table < i_body_below
    # Provider order wins over geometry for blocks: furniture at y=20 stays after the
    # title (y=40) because that is the order the provider gave.
    assert out.index("ACME ONBOARDING FORM") < out.index("Page 1 of 2")
    # Every spliced element with geometry (marks y=440/470, kv y=500) lands before the
    # no-bbox block, whose splice position is "after everything positioned".
    assert out.index("**Full Name:**") < out.index("Trailing note without geometry.")


def test_no_bbox_no_anchor() -> None:
    """A block without geometry is emitted, but never with an invented rectangle."""
    out = render(build_view())
    assert out.count("Trailing note without geometry.") == 1
    lines = out.splitlines()
    i = lines.index("Trailing note without geometry.")
    assert not lines[i - 1].startswith("<!--"), "no-bbox block must not receive an anchor"
    assert lines[i - 1] == ""


def test_seq_orders_a_page_that_has_no_geometry():
    """Two independent reviews of the first HTML/XLSX output found the same defect: with
    every element at y=inf, `inf <= inf` spliced all tables ahead of all text, so a filing's
    financial statements rendered before its prose and a sheet's table before its own name.
    `seq` is the fix — provider order made expressible without inventing rectangles — and an
    element carrying seq must never gain an anchor from it."""
    from dpc.models import Cell, LayoutView, PageInfo, Table, TextBlock, Zone

    view = LayoutView(
        doc_id="seq",
        pages=[PageInfo(page=1)],
        blocks=[
            TextBlock(text="Sheet One", zone=Zone.heading, page=1, seq=0),
            TextBlock(text="after the table", zone=Zone.body, page=1, seq=2),
        ],
        tables=[
            Table(
                table_id="t1", page=1, row_count=1, col_count=1,
                cells=[Cell(row=0, col=0, text="cell")], seq=1,
            )
        ],
    )
    md = to_pmd(view, source="document", provider="test")
    heading = md.index("## Sheet One")
    table = md.index("| cell |")
    after = md.index("after the table")
    assert heading < table < after
    assert "<!-- @" not in md, "seq is ordering, never an anchor"


def test_without_seq_geometry_free_extras_append_at_page_end():
    """The docstring's promise, now actually true: `inf <= inf` used to splice them first."""
    from dpc.models import Cell, LayoutView, PageInfo, Table, TextBlock, Zone

    view = LayoutView(
        doc_id="noseq",
        pages=[PageInfo(page=1)],
        blocks=[TextBlock(text="prose first", zone=Zone.body, page=1)],
        tables=[
            Table(
                table_id="t1", page=1, row_count=1, col_count=1,
                cells=[Cell(row=0, col=0, text="cell")],
            )
        ],
    )
    md = to_pmd(view, source="document", provider="test")
    assert md.index("prose first") < md.index("| cell |")
