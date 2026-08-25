"""LayoutView -> PMD, the positional markdown this service exists to produce.

PMD ("positional markdown") is ordinary GitHub-flavoured markdown with one addition: an HTML
comment **anchor** on the line before each element carrying the page and the bounding
rectangle it came from::

    <!-- @2 [93,319,434,347] title -->
    # UNITED STATES OF AMERICA

The design constraints, in the order they were traded against each other:

1. **It must read as plain markdown.** Downstream agents get headings, tables, checkboxes and
   paragraphs with zero parsing beyond what they already do. Anchors are HTML comments, which
   every markdown renderer hides and every LLM can be told to read or ignore.
2. **Position must survive CHUNKING.** The obvious alternative — one geometry appendix at the
   end of the file keyed by block id — dies the moment a RAG pipeline cuts the file into
   chunks, which is the first thing every pipeline does. An anchor on the line above its
   element travels with any chunk that contains the element. This constraint is why the
   metadata is inline and why each anchor is self-contained.
3. **Reading order is the provider's, geometry is the truth.** Azure emits paragraphs in
   reading order and PyMuPDF emits blocks in document order; both are better orderings than
   anything recomputable from rectangles (a naive y-sort interleaves columns line by line).
   So elements keep provider order, and tables/marks/key-values are spliced into that order
   by their y-position. The anchors carry the exact rectangles for anyone who needs more.
4. **Deterministic bytes.** Same view in, same file out — the stored sha256 is meaningful and
   a re-conversion is comparable. Everything volatile (timestamps) is caller-supplied.

What each element becomes:

    role/zone title      -> "# text"
    role sectionHeading  -> "## text"
    zone furniture       -> plain text, anchor tagged "furniture[:role]"
    body                 -> plain paragraph
    Table                -> GFM table ("|" escaped, newlines -> "<br>"); spans land top-left
    Mark                 -> "- [x]" / "- [ ]"
    KeyValue             -> "**key:** value"
    page break           -> "<!-- page N size=612x792 unit=point -->"

Blocks whose zone is ``table`` are **skipped**: the adapters re-zone paragraphs that overlap
a detected table precisely so their text is not emitted twice.
"""
from __future__ import annotations

from typing import Any

from dpc.models import KeyValue, LayoutView, Quad, Table, TextBlock, Zone

PMD_VERSION = "1.0"
GENERATOR = "document-processor-convertor"


def _rect(quad: Quad | None) -> tuple[int, int, int, int] | None:
    """A quad's axis-aligned bounding rectangle, rounded to integers.

    Rounded because sub-point precision is provider noise, and stable integers are what make
    the output byte-deterministic across float formatting differences.
    """
    if not quad or len(quad) < 8:
        return None
    xs, ys = quad[0::2], quad[1::2]
    return (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys)))


def _kv_rect(kv: KeyValue) -> tuple[int, int, int, int] | None:
    """The union of a pair's key and value rectangles — the pair is one visual unit."""
    rects = [r for r in (_rect(kv.key_bbox), _rect(kv.value_bbox)) if r]
    if not rects:
        return None
    return (
        min(r[0] for r in rects), min(r[1] for r in rects),
        max(r[2] for r in rects), max(r[3] for r in rects),
    )


def _anchor(page: int, rect: tuple[int, int, int, int] | None, tag: str) -> str | None:
    """One anchor comment, or ``None`` when there is no geometry to anchor.

    No geometry means no anchor — an invented rectangle would be worse than an absent one,
    because a consumer cannot tell measured from made-up.
    """
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    return f"<!-- @{page} [{x0},{y0},{x1},{y1}] {tag} -->"


def _clean(text: str) -> str:
    """Body text, made safe to sit inside a markdown document.

    Only what could break DOCUMENT STRUCTURE is touched: a literal ``<!--`` would open a
    comment and swallow everything to the next ``-->``, including our own anchors. Everything
    else is left verbatim — escaping every markdown metacharacter would destroy readability,
    which is the format's first constraint.
    """
    return text.replace("<!--", "<! --").strip()


def _cell(text: str) -> str:
    """A table cell: pipes escaped, newlines flattened to ``<br>``."""
    return _clean(text).replace("|", "\\|").replace("\n", "<br>")


def _heading(block: TextBlock) -> str:
    text = _clean(block.text)
    # A leading '#' in the document's own text would silently deepen the heading level.
    if text.startswith("#"):
        text = "\\" + text
    return ("# " if block.zone is Zone.title else "## ") + text


def _table_md(table: Table) -> str:
    grid = table.grid()
    if not grid:
        return ""
    header, *body = grid
    lines = [
        "| " + " | ".join(_cell(c) for c in header) + " |",
        "|" + "|".join(" --- " for _ in header) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in body]
    return "\n".join(lines)


