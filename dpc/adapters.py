"""Provider payloads -> :class:`~dce.models.LayoutView`.

Everything downstream (classifier, locators, validators) reads a ``LayoutView`` and nothing
else. This module is the only place that knows what a provider's JSON looks like, which is
what lets a business unit adopt the service without adopting our OCR stack:

* :func:`from_azure_layout` — Azure AI Document Intelligence v4.0 ``prebuilt-layout``
  (the reference producer). Also tolerates a ``prebuilt-read``-shaped payload with lines but
  no paragraphs.
* :func:`from_azure_read` — Azure AI Vision **Read v3.2**, a completely different JSON shape
  and a materially weaker one. See its docstring: it carries no paragraph roles at all.
* :func:`from_azure` — dispatches between those two on the payload's own shape, and reports
  which one it chose. A caller holding "an Azure result" rarely wants to care which product
  produced it; the service is required to care, and to say so.
* :func:`from_des_ocr` — the Document Enrichment Service's
  ``GET /api/runs/{run_id}/pages/{n}/ocr`` response, in either its enveloped
  ``{"page": …, "raw": …}`` form or a bare page model dump.
* :func:`from_plain_text` — the degraded path. A caller with no layout provider still gets a
  classification; it just loses the zone weighting, which is stated plainly rather than
  faked (nothing is promoted to ``title`` on a guess).

Two design decisions worth stating:

**Zones over geometry.** A term in a title is worth far more than the same term in page
furniture that repeats on every page. Azure already predicts ``paragraphs[].role``, so the
mapping is a lookup, not a font-height heuristic: ``title`` -> :attr:`Zone.title`,
``sectionHeading`` -> :attr:`Zone.heading`, ``pageHeader``/``pageFooter``/``pageNumber`` ->
:attr:`Zone.furniture`, everything else -> :attr:`Zone.body`. Text that lives inside a table
is re-zoned to :attr:`Zone.table` by span overlap (falling back to bbox containment), because
Azure's ``paragraphs[]`` stream *includes* table-cell content — emitting cells as extra blocks
would double-count every cell in the lexical score.

**Never raise.** A partially-understood payload is worth far more than a failed request: every
coercion below degrades to a sensible default. A malformed table costs you that table, not the
classification.

**Roles come from paragraphs, position comes from lines.** A paragraph's rectangle is the union
hull of its lines, which for wrapped text spans several visual rows and so states the column
correctly and the row not at all. :func:`_attach_lines` therefore joins ``pages[].lines[]`` onto
the paragraph blocks by *span overlap* — both streams index the same top-level ``content``
string, so the join is exact rather than geometric — and leaves each block holding the boxes a
spatial renderer can actually place. A reader with no line stream keeps an empty ``lines`` list;
nothing is ever synthesised by splitting text on newlines, because an invented rectangle is
worse than no rectangle.

Geometry is the Azure quad convention: 8 floats, 4 ``(x, y)`` points clockwise from top-left,
in the page's own ``unit``.
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Any

from dpc.doctree.harvest import harvest_structure, to_raw
from dpc.models import (
    Cell,
    KeyValue,
    LayoutView,
    Mark,
    PageInfo,
    Quad,
    Table,
    TextBlock,
    TextLine,
    Zone,
)

#: ``paragraphs[].role`` -> zone. Roles outside this map (``footnote``, ``formulaBlock``, or
#: anything Azure adds later) fall through to :attr:`Zone.body` rather than being dropped.
ROLE_ZONES: dict[str, Zone] = {
    "title": Zone.title,
    "sectionHeading": Zone.heading,
    "pageHeader": Zone.furniture,
    "pageFooter": Zone.furniture,
    "pageNumber": Zone.furniture,
}

#: DES stores Azure's verbatim per-page payload; these keys identify it as Azure-shaped.
_AZURE_PAGE_KEYS = ("pageNumber", "selectionMarks", "lines", "words")

#: ``LayoutView.raw["provider"]`` values. Named, because three other modules key off them —
#: the ingestion pipeline, ``/readyz`` and the response provenance — and a typo in a string
#: literal that only shows up as a blank field in an audit trail is not worth the saving.
PROVIDER_AZURE_LAYOUT = "azure-prebuilt-layout"
PROVIDER_AZURE_READ = "azure-read-v3.2"
PROVIDER_DES_OCR = "des-ocr"
PROVIDER_PLAIN_TEXT = "plain-text"

_ZERO_QUAD: Quad = [0.0] * 8


# ---------------------------------------------------------------------------
# Defensive coercions — the mapper never trusts the payload's shape
# ---------------------------------------------------------------------------
def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    """Every dict in ``value``, ignoring anything else it happens to contain."""
    return [item for item in _as_list(value) if isinstance(item, dict)]


def _text_of(node: dict[str, Any], *keys: str) -> str:
    """First non-empty string among ``keys`` (v4.0 uses ``content``; ``text`` is a fallback)."""
    for key in (*keys, "content", "text"):
        value = node.get(key)
        if value:
            return str(value)
    return ""


def _polygon_to_quad(polygon: Any) -> Quad | None:
    """Coerce an Azure ``polygon`` (or a bbox list) into an 8-number quad.

    Args:
        polygon: Normally 8 numbers (4 ``x, y`` points clockwise from top-left). A 4-number
            ``[x0, y0, x1, y1]`` rectangle and >8-point polygons are tolerated by taking the
            axis-aligned extent.

    Returns:
        Eight floats in quad order, or ``None`` when nothing usable was supplied.
    """
    values = [_as_float(v) for v in _as_list(polygon)]
    if len(values) == 8:
        return values
    if len(values) == 4:
        x0, y0, x1, y1 = values
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    if len(values) >= 6 and len(values) % 2 == 0:
        xs, ys = values[0::2], values[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    return None


def _regions(node: Any) -> list[dict[str, Any]]:
    return _dicts(_as_dict(node).get("boundingRegions"))


def _region_page(node: Any, default: int = 1) -> int:
    """Page of an element's first bounding region (1-based)."""
    regions = _regions(node)
    if not regions:
        return default
    return _as_int(regions[0].get("pageNumber"), default) or default


