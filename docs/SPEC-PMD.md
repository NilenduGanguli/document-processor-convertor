# PMD 1.0 — Positional Markdown

**Status:** normative. This document specifies the output format of
`dpc.emitter.to_pmd`; the reference test vector lives in `tests/test_emitter.py`
(`GOLDEN`). The words MUST, MUST NOT, and MAY are used as in RFC 2119.

PMD is ordinary GitHub-flavoured markdown with one addition: an HTML-comment **anchor** on
the line before each element, carrying the page number and the bounding rectangle the element
came from:

```markdown
<!-- @2 [93,319,434,347] title -->
# UNITED STATES OF AMERICA
```

## 1. Design constraints (why the format is shaped this way)

These four constraints, in priority order, decided every rule below. They are restated here
normatively because a conforming producer that violates the rationale will eventually violate
a rule.

1. **It MUST read as plain markdown.** Downstream agents get headings, tables, checkboxes
   and paragraphs with zero parsing beyond what they already do. Anchors are HTML comments,
   which every markdown renderer hides and every LLM can be told to read or ignore.
2. **Position MUST survive chunking.** A geometry appendix at the end of the file, keyed by
   block id, dies the moment a RAG pipeline cuts the file into chunks — which is the first
   thing every pipeline does. An anchor on the line above its element travels with any chunk
   that contains the element. This is why the metadata is inline and why each anchor is
   self-contained (page + rectangle + tag, no references elsewhere).
3. **Reading order is the provider's; geometry is the truth.** Providers order text better
   than rectangles can reconstruct (a naive y-sort interleaves columns line by line).
   Elements therefore keep provider order, and geometry is carried in the anchors for anyone
   who needs more. See §6.
4. **Deterministic bytes.** Same view in, same file out, so a stored sha256 is meaningful
   and re-conversions are comparable. Everything volatile (timestamps) is caller-supplied.
   See §8.

## 2. Document structure

A PMD file is, in order:

1. A front-matter block (§3).
2. One blank line.
3. For each page, in ascending page number: a page marker (§4), a blank line, then the
   page's elements — each element being an optional anchor line (§5) immediately followed
   by its markdown rendering (§7), followed by one blank line.

The file MUST be UTF-8 and MUST end with exactly one trailing newline.

## 3. Front matter

Delimited by `---` lines. Each field is one `key: value` line. Fields appear in exactly this
order:

| Field | Meaning |
|---|---|
| `pmd` | Format version. This document specifies `1.0`. |
| `generator` | Producing software, `document-processor-convertor`. |
| `source` | Input kind: `document` \| `azure_read` \| `azure_layout` \| `des_ocr`. |
| `provider` | Concrete reader, e.g. `pymupdf`, `azure-prebuilt-layout`, `azure-read-v3.2`, `des-ocr`. |
| `doc_id` | Caller's identifier, carried verbatim. |
| `pages` | Count of distinct pages that appear in the body. |
| `blocks` | Count of text blocks in the source view — **including** `zone=table` blocks that §6.4 suppresses from the body. The counts describe the view, not the rendering. |
| `tables` | Count of tables. |
| `marks` | Count of selection marks. |
| `key_values` | Count of key/value pairs. |
| `chars` | Sum of text lengths over all blocks in the view. |
| `generated` | Caller-supplied ISO-8601 timestamp. |

Rules:

- A field whose value is the empty string or null MUST be omitted (e.g. `doc_id` when the
  caller supplied none).
- Caller-supplied extra fields (e.g. `sha256_input`) are appended after `generated`,
  **sorted by key**, so extras cannot perturb determinism.

## 4. Page marker

```
<!-- page <n> size=<W>x<H> unit=<unit> -->
```

- `<n>` is the 1-based page number. Pages appear in ascending numeric order. A page number
  is included if **any** element (block, table, mark, key/value) or page-info entry carries
  it.
- `size=<W>x<H> unit=<unit>` states the page's dimensions (`W`, `H` rounded to integers)
  and coordinate unit (`point`, `pixel`, `inch` — verbatim from the provider). The clause
  MUST be omitted when the page's dimensions are unknown; a made-up size is worse than an
  absent one.

## 5. Anchor grammar

```
anchor = "<!-- @" page " [" x0 "," y0 "," x1 "," y1 "] " tag " -->"
page   = 1*DIGIT                          ; 1-based page number
x0 y0 x1 y1 = ["-"] 1*DIGIT               ; integers, page units, origin top-left
tag    = see §5.2
```

### 5.1 Rectangle

- The rectangle is the **axis-aligned bounding box** of the element's source quadrilateral:
  `x0,y0` its top-left, `x1,y1` its bottom-right, in the coordinate unit named by the page
  marker, origin at the page's top-left.
- Coordinates MUST be rounded to integers. Sub-unit precision is provider noise, and stable
  integers are what make the output byte-deterministic across float-formatting differences.
- For a key/value pair the rectangle is the **union** of the key's and the value's
  rectangles — the pair is one visual unit (§7).

### 5.2 Tag vocabulary

| Tag | Element |
|---|---|
| `title` | Document title block |
| `heading` | Section heading block |
| `p` | Body block with no provider role |
| *verbatim role* | Body block with a provider role, e.g. `footnote`, `formulaBlock` — the role string is the tag, verbatim |
| `furniture` | Page furniture (header/footer/page number) with no recorded role |
| `furniture:<role>` | Page furniture with its provider role, e.g. `furniture:pageNumber` |
| `mark` | Selection mark (checkbox) |
| `kv` | Key/value pair |
| `table <R>x<C>` | Table with `R` rows and `C` columns |

Consumers MUST treat unknown tags as opaque body-level tags: the vocabulary grows with
provider roles.