def _y_of(rect: tuple[int, int, int, int] | None) -> float:
    return float(rect[1]) if rect else float("inf")


def _page_elements(
    view: LayoutView, page: int
) -> list[tuple[str | None, str]]:
    """Every element of one page as ``(anchor, markdown)``, in emission order.

    Blocks keep provider order — the best reading order available (constraint 3 above).
    Tables, marks and key-values are spliced in by y-position: each lands before the first
    block that starts lower on the page than it does. Elements without geometry append at the
    end of the page rather than guessing a position.
    """
    blocks = [b for b in view.blocks if b.page == page and b.zone is not Zone.table]

    # (splice_y, sequence, anchor, markdown) — sequence keeps the sort stable so same-y
    # elements keep their relative order.
    spliced: list[tuple[float, int, str | None, str]] = []
    seq = 0

    for table in (t for t in view.tables if t.page == page):
        rect = _rect(table.bbox)
        md = _table_md(table)
        if md:
            tag = f"table {table.row_count}x{table.col_count}"
            spliced.append((_y_of(rect), seq, _anchor(page, rect, tag), md))
            seq += 1
    for mark in (m for m in view.marks if m.page == page):
        rect = _rect(mark.bbox)
        box = "[x]" if mark.state == "selected" else "[ ]"
        spliced.append((_y_of(rect), seq, _anchor(page, rect, "mark"), f"- {box}"))
        seq += 1
    for kv in (k for k in view.key_values if k.page == page):
        rect = _kv_rect(kv)
        md = f"**{_clean(kv.key)}:** {_clean(kv.value)}".rstrip()
        spliced.append((_y_of(rect), seq, _anchor(page, rect, "kv"), md))
        seq += 1
    spliced.sort(key=lambda item: (item[0], item[1]))

    out: list[tuple[str | None, str]] = []
    pending = iter(spliced)
    next_item = next(pending, None)
    for block in blocks:
        rect = _rect(block.bbox)
        block_y = _y_of(rect)
        while next_item is not None and next_item[0] <= block_y:
            out.append((next_item[2], next_item[3]))
            next_item = next(pending, None)
        if block.zone in (Zone.title, Zone.heading):
            tag = str(block.zone)
            md = _heading(block)
        elif block.zone is Zone.furniture:
            tag = f"furniture:{block.role}" if block.role else "furniture"
            md = _clean(block.text)
        else:
            tag = block.role or "p"
            md = _clean(block.text)
        if md:
            out.append((_anchor(page, rect, tag), md))
    while next_item is not None:
        out.append((next_item[2], next_item[3]))
        next_item = next(pending, None)
    return out


def to_pmd(
    view: LayoutView,
    *,
    source: str,
    provider: str,
    doc_id: str = "",
    generated: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """Render ``view`` as a PMD document. Deterministic for a given set of arguments.

    Args:
        view: The provider-neutral read of the document.
        source: What kind of input produced it — ``document`` | ``azure_read`` |
            ``azure_layout`` | ``des_ocr``.
        provider: The concrete reader, e.g. ``azure_layout_v4``, ``pymupdf``.
        doc_id: Caller's identifier, carried verbatim into the front matter.
        generated: ISO timestamp. Caller-supplied so the emitter stays a pure function —
            tests pass a constant, the API passes now().
        extra: Additional front-matter fields (e.g. the input's sha256). Keys are emitted
            sorted, so extras cannot break determinism.
    """
    pages = sorted({p.page for p in view.pages} | {b.page for b in view.blocks}
                   | {t.page for t in view.tables} | {m.page for m in view.marks}
                   | {k.page for k in view.key_values})
    info = {p.page: p for p in view.pages}

    chars = sum(len(b.text) for b in view.blocks)
    front = {
        "pmd": PMD_VERSION,
        "generator": GENERATOR,
        "source": source,
        "provider": provider,
        "doc_id": doc_id,
        "pages": len(pages),
        "blocks": len(view.blocks),
        "tables": len(view.tables),
        "marks": len(view.marks),
        "key_values": len(view.key_values),
        "chars": chars,
        "generated": generated,
    }
    for key in sorted(extra or {}):
        front[key] = (extra or {})[key]

    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in front.items() if v != "" and v is not None]
    lines += ["---", ""]

    for page in pages:
        pi = info.get(page)
        size = f" size={round(pi.width)}x{round(pi.height)} unit={pi.unit}" if pi and pi.width else ""
        lines.append(f"<!-- page {page}{size} -->")
        lines.append("")
        for anchor, md in _page_elements(view, page):
            if anchor:
                lines.append(anchor)
            lines.append(md)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["GENERATOR", "PMD_VERSION", "to_pmd"]