def _region_quad(node: Any) -> Quad | None:
    """Quad of an element's first bounding region, if it has geometry."""
    regions = _regions(node)
    if not regions:
        return _polygon_to_quad(_as_dict(node).get("polygon"))
    return _polygon_to_quad(regions[0].get("polygon"))


def _spans(node: Any) -> list[tuple[int, int]]:
    """Character spans as ``(offset, length)``; accepts ``spans: [...]`` and ``span: {...}``."""
    obj = _as_dict(node)
    raw = obj.get("spans")
    if raw is None and isinstance(obj.get("span"), dict):
        raw = [obj["span"]]
    out: list[tuple[int, int]] = []
    for span in _dicts(raw):
        offset = _as_int(span.get("offset"), -1)
        if offset >= 0:
            out.append((offset, _as_int(span.get("length"))))
    return out


def _spans_overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    return any(
        a_off < b_off + max(b_len, 1) and b_off < a_off + max(a_len, 1)
        for a_off, a_len in a
        for b_off, b_len in b
    )


def _quad_center(quad: Quad) -> tuple[float, float]:
    xs, ys = quad[0::2], quad[1::2]
    return (sum(xs) / len(xs), sum(ys) / len(ys)) if xs and ys else (0.0, 0.0)


def _point_in_quad(point: tuple[float, float], quad: Quad) -> bool:
    """Containment against a quad's axis-aligned extent (rotation-tolerant enough)."""
    xs, ys = quad[0::2], quad[1::2]
    if not xs or not ys:
        return False
    return min(xs) <= point[0] <= max(xs) and min(ys) <= point[1] <= max(ys)


