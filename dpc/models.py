"""The provider-neutral view of a read document — the input every converter shares.

Ported from the DCE service's ``dce/models.py`` (same author, same layout semantics), so a
payload that one service can read the other can too. Only the layout half is here: this
service converts documents, it does not classify them, and carrying classification types it
never uses would imply a coupling that does not exist.

The one property that matters for THIS service and not for DCE: **geometry**. ``TextBlock.
bbox`` is what lets the emitted markdown preserve where on the page a block sat, which is the
entire point of the format. The Azure adapters populate it from ``polygon``; the PDF reader
populates it from PyMuPDF block rectangles. A block without a bbox is still emitted — order
alone carries meaning — but it is emitted without an anchor, honestly, rather than with an
invented one.
"""
from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field

Quad = list[float]  # 8 floats: 4 (x, y) points, clockwise from top-left


class Zone(enum.StrEnum):
    title = "title"
    heading = "heading"
    table = "table"
    body = "body"
    furniture = "furniture"


class TextLine(BaseModel):
    """One provider LINE inside a block — the PLACEMENT unit for spatial rendering.

    Deliberately distinct from :class:`TextBlock`. Azure's definition of a line is the
    column-preserving unit: content in the same horizontal plane but separated by more than a
    single visual space is split into separate lines. A line's rectangle is therefore a real
    2-D box that can be placed on a grid.

    A PARAGRAPH's rectangle is the union hull of its lines. For wrapped text that hull spans
    many visual rows, so it states the column correctly and the row not at all. Paragraphs are
    the right unit for roles and the wrong unit for position — which is why the spatial
    emitter places lines and takes roles from the parent block.
    """

    text: str
    bbox: Quad | None = None


class TextBlock(BaseModel):
    """One paragraph/line of text with its zone and geometry."""

    text: str
    zone: Zone = Zone.body
    page: int = 1
    bbox: Quad | None = None
    role: str | None = None      # verbatim provider role, when it had one
    #: Provider lines, in provider order, joined to this block by span overlap. EMPTY is the
    #: honest state for any reader with no line stream (Office/HTML, plain text, DES) — never
    #: synthesised by splitting ``text`` on newlines, because a synthetic line has no
    #: rectangle and an invented rectangle is worse than none.
    lines: list[TextLine] = Field(default_factory=list)
    #: Provider document-order index across ALL element kinds on the document. Orthogonal to
    #: geometry on purpose: a reader with no coordinates (HTML, XLSX) can still state the
    #: order things appeared in, without inventing rectangles — ordering is a claim about
    #: sequence, an anchor is a claim about position, and only the second needs a bbox.
    seq: int | None = None


class Cell(BaseModel):
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    text: str = ""
    is_header: bool = False
    bbox: Quad | None = None


class Table(BaseModel):
    table_id: str
    page: int = 1
    row_count: int = 0
    col_count: int = 0
    cells: list[Cell] = Field(default_factory=list)
    bbox: Quad | None = None
    #: See TextBlock.seq.
    seq: int | None = None

    def grid(self) -> list[list[str]]:
        """Cells as a dense row-major grid; a span's text lands top-left, blanks elsewhere."""
        out = [["" for _ in range(self.col_count)] for _ in range(self.row_count)]
        for cell in self.cells:
            if 0 <= cell.row < self.row_count and 0 <= cell.col < self.col_count:
                out[cell.row][cell.col] = cell.text
        return out


class Mark(BaseModel):
    """A selection mark — a checkbox, selected or not."""

    state: str                   # "selected" | "unselected"
    page: int = 1
    bbox: Quad | None = None

    @property
    def selected(self) -> bool:
        return self.state == "selected"


class KeyValue(BaseModel):
    key: str
    value: str
    page: int = 1
    key_bbox: Quad | None = None
    value_bbox: Quad | None = None
    confidence: float | None = None


class PageInfo(BaseModel):
    page: int
    width: float = 0.0
    height: float = 0.0
    unit: str = "pixel"
    angle: float = 0.0


class LayoutView(BaseModel):
    """Everything the emitter is allowed to see. No notion of where the bytes came from."""

    doc_id: str = ""
    pages: list[PageInfo] = Field(default_factory=list)
    blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    marks: list[Mark] = Field(default_factory=list)
    key_values: list[KeyValue] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages) or (max((b.page for b in self.blocks), default=0))

    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


__all__ = [
    "Cell",
    "KeyValue",
    "LayoutView",
    "Mark",
    "PageInfo",
    "Quad",
    "Table",
    "TextBlock",
    "TextLine",
    "Zone",
]
