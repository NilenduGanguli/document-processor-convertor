"""Tests for :func:`dpc.htmlread.read_html` — HTML bytes to a LayoutView the emitter accepts.

Two layers: a deliberately messy synthetic fixture (entities, a nested table, colspan/rowspan,
script/style/noscript/head junk, EDGAR-style bold-span headings), and one REAL corpus file —
a Coca-Cola 8-K, Workiva inline XBRL — pushed end-to-end through ``read_html`` + ``to_pmd``.
The corpus test is the one that keeps the reader honest: that filing has zero ``h*`` tags,
XHTML self-closing cells, 100+ colspans, and a ``display:none`` XBRL header that must not
leak into the output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dpc.emitter import to_pmd
from dpc.htmlread import read_html
from dpc.models import Zone

MESSY = b"""<!DOCTYPE html>
<html lang="en-US">
<head>
<meta charset="utf-8">
<title>Ignore me entirely</title>
<style>body { color: red; }</style>
<script>var trap = "<td>fake cell</td>";</script>
</head>
<body>
<h1>Annual &amp; Quarterly Report</h1>
<p>Filed pursuant to Rule 13a&ndash;11.<br>Second line.</p>
<h2>Item 2.02 &mdash; Results</h2>
<div>Revenue grew&nbsp;12%.</div>
<ul>
  <li>First bullet</li>
  <li>Second &lt;bullet&gt;</li>
</ul>
<div style="text-align:center"><span style="font-weight:700">EXHIBIT INDEX</span></div>
<p>This paragraph mentions <b>bold words</b> in passing and must stay body text.</p>
<table>
 <tr><th colspan="2">Metric</th><th>FY2025</th></tr>
 <tr><td rowspan="2">Revenue</td><td>Product</td><td>$1,000</td></tr>
 <tr><td>Service</td><td>$500</td></tr>
 <tr><td>Note</td><td colspan="2"><table><tr><td>inner A</td><td>inner B</td></tr></table></td></tr>