# ---------------------------------------------------------------------------
# Azure prebuilt-layout
# ---------------------------------------------------------------------------
def _unwrap_analyze_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept a whole Layout job, an ``analyzeResult``, or a ``{"analyzeResult": …}`` wrapper."""
    node = _as_dict(payload)
    inner = _as_dict(node.get("analyzeResult"))
    return inner or node


def _map_pages(result: dict[str, Any]) -> list[PageInfo]:
    pages: list[PageInfo] = []
    for index, page in enumerate(_dicts(result.get("pages"))):
        pages.append(
            PageInfo(
                page=_as_int(page.get("pageNumber"), index + 1) or index + 1,
                width=_as_float(page.get("width")),
                height=_as_float(page.get("height")),
                unit=str(page.get("unit") or "pixel"),
                angle=_as_float(page.get("angle")),
            )
        )
    return pages


def _map_cell(node: dict[str, Any]) -> Cell:
    kind = str(node.get("kind") or node.get("cell_kind") or "content")
    return Cell(
        row=_as_int(node.get("rowIndex", node.get("row_index"))),
        col=_as_int(node.get("columnIndex", node.get("column_index"))),
        row_span=max(1, _as_int(node.get("rowSpan", node.get("row_span")), 1)),
        col_span=max(1, _as_int(node.get("columnSpan", node.get("column_span")), 1)),
        text=_text_of(node).replace("\n", " ").strip(),
        is_header=kind in ("columnHeader", "rowHeader"),
        bbox=_region_quad(node) or _polygon_to_quad(node.get("bbox")),
    )


def _map_tables(result: dict[str, Any]) -> list[Table]:
    """Map ``tables[]``, deriving row/column counts when the payload omits them."""
    tables: list[Table] = []
    per_page: dict[int, int] = {}
    for node in _dicts(result.get("tables")):
        page = _region_page(node)
        index = per_page.get(page, 0)
        per_page[page] = index + 1
        cells = [_map_cell(cell) for cell in _dicts(node.get("cells"))]
        tables.append(
            Table(
                table_id=str(node.get("table_id") or f"p{page}-tbl{index}"),
                page=page,
                row_count=_as_int(node.get("rowCount", node.get("row_count")))
                or max((c.row + c.row_span for c in cells), default=0),
                col_count=_as_int(node.get("columnCount", node.get("column_count")))
                or max((c.col + c.col_span for c in cells), default=0),
                cells=cells,
                bbox=_region_quad(node),
            )
        )
    return tables


def _map_marks(result: dict[str, Any]) -> list[Mark]:
    """Map selection marks from ``pages[].selectionMarks[]`` and any top-level list.

    Checkboxes are load-bearing on KYC forms — which box is ticked frequently *is* the answer —
    so both placements are swept rather than assuming the v4.0 per-page one.
    """
    marks: list[Mark] = []
    for index, page in enumerate(_dicts(result.get("pages"))):
        number = _as_int(page.get("pageNumber"), index + 1) or index + 1
        for node in _dicts(page.get("selectionMarks")):
            marks.append(
                Mark(
                    state=str(node.get("state") or "unselected"),
                    page=number,
                    bbox=_polygon_to_quad(node.get("polygon")) or _region_quad(node),
                )
            )
    for node in _dicts(result.get("selectionMarks")):
        marks.append(
            Mark(
                state=str(node.get("state") or "unselected"),
                page=_region_page(node),
                bbox=_region_quad(node),
            )
        )
    return marks


def _map_key_values(result: dict[str, Any]) -> list[KeyValue]:
    """Map ``keyValuePairs[]``; a pair with no key text carries no addressable signal."""
    pairs: list[KeyValue] = []
    for node in _dicts(result.get("keyValuePairs")):
        key = _as_dict(node.get("key"))
        value = _as_dict(node.get("value"))
        key_text = _text_of(key).strip()
        if not key_text:
            continue
        confidence = node.get("confidence")
        pairs.append(
            KeyValue(
                key=key_text,
                value=_text_of(value).strip(),
                page=_region_page(key) or _region_page(value),
                key_bbox=_region_quad(key),
                value_bbox=_region_quad(value),
                confidence=None if confidence is None else _as_float(confidence),
            )
        )
    return pairs


def _map_languages(result: dict[str, Any]) -> list[str]:
    """Distinct locales from ``languages[]``, in payload order."""
    seen: dict[str, None] = {}
    for node in _dicts(result.get("languages")):
        locale = str(node.get("locale") or "").strip()
        if locale:
            seen.setdefault(locale, None)
    return list(seen)


def _table_geometry(tables: list[Table], result: dict[str, Any]) -> tuple[
    dict[int, list[tuple[int, int]]], dict[int, list[Quad]]
]:
    """Per-page table spans and quads — the inputs to the ``Zone.table`` re-zoning test."""
    spans_by_page: dict[int, list[tuple[int, int]]] = {}
    quads_by_page: dict[int, list[Quad]] = {}
    for node in _dicts(result.get("tables")):
        page = _region_page(node)
        spans = _spans(node) or [s for cell in _dicts(node.get("cells")) for s in _spans(cell)]
        if spans:
            spans_by_page.setdefault(page, []).extend(spans)
    for table in tables:
        quads = [c.bbox for c in table.cells if c.bbox] or ([table.bbox] if table.bbox else [])
        if quads:
            quads_by_page.setdefault(table.page, []).extend(quads)
    return spans_by_page, quads_by_page


def _zone_for(
    role: str | None,
    page: int,
    spans: list[tuple[int, int]],
    quad: Quad | None,
    table_spans: dict[int, list[tuple[int, int]]],
    table_quads: dict[int, list[Quad]],
) -> Zone:
    """Zone of one text element: role first, then table membership, then body."""
    if role and role in ROLE_ZONES:
        return ROLE_ZONES[role]
    if spans and _spans_overlap(spans, table_spans.get(page, [])):
        return Zone.table
    if quad and any(_point_in_quad(_quad_center(quad), q) for q in table_quads.get(page, [])):
        return Zone.table
    return Zone.body


def _map_blocks(
    result: dict[str, Any],
    table_spans: dict[int, list[tuple[int, int]]],
    table_quads: dict[int, list[Quad]],
) -> list[TextBlock]:
    """Text blocks from ``paragraphs[]``, falling back to ``pages[].lines[]``.

    Only one of the two streams is used: Azure's paragraphs already cover the line text (and
    the table-cell text), so mapping both would count every term twice.
    """
    blocks: list[TextBlock] = []
    for node in _dicts(result.get("paragraphs")):
        text = _text_of(node).strip()
        if not text:
            continue
        role = node.get("role")
        role = str(role) if role else None
        page = _region_page(node)
        quad = _region_quad(node)
        blocks.append(
            TextBlock(
                text=text,
                zone=_zone_for(role, page, _spans(node), quad, table_spans, table_quads),
                page=page,
                bbox=quad,
                role=role,
            )
        )
    if blocks:
        return blocks

    # prebuilt-read (or a Layout payload whose paragraphs were stripped): lines carry the text
    # but no roles, so everything is body unless it sits inside a table.
    for index, page_dict in enumerate(_dicts(result.get("pages"))):
        page = _as_int(page_dict.get("pageNumber"), index + 1) or index + 1
        for line in _dicts(page_dict.get("lines")):
            text = _text_of(line).strip()
            if not text:
                continue
            quad = _polygon_to_quad(line.get("polygon"))
            blocks.append(
                TextBlock(
                    text=text,
                    zone=_zone_for(None, page, _spans(line), quad, table_spans, table_quads),
                    page=page,
                    bbox=quad,
                )
            )
    if blocks:
        return blocks

    # Last resort: the flat ``content`` string. Better a degraded view than an empty one.
    content = str(result.get("content") or "")
    return [
        TextBlock(text=line.strip(), zone=Zone.body, page=1)
        for line in content.splitlines()
        if line.strip()
    ]


#: ``_block_stream`` kinds: which payload array :func:`_map_blocks` built its blocks from.
_STREAM_PARAGRAPHS = "paragraphs"
_STREAM_LINES = "lines"
_STREAM_CONTENT = "content"


def _block_stream(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Which stream :func:`_map_blocks` used, and the nodes it turned into blocks, in order.

    Mirrors :func:`_map_blocks`'s own preference order and its one filter (a node whose text is
    blank after stripping produces no block), so ``zip(nodes, blocks)`` pairs each block with
    the node it came from. It deliberately does *not* reproduce the last-resort
    ``content``-splitting path: those blocks have no provider node and therefore no spans, so
    that case is reported as :data:`_STREAM_CONTENT` with no nodes, and the length check in
    :func:`_attach_lines` is what detects it.

    The *kind* matters as much as the nodes. In the ``paragraphs[]`` case a block is a hull over
    several provider lines and the span join is the only thing that can recover the rows. In the
    ``pages[].lines[]`` fallback each block **is** one provider line, already holding that line's
    own single-row quad, so there is nothing to join and — critically — nothing that can fail:
    see :func:`_attach_lines`.

    Args:
        result: An unwrapped ``analyzeResult``.

    Returns:
        ``(kind, nodes)``.
    """
    paragraphs = [node for node in _dicts(result.get("paragraphs")) if _text_of(node).strip()]
    if paragraphs:
        return _STREAM_PARAGRAPHS, paragraphs
    lines = [
        line
        for page in _dicts(result.get("pages"))
        for line in _dicts(page.get("lines"))
        if _text_of(line).strip()
    ]
    if lines:
        return _STREAM_LINES, lines
    return _STREAM_CONTENT, []


