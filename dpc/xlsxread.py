"""XLSX bytes -> :class:`~dpc.models.LayoutView`, one "page" per sheet.

A spreadsheet has no page geometry, and this module refuses to invent any: every
:class:`PageInfo` is ``PageInfo(page=n)`` with the zero-size defaults, and every block and
table carries ``bbox=None`` — the emitter then writes no anchor at all, which is the honest
rendering of a format that has grids but no rectangles.

What each sheet becomes:

* one page (``page=n``, sheets in workbook order),
* the sheet name as a ``Zone.heading`` block with ``role="sheet"``,
* one :class:`Table` covering the sheet's used range — **the first row is emitted with**
  ``is_header=True`` **by convention**: XLSX marks no header row, and the first row of a
  used range is the header far more often than not. Consumers who disagree can ignore the
  flag; the text is unchanged either way.

Reading is ``openpyxl`` in ``read_only=True, data_only=True`` mode:

* ``read_only`` streams rows instead of materialising the sheet, which is what makes the
  row/column caps below meaningful for large files;
* ``data_only`` returns a formula cell's **cached** value — the number Excel stored at last
  save. A workbook that was never calculated and saved by a spreadsheet application (e.g.
  one written by openpyxl itself) carries no cache, so its formula cells read as ``None``
  and are emitted as ``""``. That blank is the truth about the file, not a defect here.

``read_only`` mode also drops merged-cell ranges (``ReadOnlyWorksheet`` has no
``merged_cells``), so merges are recovered by a sidecar parse of the package XML itself —
``xl/workbook.xml`` + its rels for the sheet parts, then each part's ``<mergeCell ref=…>``
elements. That is the OOXML package format, stable by standard, not an openpyxl internal.
A merged range puts its value in the top-left cell with ``row_span``/``col_span`` from the
range; the covered positions are not emitted as cells.

Bounds: fully-empty **trailing** rows and columns are trimmed (a row or column that only a
value-bearing merge reaches over counts as occupied, so a merge block at the sheet's edge
keeps its full span). Each sheet is capped at ``MAX_ROWS`` x ``MAX_COLS``; when content
actually exists beyond a cap, ``view.raw["truncated"]`` is set — silently dropping data
without a trace would make the output lie by omission.

Cell values: ints and int-valued floats render without float noise (``1``, never ``1.0``),
dates and times as ISO 8601 (a midnight datetime renders as its date), booleans as
``TRUE``/``FALSE`` as Excel displays them, ``None`` as ``""``.

Log lines carry counts only — never sheet names or cell text (KYC service; log lines
travel).
"""
from __future__ import annotations

import datetime
import io
import logging
import posixpath
import zipfile
from typing import Any
from xml.etree import ElementTree

import openpyxl
from openpyxl.utils import range_boundaries

from dpc.models import Cell, LayoutView, PageInfo, Table, TextBlock, Zone

logger = logging.getLogger(__name__)

#: Provider name as the API reports it (`to_pmd(provider=…)`).
PROVIDER_OPENPYXL = "openpyxl"

#: Per-sheet caps. Content beyond either bound sets ``view.raw["truncated"]``.
MAX_ROWS = 500
MAX_COLS = 64

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL_ATTR = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: (min_col, min_row, max_col, max_row), 1-based, as ``range_boundaries`` returns them.
_Bounds = tuple[int, int, int, int]


