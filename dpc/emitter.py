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
3. **Reading order is the provider's — except where the page is genuinely two-dimensional.**
   Azure emits paragraphs in reading order, which beats anything recomputable from rectangles
   by a naive y-sort (that interleaves columns line by line). So elements keep provider order
   and tables/marks/key-values are spliced in by y-position — the PMD 1.0 rule, still used
   verbatim for every *linear region*.

   But a linear stream cannot express side-by-side content, and pretending otherwise is not
   merely lossy: two columns flattened into one list read as SEQUENTIAL, so a consumer binds
   the right column's value to the left column's heading. PMD 2.0 therefore detects the bands
   of a page that hold genuinely parallel content and renders exactly those as a **canvas** —
   a space-padded monospace block inside a ```text fence, with the frame table on its anchor
   so it inverts back to page coordinates exactly. Everything else on the page stays ordinary
   markdown. The canvas fires only where the linear rendering was actively misrepresenting the
   page; a page that produces no canvas is emitted by the untouched linear path and is
   byte-identical to PMD 1.0. See ``docs/SPEC-PMD-2.md``.

   Geometry is only as good as the provider's. Azure reports PDF pages in INCHES, and PMD 1.0
   rounded those coordinates to integers — collapsing a US-Letter page onto an 8x11 grid, so
   distinct rows emitted identical rectangles. Inch pages now anchor in milli-inches under a
   declared ``scale=1000``; see :func:`page_scale`.
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

import math
import unicodedata
from collections.abc import Sequence
from typing import Any

from dpc.canvas import CANVAS_LEGEND_MAX, PageLayout, Segment, page_layout, segment
from dpc.geom import page_scale
from dpc.geom import rect_scale as _rect
from dpc.models import KeyValue, LayoutView, PageInfo, Table, TextBlock, Zone

PMD_VERSION = "1.0"
PMD_VERSION_LINEAR = "1.0"
PMD_VERSION_BAND = "2.0"
GENERATOR = "document-processor-convertor"


# ``page_scale`` and ``_rect`` were born here; SPEC-DOCTREE-1 R13 promoted them to
# ``dpc/geom.py`` (as ``page_scale``/``rect_scale``) so the doctree builder unions node
# bboxes with the exact rounding these anchors use — one implementation, or the 2.0/3.0
# anchor-equality gate quietly breaks. They are imported in the block above: ``page_scale``
# stays a public re-export and ``_rect`` keeps its historical private name, so every
# in-module caller and every existing test is untouched. An import-only change, proven
# byte-identical by ``tests/test_geom.py``.


def _kv_rect(kv: KeyValue, scale: int = 1) -> tuple[int, int, int, int] | None:
    """The union of a pair's key and value rectangles — the pair is one visual unit."""
    rects = [r for r in (_rect(kv.key_bbox, scale), _rect(kv.value_bbox, scale)) if r]
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


def _block_md(block: TextBlock) -> tuple[str, str]:
    """One block's markdown and its anchor tag."""
    if block.zone in (Zone.title, Zone.heading):
        return _heading(block), str(block.zone)
    if block.zone is Zone.furniture:
        tag = f"furniture:{block.role}" if block.role else "furniture"
        return _clean(block.text), tag
    return _clean(block.text), block.role or "p"


def _linear_elements(
    view: LayoutView,
    page: int,
    *,
    block_ixs: Sequence[int] | None = None,
    table_ixs: Sequence[int] | None = None,
    mark_ixs: Sequence[int] | None = None,
    kv_ixs: Sequence[int] | None = None,
    scale: int = 1,
) -> list[tuple[str | None, str]]:
    """Every element of one page as ``(anchor, markdown)``, in emission order.

    Blocks keep provider order — the best reading order available (constraint 3 above).
    Tables, marks and key-values are spliced in by y-position: each lands before the first
    block that starts lower on the page than it does. Elements without geometry append at the
    end of the page rather than guessing a position.

    ``None`` for every index list means the whole page, which is exactly the pre-2.0
    ``_page_elements``. There is only one implementation of the linear path, so a linear
    region inside a spatially-rendered page cannot drift away from the 1.0 bytes by accident.

    Args:
        block_ixs: Indices into ``view.blocks`` to include, or ``None`` for all on this page.
        table_ixs: As above for ``view.tables``.
        mark_ixs: As above for ``view.marks``.
        kv_ixs: As above for ``view.key_values``.
        scale: Anchor scale for this page — see :func:`page_scale`.
    """
    keep_b = None if block_ixs is None else set(block_ixs)
    keep_t = None if table_ixs is None else set(table_ixs)
    keep_m = None if mark_ixs is None else set(mark_ixs)
    keep_k = None if kv_ixs is None else set(kv_ixs)

    blocks = [
        b for i, b in enumerate(view.blocks)
        if b.page == page and b.zone is not Zone.table and (keep_b is None or i in keep_b)
    ]

    # A reader with no geometry can still state ORDER: when anything on the page carries a
    # provider sequence, the whole page is emitted in (seq, kind) order and the y-splice
    # below never runs. Found by two independent reviews of the first HTML/XLSX output —
    # with every element at y=inf, `inf <= inf` spliced all tables ahead of all text, so a
    # filing's financial statements rendered before its prose and a sheet's table before
    # its own name.
    tables = [
        t for i, t in enumerate(view.tables)
        if t.page == page and (keep_t is None or i in keep_t)
    ]
    marks = [
        m for i, m in enumerate(view.marks)
        if m.page == page and (keep_m is None or i in keep_m)
    ]
    kvs = [
        k for i, k in enumerate(view.key_values)
        if k.page == page and (keep_k is None or i in keep_k)
    ]

    seq_items: list[tuple[int, int, str | None, str]] = []
    any_seq = any(b.seq is not None for b in blocks) or any(t.seq is not None for t in tables)
    if any_seq:
        order = 0
        for block in blocks:
            md, tag = _block_md(block)
            if md:
                seq_items.append((block.seq if block.seq is not None else 10**9, order,
                                  _anchor(page, _rect(block.bbox, scale), tag), md))
            order += 1
        for table in tables:
            md = _table_md(table)
            if md:
                tag = f"table {table.row_count}x{table.col_count}"
                seq_items.append((table.seq if table.seq is not None else 10**9, order,
                                  _anchor(page, _rect(table.bbox, scale), tag), md))
            order += 1
        seq_items.sort(key=lambda item: (item[0], item[1]))
        return [(anchor, md) for _, _, anchor, md in seq_items]

    # (splice_y, sequence, anchor, markdown) — sequence keeps the sort stable so same-y
    # elements keep their relative order.
    spliced: list[tuple[float, int, str | None, str]] = []
    seq = 0

    for table in tables:
        rect = _rect(table.bbox, scale)
        md = _table_md(table)
        if md:
            tag = f"table {table.row_count}x{table.col_count}"
            spliced.append((_y_of(rect), seq, _anchor(page, rect, tag), md))
            seq += 1
    for mark in marks:
        rect = _rect(mark.bbox, scale)
        box = "[x]" if mark.state == "selected" else "[ ]"
        spliced.append((_y_of(rect), seq, _anchor(page, rect, "mark"), f"- {box}"))
        seq += 1
    for kv in kvs:
        rect = _kv_rect(kv, scale)
        md = f"**{_clean(kv.key)}:** {_clean(kv.value)}".rstrip()
        spliced.append((_y_of(rect), seq, _anchor(page, rect, "kv"), md))
        seq += 1
    spliced.sort(key=lambda item: (item[0], item[1]))

    out: list[tuple[str | None, str]] = []
    pending = iter(spliced)
    next_item = next(pending, None)
    for block in blocks:
        rect = _rect(block.bbox, scale)
        block_y = _y_of(rect)
        # Strictly-below only, and never for items with NO geometry (y=inf): those append
        # at the end of the page, exactly as the module docstring promises.
        while (
            next_item is not None
            and next_item[0] != float("inf")
            and next_item[0] <= block_y
        ):
            out.append((next_item[2], next_item[3]))
            next_item = next(pending, None)
        md, tag = _block_md(block)
        if md:
            out.append((_anchor(page, rect, tag), md))
    while next_item is not None:
        out.append((next_item[2], next_item[3]))
        next_item = next(pending, None)
    return out


def _page_elements(view: LayoutView, page: int) -> list[tuple[str | None, str]]:
    """PMD 1.0's whole-page linear rendering. Retained as the name the format spec uses."""
    return _linear_elements(view, page)


def _fence(rows: Sequence[str]) -> tuple[str, str]:
    """Opening and closing fences for a canvas block.

    A fence, not indentation: four leading spaces is an indented code block in CommonMark, and
    a canvas's leading spaces ARE its payload. Inside a fence the document's own ``#``, ``|``
    and ``*`` are literal, so a canvas reproduces the source characters more faithfully than a
    linear paragraph does.

    ``text`` as the info string so highlighters do not guess and ``grep '^```text'`` finds
    every canvas. When a row itself contains a run of three or more backticks the fence grows
    to one longer than the longest run — a pure function of the content, and lossless: canvas
    text is never mutated to protect its own fence.
    """
    longest = 0
    for row in rows:
        run = 0
        for char in row:
            run = run + 1 if char == "`" else 0
            longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return ticks + "text", ticks


def _mu_to_scale(value: int, scale: int) -> int:
    """A canvas milli-unit coordinate expressed in the page's anchor scale.

    :mod:`dpc.canvas` works entirely in milli-units of the page's own unit, while anchors are
    written at the page's ``scale`` (1000 for inches, 1 for points and pixels — see
    :func:`page_scale`). On an inch page the two coincide and this is the identity; on a point
    page it divides by 1000. Integer half-up throughout, because the whole point of the canvas
    pipeline is that no float arithmetic happens after :func:`dpc.canvas.mu`.
    """
    return (value * scale + 500) // 1000


def _scaled_rect(rect: tuple[int, int, int, int], scale: int) -> str:
    x0, y0, x1, y1 = (_mu_to_scale(v, scale) for v in rect)
    return f"{x0},{y0},{x1},{y1}"


def _canvas_anchor(page: int, seg: Segment, scale: int) -> str:
    """The self-contained anchor above one canvas segment.

    Carries everything needed to invert the canvas back to page coordinates *without* the rest
    of the file: the segment's own hull, its grid size, which segment it is, its row window,
    the em, and the frame table. ``frames`` is what makes x exactly invertible —
    ``x(col) = left_j + (col - col_start_j) * adv_j`` — and it is per-segment precisely so a
    chunker that cuts here does not orphan the payload.
    """
    frames = "|".join(
        f"{_mu_to_scale(f.x0, scale)}:{_mu_to_scale(f.x1, scale)}:{f.cells}:"
        f"{_mu_to_scale(f.adv, scale)}"
        for f in seg.frames
    )
    head = (
        f"<!-- @{page} [{_scaled_rect(seg.rect, scale)}] canvas {seg.cols}x{len(seg.rows)}"
        f" seg={seg.index}/{seg.total} rows={seg.row_lo}..{seg.row_hi}"
        f" em={_mu_to_scale(seg.em, scale)} frames={frames}"
    )
    if len(seg.legend) > CANVAS_LEGEND_MAX:
        tags = sorted({p.tag for p in seg.legend if p.tag})
        head += f" has={','.join(tags)}"
    return head + " -->"


def _legend_lines(page: int, seg: Segment, scale: int) -> list[str]:
    """One anchor per *tagged* atom in the segment, carrying page rect AND cell rect.

    Plain body lines get no entry — their position is visible in the payload. This is what
    replaces the ``#`` a column-level heading loses inside a canvas, and it is a better input
    to a structure-aware chunker than the ``#`` was: it states the heading's page rectangle
    *and* the cell it occupies, which a hash never did.
    """
    if len(seg.legend) > CANVAS_LEGEND_MAX:
        return []
    return [
        f"<!-- @{page} [{_scaled_rect(p.rect, scale)}] {p.tag}"
        f" cell=[{p.col0},{p.row},{p.col1},{p.row}] -->"
        for p in seg.legend
    ]


def _clean_row(row: str) -> str:
    """A canvas row, made safe to sit in the document. Leading spaces are the payload."""
    return row.replace("<!--", "<! --")


def _norm(text: str) -> str:
    """Whitespace-normalised text, for the already-on-canvas comparison."""
    return " ".join(text.split()).casefold()


def _kv_region_ix(kv: KeyValue, layout: PageLayout, scale: int) -> int | None:
    """Index of the region whose vertical extent contains this pair, or ``None``.

    Pairs are placed by the y-centre of their union rectangle, so a pair straddling a region
    boundary lands in exactly one region rather than both or neither.
    """
    del scale  # pairs are located in canvas milli-units, never in the anchor scale
    rect = _kv_rect(kv, 1000)
    if rect is None:
        return None
    centre = (rect[1] + rect[3]) // 2
    for ix, region in enumerate(layout.regions):
        if region.y0 <= centre <= region.y1:
            return ix
    return None


def _page_marker(page: int, info: PageInfo | None, scale: int) -> str:
    """The page break comment, with the ``scale`` clause when anchors are not in page units.

    ``scale=`` is omitted when it is 1, so a file with no ``scale=`` clause anywhere and no
    canvas is byte-identical to PMD 1.0.
    """
    size = ""
    if info and info.width:
        width = math.floor(info.width * scale + 0.5)
        height = math.floor(info.height * scale + 0.5)
        size = f" size={width}x{height} unit={info.unit}"
    tail = f" scale={scale}" if scale != 1 else ""
    return f"<!-- page {page}{size}{tail} -->"


def to_pmd(
    view: LayoutView,
    *,
    source: str,
    provider: str,
    doc_id: str = "",
    generated: str = "",
    extra: dict[str, Any] | None = None,
    layout: str = "band",
    rect_scale: str = "auto",
    tab_snap: bool = True,
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

    body: list[str] = []
    canvases = 0
    kv_in_canvas = 0
    used_scale = False

    for page in pages:
        pi = info.get(page)
        scale = page_scale(pi.unit if pi else "", rect_scale)
        used_scale = used_scale or scale != 1
        body.append(_page_marker(page, pi, scale))
        body.append("")

        plan = page_layout(view, page, tab_snap=tab_snap) if layout == "band" else None

        # The whole-page linear shortcut. `regions` is empty exactly when the spatial pass
        # produced no canvas, and then the page is rendered by the UNMODIFIED linear path over
        # the whole page — so every page that gains nothing from the spatial pass is
        # byte-identical to PMD 1.0 by construction, not by test.
        if plan is None or not plan.regions:
            for anchor, md in _linear_elements(view, page, scale=scale):
                if anchor:
                    body.append(anchor)
                body.append(md)
                body.append("")
            continue

        kv_by_region: dict[int, list[int]] = {}
        kv_floating: list[int] = []
        for kv_ix, kv in enumerate(view.key_values):
            if kv.page != page:
                continue
            region_ix = _kv_region_ix(kv, plan, scale)
            if region_ix is None:
                kv_floating.append(kv_ix)
            else:
                kv_by_region.setdefault(region_ix, []).append(kv_ix)

        for region_ix, region in enumerate(plan.regions):
            if region.kind == "linear":
                for anchor, md in _linear_elements(
                    view, page,
                    block_ixs=region.block_ixs, table_ixs=region.table_ixs,
                    mark_ixs=region.mark_ixs, kv_ixs=kv_by_region.get(region_ix, []),
                    scale=scale,
                ):
                    if anchor:
                        body.append(anchor)
                    body.append(md)
                    body.append("")
                continue

            canvases += 1
            for seg in segment(region, region.rows):
                body.append(_canvas_anchor(page, seg, scale))
                body += _legend_lines(page, seg, scale)
                open_fence, close_fence = _fence(seg.rows)
                body.append(open_fence)
                body += [_clean_row(row) for row in seg.rows]
                body.append(close_fence)
                body.append("")

            # A pair whose key AND value are already visible on the canvas is suppressed:
            # re-emitting it under the picture is near-duplicate text in a retrieval index and
            # reads as noise. A pair that genuinely ADDS text is never dropped.
            on_canvas = {_norm(a.text) for a in region.atoms}
            additive: list[int] = []
            for kv_ix in kv_by_region.get(region_ix, []):
                kv = view.key_values[kv_ix]
                covered = any(_norm(kv.key) in t for t in on_canvas) and any(
                    _norm(kv.value) in t for t in on_canvas
                )
                if covered:
                    kv_in_canvas += 1
                else:
                    additive.append(kv_ix)
            if additive:
                for anchor, md in _linear_elements(
                    view, page, block_ixs=[], table_ixs=[], mark_ixs=[],
                    kv_ixs=additive, scale=scale,
                ):
                    if anchor:
                        body.append(anchor)
                    body.append(md)
                    body.append("")

        # Anything the spatial pass could not place at all: no geometry, so no anchor and no
        # invented position — appended at the end of the page, exactly PMD 1.0's rule.
        trailing = _linear_elements(
            view, page,
            block_ixs=plan.floating_blocks, table_ixs=plan.floating_tables,
            mark_ixs=plan.floating_marks, kv_ixs=plan.floating_kvs + kv_floating,
            scale=scale,
        )
        for anchor, md in trailing:
            if anchor:
                body.append(anchor)
            body.append(md)
            body.append("")

    attached, total = (view.raw.get("_line_join") or [0, 0])[:2]
    chars = sum(len(b.text) for b in view.blocks)
    is_v2 = bool(canvases or used_scale)
    front: dict[str, Any] = {
        # A file is only 2.0 when it USES a 2.0 feature. This makes PMD 1.0 byte-identity a
        # structural property of the ~90% of documents that are not columnar, rather than
        # something a test has to keep discovering.
        "pmd": PMD_VERSION_BAND if is_v2 else PMD_VERSION_LINEAR,
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
    }
    # Every 2.0 field is emitted AFTER `chars` and ONLY on a 2.0 file, so a document that used
    # no 2.0 feature is byte-identical to PMD 1.0 — front matter included. That identity is
    # what lets a stored sha256 from before this change still verify.
    if is_v2:
        front["layout"] = (
            "linear" if layout != "band" else "band" if canvases else "linear-only"
        )
        if canvases:
            front["canvases"] = canvases
            # Only a canvas's bytes depend on the East-Asian-Width tables, so only a canvas
            # surrenders hash stability across a Python upgrade — and it does so visibly.
            front["unicode"] = unicodedata.unidata_version
        if kv_in_canvas:
            front["kv_in_canvas"] = kv_in_canvas
        if total:
            front["line_join"] = f"{attached}/{total}"
    front["generated"] = generated
    for key in sorted(extra or {}):
        front[key] = (extra or {})[key]

    lines = ["---"]
    lines += [f"{k}: {v}" for k, v in front.items() if v != "" and v is not None]
    lines += ["---", ""]
    lines += body
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "GENERATOR",
    "PMD_VERSION",
    "PMD_VERSION_BAND",
    "PMD_VERSION_LINEAR",
    "page_scale",
    "to_pmd",
]