def _span_extent(spans: list[tuple[int, int]]) -> tuple[int, int]:
    """Half-open ``[low, high)`` character extent covering every span in ``spans``.

    A zero-length span is treated as one character wide, exactly as :func:`_spans_overlap` does,
    so an empty paragraph marker still has a position rather than vanishing between two lines.
    """
    low = min(offset for offset, _ in spans)
    high = max(offset + max(length, 1) for offset, length in spans)
    return low, high


def _adopt_own_lines(
    nodes: list[dict[str, Any]], blocks: list[TextBlock]
) -> tuple[int, int]:
    """The ``pages[].lines[]`` fallback: every block already IS the line it needs.

    When :func:`_map_blocks` had no ``paragraphs[]`` to work from, each block was built from one
    provider line and the line's own single-row quad is right there on the node. There is no
    join to do and — the point — nothing that can fail: a payload whose lines carry no ``spans``
    at all (Azure omits them on a ``prebuilt-read`` result, and DES stores such pages verbatim)
    would otherwise build zero span candidates, drop every line, and report ``0/N`` on the one
    field §11.2 elevates to a health metric. That is a false alarm on top of throwing away
    geometry the payload handed us, which is why this path exists at all. It is exactly what
    :func:`from_azure_read` already does for its own line-shaped provider.

    :attr:`Zone.table` blocks are still emptied rather than filled, for the same reason the span
    join skips them: the emitter suppresses them from the body, so their lines are placed
    nowhere while ``tables[]`` emits the same text again.

    Assignment, not append — so a second call over the same blocks is a no-op, not a duplicate.

    Args:
        nodes: The ``pages[].lines[]`` nodes, in block order (from :func:`_block_stream`).
        blocks: The blocks those nodes produced. Mutated in place.

    Returns:
        ``(attached, total)``.
    """
    attached = 0
    for node, block in zip(nodes, blocks, strict=True):
        if block.zone is Zone.table:
            block.lines = []
            continue
        block.lines = [
            TextLine(text=_text_of(node).strip(), bbox=_polygon_to_quad(node.get("polygon")))
        ]
        attached += 1
    return attached, len(nodes)


