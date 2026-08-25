"""HTML -> :class:`~dpc.models.LayoutView`, stdlib only (``html.parser``).

HTML has no geometry, so every block and table carries ``bbox=None`` — the emitter then
emits no anchor, which is the honest rendering (SPEC rule: never invent geometry). The view
declares a single ``PageInfo(page=1)`` with width/height left at 0; the emitter's page marker
conditionally omits the size for a zero width, so the marker renders ``<!-- page 1 -->``.

Zoning:

* the first ``<h1>`` -> ``Zone.title``; every later ``<h1>`` and all ``<h2>``..``<h6>`` ->
  ``Zone.heading``; ``role`` carries the source tag verbatim (``"h2"``, ``"p"``, ``"div"``).
* ``<p>`` / ``<div>`` / ``<li>`` text runs -> ``Zone.body`` blocks; ``<li>`` gets ``"- "``.
* **Bold-run headings**: real filings (EDGAR/Workiva inline XBRL) contain zero ``h*`` tags —
  their headings are ``<div><span style="font-weight:700">..`` runs. A short block whose text
  is entirely bold (``<b>``/``<strong>`` or a ``font-weight`` of bold/600+) is therefore
  zoned ``heading``. Never ``title``: the title slot belongs to ``<h1>`` alone.

Tables become :class:`~dpc.models.Table` with ``colspan``/``rowspan`` honoured by the
next-free-column placement algorithm; ``<th>`` -> ``is_header``. A nested table is flattened
into the cell of the outer table that contains it — the view never holds a Table inside a
Table. Cell text is captured only into cells, never additionally as body blocks. Tables whose
every cell is empty (EDGAR's border-only spacer tables) are dropped.

Stripped entirely: ``<script>``, ``<style>``, ``<noscript>``, ``<head>`` (with an implicit
exit from head when body content starts, for documents that never close it), and any subtree
styled ``display:none`` (inline XBRL hides its machine-readable header that way — that text
is metadata, not document text). ``<br>`` becomes a newline inside the current block or cell.

Decoding honours a ``<meta charset>`` (or a BOM), falling back to UTF-8 with ``replace``.
``languages`` comes from ``<html lang>`` (or XHTML's ``xml:lang``).
"""
from __future__ import annotations

import codecs
import re
from html.parser import HTMLParser