### 5.3 The honesty rule: no bbox, no anchor

An element without source geometry MUST be emitted **without** an anchor line — its markdown
stands alone. An invented rectangle would be worse than an absent one, because a consumer
cannot tell measured from made-up. The absence of an anchor is itself information: "this
element's position was not measured."

## 6. Ordering

### 6.1 Blocks keep provider order

Within a page, text blocks appear in the order the provider emitted them (Azure: reading
order; PyMuPDF: document order) — **never** re-sorted by geometry. Provider order wins even
when it disagrees with y-position (a header block may sit above the title on the page yet
after it in the file).

### 6.2 Tables, marks and key/values are spliced by y-position

These come from separate provider arrays with no position in the block sequence, so they are
inserted by geometry: each is emitted immediately before the first block whose top edge `y`
is greater than or equal to the element's own top edge `y` (ties: spliced element first).
Elements that outlast every block land at the end of the page.

At equal `y`, spliced elements keep a stable relative order: tables, then marks, then
key/value pairs, each group in provider order.

### 6.3 Elements without geometry

A spliced element with no geometry sorts as `y = +∞`: it appears at the end of its page
(and, per §5.3, without an anchor). A *block* without geometry keeps its provider slot but
likewise flushes all still-pending spliced elements before it.

### 6.4 `zone=table` blocks are suppressed

A block whose zone is `table` MUST NOT be rendered. The adapters re-zone paragraphs that
overlap a detected table precisely so that their text is not emitted twice — once as prose,
once inside the GFM table. The table rendering is the single source for that text. (The
suppressed blocks still count in the front matter, which describes the source view.)

### 6.5 Empty renderings

A block or table whose rendering is empty (no text after cleaning, no grid) is dropped
entirely — no anchor, no blank element.

## 7. Element renderings

| Element | Rendering |
|---|---|
| `zone=title` block | `# <text>` |
| `zone=heading` block | `## <text>` |
| `zone=furniture` block | plain text (no markdown decoration) |
| body block | plain paragraph |
| Table | GFM table (§7.1) |
| Mark | `- [x]` if selected, `- [ ]` otherwise |
| KeyValue | `**<key>:** <value>` (trailing whitespace stripped when the value is empty) |

### 7.1 Tables

- Rendered as a GFM pipe table. Grid row 0 is the header row; the delimiter row uses
  `---` per column.
- The grid is dense, `row_count` × `col_count`. A spanning cell's text lands in its
  **top-left** grid position; every other position it covers is an empty cell. The span
  extent survives in the `table RxC` tag and in the source, not in the markdown.
- Cell text is escaped per §7.2.

### 7.2 Escaping rules

Escaping is minimal by design: only what could break **document structure** is touched.
Escaping every markdown metacharacter would destroy readability, which is the format's first
constraint (§1.1). Text is whitespace-trimmed and otherwise verbatim.

- **All text** — every occurrence of `<!--` becomes `<! --`. A literal comment opener would
  open an HTML comment and swallow everything to the next `-->`, including the file's own
  anchors.
- **Table cells** — each pipe character is backslash-escaped (an unescaped pipe grows a
  column), and each newline becomes `<br>` (a newline ends the GFM row).
- **Headings** — a leading `#` in the document's own text is backslash-escaped: a title like
  "#1 Priority Client" would otherwise silently deepen the heading level.

## 8. Determinism guarantee

`to_pmd` is a pure function: for equal inputs (view and keyword arguments) the output MUST
be **byte-identical**, across calls, processes and machines. The producer guarantees this
by construction:

- No clocks, no randomness, no environment reads. The `generated` timestamp is
  caller-supplied (tests pass a constant; the API passes now()).
- Extra front-matter fields are emitted sorted (§3).
- Rectangles and page sizes are rounded to integers (§5.1), eliminating float-formatting
  variance.
- Ordering is fully specified (§6), including tie-breaks.

Consequence: the sha256 of a PMD file identifies its content, a stored hash is meaningful,
and a re-conversion of the same input is comparable byte-for-byte.

## 9. Chunking property

Any contiguous slice of a PMD file that contains an element's rendering also contains its
anchor (the line immediately above), and every anchor is self-contained — page, rectangle,
tag, no reference to any other part of the file. Therefore position metadata survives
arbitrary chunking, the front matter being the only part that is lost when a chunk excludes
it. This property is the reason anchors are inline (§1.2) and MUST be preserved by any
future revision of the format.

---

## Superseded in part by PMD 2.0

`docs/SPEC-PMD-2.md` extends this format with **canvas segments** — space-padded monospace
blocks that preserve where text sat on the page — and corrects the anchor scale on inch-unit
pages. This document remains the specification for **linear regions**, which is what every
non-columnar page still is, byte for byte.

Two changes a 1.0 consumer must know about:

| Change | Effect on a 1.0 consumer |
|---|---|
| `<!-- page N … scale=1000 -->` on inch-unit pages, with anchors in milli-inches | Anchors on Azure-read PDFs changed, because they were **wrong**: rounding inch coordinates to integers put a whole page on an 8x11 grid, so distinct rows shared a rectangle and titles had zero height. Read the `scale=` clause and divide. `rect_scale="legacy"` reproduces the old numbers. |
| ` ```text ` canvas blocks with a `canvas …` anchor | Only on pages that produced one. §5.2 already required unknown anchor tags to be treated as opaque, so a 1.0 parser reads the anchor as ordinary and the fence as an ordinary code block. |

A file that used no 2.0 feature still declares `pmd: 1.0` and is byte-identical to what this
specification produced. That is structural — a page with no canvas is rendered by the
unmodified linear path — not a property maintained by testing.