def _attach_lines(blocks: list[TextBlock], result: dict[str, Any]) -> tuple[int, int]:
    """Attach ``pages[].lines[]`` geometry to paragraph blocks. Returns ``(attached, total)``.

    Both streams index into the same top-level ``content`` string, so the join is EXACT via
    :func:`_spans_overlap` rather than geometric. Each line goes to the block with the smallest
    ``(first_span_offset, block_index)`` among those whose spans overlap it, where
    ``first_span_offset`` is the block's SMALLEST span offset — ``min(offset)``, not
    ``spans[0].offset``. That distinction is the whole determinism argument: nothing in
    Microsoft's docs orders the entries *inside* a node's ``spans[]`` array, so a key read off
    ``spans[0]`` would move a line to a different block — hence a different canvas, hence a
    different sha256 — from the same bytes with one array reordered.

    What the key does NOT promise is total independence from payload order. It is a total order
    because ``block_index`` is unique, and ``block_index`` is the node's position in
    ``paragraphs[]``. So two blocks that tie on ``min(offset)`` are separated by ``paragraphs[]``
    order, and reordering *those two* moves the line between them. That is the spec's own rule
    (§3.2) and it is deterministic for any given payload; it is not order-INDEPENDENCE, and
    claiming it were would be a promise this function cannot keep.

    A line that no block claims is DROPPED, not promoted: unclaimed lines are almost always
    table-cell content, which ``tables[]`` already carries, and promoting them would emit the
    text twice. Blocks whose zone is :attr:`Zone.table` are skipped for the same reason — the
    emitter suppresses them from the body, so lines given to them would be placed nowhere.

    The returned counts become the front matter's ``line_join`` field, whose whole purpose is to
    make the one failure this design cannot prevent legible: if the two span streams disagree,
    every line is dropped, every block falls back to its multi-row hull, and the output is a
    perfectly ordinary file with no columns and no error. ``line_join: 0/430`` is the difference
    between "this document has no columns" and "the join broke".

    ``attached`` counts lines a block CLAIMED, not lines that can be placed. A claimed line whose
    provider node had no ``polygon`` is attached with ``bbox=None`` and counted: it is a
    geometry gap, not a join failure, and §4.2 already refuses to make an atom of it while the
    ``reason`` histogram's ``no-geometry`` bucket names it. Folding the two together would fire
    §11.2's kill criterion — "switch to geometric line→paragraph assignment" — on a document
    whose join was perfect and whose provider simply omitted boxes, which is the one conclusion
    that evidence cannot support.

    ``total`` counts lines that have text: a blank line is not placeable content, and counting
    it could only depress a ratio that exists to be read as a health signal.

    Args:
        blocks: The blocks :func:`_map_blocks` produced from ``result``. Mutated in place — a
            block that enters the join has its ``lines`` REPLACED, so a second call over the
            same blocks reproduces the first rather than appending every line twice.
        result: The same unwrapped ``analyzeResult`` those blocks were mapped from.

    Returns:
        ``(attached, total)`` — lines given to a block, and lines offered.
    """
    kind, sources = _block_stream(result)
    # A length mismatch means these blocks did not come from this stream (the ``content`` last
    # resort, or a payload edited between the two calls). There is then no node to read spans
    # from, so nothing is claimed and the count reports it, rather than joining by guesswork.
    paired = len(sources) == len(blocks)
    if kind == _STREAM_LINES and paired:
        return _adopt_own_lines(sources, blocks)

    candidates: list[tuple[int, int, int, list[tuple[int, int]], TextBlock]] = []
    if paired:
        for index, (node, block) in enumerate(zip(sources, blocks, strict=True)):
            if block.zone is Zone.table:
                continue
            spans = _spans(node)
            if not spans:
                continue
            low, high = _span_extent(spans)
            # Replaced, not appended to: idempotence, so a re-run cannot duplicate text onto a
            # canvas. Only blocks that actually enter the join are touched.
            block.lines = []
            candidates.append((low, high, index, spans, block))
    # Scan order is by extent so the two cuts below can be made; claim order is the spec's
    # (min_span_offset, block_index), applied by taking the minimum over the matches.
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    lows = [c[0] for c in candidates]
    # Running maximum of ``high`` over ``candidates[:j + 1]``. ``lows`` is sorted, so a bisect
    # cuts the tail (a candidate starting at or after the line's end cannot overlap it) — but
    # the head is the expensive half: without this array every line still tested every candidate
    # starting before it, which is O(lines x blocks) and measured 49 s on 400 pages, inside a
    # synchronous mapper, on a provider that accepts 2000. ``pmax[j] <= line_low`` proves that
    # NO candidate at or below ``j`` reaches the line, so the walk stops there instead of at 0.
    pmax: list[int] = []
    run = -1
    for candidate in candidates:
        run = max(run, candidate[1])
        pmax.append(run)

    attached = 0
    total = 0
    for page in _dicts(result.get("pages")):
        for line in _dicts(page.get("lines")):
            text = _text_of(line).strip()
            if not text:
                continue
            total += 1
            line_spans = _spans(line)
            if not line_spans:
                continue
            line_low, line_high = _span_extent(line_spans)
            winner: TextBlock | None = None
            best: tuple[int, int] | None = None
            index_j = bisect_left(lows, line_high) - 1
            while index_j >= 0 and pmax[index_j] > line_low:
                low, _, index, spans, block = candidates[index_j]
                index_j -= 1
                if not _spans_overlap(line_spans, spans):
                    continue
                key = (low, index)
                if best is None or key < best:
                    best, winner = key, block
            if winner is None:
                continue
            winner.lines.append(TextLine(text=text, bbox=_polygon_to_quad(line.get("polygon"))))
            attached += 1
    return attached, total


def from_azure_layout(analyze_result: dict[str, Any]) -> LayoutView:
    """Build a :class:`LayoutView` from an Azure Document Intelligence Layout payload.

    Args:
        analyze_result: The ``analyzeResult`` object, or the whole job JSON
            (``{"status": …, "analyzeResult": {…}}``) — both are accepted. A
            ``prebuilt-read``-shaped payload (lines, no paragraphs) also maps, with every
            block landing in :attr:`Zone.body`.

    Returns:
        The provider-neutral view. Missing or malformed sections yield empty collections
        rather than an exception.
    """
    result = _unwrap_analyze_result(analyze_result)
    tables = _map_tables(result)
    table_spans, table_quads = _table_geometry(tables, result)
    blocks = _map_blocks(result, table_spans, table_quads)
    # Unconditional: the join is cheap, purely additive (a block nobody claims for keeps its
    # empty ``lines``), and the counts are the only evidence a reader gets that it worked.
    line_join = _attach_lines(blocks, result)
    pages = _map_pages(result)
    if not pages and blocks:
        pages = [PageInfo(page=n) for n in sorted({b.page for b in blocks})]
    view = LayoutView(
        pages=pages,
        blocks=blocks,
        tables=tables,
        marks=_map_marks(result),
        key_values=_map_key_values(result),
        languages=_map_languages(result),
        # Provenance, not the payload: the service never needs the original document, and
        # holding a multi-megabyte analyzeResult per in-flight request buys nothing.
        raw={
            "provider": PROVIDER_AZURE_LAYOUT,
            "api_version": str(result.get("apiVersion") or ""),
            "model_id": str(result.get("modelId") or ""),
            "content_chars": len(str(result.get("content") or "")),
            # ``[attached, total]`` from the line join, for the emitter's ``line_join`` field.
            # A list, not a tuple, so a stored view round-trips through JSON unchanged.
            "_line_join": [line_join[0], line_join[1]],
        },
    )
    # SPEC-DOCTREE-1 §3.1: sections/figures were being dropped here — the doctree's provider
    # seed. Harvested as index refs only (never text) and stored in JSON form so a recorded
    # view rebuilds the tree with no re-fetch. Key absent (not null) when the payload has no
    # sections: absence IS the signal downstream (``passes.provider_sections = "absent"``).
    structure = harvest_structure(result)
    if structure is not None:
        view.raw["structure"] = to_raw(structure)
    return view