from dpc.models import Cell, LayoutView, PageInfo, Table, TextBlock, Zone

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_ROLE_TAGS = frozenset({"p", "div", "li"}) | _HEADING_TAGS
_STRIP_TAGS = frozenset({"script", "style", "noscript"})
_HEAD_ONLY_TAGS = frozenset({"title", "meta", "link", "base", "basefont", "template"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
# Tags that end the current text run (and separate content inside a table cell).
_BLOCK_TAGS = _HEADING_TAGS | frozenset({
    "address", "article", "aside", "blockquote", "caption", "center", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "header", "hr", "li",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul",
})

# A block this short whose text is ENTIRELY bold reads as a display heading, not a paragraph.
_BOLD_HEADING_MAX_CHARS = 120

_BOLD_RE = re.compile(r"font-weight\s*:\s*(?:bold\b|[6-9]00\b)", re.IGNORECASE)
_HIDDEN_RE = re.compile(r"display\s*:\s*none\b", re.IGNORECASE)
_CHARSET_RE = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)
_MAX_SPAN = 1000  # a colspan/rowspan larger than this is markup garbage, not layout


def _collapse(text: str) -> str:
    """Whitespace collapsed; ``<br>`` newlines kept (one per run), everything else a space."""
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


def _span_of(attrs: dict[str, str | None], name: str) -> int:
    try:
        value = int(str(attrs.get(name) or "1").strip())
    except ValueError:
        return 1
    return max(1, min(value, _MAX_SPAN))


class _Reader(HTMLParser):
    """Streaming HTML -> blocks + tables. One instance per document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[TextBlock] = []
        self._seq = 0
        self.tables: list[Table] = []
        self.lang: str = ""
        # --- suppression ---
        self._strip_depth = 0        # inside <script>/<style>/<noscript>
        self._in_head = False
        # --- inline style inheritance (bold / display:none), a simplified open-tag stack ---
        self._styles: list[tuple[str, bool, bool]] = []   # (tag, adds_bold, adds_hidden)
        self._bold_depth = 0
        self._hidden_depth = 0
        # --- current text run ---
        self._buf: list[str] = []
        self._role: str | None = None
        self._bold_chars = 0
        self._text_chars = 0
        self._seen_h1 = False
        # --- table capture ---
        self._table_depth = 0
        self._rows: list[list[dict[str, object]]] = []
        self._cell: dict[str, object] | None = None
        self._table_count = 0

    # ------------------------------------------------------------------ tag handling

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        adict = dict(attrs)
        if tag == "html":
            self.lang = (adict.get("lang") or adict.get("xml:lang") or "").strip()
            return
        if tag in _STRIP_TAGS:
            self._strip_depth += 1
            return
        if self._strip_depth:
            return
        if tag == "head":
            self._in_head = True
            return
        if self._in_head:
            if tag in _HEAD_ONLY_TAGS:
                return
            self._in_head = False  # body content started without </head>

        if tag == "br":
            self._append_text("\n", literal=True)
            return
        if tag not in _VOID_TAGS and tag != "body":
            adds_bold = tag in ("b", "strong") or bool(_BOLD_RE.search(adict.get("style") or ""))
            adds_hidden = bool(_HIDDEN_RE.search(adict.get("style") or ""))
            self._styles.append((tag, adds_bold, adds_hidden))
            self._bold_depth += adds_bold
            self._hidden_depth += adds_hidden

        if tag == "table":
            if self._table_depth == 0:
                self._flush_block()
                self._rows = []
                self._cell = None
            else:
                self._cell_sep("\n")
            self._table_depth += 1
            return
        if self._table_depth:
            if self._table_depth == 1:
                if tag == "tr":
                    self._close_cell()
                    self._rows.append([])
                elif tag in ("td", "th"):
                    self._close_cell()
                    if not self._rows:
                        self._rows.append([])  # stray cell without a <tr>
                    self._cell = {
                        "buf": [],
                        "is_header": tag == "th",
                        "row_span": _span_of(adict, "rowspan"),
                        "col_span": _span_of(adict, "colspan"),
                    }
                elif tag in _BLOCK_TAGS:
                    self._cell_sep("\n")
            else:  # inside a nested table: cell/row boundaries become plain separators
                if tag in ("td", "th"):
                    self._cell_sep(" ")
                elif tag in _BLOCK_TAGS:
                    self._cell_sep("\n")
            return

        if tag in _BLOCK_TAGS:
            self._flush_block()
            self._role = tag if tag in _ROLE_TAGS else None

    def handle_endtag(self, tag: str) -> None:
        if tag in _STRIP_TAGS:
            self._strip_depth = max(0, self._strip_depth - 1)
            return
        if self._strip_depth:
            return
        if tag == "head":
            self._in_head = False
            return
        if self._in_head:
            return
        self._pop_style(tag)

        if tag == "table":
            if self._table_depth == 0:
                return
            self._table_depth -= 1
            if self._table_depth == 0:
                self._close_cell()
                self._finish_table()
                self._role = None
            else:
                self._cell_sep("\n")
            return
        if self._table_depth:
            if self._table_depth == 1:
                if tag in ("td", "th", "tr"):
                    self._close_cell()
                elif tag in _BLOCK_TAGS:
                    self._cell_sep("\n")
            else:
                self._cell_sep(" " if tag in ("td", "th") else "\n")
            return

        if tag in _BLOCK_TAGS:
            self._flush_block()
            self._role = None

    def handle_data(self, data: str) -> None:
        self._append_text(data, literal=False)

    # ------------------------------------------------------------------ text plumbing

    def _append_text(self, data: str, *, literal: bool) -> None:
        """Route text to the open cell or the current block; drop it when suppressed."""
        if self._strip_depth or self._in_head or self._hidden_depth:
            return
        if not literal:
            data = re.sub(r"\s+", " ", data)  # source whitespace collapses; br newlines don't
            if not data.strip() and not (self._buf or self._cell):
                return
        if self._table_depth:
            if self._cell is not None:
                self._cell["buf"].append(data)  # type: ignore[union-attr]
            return
        self._buf.append(data)
        n = len(re.sub(r"\s", "", data))
        self._text_chars += n
        if self._bold_depth:
            self._bold_chars += n


    def _next_seq(self) -> int:
        """Document-order index across blocks AND tables — what lets the emitter interleave
        them faithfully with no geometry to sort by."""
        value = self._seq
        self._seq += 1
        return value

    def _flush_block(self) -> None:
        text = _collapse("".join(self._buf))
        bold, total = self._bold_chars, self._text_chars
        self._buf, self._bold_chars, self._text_chars = [], 0, 0
        if not text:
            return
        role = self._role
        if role in _HEADING_TAGS:
            if role == "h1" and not self._seen_h1:
                self._seen_h1 = True
                zone = Zone.title
            else:
                zone = Zone.heading
        elif role == "li":
            zone = Zone.body
            text = "- " + text
        elif total > 0 and bold == total and len(text) <= _BOLD_HEADING_MAX_CHARS:
            zone = Zone.heading
        else:
            zone = Zone.body
        self.blocks.append(
            TextBlock(text=text, zone=zone, page=1, bbox=None, role=role, seq=self._next_seq())
        )

    def _pop_style(self, tag: str) -> None:
        """Close ``tag``: implicit-close everything opened after it, adjusting bold/hidden."""
        for i in range(len(self._styles) - 1, -1, -1):
            if self._styles[i][0] == tag:
                for _, adds_bold, adds_hidden in self._styles[i:]:
                    self._bold_depth -= adds_bold
                    self._hidden_depth -= adds_hidden
                del self._styles[i:]
                return

    # ------------------------------------------------------------------ table plumbing

    def _cell_sep(self, sep: str) -> None:
        if self._cell is not None and self._cell["buf"]:  # type: ignore[truthy-bool]
            self._cell["buf"].append(sep)  # type: ignore[union-attr]

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        self._cell["text"] = _collapse("".join(self._cell["buf"]))  # type: ignore[arg-type]
        self._rows[-1].append(self._cell)
        self._cell = None

    def _finish_table(self) -> None:
        rows = self._rows
        self._rows = []
        if not any(row for row in rows):
            return
        occupied: set[tuple[int, int]] = set()
        cells: list[Cell] = []
        for r, row in enumerate(rows):
            c = 0
            for spec in row:
                while (r, c) in occupied:
                    c += 1
                row_span = int(spec["row_span"])  # type: ignore[arg-type]
                col_span = int(spec["col_span"])  # type: ignore[arg-type]
                for i in range(row_span):
                    for j in range(col_span):
                        occupied.add((r + i, c + j))
                cells.append(Cell(
                    row=r, col=c, row_span=row_span, col_span=col_span,
                    text=str(spec["text"]), is_header=bool(spec["is_header"]), bbox=None,
                ))
                c += col_span
        if not any(cell.text for cell in cells):
            return  # a spacer/rule table: geometry with no content
        self._table_count += 1
        self.tables.append(Table(
            seq=self._next_seq(),
            table_id=f"t{self._table_count}",
            page=1,
            row_count=len(rows),
            col_count=max(c for _, c in occupied) + 1,
            cells=cells,
            bbox=None,
        ))

    # ------------------------------------------------------------------ end of input

    def finish(self) -> None:
        if self._table_depth:  # unterminated table: finalize what we have
            self._table_depth = 0
            self._close_cell()
            self._finish_table()
        self._flush_block()


def _decode(data: bytes) -> str:
    """Bytes -> str: BOM first, then a ``<meta charset>``, else UTF-8 with ``replace``."""
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, errors="replace")
    head = data[:2048].decode("latin-1", errors="ignore")
    match = _CHARSET_RE.search(head)
    if match:
        try:
            codecs.lookup(match.group(1))
        except LookupError:
            pass
        else:
            return data.decode(match.group(1), errors="replace")
    return data.decode("utf-8", errors="replace")


def read_html(data: bytes) -> LayoutView:
    """Parse HTML bytes into the provider-neutral view the emitter consumes."""
    reader = _Reader()
    reader.feed(_decode(data))
    reader.close()
    reader.finish()
    return LayoutView(
        pages=[PageInfo(page=1)],  # width/height stay 0: HTML has no page geometry
        blocks=reader.blocks,
        tables=reader.tables,
        languages=[reader.lang] if reader.lang else [],
    )


__all__ = ["read_html"]