def _fmt(value: Any) -> str:
    """One cell value as text, without representation noise.

    ``None`` -> ``""``; booleans as Excel shows them; int-valued floats without the ``.0``;
    datetimes/dates/times as ISO 8601, a midnight datetime collapsing to its date (XLSX
    stores dates as datetimes, so midnight almost always means "a date").
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time.min and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


def _merged_ranges(data: bytes) -> dict[str, list[_Bounds]]:
    """Sheet name -> merged ranges, read from the package XML directly.

    ``read_only`` worksheets do not carry ``merged_cells``, and loading the workbook a
    second time in normal mode would materialise exactly what read-only mode exists to
    avoid. The package format is simpler than either: ``xl/workbook.xml`` names the sheets
    and their relationship ids, the rels part maps ids to sheet parts, and each sheet part
    lists its merges as ``<mergeCell ref="A3:B4"/>`` elements. ``iterparse`` streams each
    part so the (potentially huge) ``sheetData`` is never held whole.
    """
    out: dict[str, list[_Bounds]] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            return out
        targets: dict[str, str] = {}
        for rel in ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels")):
            rel_id, target = rel.get("Id"), rel.get("Target")
            if rel_id and target:
                targets[rel_id] = target
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        for sheet in book.iter(f"{{{_NS_MAIN}}}sheet"):
            title = sheet.get("name")
            target = targets.get(sheet.get(f"{{{_NS_REL_ATTR}}}id") or "", "")
            if not title or not target:
                continue
            part = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            if part not in names:
                continue
            ranges: list[_Bounds] = []
            with archive.open(part) as handle:
                for _event, element in ElementTree.iterparse(handle):
                    if element.tag == f"{{{_NS_MAIN}}}mergeCell":
                        ref = element.get("ref")
                        if ref:
                            try:
                                ranges.append(range_boundaries(ref))
                            except ValueError:
                                pass  # malformed ref: the merge is lost, the data is not
                    element.clear()
            if ranges:
                out[title] = ranges
    return out


def _scan_rows(worksheet: Any) -> tuple[list[list[str]], bool]:
    """Stream one sheet into a capped grid of formatted strings.

    Returns ``(rows, truncated)`` where ``truncated`` is True only when actual content —
    not mere sheet dimensions — exists beyond ``MAX_ROWS`` or ``MAX_COLS``. Past the row
    cap the scan continues just far enough to find one non-empty cell, then stops.
    """
    rows: list[list[str]] = []
    truncated = False
    for index, row in enumerate(worksheet.iter_rows(values_only=True)):
        if index >= MAX_ROWS:
            if any(_fmt(value) for value in row):
                truncated = True
                break
            continue
        values = [_fmt(value) for value in row]
        if len(values) > MAX_COLS:
            if any(values[MAX_COLS:]):
                truncated = True
            values = values[:MAX_COLS]
        rows.append(values)
    return rows, truncated


def _sheet_table(
    rows: list[list[str]], merges: list[_Bounds], *, page: int
) -> Table | None:
    """The capped row scan of one sheet as a :class:`Table`, or ``None`` when it is empty.

    Trailing fully-empty rows and columns are trimmed; a position a value-bearing merge
    reaches over counts as occupied, so a merged block at the used range's edge keeps its
    span instead of having it trimmed off. Merge ranges are clamped to the final grid.
    Covered (non-top-left) merge positions are not emitted as cells.
    """
    last_row = max((r for r, row in enumerate(rows) if any(row)), default=-1)
    last_col = max(
        (c for row in rows for c, text in enumerate(row) if text), default=-1
    )
    for min_col, min_row, max_col, max_row in merges:
        top_r, top_c = min_row - 1, min_col - 1
        if top_r >= len(rows) or top_c >= len(rows[top_r]) or not rows[top_r][top_c]:
            continue
        last_row = max(last_row, min(max_row - 1, len(rows) - 1))
        last_col = max(last_col, min(max_col - 1, MAX_COLS - 1))
    if last_row < 0 or last_col < 0:
        return None
    row_count, col_count = last_row + 1, last_col + 1
    grid = [(row + [""] * col_count)[:col_count] for row in rows[:row_count]]

    spans: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for min_col, min_row, max_col, max_row in merges:
        top = (min_row - 1, min_col - 1)
        if top[0] >= row_count or top[1] >= col_count:
            continue
        end_r = min(max_row - 1, row_count - 1)
        end_c = min(max_col - 1, col_count - 1)
        spans[top] = (end_r - top[0] + 1, end_c - top[1] + 1)
        covered.update(
            (r, c)
            for r in range(top[0], end_r + 1)
            for c in range(top[1], end_c + 1)
            if (r, c) != top
        )

    cells = [
        Cell(
            row=r,
            col=c,
            row_span=spans.get((r, c), (1, 1))[0],
            col_span=spans.get((r, c), (1, 1))[1],
            text=grid[r][c],
            is_header=(r == 0),
        )
        for r in range(row_count)
        for c in range(col_count)
        if (r, c) not in covered
    ]
    return Table(
        table_id=f"sheet{page}",
        page=page,
        row_count=row_count,
        col_count=col_count,
        cells=cells,
    )


def read_xlsx(data: bytes) -> LayoutView:
    """Read XLSX bytes into a provider-neutral view. See the module docstring for the shape.

    Args:
        data: The workbook, decoded (the API decodes base64 before calling).

    Returns:
        A :class:`LayoutView` with one page + heading block per sheet and one table per
        non-empty sheet. ``raw`` carries ``provider``, ``sheets``, and — only when content
        was actually dropped by the caps — ``truncated: True``.

    Raises:
        ValueError: The bytes are not a workbook openpyxl can open. The exception names
            the failure type, never file content.
    """
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # openpyxl raises zipfile/InvalidFileException/…
        raise ValueError(f"cannot open XLSX: {type(exc).__name__}") from exc

    merges = _merged_ranges(data)
    pages: list[PageInfo] = []
    blocks: list[TextBlock] = []
    tables: list[Table] = []
    truncated = False
    try:
        for number, worksheet in enumerate(workbook.worksheets, start=1):
            pages.append(PageInfo(page=number))
            blocks.append(
                TextBlock(
                    text=worksheet.title,
                    zone=Zone.heading,
                    page=number,
                    bbox=None,
                    role="sheet",
                    # The sheet's name reads before its table; with no geometry on either,
                    # seq is the only thing that says so.
                    seq=0,
                )
            )
            rows, sheet_truncated = _scan_rows(worksheet)
            truncated = truncated or sheet_truncated
            table = _sheet_table(rows, merges.get(worksheet.title, []), page=number)
            if table is not None:
                table.seq = 1
                tables.append(table)
    finally:
        workbook.close()

    raw: dict[str, Any] = {"provider": PROVIDER_OPENPYXL, "sheets": len(pages)}
    if truncated:
        raw["truncated"] = True
    logger.info(
        "read.xlsx sheets=%d tables=%d cells=%d truncated=%s",
        len(pages),
        len(tables),
        sum(len(t.cells) for t in tables),
        truncated,
    )
    return LayoutView(pages=pages, blocks=blocks, tables=tables, raw=raw)


__all__ = ["MAX_COLS", "MAX_ROWS", "PROVIDER_OPENPYXL", "read_xlsx"]