# ---------------------------------------------------------------------------
# Azure AI Vision Read v3.2
# ---------------------------------------------------------------------------
def _read_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``readResults`` entry in a Read v3.2 payload, whichever wrapper it arrived in.

    Accepted: the whole job (``{"status": …, "analyzeResult": {"readResults": […]}}``), a bare
    ``analyzeResult``, a bare ``{"readResults": […]}``, and a single read-result dict.
    """
    node = _as_dict(payload)
    inner = _as_dict(node.get("analyzeResult"))
    source = inner or node
    entries = _dicts(source.get("readResults"))
    if entries:
        return entries
    # A single page handed over without its envelope.
    if "lines" in source and "readResults" not in source:
        return [source]
    return []


def _read_page_number(entry: dict[str, Any], default: int) -> int:
    """Read v3.2 numbers its pages ``page``; ``pageNumber`` is tolerated for safety."""
    for key in ("page", "pageNumber"):
        number = _as_int(entry.get(key), 0)
        if number > 0:
            return number
    return default


def from_azure_read(analyze_result: dict[str, Any]) -> LayoutView:
    """Build a :class:`LayoutView` from an Azure AI Vision **Read v3.2** payload.

    **Every block is** :attr:`Zone.body`, and that is not a shortcut — it is what Read is.
    Read v3.2 returns ``readResults[].lines[].words[]`` and nothing else: no ``paragraphs``,
    no ``role``, no ``tables``, no ``selectionMarks``, no ``keyValuePairs``. There is no
    ``title`` to map because the product does not predict one, and inferring one from font
    height or page position is precisely the promotion :func:`from_plain_text` refuses to
    make — a wrong ``title`` is amplified by ``zone_weight_title`` and turns an abstention
    into a confident mistake.

    **This is a real accuracy difference between the two Azure providers, not a detail.** A
    ``DocTypeSpec`` anchor may be *zone-gated* (``Anchor.zone``): it counts only when it is
    found in the zone it names. The shipped registry declares 30 anchors gated on ``title``,
    21 of them decisive, and a document read by Read v3.2 can never satisfy one of them,
    because no block will ever be in the title zone. The same document read by Document
    Intelligence ``prebuilt-layout`` can. The cascade already knows the difference between
    "the claim was heard and failed" and "the claim could not be evaluated" — it counts the
    second as ``AnchorChannel.muted_decisive`` — and a Read payload puts every title-gated
    decisive anchor into that second bucket.

    So Read is a *lower-recall* provider: it abstains where Layout accepts. It is not a
    lower-*precision* one, because a gate that cannot be evaluated only ever withholds
    evidence, never manufactures it. That is the right direction for this service to fail in,
    and it is still a reason to prefer ``prebuilt-layout`` wherever both are available.

    Geometry: Read's ``boundingBox`` is a flat 8-number array in the same clockwise-from-
    top-left order as Layout's ``polygon``, so it maps straight onto :data:`dce.models.Quad`.
    Page dimensions are in the entry's own ``unit`` (``pixel`` for images, ``inch`` for PDFs).

    Args:
        analyze_result: The Read job JSON, its ``analyzeResult``, a bare
            ``{"readResults": […]}``, or a single read-result entry — all four are accepted.

    Returns:
        The provider-neutral view. Missing or malformed sections yield empty collections
        rather than an exception; a payload with no ``readResults`` yields an empty view.
    """
    entries = _read_results(analyze_result)
    pages: list[PageInfo] = []
    blocks: list[TextBlock] = []
    languages: dict[str, None] = {}
    for index, entry in enumerate(entries):
        number = _read_page_number(entry, index + 1)
        pages.append(
            PageInfo(
                page=number,
                width=_as_float(entry.get("width")),
                height=_as_float(entry.get("height")),
                unit=str(entry.get("unit") or "pixel"),
                angle=_as_float(entry.get("angle")),
            )
        )
        locale = str(entry.get("language") or "").strip()
        if locale:
            languages.setdefault(locale, None)
        for line in _dicts(entry.get("lines")):
            text = _text_of(line).strip()
            if not text:
                continue
            quad = _polygon_to_quad(line.get("boundingBox"))
            blocks.append(
                TextBlock(
                    text=text,
                    zone=Zone.body,
                    page=number,
                    bbox=quad,
                    # In Read there is no paragraph stream, so each block IS one provider line
                    # and no span join is needed: the block's own rectangle is a real single-row
                    # box, which is exactly what ``lines`` promises. Read gets full spatial
                    # support for free — it loses zones, not geometry. Without a quad there is
                    # nothing to place, and an empty list is the honest state.
                    lines=[TextLine(text=text, bbox=quad)] if quad else [],
                )
            )
    version = _as_dict(_as_dict(analyze_result).get("analyzeResult")).get("version")
    return LayoutView(
        pages=pages or [PageInfo(page=n) for n in sorted({b.page for b in blocks})],
        blocks=blocks,
        languages=list(languages),
        raw={
            "provider": PROVIDER_AZURE_READ,
            "api_version": str(version or _as_dict(analyze_result).get("version") or ""),
            "model_id": "read",
            "pages": len(entries),
            # Read runs no span join, but the front matter's ``line_join`` field is read as a
            # health signal across the whole corpus, and a Read page that silently carried no
            # such field would be invisible in exactly the sweep §11.2 exists to support.
            # Here it means what it can mean: lines that ended up placeable over lines offered.
            # A list, not a tuple, so a stored view round-trips through JSON unchanged.
            "_line_join": [sum(1 for block in blocks if block.lines), len(blocks)],
            # Said in the payload itself, not only in a docstring: an auditor reading a stored
            # LayoutView must be able to see why this document had no title zone.
            "zones": "body only — Read v3.2 predicts no paragraph roles",
        },
    )


# ---------------------------------------------------------------------------
# Azure, either product
# ---------------------------------------------------------------------------
def azure_payload_kind(payload: dict[str, Any]) -> str:
    """Which Azure product produced this payload: ``"read"`` or ``"layout"``.

    Decided on the one key that cannot be confused: Read v3.2 is the only shape with a
    ``readResults`` array. Document Intelligence v4.0 uses ``pages`` / ``paragraphs`` /
    ``tables``. Anything unrecognised is reported as ``"layout"``, because
    :func:`from_azure_layout` is the more forgiving of the two mappers (it degrades to
    ``pages[].lines[]`` and then to a flat ``content`` string) and so is the safer default
    for a payload we could not identify.

    Args:
        payload: An Azure job JSON, an ``analyzeResult``, or a bare result body.

    Returns:
        ``"read"`` or ``"layout"``.
    """
    return "read" if _read_results(payload) else "layout"


def from_azure(payload: dict[str, Any]) -> LayoutView:
    """Adapt an Azure payload of **either** product, choosing the mapper by shape.

    Auto-detection is a kindness to callers, not a licence to be vague: the chosen mapper is
    recorded in ``LayoutView.raw["provider"]`` (:data:`PROVIDER_AZURE_READ` or
    :data:`PROVIDER_AZURE_LAYOUT`) and the API surfaces it on every response, because the two
    providers do not classify equally well and a caller who accidentally sent Read where they
    meant Layout has to be able to see that from the answer.

    Args:
        payload: A Read v3.2 or Document Intelligence v4.0 payload.

    Returns:
        The provider-neutral view.
    """
    if azure_payload_kind(payload) == "read":
        return from_azure_read(payload)
    return from_azure_layout(payload)


# ---------------------------------------------------------------------------
# DES OCR page payload
# ---------------------------------------------------------------------------
def _looks_azure(node: dict[str, Any]) -> bool:
    """Whether a dict is Azure's verbatim page shape rather than a DES model dump."""
    return any(key in node for key in _AZURE_PAGE_KEYS)