</table>
<noscript>You need JS enabled.</noscript>
<h1>Signatures</h1>
<p>After the table.</p>
</body>
</html>"""


@pytest.fixture(scope="module")
def view():
    return read_html(MESSY)


@pytest.fixture(scope="module")
def pmd(view):
    return to_pmd(view, source="document", provider="htmlread", doc_id="MESSY-1",
                  generated="2026-01-01T00:00:00Z")


# ------------------------------------------------------------------------------- zoning


def test_first_h1_is_title_later_h1_is_heading(view):
    titles = [b for b in view.blocks if b.zone is Zone.title]
    assert [b.text for b in titles] == ["Annual & Quarterly Report"]
    assert titles[0].role == "h1"
    sigs = next(b for b in view.blocks if b.text == "Signatures")
    assert sigs.zone is Zone.heading and sigs.role == "h1"


def test_h2_is_heading_with_source_role(view):
    h2 = next(b for b in view.blocks if b.role == "h2")
    assert h2.zone is Zone.heading
    assert h2.text == "Item 2.02 — Results"  # &mdash; decoded


def test_p_and_div_are_body_with_source_role(view):
    p = next(b for b in view.blocks if b.text.startswith("Filed pursuant"))
    assert p.zone is Zone.body and p.role == "p"
    d = next(b for b in view.blocks if b.text.startswith("Revenue grew"))
    assert d.zone is Zone.body and d.role == "div"


def test_br_becomes_newline_and_entities_decode(view):
    p = next(b for b in view.blocks if b.text.startswith("Filed pursuant"))
    assert p.text == "Filed pursuant to Rule 13a–11.\nSecond line."


def test_nbsp_collapses_to_plain_space(view):
    d = next(b for b in view.blocks if b.role == "div" and "Revenue" in b.text)
    assert d.text == "Revenue grew 12%."


def test_li_gets_dash_prefix(view):
    lis = [b for b in view.blocks if b.role == "li"]
    assert [b.text for b in lis] == ["- First bullet", "- Second <bullet>"]
    assert all(b.zone is Zone.body for b in lis)


def test_bold_span_run_is_heading_but_partial_bold_is_body(view):
    exhibit = next(b for b in view.blocks if b.text == "EXHIBIT INDEX")
    assert exhibit.zone is Zone.heading and exhibit.role == "div"
    partial = next(b for b in view.blocks if "in passing" in b.text)
    assert partial.zone is Zone.body
    assert partial.text == "This paragraph mentions bold words in passing and must stay body text."


# ------------------------------------------------------------------------------ stripping


def test_script_style_noscript_head_stripped(view):
    everything = view.text()
    assert "fake cell" not in everything
    assert "color: red" not in everything
    assert "You need JS" not in everything
    assert "Ignore me" not in everything


# ------------------------------------------------------------------------------- tables


def test_single_table_nested_one_flattened(view):
    assert len(view.tables) == 1  # the inner table is NOT a second Table object
    t = view.tables[0]
    assert t.row_count == 4 and t.col_count == 3
    assert t.bbox is None


def test_th_is_header_and_colspan_placement(view):
    t = view.tables[0]
    metric = next(c for c in t.cells if c.text == "Metric")
    assert metric.is_header and (metric.row, metric.col) == (0, 0) and metric.col_span == 2
    fy = next(c for c in t.cells if c.text == "FY2025")
    assert fy.is_header and (fy.row, fy.col) == (0, 2)
    assert not next(c for c in t.cells if c.text == "Revenue").is_header


def test_rowspan_blocks_next_free_column(view):
    t = view.tables[0]
    rev = next(c for c in t.cells if c.text == "Revenue")
    assert (rev.row, rev.col, rev.row_span) == (1, 0, 2)
    # Row 2's first declared cell must skip col 0, still occupied by the rowspan above.
    service = next(c for c in t.cells if c.text == "Service")
    assert (service.row, service.col) == (2, 1)
    assert (next(c for c in t.cells if c.text == "$500").row,
            next(c for c in t.cells if c.text == "$500").col) == (2, 2)


def test_nested_table_text_lands_in_outer_cell(view):
    t = view.tables[0]
    note_row = [c for c in t.cells if c.row == 3]
    inner = next(c for c in note_row if "inner A" in c.text)
    assert inner.col == 1 and inner.col_span == 2
    assert inner.text == "inner A inner B"


def test_table_text_never_duplicated_as_blocks(view):
    assert all(b.zone is not Zone.table for b in view.blocks)
    body = view.text()
    for cell_text in ("Metric", "$1,000", "$500", "inner A", "FY2025"):
        assert cell_text not in body


# ------------------------------------------------------------------- page / lang / decode


def test_single_page_without_size(view):
    assert len(view.pages) == 1
    page = view.pages[0]
    assert page.page == 1 and page.width == 0 and page.height == 0


def test_languages_from_html_lang(view):
    assert view.languages == ["en-US"]


def test_meta_charset_honoured():
    doc = (b'<html><head><meta http-equiv="Content-Type" '
           b'content="text/html; charset=iso-8859-1"></head>'
           b'<body><p>caf\xe9 cr\xe8me</p></body></html>')
    v = read_html(doc)
    assert v.blocks[0].text == "caf\xe9 cr\xe8me"


def test_bad_bytes_fall_back_to_utf8_replace():
    v = read_html(b"<p>bad \xff byte</p>")
    assert v.blocks and "�" in v.blocks[0].text


def test_unknown_charset_falls_back():
    v = read_html(b'<meta charset="no-such-codec"><p>still works</p>')
    assert v.blocks[0].text == "still works"


# ------------------------------------------------------------------------- PMD rendering


def test_pmd_page_marker_has_no_size(pmd):
    assert "<!-- page 1 -->" in pmd
    assert "size=" not in pmd  # width 0 must mean NO size, never "size=0x0"


def test_pmd_headings_tables_and_no_script(pmd):
    lines = pmd.splitlines()
    assert "# Annual & Quarterly Report" in lines
    assert "## Item 2.02 — Results" in lines
    assert any(line.startswith("| ") and line.endswith(" |") for line in lines)
    assert "<script" not in pmd.lower()
    # bbox=None everywhere: not a single anchor may be invented.
    assert "<!-- @" not in pmd


# ----------------------------------------------------------------------- the real corpus

CORPUS = Path.home() / "document-classification-extraction/corpus/us/us_sec_8k.htm"


@pytest.mark.skipif(not CORPUS.exists(), reason="corpus checkout not present")
def test_us_sec_8k_end_to_end():
    view = read_html(CORPUS.read_bytes())
    pmd = to_pmd(view, source="document", provider="htmlread", doc_id="us_sec_8k",
                 generated="2026-01-01T00:00:00Z")
    lines = pmd.splitlines()

    # Front matter renders and is well-formed.
    assert pmd.startswith("---\n")
    assert "pmd: 1.0" in lines and "provider: htmlread" in lines
    assert lines.index("---", 1) > 1  # front matter closes

    # At least one heading — this filing has ZERO h* tags; headings come from bold runs.
    heading_lines = [ln for ln in lines if ln.startswith(("# ", "## "))]
    assert heading_lines
    assert "## UNITED STATES" in lines
    assert "## SECURITIES AND EXCHANGE COMMISSION" in lines

    # At least one GFM table row, with its separator, carrying real cell text.
    assert any(ln.startswith("| ") and ln.endswith(" |") for ln in lines)
    assert any(ln.startswith("|") and set(ln) <= {"|", "-", " "} and "---" in ln
               for ln in lines)
    nyse = [ln for ln in lines if "New York Stock Exchange" in ln]
    assert nyse and any(ln.startswith("|") for ln in nyse)

    # No script anywhere, no invented geometry, sane page marker.
    assert "<script" not in pmd.lower()
    assert "<!-- @" not in pmd
    assert "<!-- page 1 -->" in pmd and "size=" not in pmd

    # The display:none inline-XBRL header (CIK lives only there) must not leak.
    assert "0000021344" not in pmd

    # Language came from XHTML's xml:lang.
    assert view.languages == ["en-US"]