def _des_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise the accepted DES shapes into a list of per-page entries."""
    node = _as_dict(payload)
    if isinstance(node.get("pages"), list):
        return _dicts(node.get("pages"))
    if isinstance(node.get("page"), dict) or isinstance(node.get("raw"), dict):
        return [node]
    return [node] if node else []


def _des_page_number(meta: dict[str, Any], default: int = 1) -> int:
    for key in ("page_number", "pageNumber", "page"):
        value = meta.get(key)
        if isinstance(value, int | float | str):
            number = _as_int(value)
            if number > 0:
                return number
    return default


def _renumber(view: LayoutView, page: int) -> LayoutView:
    """Force every element of a single-page view onto ``page``.

    DES keys pages by its own ``page_number`` column; a verbatim Azure page payload that lost
    its ``pageNumber`` would otherwise silently collapse every page onto page 1.
    """
    for info in view.pages:
        info.page = page
    for block in view.blocks:
        block.page = page
    for table in view.tables:
        table.page = page
    for mark in view.marks:
        mark.page = page
    for kv in view.key_values:
        kv.page = page
    return view


def _from_des_normalized(page: dict[str, Any]) -> LayoutView:
    """Map a DES :class:`des.models.OcrPage` model dump (snake_case, no Azure keys)."""
    number = _des_page_number(page)
    info = PageInfo(
        page=number,
        width=_as_float(page.get("width")),
        height=_as_float(page.get("height")),
        unit=str(page.get("unit") or "pixel"),
        angle=_as_float(page.get("angle")),
    )
    tables = [
        Table(
            table_id=str(node.get("table_id") or f"p{number}-tbl{index}"),
            page=_as_int(node.get("page"), number) or number,
            row_count=_as_int(node.get("row_count")),
            col_count=_as_int(node.get("column_count", node.get("col_count"))),
            cells=[_map_cell(cell) for cell in _dicts(node.get("cells"))],
            bbox=_polygon_to_quad(node.get("bbox")),
        )
        for index, node in enumerate(_dicts(page.get("tables")))
    ]
    table_quads: dict[int, list[Quad]] = {}
    for table in tables:
        quads = [c.bbox for c in table.cells if c.bbox] or ([table.bbox] if table.bbox else [])
        if quads:
            table_quads.setdefault(table.page, []).extend(quads)

    blocks: list[TextBlock] = []
    for node in _dicts(page.get("paragraphs")):
        text = _text_of(node).strip()
        if not text:
            continue
        role = node.get("role")
        role = str(role) if role else None
        quad = _polygon_to_quad(node.get("bbox"))
        block_page = _as_int(node.get("page"), number) or number
        blocks.append(
            TextBlock(
                text=text,
                zone=_zone_for(role, block_page, [], quad, {}, table_quads),
                page=block_page,
                bbox=quad,
                role=role,
            )
        )
    if not blocks:
        for line in _dicts(page.get("lines")):
            text = _text_of(line).strip()
            if not text:
                continue
            quad = _polygon_to_quad(line.get("bbox"))
            blocks.append(
                TextBlock(
                    text=text,
                    zone=_zone_for(None, number, [], quad, {}, table_quads),
                    page=number,
                    bbox=quad,
                )
            )
    marks = [
        Mark(
            state=str(node.get("state") or "unselected"),
            page=number,
            bbox=_polygon_to_quad(node.get("bbox")),
        )
        for node in _dicts(page.get("selection_marks"))
    ]
    return LayoutView(
        pages=[info],
        blocks=blocks,
        tables=tables,
        marks=marks,
        raw={"provider": PROVIDER_DES_OCR, "page": number},
    )


def _line_join_of(view: LayoutView) -> tuple[int, int] | None:
    """A view's ``(attached, total)`` line-join counts, or ``None`` when it ran no join."""
    value = view.raw.get("_line_join")
    if not isinstance(value, list) or len(value) < 2:
        return None
    return _as_int(value[0]), _as_int(value[1])


def _merge(views: list[LayoutView]) -> LayoutView:
    """Concatenate per-page views into one document view, de-duplicating languages.

    The per-page ``_line_join`` counts are SUMMED, not dropped. DES pages go through the same
    span join ``from_azure_layout`` runs, so §11.2's silent failure is reachable on this path
    too, and a document-level ``line_join`` is the only evidence of it a reader ever gets.
    Pages that ran no join (a normalized DES row, which has no line stream) contribute nothing,
    and when NO page ran one the key is absent rather than a misleading ``0/0``.
    """
    merged = LayoutView(raw={"provider": PROVIDER_DES_OCR, "pages": len(views)})
    languages: dict[str, None] = {}
    attached = 0
    total = 0
    joined = False
    for view in views:
        merged.pages.extend(view.pages)
        merged.blocks.extend(view.blocks)
        merged.tables.extend(view.tables)
        merged.marks.extend(view.marks)
        merged.key_values.extend(view.key_values)
        for locale in view.languages:
            languages.setdefault(locale, None)
        counts = _line_join_of(view)
        if counts is not None:
            joined = True
            attached += counts[0]
            total += counts[1]
    merged.pages.sort(key=lambda p: p.page)
    merged.languages = list(languages)
    if joined:
        merged.raw["_line_join"] = [attached, total]
    return merged


def _des_raw(view: LayoutView, number: int) -> dict[str, Any]:
    """DES provenance for a page mapped through :func:`from_azure_layout`.

    Replaces the Azure provenance (the caller asked DES, not Azure) but KEEPS ``_line_join``:
    the span join genuinely ran on this page, so §11.2's silent failure is reachable here, and
    dropping the counts would leave the DES path — the one that actually runs the join in
    production — with no diagnostic at all.
    """
    raw: dict[str, Any] = {"provider": PROVIDER_DES_OCR, "page": number}
    counts = _line_join_of(view)
    if counts is not None:
        raw["_line_join"] = [counts[0], counts[1]]
    return raw


def from_des_ocr(payload: dict[str, Any]) -> LayoutView:
    """Build a :class:`LayoutView` from a DES OCR payload.

    Accepts every shape DES hands out: the enveloped ``{"page": {…}, "raw": {…}}`` of
    ``GET /api/runs/{run_id}/pages/{n}/ocr``, a list of those under ``{"pages": [...]}``, and a
    bare :class:`des.models.OcrPage` model dump. When the verbatim Azure payload is present it
    is preferred — it carries the paragraph roles, table spans and selection marks that the
    normalized row does not — and the page number from the DES row wins over the payload's.

    Args:
        payload: One of the shapes above.

    Returns:
        The merged view across whatever pages the payload contained; empty rather than raising
        when it contained none.
    """
    views: list[LayoutView] = []
    for entry in _des_entries(payload):
        meta = _as_dict(entry.get("page")) or entry
        raw = _as_dict(entry.get("raw"))
        number = _des_page_number(meta, default=_des_page_number(raw))
        if raw and _looks_azure(raw):
            view = from_azure_layout(
                {
                    "pages": [raw],
                    "paragraphs": raw.get("paragraphs"),
                    "tables": raw.get("tables"),
                    "keyValuePairs": raw.get("keyValuePairs"),
                    "languages": raw.get("languages"),
                }
            )
            view.raw = _des_raw(view, number)
        elif _looks_azure(meta):
            view = from_azure_layout({"pages": [meta]})
            view.raw = _des_raw(view, number)
        else:
            view = _from_des_normalized(meta)
        views.append(_renumber(view, number))
    return _merge(views)


# ---------------------------------------------------------------------------
# Plain text (degraded path)
# ---------------------------------------------------------------------------
def from_plain_text(text: str) -> LayoutView:
    """Build a :class:`LayoutView` from raw text, with every block in :attr:`Zone.body`.

    This is the honest degradation for a caller with no layout provider. Nothing is promoted to
    ``title`` on a guess — a wrong title would be amplified by ``zone_weight_title`` (3.0) and
    make a confident mistake more likely than an abstention, which is the wrong trade in a KYC
    system. One block per non-empty line keeps label-anchored extraction usable.

    Args:
        text: The document's text, in reading order.

    Returns:
        A single-page view; empty ``blocks`` when ``text`` has no content.
    """
    blocks = [
        TextBlock(text=line.strip(), zone=Zone.body, page=1)
        for line in (text or "").splitlines()
        if line.strip()
    ]
    return LayoutView(
        pages=[PageInfo(page=1)],
        blocks=blocks,
        raw={"provider": PROVIDER_PLAIN_TEXT, "chars": len(text or "")},
    )
