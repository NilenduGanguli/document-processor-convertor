# PMD 2.0 — Band Mode

**Status:** normative design, ready to implement. Supersedes `docs/SPEC-PMD.md` §4, §6 and
§8 and extends §3 and §5; every other section of PMD 1.0 stands unchanged and is still the
normative description of `layout="linear"` output.

**Ground truth read before writing this:** `dpc/emitter.py`, `dpc/models.py`,
`dpc/adapters.py`, `dpc/pdfread.py`, `dpc/api.py`, `dpc/config.py`, `dpc/ocr_client.py`,
`dpc/schema.sql`, `dpc/htmlread.py`, `dpc/xlsxread.py`, `docs/SPEC-PMD.md`,
`tests/test_emitter.py`, `tests/test_pdfread.py`, `tools/corpus_sweep.py`, `CONTRACTS.md`.

---

## 1. Decision summary

1. **Band mode.** A page decomposes into horizontal *bands*; contiguous runs of bands that
   share a genuine vertical whitespace corridor become one fenced, space-padded **canvas**;
   every other run stays native GFM — headings, pipe tables, checkboxes, anchors, all of it.
2. **The fence is a per-band decision, never a per-document one.** Four leading spaces is an
   indented code block in CommonMark, so padded text must be fenced; fencing a whole page
   would forfeit `#`, `|` and `- [x]` everywhere. Band mode pays the fence only in the bands
   where linear markdown was actively lying about the page.
3. **Lines place, paragraphs mean.** Azure's `pages[].lines[]` polygons drive geometry;
   `paragraphs[].role` drives semantics. Joined once, in the adapter, by span overlap.
4. **Tables are never inside a canvas.** A `Table` is always a band separator and always a
   GFM pipe table. GFM preserves cell identity, row/column indices and the `table RxC` tag;
   an ASCII grid preserves none of them.
5. **After `mu()`, the layout path performs zero floating-point operations.** Every
   threshold is an exact integer rational, every column index an integer floor division.
   Determinism is guaranteed by construction, not by testing.
6. **No local text extraction for the `document` source.** PDFs and images go to Azure DI
   `prebuilt-layout`. The PyMuPDF *text* path is deleted; PyMuPDF survives only as a
   validity pre-check and as the offline fixture synthesiser.
7. **Canvas segments are chunk-sized and self-describing.** A canvas is emitted in segments
   of at most 20 rows / 1400 characters, each preceded by its own complete anchor and its own
   legend of tagged atoms, so any RAG chunk that contains canvas text also contains the
   transform and the semantics for that text.
8. **Version rule:** `pmd: 1.0` — byte-identical to today — unless the file actually uses a
   2.0 feature (a canvas, or a `scale=` page-marker clause). Documents that gain nothing keep
   their stored sha256.
9. **Anchor rectangles are fixed.** `_rect()` rounds to integers *in the page unit*, and
   Azure returns `unit: "inch"` for every PDF, so a block at x ∈ [1.2, 3.4] anchors today as
   `[1,1,3,2]`. That is total loss, not rounding loss. Inch pages now anchor in milli-inches
   with `scale=1000` on the page marker.
10. **What was rejected:** a whole-page monospace canvas (fences the entire corpus, reserves
    GFM tables for the pathological case, interleaves columns character-by-character in every
    chunk); pure region reordering with no padding (fixes reading order, does not make the
    file *look* like the page, which is what was asked, and risks flipping label/value order
    on form pages); a two-cell GFM table per page (cells cannot contain newlines, so the page
    becomes one unsplittable row); Azure's own `outputContentFormat=markdown` (linearises,
    carries no column position, and replaces GFM tables with HTML).

---

## 2. Routing: every `document` input goes to Azure DI

The emitter is useless without line polygons, and only Azure DI produces them. This is a
`dpc/pdfread.py` change and it is a precondition, not an accessory.

### 2.1 The new `read_document`

```python
#: Provider names as the API reports them.
PROVIDER_AZURE_LAYOUT = "azure_layout"     # unchanged
PROVIDER_OPENPYXL = "openpyxl"             # unchanged
PROVIDER_HTMLREAD = "htmlread"             # unchanged
PROVIDER_PLAIN_TEXT = "plain-text"         # new, from adapters.from_plain_text
PROVIDER_PYMUPDF = "pymupdf"               # RETAINED AS A NAME ONLY — never returned
                                           # unless settings.allow_local_pdf_text is on.


def read_document(
    data: bytes, *, filename: str | None, settings: Settings
) -> tuple[LayoutView, str]:
    """Raw bytes -> (view, provider). Azure DI is the only text reader for renderable input.

    Routing, in order, decided by the BYTES (magic first, filename only as a tie-break):

    ==============================  ====================================================
    PDF, JPEG, PNG, BMP, TIFF, HEIF  Azure DI ``prebuilt-layout``, whole document, always.
    DOCX / PPTX / XLSX / HTML        ``settings.office_route`` (§2.2).
    TXT / CSV / MD / EML / MSG       ``settings.text_route`` (§2.4).
    anything else                    HTTP 415 ``unsupported_media_type``.
    ==============================  ====================================================

    There is no per-page scan test any more. ``settings.min_alnum_chars`` is retired: the
    question it answered ("is this page a scan, i.e. must it go to DI?") has one answer now.

    Raises:
        NeedsRecognition: no ``azure_di_endpoint`` is configured (API: 422). See §2.3.
        UnsupportedFormat: DI does not accept this format and no route is configured (415).
        ValueError: over ``max_bytes``, or a PDF that cannot be opened at all.
    """
```

PyMuPDF is still imported, for exactly two jobs, both of which are *pre-flight checks on
bytes* and neither of which extracts text:

* `_precheck_pdf(data)` — open, reject `needs_pass` (encrypted) and zero-page PDFs before
  spending an Azure call, and report `page_count` so `max_pages` can refuse early with a
  useful message. Azure charges per page; a 2100-page PDF should be refused locally.
* `tools/di_stub.py` (§2.5) — the offline fixture synthesiser. Test-time only.

`_page_blocks()` and the `sparse and page.get_images()` scan heuristic are **deleted**. They
are the "internal OCR" the requirement names, and their output — PyMuPDF block hulls — is
precisely the geometry that cannot be placed on a canvas (a hull spans many visual rows), so
keeping them would produce a corpus that is silently half spatial and half not.

An escape hatch exists and is off: `DPC_ALLOW_LOCAL_PDF_TEXT=false`. Set true and the old
PyMuPDF path returns, for air-gapped development. It is not a supported production mode and
the front matter says `provider: pymupdf`, so an artifact produced that way is identifiable.

### 2.2 (a) What happens to `openpyxl` and `htmlread`

Both stay, and they stay **on by default**. This is a deliberate, argued exception to
"everything goes to Azure", not an oversight.

Microsoft states twice, officially, that DOCX/XLSX/PPTX/HTML are **not rendered**, so DI
returns for them: no `polygon`, no `boundingRegions`, no `pages[].lines[]`, no `angle`, no
`width`/`height`/`unit`. Additionally, for XLSX specifically, "*Table analysis isn't
supported if the input file is XLSX*". So routing a spreadsheet to Azure would:

* gain nothing spatial — there is no geometry to gain, under any configuration; and
* **lose the tables**, which `dpc/xlsxread.py` produces today with correct row/column
  structure and `seq` ordering.

That is obeying the letter of the requirement while destroying the output. `read_html` is the
same trade one notch less severe: HTML through DI keeps its tables but loses the `<h1>`/bold-run
zoning that `htmlread` measured its way to, and Azure bills HTML at 3000 characters per "page",
so a `<!-- page N -->` marker on a DI-read HTML file is a claim about a billing slice, not
about paper.

Three routes, `DPC_OFFICE_ROUTE`:

| Value | Behaviour |
|---|---|
| `local` **(default)** | `xlsxread` / `htmlread` as today. DOCX/PPTX → 415 (no local reader exists). Output is `layout: linear-only`, `pmd: 1.0`, byte-identical to today. |
| `render` | Render to PDF with `DPC_RENDER_CMD` (headless LibreOffice), then the PDF route. This is the **only** way to get spatial fidelity on a DOCX, and it is a real one. Records `renderer: <version>` in the front matter, because the renderer version joins the determinism envelope (§7.4). |
| `azure` | Send the original bytes to DI. Honest but lossy; provided because the requirement asked for it. Logs `route.azure_office_no_geometry format=xlsx` at WARNING once per request. |

The gate that decides whether a page gets a canvas keys off `bbox is not None` and measured
page dimensions — **never off the provider name** (§4.2 step 2). So a DOCX that went to Azure
lands in `layout: linear-only` by measurement, and the file says so.

### 2.3 (b) `DPC_AZURE_DI_ENDPOINT` unset: refuse

**Refuse. 422 `needs_ocr`, using the existing `NeedsRecognition` exception and the existing
handler in `dpc/api.py`.** Falling back to PyMuPDF would be the worst available outcome, and
the reason is specific rather than aesthetic:

A PyMuPDF fallback produces a *perfectly valid PMD file*. It has pages, blocks, anchors, and
no canvases — because block hulls are multi-row atoms and §4.2 step 6 sends them to linear.
Nothing errors. Nothing warns. The user reports "the columns feature doesn't work on our
documents" and there is nothing in the artifact to look at. A silent degradation that is
indistinguishable from a correct answer is worse than a refusal a caller can read, and this
repo's stated posture is refuse-don't-guess. The refusal reason names configuration and
counts, never document text (KYC: error bodies travel).

```
422 {"error": "needs_ocr",
     "detail": "the document source requires Azure Document Intelligence (application/pdf,
                14 page(s)); no endpoint is configured (DPC_AZURE_DI_ENDPOINT)"}
```

The `azure_read_result`, `azure_analyze_result` and `des_ocr` sources are unaffected — they
supply their own payload and never need an endpoint.

### 2.4 (c) Formats DI will not accept

DI v4.0 (`2024-11-30`) accepts exactly: `application/pdf`, `image/jpeg`, `image/png`,
`image/tiff`, `image/bmp`, `image/heif`, `text/html`, and the three OOXML media types. It does
**not** accept `.txt`, `.csv`, `.md`, `.rtf`, `.eml`, `.msg`, `.json`, `.epub`, `.odt`. (A web
search will confidently say it does; that is Azure AI **Content Understanding**, a different
product with a different limits page. Do not let it into this codebase.)

`DPC_TEXT_ROUTE`:

| Value | Behaviour |
|---|---|
| `plain` **(default)** | `adapters.from_plain_text(data.decode(...))`, `provider: plain-text`, `layout: linear-only`, one block per paragraph with `seq` set and no bbox. Honest: no geometry claimed, ordering stated. `.eml`/`.msg` take their decoded text body only, and the response carries `"warning": "message envelope discarded"`. |
| `render` | Wrap to PDF (10 pt DejaVu Sans Mono, 1-inch margins, fixed) and take the PDF route. Deterministic given a pinned font; gives a canvas to a fixed-width text file, which is often exactly right for a `.txt` fixed-column report. |
| `refuse` | 415 `unsupported_media_type`, naming the format and the two remedies. |

Anything not in the accept list and not text-like is 415 regardless of `text_route`. `.gif` and
`.webp` are in `_EXTENSION_TYPES` today and are **not** in DI's accept list; they become 415
with a message naming the accepted image types. This is a behaviour change and is listed in
§8.

### 2.5 (d) Tests and the corpus sweep without Azure credentials

Three mechanisms, all of which exist today in embryo and are being formalised. **No unit test
opens a socket; no test requires a key.**

**(i) The client seam.** `dpc/pdfread.py` gains one indirection:

```python
def _ocr_client(settings: Settings) -> OcrClient:
    """The single construction site for the DI client — the seam tests replace.

    ``tests/test_pdfread.py`` already monkeypatches ``pdfread.OcrClient``; naming the seam
    makes that an intended contract rather than a fortunate accident, and lets the stub be
    injected without touching the module's public surface.
    """
    return OcrClient(settings)
```

`httpx.MockTransport` continues to serve every protocol-level test (202 → `Operation-Location`
→ poll → terminal, timeouts, poll caps), exactly as `tests/test_pdfread.py` does now.

**(ii) Recorded payloads.** `tests/fixtures/di/*.json` holds real, scrubbed `analyzeResult`
payloads — one two-column page, one ID-card page, one heading+table page, one CJK page, one
skewed scan, one Office payload with no geometry. Emitter and canvas tests call
`adapters.from_azure_layout(json.load(...))` directly. This is the *right* level for the
determinism contract: the guarantee is "same `analyzeResult` in → same bytes out", never
"same PDF in → same bytes out", because Microsoft does not commit to byte-stable output
across model refreshes within a pinned `api-version`.

**(iii) `tools/di_stub.py` — an offline DI endpoint.** A stdlib `http.server` implementing
`POST /documentintelligence/documentModels/prebuilt-layout:analyze` and the poll GET.

```
usage: python tools/di_stub.py [--port 5007] [--fixtures tests/fixtures/di]
```

* If `sha256(body)` matches `<fixtures>/<sha256>.json`, that payload is replayed **verbatim**.
  Replay is what makes the corpus sweep's determinism column meaningful.
* Otherwise it **synthesises** a DI-shaped `analyzeResult` from the bytes using PyMuPDF
  `get_text("rawdict")`: `pages[]` with `width`/`height` in **inches** and `unit: "inch"`,
  `pages[].lines[]` with real 8-point polygons converted from points (`pt / 72`), `words[]`,
  `paragraphs[]` merged from lines by leading and left-edge with `role` inferred from font
  size, `tables[]` empty, and a single top-level `content` string with consistent `spans`.
  The synthesiser is ~150 lines and lives in the stub, never in `dpc/`.
* `--record` writes the synthesised payload into the fixtures directory so a sweep is
  reproducible on the next run.

`docker-compose.yml` already points `DPC_AZURE_DI_ENDPOINT` at `http://host.docker.internal:5007`;
that is now the stub by default. `tools/corpus_sweep.py` gains `--di-stub` which starts and
stops it around the sweep, and a new graded column `canvas` (page produced ≥1 canvas) plus a
`reason` histogram (§9).

**This is why PyMuPDF stays a dependency.** It is no longer a reader; it is the thing that
lets a developer with no Azure key run the whole suite and the whole sweep. That inversion is
the point: the product path is Azure-only, the test path never calls Azure.

---

## 3. Model and adapter changes

### 3.1 `dpc/models.py` — one new class, one new field

```python
class TextLine(BaseModel):
    """One provider LINE inside a block — the PLACEMENT unit.

    Deliberately distinct from :class:`TextBlock`. Azure's own definition of a line is the
    column-preserving unit: content in the same horizontal plane but separated by more than a
    single visual space is split into separate lines, "which enables the representation of
    textual content split into multiple columns or cells". A line's rectangle is therefore a
    real 2-D box you can put on a grid.

    A PARAGRAPH's rectangle is the union hull of its lines. For wrapped text that hull spans
    many visual rows, so it states the column correctly and the row not at all. Paragraphs are
    the right unit for roles and the wrong unit for position.
    """

    text: str
    bbox: Quad | None = None


class TextBlock(BaseModel):
    ...
    #: Provider lines, in provider order, joined to this block by span overlap. EMPTY is the
    #: honest state for any reader with no line stream (Office/HTML, plain text, DES) —
    #: never synthesised by splitting ``text`` on newlines, because a synthetic line has no
    #: rectangle and an invented rectangle is worse than none.
    lines: list[TextLine] = Field(default_factory=list)
```

Both additive and defaulted: every stored payload, every fixture and DCE's copy of the model
deserialise unchanged.

### 3.2 `dpc/adapters.py` — `_attach_lines`

```python
def _attach_lines(blocks: list[TextBlock], result: dict[str, Any]) -> tuple[int, int]:
    """Attach ``pages[].lines[]`` geometry to paragraph blocks. Returns (attached, total).

    Both streams index into the same top-level ``content`` string, so the join is EXACT via
    ``_spans_overlap`` rather than geometric. Each line goes to the block with the smallest
    ``(first_span_offset, block_index)`` among those whose spans overlap it — a total order,
    so the join is deterministic and one-to-one, and cannot depend on payload array order
    (nothing in Microsoft's docs guarantees ``paragraphs[]`` is pre-sorted by span).

    A line that no block claims is DROPPED, not promoted: unclaimed lines are almost always
    table-cell content, which ``tables[]`` already carries, and promoting them would emit the
    text twice. Blocks with ``zone is Zone.table`` are skipped for the same reason (§6.4 of
    PMD 1.0 suppresses them from the body).

    The (attached, total) counts become the front matter's ``line_join`` field. That field
    exists to make the one failure this design cannot prevent LEGIBLE — see §9.3.
    """
```

Wired into `from_azure_layout` after `_map_blocks`, unconditionally (it is cheap and purely
additive). `from_azure_read` sets `lines=[TextLine(text=text, bbox=quad)]` when it has a quad,
because in Read v3.2 each block *is* a line — Read therefore gets full spatial support for
free.

---

## 4. The emitter: `dpc/canvas.py` + `dpc/emitter.py`

### 4.1 Units — everything is an integer

```python
def mu(value: float) -> int:
    """A page coordinate as an integer in milli-units of the page's own unit.

    ``math.floor(value * 1000.0 + 0.5)``. Half-up, explicitly: Python's ``round()`` is
    banker's rounding, and a 0.5 boundary that flips on a 1-ULP change in a polygon is the
    single most-cited determinism trap in the prior art (pdfplumber's ``round(x_dist)``).

    For inch pages this is milli-inches (0.072 pt of resolution); for pixel pages,
    milli-pixels. Azure's documented examples carry four decimal places and nothing
    guarantees more, so nothing below this quantum is signal.

    AFTER THIS CALL THE PIPELINE PERFORMS NO FLOATING-POINT ARITHMETIC. Every threshold below
    is an exact rational applied as an integer multiply-and-compare; every column index is
    integer floor division; integer ceiling is ``-(-a // b)``. There is no ``round()``, no
    ``math.ceil`` on a float, no ``sum()/len()``. That is the determinism guarantee, by
    construction rather than by convention.
    """
```

**Anchor rectangles.** The page marker gains a `scale` clause:

```
<!-- page 1 size=8500x11000 unit=inch scale=1000 -->
<!-- page 1 size=612x792 unit=point -->
```

`scale = 1000` when `unit == "inch"`, `1` otherwise. At `scale=1` the existing `_rect()` is
called verbatim, so point and pixel pages keep today's integers byte-for-byte. At
`scale=1000` every rectangle in the file — element anchors, canvas anchors, frame extents —
is the milli-unit integer. `DPC_PMD_RECT_SCALE=legacy` forces `scale=1` everywhere and
reproduces PMD 1.0 rounding for a caller who must regenerate a stored hash.

### 4.2 The per-page algorithm

Steps 1–11 run per page. Every step that can decline names a `reason`, and a declined page is
rendered by `_linear_elements(view, page)` on the **whole page**, which is `_page_elements`
verbatim — see the whole-page linear shortcut in step 11.

---

**Step 1 — atoms.**

```python
Kind = Literal["line", "mark", "table"]
KIND_RANK: dict[str, int] = {"line": 0, "mark": 1, "table": 2}

@dataclass(frozen=True, slots=True)
class Atom:
    """One placeable thing. All coordinates in milli-units, origin page top-left."""
    kind: Kind
    text: str            # line content, or "[x]"/"[ ]" for a mark, or "" for a table
    x0: int; y0: int; x1: int; y1: int
    skew_num: int        # top-edge dy, milli-units
    skew_den: int        # top-edge dx, milli-units (>= 1)
    source_ix: int       # index in view.blocks / view.marks / view.tables
    sub_ix: int          # line index within its block; 0 otherwise
    block_ix: int | None # owning block, for line atoms
    multiline: bool      # see below
    tag: str             # legend tag: "" for a plain body line
```

* A block with `zone is Zone.table` contributes nothing.
* A block with `lines` contributes **one `line` atom per line that has a bbox**, and
  **`multiline` is `False` for every one of them.** A provider line is one visual row by
  construction; that is the whole reason lines are the placement unit.
* A block with no lines but a bbox contributes one hull atom, with
  `multiline = (y1 - y0) * 5 > em * 8` (taller than 1.6 em, i.e. more than one visual row).
  `multiline` is a property of *this atom's own rectangle*, never inherited from its parent.
* Each `Mark` contributes a `mark` atom, `text = "[x]"` or `"[ ]"`.
  ASCII, not Azure's `☒`/`☐`: `U+2611` has East-Asian-Width `A` (Ambiguous) and renders as
  1 or 2 cells depending on the reader's locale — a cell-width ambiguity inside a grid whose
  entire purpose is cell alignment. `[x]` is 3 cells everywhere, and the single downstream
  regex `\[([x ])\]` matches both `- [x]` (linear) and `[x]` (canvas).
* Each `Table` contributes a `table` atom with `text = ""`. It is a separator; its markdown
  is the GFM table.
* Key/value pairs are **never** atoms — their text is text the lines already carry, so
  placing them would print it twice. They are handled in step 10.
* `skew_num/skew_den` come from the quad's own top edge: `(mu(q[3]) - mu(q[1]),
  max(mu(q[2]) - mu(q[0]), 1))`. **`page.angle` is never read.** Microsoft does not document
  whether polygons are in the raw or a deskew-corrected frame, so the polygon's own edge is
  the only trustworthy signal.
* `tag` follows PMD 1.0's §5.2 vocabulary: `title`, `heading`, `furniture[:role]`, the
  verbatim provider role, `mark`. A body line with no role has `tag = ""`.

Blocks and key/values with **no geometry at all** are collected separately as *floating*, and
handled in step 11.

**Step 2 — scale and the two gates.**

```python
def page_em(atoms) -> int:
    """The page's em, in milli-units: ``sorted(heights)[(n - 1) // 4]`` — the lower quartile.

    An exact index selection, never an interpolated percentile, because interpolation
    reintroduces a division whose result a 1-ULP change can move.

    Quartile rather than median because the atom population is mixed. On a line-atom page the
    heights cluster tightly and q25 ≈ median ≈ the true em. On a degraded hull-atom page the
    single-line atoms are always the shorter part of the distribution, and q25 sits inside
    them whenever at least a quarter of the blocks are single-line — true on every real page
    (headings, labels, list items, captions). One rule covers both populations, so there is no
    mode switch to get wrong.
    """
```

Decline to **linear-only** if any of:

| Condition | `reason` |
|---|---|
| no atoms, or every atom has no bbox | `no-geometry` |
| `page_em(atoms) <= 0` | `no-geometry` |
| `PageInfo.width <= 0` or `height <= 0` | `no-geometry` |
| `median(\|skew\|) > MAX_SKEW` over line atoms, i.e. `50 * abs(skew_num) > skew_den` | `skew` |
| `len(atoms) > MAX_ATOMS_PER_PAGE` | `too-dense` |

The geometry gate keys on **measured data**, never on the provider name.

**Step 3 — bands (frozen-seed sweep).**

Sort atoms by `(y0, x0, KIND_RANK[kind], source_ix, sub_ix)` — total, because
`(kind, source_ix, sub_ix)` is unique per atom. Sweep forward: the first unassigned atom seeds
a band and **fixes the test interval at `[seed.y0, seed.y1]`**. A later atom joins iff

```
2 * max(0, min(a.y1, s.y1) - max(a.y0, s.y0)) >= min(a.y1 - a.y0, s.y1 - s.y0)
```

The band's *reported* extent grows to the union; the *test* interval never does.

Freezing the seed is what makes this not single-linkage clustering. pdfplumber's
`cluster_list` compares each element to the **previous** one, so on a page whose line tops
drift by less than the tolerance — a half-degree skew, a table with uneven leading — the whole
page chains into one cluster. That is a step function of a continuous input and it is the
worst determinism hazard in the prior art. A frozen seed cannot creep: forty body lines yield
forty bands, at any leading.

A band that reaches `MAX_ATOMS_PER_BAND` stops accepting; overflow atoms seed the next band.
Exact duplicates within a band (same milli-unit rect **and** same text — the fake-bold/shadow
case poppler handles with `minDupBreakOverlap`) are dropped, first-in-sort-order kept.

Bands are emitted in seed order, which is ascending `y0`. There is no second sort.

**Step 4 — separators.** A band is a separator iff it contains a separator atom:

* **(a)** any `table` atom. Always. A GFM pipe table preserves cell identity, spans in the
  `RxC` tag, and every consumer's `^| ` grep; an ASCII grid preserves none of it. Making
  tables separators is what satisfies the "do not destroy GFM tables" constraint *by
  construction* rather than by argument.
* **(b)** any **non-multiline** line atom at least `FULLWIDTH_FRAC` of the content width:
  `10 * (a.x1 - a.x0) >= 9 * (content_x1 - content_x0)`.

The `multiline` exclusion in (b) matters: a justified paragraph hull frequently spans the full
measure, and if that counted as a separator every wrapped paragraph would shatter its own
region. A full-measure hull is a block's measure, not a page divider. (Line atoms are never
multiline, so on the Azure path (b) reduces to "a full-measure single line", which is exactly
a headline or a rule — the correct reading.)

**Step 5 — candidate regions.**

1. Each separator band is its own singleton **linear** region.
2. Maximal runs of consecutive non-separator bands are candidates.
3. A candidate containing any `multiline` atom is **linear**. A hull spanning five visual rows
   cannot be put on one canvas row without either printing it five times or squashing it;
   refusing degrades to exactly PMD 1.0, which is the honest answer.
4. **The coverage gate.** A candidate is linear unless, for every block owning a line atom in
   it, *all* of that block's line atoms are in this candidate **and** the whitespace-normalised
   concatenation of those line texts equals the whitespace-normalised `block.text`.
   Normalisation collapses every Unicode whitespace run to one space and strips.
   This is what makes the information-loss test pass **by construction**: a block is rendered
   in exactly one place, and a block rendered on a canvas has every character of its text on
   that canvas. A block whose lines do not reconstruct it (a span-join partial failure, a
   provider oddity) falls back to linear, where `block.text` is emitted whole.

**Step 6 — gutters.** For each surviving candidate, with `K` bands:

1. Bucket width `b = max(em // BUCKETS_PER_EM, 1)`; bucket index of x is
   `(x - content_x0) // b`. Integer floor division throughout.
2. `occ[i]` = number of **bands** with at least one atom covering bucket `i`. Counting bands,
   not atoms, is what makes one wide element unable to veto a corridor that twenty other bands
   respect — it removes the need for a separate cross-layout pre-mask pass, and it is
   non-circular (no gutter is needed to compute it).
3. **Blocker budget, absolute:** bucket `i` is CLEAR iff `occ[i] <= MAX_BLOCKING_BANDS` **and**
   `K - occ[i] >= MIN_GUTTER_ROWS`.
4. A gutter is a maximal run of clear buckets that (i) does not touch bucket 0 or the last
   bucket — edge whitespace is a margin, and a margin is not a corridor — and (ii) is at least
   1.5 em wide: `2 * run_width_mu >= 3 * em`.
5. If `K < MIN_GUTTER_ROWS`, return no gutters.

A candidate with ≥1 gutter becomes **spatial**; with none, **linear**.

> **Resolved disagreement.** An earlier draft used a proportional budget
> `allowed = max(1, K // 10)`. That is a threshold that is a function of corpus size rather
> than of the thing being measured — the exact defect class recorded in
> `dce-classification-lessons.md` — and it made a 30-row two-column table of contents keep its
> columns while the same content split across a page break lost them on the short half. The
> budget is now **absolute** (`MAX_BLOCKING_BANDS = 3`, poppler's straggler cap read literally)
> with an absolute floor of clear evidence (`K - occ >= 4`). Same layout, same answer,
> independent of how many rows it happens to have.

**Step 7 — frames.** The `m` gutters cut the candidate into `m+1` frames, left to right. An
atom belongs to the frame containing its **x-centre** (centres, never edges, so an atom that
slightly overhangs a gutter lands in exactly one frame). For frame `j`:

```
adv(L)  = (L.x1 - L.x0) // cell_width(L.text)            # milli-units per cell
adv_j   = lower median of adv(L) over line atoms with cell_width(L.text) >= MIN_MEASURE_CELLS
          (or em // 2 when fewer than one qualifies)
off(L)  = (L.x0 - frame.x0) // adv_j
cells_j = max( -(-(frame.x1 - frame.x0) // adv_j),
               max over L of ( off(L) + cell_width(L.text) ) )
col_start_0     = 0
col_start_{j+1} = col_start_j + cells_j + GUTTER_CELLS
```

`adv_j` is the frame's own **measured** character advance, so a frame of 8 pt values and a
frame of 14 pt labels each get their own correct density. pdfplumber uses one global
`x_density = 7.25 pt` (the advance of 12 pt Courier, unrelated to the document) and its
documented consequence is drift. poppler derives a block's width from its own longest line and
ignores intra-block x offsets, so an indent inside a column is lost and one long line pushes
every neighbouring column right for the whole page. LLMWhisperer ships a
`horizontal_stretch_factor` knob precisely so a human can hand-patch this per document; here
it is measured per frame, automatically.

The `max`-with-required-extent term in `cells_j` is what makes intra-frame overflow
**impossible by construction**: the frame is defined to be as wide as its own widest actual
placement, so no atom can be pushed past its own frame's end.

**Step 7b — tab snapping.** Within each frame, collect the multiset of `off(L)` over its line
and mark atoms. Candidate stops are the distinct values sorted by `(-count, off)`; accept
greedily, skipping any candidate within `TAB_SNAP` of an already-accepted stop. Then each atom
whose `off` is within `TAB_SNAP` of an accepted stop snaps to it (ties → the lower stop).

This is Tesseract's tab-stop idea reduced to an integer pass, and it fixes the single most
eyeball-salient defect of any padded renderer: a numeric column that aligns on its right edge
lands ±1 cell apart when two values of different length round their left edges differently.
Snapping cannot create an overlap, because step 9's cursor rule still enforces at least one
space between neighbours. It is the first thing to disable (`DPC_CANVAS_TAB_SNAP=false`) if a
fidelity regression appears.

**Step 8 — block ownership.** Each block is owned by the region containing its first line atom
in sort order; its line atoms appearing in any other region are dropped from that region.
Reachable only when a `Table` interleaves a paragraph's y-range — the coverage gate (step 5.4)
then sends the owning region to linear anyway, so no text is lost.

**Step 9 — canvas rows.**

```python
def cell_width(text: str) -> int:
    """Display cells the text occupies in a monospace canvas.

    0 for combining marks and format characters (``unicodedata.combining(ch)`` non-zero, or
    category in Mn/Me/Cf); 2 for East-Asian-Width W and F; 1 otherwise. Ambiguous (A) counts
    as 1, matching every ``wcwidth`` narrow default and what a Western monospace face renders.

    poppler is measurably WRONG here: ``col[]`` counts one column per code point under UTF-8
    (``TextOutputDev.cc:1265-1273``), so every CJK block under-counts its own width by about
    half and everything to its right is under-padded; its legacy byte-counting branch gets
    Shift-JIS right by accident. Azure returns CJK and this is a KYC corpus, so this is not
    hypothetical.

    A pure function of the string given a Unicode version. That version is pinned into the
    front matter (``unicode:``) whenever a canvas exists, so a Python upgrade that moves the
    East-Asian-Width tables shows up as a visible field change rather than a mystery hash
    drift.
    """
```

Vertical: `gap_rows(k) = min(MAX_ROW_GAP, max(0, (band_k.y0 - band_{k-1}.y1) // em))` blank
rows before each band. poppler's clamp, floor 0 rather than 1 because a row is already emitted
per band.

Horizontal, per band, atoms in `(frame_ix, true_col, x0, KIND_RANK, source_ix, sub_ix)` order:

```
true_col = frame.col_start + snapped_off(a)                      # LTR
true_col = colpos(a.x1) - cell_width(a.text)                     # RTL, clamped to frame
placed   = max(true_col, cursor + 1) if cursor > 0 else true_col
if placed - true_col > MAX_DRIFT:
    start a new row; cursor = frame.col_start; placed = max(true_col, cursor)
row     += " " * (placed - cursor) + a.text
cursor   = placed + cell_width(a.text)
```

The overflow policy is poppler's break-the-line, then one step better. pdfplumber's
`max(min(1, line_len), round(x_dist) - line_len)` never goes backwards, so the first over-long
token shifts every remaining word on that line right, permanently and silently — unbounded
damage. poppler breaks the row and resets the cursor to column 0, which is bounded but throws
away the columnar structure for the rest of the row. Resetting to **the overflowing atom's own
frame start** means the atom still lands in its true column and the rows above and below still
align: bounded damage *and* preserved columns, at most one extra row per overflowing atom.

**RTL:** logical order, no bidi control characters. poppler emits visual order bracketed with
`U+202B`/`U+202A`/`U+202C`, which makes `len(str)` differ from display width and means any
consumer that normalises the string changes the rendering — and poppler's own source comments
admit its numeral handling is wrong. Azure returns line `content` in logical order, so logical
order is the zero-transformation choice, and zero transformation is the determinism-safe
choice. The one thing that must be handled is the anchor point: an RTL line's `x0` is its
visual end, so an RTL line is placed by its right edge. `is_rtl(text)` is a strict majority of
the strong-directional characters being R or AL.

Every row is `rstrip()`ed. **Leading spaces are never stripped — they are the payload.**

**Step 10 — segments.** A spatial region's rows are cut into segments at **band boundaries**
so that each segment is at most `CANVAS_SEG_ROWS` rows and `CANVAS_SEG_CHARS` characters. A
single band that alone exceeds the cap becomes its own oversized segment rather than being cut
mid-row. Each segment re-uses the region's frames (so columns line up across segments) and
carries its own complete anchor and legend (§5.3).

`CANVAS_SEG_CHARS = 1400` ≈ 350 tokens at the ~4 chars/token English ratio. Sizing to the
*largest tolerable* chunk window is the wrong optimisation: a 4000-character canvas cut by a
512-token chunker leaves the second half with no anchor, no frame table and no `em` — an
orphaned block of space-padded text no consumer can invert. 1400 fits inside a 512-token
window with room for the anchor and the legend, which is the smallest window in common use.

**Step 11 — emission order, and the whole-page linear shortcut.**

> **If a page produced zero spatial regions, the page is emitted by
> `_linear_elements(view, page)` over the WHOLE page, and the region decomposition is
> discarded.**

This is a structural guarantee, not a test result: every page without a canvas is byte-identical
to PMD 1.0 (subject only to the `scale` clause of §4.1). It removes the region-walk's
reordering risk from the ~90% of pages that gain nothing from it.

For a page that *did* produce a canvas, regions are emitted in band order:

* **linear region** → `_linear_elements(view, page, block_ixs=…, table_ixs=…, mark_ixs=…,
  kv_ixs=…)`, the same function restricted to that region's indices. Provider block order,
  y-splice of tables/marks/key-values, the empty-rendering drop, the `zone=table` suppression:
  all of PMD 1.0 §6, unmodified, inside every linear region.
  Blocks are rendered from `block.text`, **never** re-joined from `block.lines` — the text of
  record is the provider's paragraph content, so a linear region's bytes do not depend on
  whether lines were attached at all.
* **spatial region** → the segments of §5.3.
* Key/value pairs whose union rect falls inside a spatial region are emitted after its last
  segment **only when they add text**: a pair whose normalised key text *and* value text both
  already appear in the region's atom texts is suppressed, and the suppressed count goes in
  the front matter as `kv_in_canvas`. Emitting `**Full legal name:** Alastair J. Whitcombe`
  directly under a canvas that already shows it in place is near-duplicate text in the
  retrieval index and reads as noise under the picture. A pair that genuinely adds text (a
  value Azure resolved from a checkbox, a key that is an image label) is emitted.
* **Floating** blocks and key/values (no geometry at all) append to the page's last linear
  region in provider order; if the last region is spatial, they form a trailing linear region.
  This reproduces PMD 1.0's "no geometry → end of page, no anchor" rule.

**Complexity.** One `O(n log n)` sort; `O(n)` sweep; `O(n + K·B)` for the profile with
`B ≤ 4 · content_width / em` (≈250 buckets on a Letter page); `O(n)` placement bounded by
`MAX_ATOMS_PER_BAND`. Every loop is bounded by a named constant.

### 4.3 Constants — every one sourced

```python
BUCKETS_PER_EM    = 4          # profile resolution
MIN_GUTTER_EM     = (3, 2)     # 1.5 em, exact rational
MIN_GUTTER_ROWS   = 4
MAX_BLOCKING_BANDS= 3
FULLWIDTH_FRAC    = (9, 10)    # 0.90
BAND_OVERLAP      = (1, 2)     # 0.50
SINGLE_ROW_EM     = (8, 5)     # 1.60 em
MIN_MEASURE_CELLS = 4
FALLBACK_ADV      = (1, 2)     # em / 2
GUTTER_CELLS      = 3
TAB_SNAP          = 1
MAX_ROW_GAP       = 5
MAX_DRIFT         = 2
MAX_SKEW          = (1, 50)    # |tan θ| <= 0.02
MAX_ATOMS_PER_BAND= 64
MAX_ATOMS_PER_PAGE= 4000
CANVAS_SEG_ROWS   = 20
CANVAS_SEG_CHARS  = 1400
CANVAS_LEGEND_MAX = 12
```

| Constant | Where the number comes from |
|---|---|
| `MIN_GUTTER_EM = 1.5` | poppler's `maxWordSpacing = 1.5 em` is the **largest gap that can still sit inside one line**. A gutter floor there is provably wider than any intra-line word gap, so a gutter can never bisect a line. (poppler's own `minColSpacing2 = 1.0 em` is the looser of the two constants it ships; 1.5 costs nothing here because Azure lines already never straddle a wide gap.) |
| `MIN_GUTTER_ROWS = 4` | poppler `TextOutputDev.cc:3181,3228` — `if (n > 0 && n <= 3)` absorbs left/right stragglers into the neighbouring block. Four is the first count poppler treats as a genuine column. Inverted verbatim. |
| `MAX_BLOCKING_BANDS = 3` | The same straggler cap, applied to the corridor instead of the block: up to three rows may cross a corridor and it is still a corridor; four make it a body. Absolute, so it does not vary with region size. |
| `FULLWIDTH_FRAC = 0.90` | A line at 90% of the content width leaves ≤10%. On a 6.5-in measure that is 0.65 in ≈ 4.6 em at 10 pt — about five characters, which is not a column. This is XY-Cut++'s cross-layout mask expressed against the measured content extent, which removes its `β` parameter. |
| `BAND_OVERLAP = 0.50` | pdfminer.six `LAParams.line_overlap = 0.5`, which is exactly this quantity ("if two characters have more overlap vertically than this, they are considered to be on the same line"). Below half, the shorter box is more outside the seed row than inside it. |
| `SINGLE_ROW_EM = 1.60` | One line's polygon is ≈1.0–1.3 em tall; two single-spaced lines are ≥2.0 em. 1.6 is the midpoint; anything in (1.35, 1.9) classifies real data identically. |
| `MIN_MEASURE_CELLS = 4` | poppler refuses to trust a measured gap unless a line has more than one word (`TextLine::coalesce`: `if (words->len() > 1 …) minSpace = 0`). A 1–3 character line's polygon hugs two or three glyphs and its implied advance is not representative. |
| `FALLBACK_ADV = em/2` | The standard average-character-width figure for proportional type. Cross-checks: Courier's advance is 0.60 em; pdfplumber's `DEFAULT_X_DENSITY = 7.25 pt` at 12 pt is also 0.60 em. 0.5 is the conservative (denser) end, which errs toward *more* cells — more resolution, never overlap, because `cells_j` takes the max with the required extent. |
| `GUTTER_CELLS = 3` | poppler's column assignment, verbatim: `col2 = blk1->col + blk1->nColumns + 3`. One space reads as a word gap, two as a sentence gap; three is the shortest run that reads unambiguously as a column separator in a monospace face. |
| `TAB_SNAP = 1` | One cell. Two would let a genuine one-character indent collapse into its neighbour's stop. |
| `MAX_ROW_GAP = 5` | poppler `d = clamp((base_next − base) / fontSize, 1, 5)` in `TextPage::dump`. Its rationale — a half-empty page must not become forty blank lines — applies unchanged. |
| `MAX_DRIFT = 2` | Strictly less than `GUTTER_CELLS = 3`, so an atom nudged right to clear its neighbour can never be pushed into the next frame's territory. A causal bound, not a tuned one. |
| `MAX_SKEW = 0.02` | tan(1.15°). Over a 6.5-in content width a 1.15° skew drifts the baseline 0.13 in ≈ one line height — precisely the skew at which band membership becomes wrong by one row across the page. Derived from the failure. (poppler's `diagonalThreshold = 0.1` is 10× looser because it decides whether to *discard* text, not whether it can be banded.) |
| `MAX_ATOMS_PER_BAND = 64` | 64 atoms on one visual row at ≥3 cells each is ≥250 cells of content. Past that the row is noise, not a layout. Also bounds the intra-band placement loop. |
| `MAX_ATOMS_PER_PAGE = 4000` | Azure returns ~60–120 lines for a dense A4 page. 4000 is 30×, i.e. pathological. |
| `CANVAS_SEG_ROWS / CHARS = 20 / 1400` | ≈350 tokens: fits inside the smallest chunk window in common use (512) with room for the anchor and legend. |
| `CANVAS_LEGEND_MAX = 12` | Beyond twelve tagged atoms in a 20-row segment the legend costs more lines than the canvas it describes; the segment falls back to a `has=` tag list. |

### 4.4 Module layout and signatures

**`dpc/canvas.py`** (new, ~470 lines) — geometry only. Emits no markdown and imports nothing
from `emitter.py`; the dependency runs one way.

```python
"""LayoutView geometry -> horizontal bands, column frames, and canvas rows.

Everything here is an integer in milli-units of the page's own unit, so one set of thresholds
serves an inch-unit PDF, a pixel-unit 300-DPI scan and a 72-dpi image with no unit switch
anywhere. The conversion happens once, in :func:`mu`, and never again.
"""

def mu(value: float) -> int: ...
def cell_width(text: str) -> int: ...
def is_rtl(text: str) -> bool: ...

def atoms_for_page(view: LayoutView, page: int) -> tuple[list[Atom], list[int], list[int]]:
    """(atoms, floating block indices, floating key/value indices) for one page."""

def page_em(atoms: Sequence[Atom]) -> int: ...
def page_skew_ok(atoms: Sequence[Atom]) -> bool: ...
def build_bands(atoms: Sequence[Atom], em: int) -> list[Band]: ...
def mark_separators(bands: list[Band], x0: int, x1: int) -> list[Band]: ...
def find_gutters(bands, em, x0, x1) -> list[tuple[int, int]]: ...
def build_frames(gutters, x0, x1, bands, em) -> list[Frame]: ...
def snap_tabs(frames: list[Frame], bands: Sequence[Band]) -> dict[tuple[int, int], int]:
    """(source_ix, sub_ix) -> snapped column offset. Empty when tab snapping is off."""
def build_regions(bands, em, x0, x1, blocks: Sequence[TextBlock]) -> list[Region]:
    """Bands -> ordered regions, including the multiline gate and the coverage gate."""
def render_canvas(region: Region, snap: dict) -> list[str]:
    """A spatial region as canvas rows: no fence, no trailing spaces, one row per band."""
def segment(region: Region, rows: list[str]) -> list[Segment]: ...

def page_layout(view: LayoutView, page: int, *, tab_snap: bool = True) -> PageLayout:
    """Steps 1-10 for one page. NEVER raises: every failure returns a PageLayout whose
    ``regions`` is empty and whose ``reason`` is populated, which the emitter treats as
    'emit this page exactly as PMD 1.0 would have'."""
```

**`dpc/emitter.py`** (changed):

```python
PMD_VERSION_LINEAR = "1.0"
PMD_VERSION_BAND = "2.0"

def _linear_elements(
    view: LayoutView, page: int, *,
    block_ixs: Sequence[int] | None = None,
    table_ixs: Sequence[int] | None = None,
    mark_ixs: Sequence[int] | None = None,
    kv_ixs: Sequence[int] | None = None,
    scale: int = 1,
) -> list[tuple[str | None, str]]:
    """PMD 1.0's page rendering, restricted to a subset of the view.

    ``None`` for every index list is the whole page, which is EXACTLY the pre-2.0
    ``_page_elements``. There is only one implementation of the linear path, so it cannot
    drift away from the 1.0 bytes by accident.
    """

def _page_elements(view: LayoutView, page: int) -> list[tuple[str | None, str]]:
    return _linear_elements(view, page)

def _canvas_anchor(page: int, seg: Segment, scale: int) -> str: ...
def _legend_lines(page: int, seg: Segment, scale: int) -> list[str]: ...
def _fence(rows: Sequence[str]) -> tuple[str, str]: ...
def _page_marker(page: int, info: PageInfo | None, scale: int) -> str: ...

def to_pmd(
    view: LayoutView, *, source: str, provider: str,
    doc_id: str = "", generated: str = "", extra: dict[str, Any] | None = None,
    layout: str = "band", rect_scale: str = "auto", tab_snap: bool = True,
) -> str:
    """Render ``view`` as a PMD document. Deterministic for a given set of arguments.

    Args:
        layout: ``"band"`` (default) runs the spatial pass; ``"linear"`` skips it entirely and
            reproduces PMD 1.0. ``layout="linear", rect_scale="legacy"`` is byte-identical to
            pre-2.0 output, forever, and is the escape hatch for a caller regenerating a
            stored sha256.
        rect_scale: ``"auto"`` uses milli-units on inch pages (§4.1); ``"legacy"`` pins PMD
            1.0's integer-in-page-unit rounding on every page.
        tab_snap: Frame-local tab-stop snapping (§4.2 step 7b).
    """
```

---

## 5. Output format

### 5.1 Page marker

```
<!-- page 1 size=8500x11000 unit=inch scale=1000 -->
<!-- page 1 size=612x792 unit=point -->
<!-- page 1 -->
```

`scale=<n>` is present only when `n != 1`; a file with no `scale=` clause anywhere and no
canvas is a PMD 1.0 file. `size=…` is still omitted when the page's dimensions are unknown.

### 5.2 Front matter

```
---
pmd: 2.0
generator: document-processor-convertor
source: document
provider: azure_layout
layout: band
doc_id: NPB-2026-004417
pages: 2
blocks: 118
tables: 1
marks: 6
key_values: 9
chars: 4182
canvases: 3
kv_in_canvas: 7
line_join: 412/430
unicode: 15.1.0
renderer: libreoffice-24.8.4.2
generated: 2026-08-25T00:00:00Z
sha256_input: deadbeef
---
```

New fields, placed after `chars` so PMD 1.0's field order is preserved for the fields it
shares. Empty/None fields are still omitted; caller extras still append sorted after
`generated`.

| Field | Rule |
|---|---|
| `layout` | `band` when ≥1 canvas was emitted; `linear-only` when the spatial pass ran and found nothing; `linear` when `layout="linear"` was requested. A caller can tell from the header alone whether they got position. |
| `canvases` | Count of spatial regions. Band mode only. |
| `kv_in_canvas` | Key/value pairs suppressed as already-on-canvas (§4.2 step 11). Emitted only when non-zero, so nothing is silently dropped. |
| `line_join` | `<attached>/<total>` from `_attach_lines`. Band mode only, omitted when `0/0`. See §9.3 — this field exists to make the one failure this design cannot prevent legible. |
| `unicode` | `unicodedata.unidata_version`. Emitted **only when `canvases > 0`**, because only a canvas's bytes depend on the East-Asian-Width tables. A document with no canvas keeps hash stability across Python upgrades; one with a canvas surrenders it *visibly*. |
| `renderer` | Office/text → PDF renderer version. Only when a render step ran. |

### 5.3 A canvas segment

````
<!-- @7 [720,2180,7780,3660] canvas 99x11 seg=1/2 rows=0..10 em=139 frames=720:4080:48:70|4430:7780:48:70 -->
<!-- @7 [720,2180,3010,2320] heading cell=[0,0,19,0] -->
<!-- @7 [4430,2180,6180,2320] heading cell=[51,0,75,0] -->
```text
…canvas rows…
```
````

Anchor grammar (an extension of PMD 1.0 §5, not a new syntax — §5.2 already requires consumers
to treat unknown tags as opaque, so a 1.0 parser reads this as an ordinary anchor):

```
canvas-anchor = "<!-- @" page " [" rect "] canvas " cols "x" rows
                " seg=" i "/" n " rows=" lo ".." hi " em=" em " frames=" frames
                [" has=" tags] " -->"
frame         = left ":" right ":" cells ":" adv          ; frames joined by "|"
legend-line   = "<!-- @" page " [" rect "] " tag " cell=[" c0 "," r0 "," c1 "," r1 "] -->"
```

* `rect` is the hull of the segment's own bands, in scaled units. Measured, never derived from
  a threshold.
* `frames` makes the canvas **invertible in x, exactly**:
  `col_start_j = Σ_{i<j}(cells_i + 3)` and `x(col) = left_j + (col − col_start_j) · adv_j`.
* `rows=lo..hi` are region-relative row indices. **y is invertible to segment granularity**
  via the segment rect, not per row — rows are bands with variable gaps, and the design does
  not claim a linear `y = origin + row·pitch` it cannot honour. `DPC_CANVAS_ROW_Y=true` adds
  a `ys=` clause listing each row's `y0`, making y exactly invertible at a cost of ~7% of
  segment bytes; it is off by default.
* The **legend** carries one line per *tagged* atom in the segment (`title`, `heading`,
  `furniture[:role]`, verbatim provider role, `mark`) with both its page rectangle and its
  cell rectangle. Plain body lines get no legend entry — their position is visible in the
  payload. When a segment has more than `CANVAS_LEGEND_MAX` tagged atoms, the legend is
  replaced by a `has=` clause on the canvas anchor listing the distinct tags, sorted.
  The legend is scoped to the *segment*, so it travels with the chunk that contains the text
  it describes.
* Fence: ` ```text `. A fence because four leading spaces is an indented code block in
  CommonMark and internal space runs collapse in HTML rendering; the fence is what makes the
  canvas render verbatim and monospace, which is exactly its semantics. `text` as the info
  string so highlighters do not guess and so `grep '^```text'` finds every canvas. If any row
  contains a run of *n* ≥ 3 backticks, the fence is *n*+1 backticks — a pure function of the
  content, and **lossless**; no canvas text is ever mutated to protect the fence.
* Canvas text is otherwise cleaned exactly as PMD 1.0 cleans body text: `<!--` → `<! --`, and
  nothing else. Inside a fence `#`, `|`, `*`, `[` are literal, so a canvas reproduces the
  document's own characters more faithfully than a linear paragraph does.

### 5.4 What is traded away, and the argument

* **GFM tables: nothing is traded.** Tables are always separators, always pipe tables, always
  tagged `table RxC`. `_table_md` is not touched.
* **Checkboxes: nothing is traded, and the canvas is strictly better.** `\[([x ])\]` matches
  both modes; in a canvas the box sits next to its label, which a linear `- [x]` on its own
  line cannot express.
* **Headings: document-level ones survive; column-level ones lose their `#`.** A full-measure
  heading is a separator band and renders `#`/`##` in a linear region exactly as today. A
  `sectionHeading` *inside* a multi-column region is a **column** heading, and emitting it as
  `##` in a linear stream is precisely the lie PMD 1.0 tells — it makes a left-column
  subheading appear to govern the right column's text too, which is worse for chunk-boundary
  detection than not marking it. **A heading only loses its hash when it was never a document
  heading.** Its rect, its page, its row and its column survive on the segment legend, which
  is a *better* input to a structure-aware chunker than a `#` was.
* **Token cost: near zero on most pages.** In §6's examples the canvases are 11, 10, 7 and 5
  rows; the rest of each page is ordinary prose with ordinary anchors. Compare a whole-page
  canvas, which costs ~1.6× on every page.

---

## 6. Worked output

All three examples are Azure `prebuilt-layout` on US-Letter PDFs, so `unit: inch` and
`scale=1000`. `em = 139` (10 pt = 0.139 in), content extent x ∈ [720, 7780] mu. The two-column
regions have one gutter at 4080–4430 mu (350 mu = 2.5 em), giving frames `[720,4080]` and
`[4430,7780]`, both with measured `adv = 70` mu, `cells = 48` each, `col_start_1 = 51`.
Every canvas row below was produced by the placement rules in §4.2 and is ≤ 99 cells.

### 6.1 A two-column policy page

````markdown
---
pmd: 2.0
generator: document-processor-convertor
source: document
provider: azure_layout
layout: band
doc_id: GP-114
pages: 41
blocks: 508
tables: 6
marks: 0
key_values: 0
chars: 96104
canvases: 38
line_join: 1962/1974
unicode: 15.1.0
generated: 2026-08-25T00:00:00Z
sha256_input: 9c2a1f7e
---

<!-- page 7 size=8500x11000 unit=inch scale=1000 -->

<!-- @7 [720,620,7780,760] furniture:pageHeader -->
MERIDIAN TRUST BANK PLC — Group Policy GP-114 rev 4.2

<!-- @7 [720,980,7780,1310] title -->
# CUSTOMER DUE DILIGENCE STANDARD

<!-- @7 [720,2180,7780,3660] canvas 99x11 seg=1/2 rows=0..10 em=139 frames=720:4080:48:70|4430:7780:48:70 -->
<!-- @7 [720,2180,3010,2320] heading cell=[0,0,19,0] -->
<!-- @7 [4430,2180,6180,2320] heading cell=[51,0,75,0] -->
```text
1. PURPOSE AND SCOPE                               3. ENHANCED DUE DILIGENCE

This Policy sets out the minimum standards         Enhanced due diligence is mandatory where
that all business units of Meridian Trust          any of the following applies:
Bank plc must apply when establishing and
maintaining a customer relationship. It              (a) the customer or a beneficial owner
applies to every entity consolidated into                is a politically exposed person;
the Group's regulatory reporting perimeter.          (b) the customer is established in a
                                                         high-risk third country listed in
Where local law imposes a higher standard                the Annex to Regulation 2016/1675;
than this Policy, local law prevails and             (c) the relationship is conducted at a
```

<!-- @7 [720,3730,7780,5070] canvas 99x10 seg=2/2 rows=11..20 em=139 frames=720:4080:48:70|4430:7780:48:70 -->
<!-- @7 [720,4150,3130,4290] heading cell=[0,3,24,3] -->
<!-- @7 [4430,4710,6350,4850] heading cell=[51,7,71,7] -->
```text
the divergence must be recorded in the                   distance with no reliable electronic
Country Deviation Register within 30 days.               identification.

2. CUSTOMER DUE DILIGENCE                          Approval for an EDD relationship rests
                                                   with the Money Laundering Reporting
Standard due diligence is the default for          Officer and may not be delegated below
all customers. Simplified due diligence may        Head of Compliance.
be applied only where the customer is listed
in Annex A and no risk factor in Annex C is        4. ONGOING MONITORING
present.
```

<!-- @7 [720,10240,7780,10380] furniture:pageFooter -->
Uncontrolled when printed. — Page 7 of 41
````

Read what happened. The title and the page furniture are full-measure single lines, so they
are separator bands and render as ordinary markdown with ordinary anchors — `# CUSTOMER DUE
DILIGENCE STANDARD` is still a real `#` heading and still a chunk boundary. The two columns are
one canvas, and section 3 sits at column 51 because on the paper it sits at 4430 mu and
`(4430 − 4430)/70 + 51 = 51`. Section 4 is visibly *below* section 3 and not after section 2 —
which is exactly the fact PMD 1.0 destroys. The sub-list indentation of (a)–(c) is measured,
not invented: those lines really do start further right. Both segments carry the full frame
table, so a chunk holding only the second fence can still recover the x of every character in
it, and the legend tells it that row 3 column 0 is a heading.

### 6.2 An ID-card-like page with scattered fields

Four frames: label/value on the left, label/value on the right. The corridors between label
and value are real corridors and the algorithm finds them; nothing about it is
two-column-specific. The MRZ at the foot is a full-measure single line — a separator band —
and lands in a linear region, correctly, because it is not part of any column structure.

````markdown
<!-- page 1 size=4921x3465 unit=inch scale=1000 -->

<!-- @1 [300,240,4620,420] title -->
# REPUBLIC OF INDIA — PASSPORT

<!-- @1 [1560,700,4620,2620] canvas 99x7 seg=1/1 rows=0..6 em=139 frames=1560:2900:20:67|2960:4060:26:67|4180:5380:18:67|5440:7100:27:67 -->
<!-- @1 [1560,700,2100,840] p cell=[0,0,4,0] -->
```text
Type                 P                             Code                 IND
Passport No.         Z4419087                      Country code         IND
Surname              WHITCOMBE                     Given names          ALASTAIR JAMES
Nationality          INDIAN                        Sex                  M
Date of birth        14/03/1986                    Place of birth       MUMBAI, MAHARASHTRA
Date of issue        02/06/2024                    Place of issue       MUMBAI
Date of expiry       01/06/2034                    File number          MH1102411870924
```

<!-- @1 [1560,760,2100,900] kv -->
**Passport No.:** Z4419087

<!-- @1 [300,2980,4620,3120] p -->
P<INDWHITCOMBE<<ALASTAIR<JAMES<<<<<<<<<<<<<<<

<!-- @1 [300,3140,4620,3280] p -->
Z44190875IND8603144M3406018<<<<<<<<<<<<<<<<04
````

Notes. The `Passport No.` key/value pair *is* emitted, because Azure resolved a value whose
normalised text is not identical to a canvas atom's; the other six pairs on this page are
suppressed and counted in `kv_in_canvas`, because both their key and value text are already
on the canvas verbatim. The two MRZ lines are separator bands (full-measure) and render as
plain paragraphs, so the MRZ stays greppable as two contiguous lines — which is what every
MRZ parser downstream wants and what an interleaved canvas would have broken. Row gaps are
zero because the fields are single-spaced; there is no invented whitespace anywhere.

### 6.3 A page with a heading, a GFM table, and a two-up sign-off

````markdown
<!-- page 2 size=8500x11000 unit=inch scale=1000 -->

<!-- @2 [720,620,7780,760] furniture:pageHeader -->
Northbridge Private Bank — Application Ref NPB-2026-004417

<!-- @2 [720,980,7780,1160] heading -->
## 4. Source of Wealth and Expected Account Activity

<!-- @2 [720,1300,7780,1690] p -->
Declare every material source of the funds and assets you expect to place with the Bank. Amounts are in GBP unless stated. Supporting documentation is required for every row marked "Evidence: required".

<!-- @2 [720,1880,7780,4020] table 6x4 -->
| Source | Approximate value | Frequency | Evidence |
| --- | --- | --- | --- |
| Salaried employment (Radiography, NHS Trust) | 68,400 p.a. | Monthly | Not required |
| Self-employed practice income (Whitcombe Architects Ltd) | 214,000 p.a. | Quarterly | Required — filed accounts |
| Sale of residential property, 4 Fenwick Rise | 745,000 | One-off, Mar 2026 | Required — completion statement |
| Inheritance, estate of J. M. Whitcombe | 310,000 | One-off, Nov 2024 | Required — grant of probate |
| Dividend income, Whitcombe Holdings Ltd | 42,000 p.a. | Annual | Required — dividend vouchers |

<!-- @2 [720,4260,7780,4440] p -->
Expected first-year credit turnover: 1,380,000. Expected number of outward payments per month: 12–18.

<!-- @2 [720,4680,7780,4860] heading -->
## 5. Sign-off

<!-- @2 [720,5020,7780,5720] canvas 99x5 seg=1/1 rows=0..4 em=139 frames=720:4080:48:70|4430:7780:48:70 -->
```text
Prepared by                                        Reviewed by

H. Okonkwo                                         S. Raghavan
Financial Crime Operations                         Head of Compliance, India
12 May 2026                                        13 May 2026
```

<!-- @2 [720,10240,7780,10380] furniture:pageFooter -->
NPB-AOF-9.2 (05/26) · Page 2 of 2
````

The table is a six-row GFM pipe table with its `table 6x4` tag and its rectangle, identical to
what PMD 1.0 produces today; it is also a separator band, so it cleanly divides the prose above
from the sign-off below. Both `##` headings are full-measure and survive as real headings.
The sign-off block — genuinely side by side, genuinely meaningless in a linear stream
("Prepared by / Reviewed by / H. Okonkwo / S. Raghavan / …") — is one five-row canvas. Total
2.0-specific overhead on this page: two comment lines and two fence lines.

---

## 7. Determinism

The contract is unchanged and it is a contract over the **`LayoutView`**, not over the input
document: equal view + equal keyword arguments ⇒ byte-identical output, across calls,
processes and machines. Microsoft does not commit to byte-stable `analyzeResult` output across
model refreshes within a pinned `api-version`, so "same PDF ⇒ same sha256" was never a
guarantee anyone could give; `sha256_input` on the conversion row anchors the chain at the
stored payload, which is where it can be anchored. §2.5(ii) is how that becomes testable.

### 7.1 Every sort key and tiebreaker

| Site | Key | Total? |
|---|---|---|
| Pages | `sorted({int})` | yes, ints unique |
| Atoms (band input) | `(y0, x0, KIND_RANK[kind], source_ix, sub_ix)` | yes — `(kind, source_ix, sub_ix)` is unique per atom |
| Bands | none — seed order from a single forward sweep | n/a |
| Duplicate drop within a band | first in the atom sort order | yes |
| Band → canvas atoms | `(frame_ix, true_col, x0, KIND_RANK, source_ix, sub_ix)` | yes, same final tiebreakers |
| Gutter runs | none — left-to-right scan of an integer array | n/a |
| Frames | none — left-to-right from the gutter list | n/a |
| Tab-stop candidates | `(-count, off)` | yes — `off` unique among candidates |
| Regions | none — band order | n/a |
| Segments | none — band order within a region | n/a |
| `adv_j` sample | `sorted(adv_values)`, lower median at `(n-1)//2` | ints; equal values interchangeable |
| `em` | `sorted(heights)`, index `(n-1)//4` | ints |
| Line → block join | `(first_span_offset, block_index)` | yes — index unique |
| Linear region blocks | provider order = `view.blocks` index | yes |
| Linear region splices | `(y0, splice_seq)` where `splice_seq` counts up in the fixed order tables → marks → key/values, each in list order | yes — PMD 1.0 §6.2 verbatim |
| Post-canvas key/values | `(y0, kv_index)` | yes |
| Segment legend | `(row, col, tag, source_ix, sub_ix)` | yes |
| `has=` tag list | `sorted(distinct tags)` | yes |
| Front-matter extras | `sorted(keys)` | yes |

Every key ends in a field that cannot tie for two distinct items, so `list.sort` never falls
back on arrival order. `test_input_permutation_invariance` (§8) is the direct proof.

### 7.2 Structural guarantees

* **No floating point after `mu()`.** Every threshold is an exact rational applied as an
  integer multiply-and-compare (`2*overlap >= min_h`, `10*w >= 9*W`, `2*run >= 3*em`,
  `50*|dy| <= dx`, `(y1-y0)*5 > em*8`). Every column index is `//`; every ceiling is
  `-(-a // b)`. There is no `round()`, no `math.ceil` on a float, no `sum()/len()`.
* **`mu()` is `floor(v*1000 + 0.5)`** — half-up, explicit, never Python's banker's `round()`.
* **No set iteration.** `set` appears only inside `sorted(...)`.
* **No dict iteration order dependence.** Every grouping map is keyed by int/str and is read
  through `sorted(keys)` or an explicit index list.
* **No clocks, no environment, no randomness.** `generated` stays caller-supplied;
  `dpc/api.py` already passes nothing.
* **Canvas rows are `rstrip()`ed**, so trailing whitespace cannot vary.
* `unicodedata` is the one external table, and it is pinned into the artifact (`unicode:`)
  whenever a canvas exists.

### 7.3 What determinism does not cover

Two envelope members are new in 2.0 and both are published in the front matter so a hash change
is always explainable:

* `unicode` — `cell_width` is a pure function of the string **and** the CPython Unicode
  database. A Python minor bump that ships a new UCD can change one code point's width, one
  column, one file's hash. The container must pin the Python patch version as tightly as it
  pins the DI `api-version`.
* `renderer` — with `DPC_OFFICE_ROUTE=render`, LibreOffice's reflow decisions are part of the
  geometry Azure sees. A patch release that moves a paragraph by one line moves the canvas.
  Pin the renderer image.

Neither applies to a document with no canvas and no render step, which is why both fields are
conditional.

---

## 8. Config surface

Every new setting, `DPC_` prefixed, on the existing flat `Settings` object.

| Env var | Type / default | Why that default |
|---|---|---|
| `DPC_PMD_LAYOUT` | `str` = `"band"` | The feature is the point. `"linear"` is the escape hatch, not the default; a default that never runs the new path is a feature nobody sees. |
| `DPC_PMD_RECT_SCALE` | `str` = `"auto"` | `auto` fixes a live data-loss bug (inch pages anchor as an 8×11 integer grid today). `legacy` exists so a caller can regenerate a stored hash exactly. |
| `DPC_CANVAS_TAB_SNAP` | `bool` = `true` | Fixes ±1-cell jitter on right-aligned numeric columns, the most eyeball-salient defect of any padded renderer. First thing to disable if a fidelity regression appears. |
| `DPC_CANVAS_SEG_ROWS` | `int` = `20` | ≈350 tokens with `SEG_CHARS`; fits the smallest chunk window in common use. Deployments with a larger chunker may raise it; none should need to lower it. |
| `DPC_CANVAS_SEG_CHARS` | `int` = `1400` | Same arithmetic, from the other side. Whichever binds first cuts the segment. |
| `DPC_CANVAS_ROW_Y` | `bool` = `false` | Adds a `ys=` clause making y exactly invertible per row, at ~7% of segment bytes and a much uglier anchor. Off because segment-granularity y is enough for citation and highlighting. |
| `DPC_CANVAS_EMIT_KV` | `str` = `"additive"` | `additive` (suppress pairs whose key and value text are already on the canvas), `always` (PMD 1.0 behaviour), `never`. `additive` because a second copy under the picture is near-duplicate retrieval text, and because a pair that adds text must never be dropped. |
| `DPC_OFFICE_ROUTE` | `str` = `"local"` | DI returns no geometry for Office/HTML and **no tables at all for XLSX**; routing there would obey the letter of "everything to Azure" while destroying the output. `render` is the honest way to get spatial fidelity on a DOCX; `azure` exists for a deployment that wants the letter. |
| `DPC_TEXT_ROUTE` | `str` = `"plain"` | DI does not accept `.txt`/`.eml`/`.msg`. `plain` keeps them convertible and honest (`layout: linear-only`, no invented geometry) rather than 415-ing a format the service handled yesterday. |
| `DPC_RENDER_CMD` | `str` = `""` | Empty because the base image has no LibreOffice. Setting it is an explicit deployment choice with an explicit determinism cost (§7.3). |
| `DPC_RENDER_VERSION` | `str` = `""` | Stamped at container build; becomes the front matter's `renderer`. Empty means no render route is available. |
| `DPC_ALLOW_LOCAL_PDF_TEXT` | `bool` = `false` | The PyMuPDF text path. False is the requirement. True is for air-gapped development only and is visible in `provider: pymupdf`. |
| `DPC_DI_FIXTURE_DIR` | `str` = `""` | Replay directory for `tools/di_stub.py`. Empty in production; set by the sweep and by compose. |

**Retired:** `DPC_MIN_ALNUM_CHARS`. Its question ("is this page a scan, i.e. must it go to
DI?") now has one answer. Left in `Settings` as a deprecated no-op for one release so an
existing `.env` does not fail validation, with a WARNING on startup if set.

`ConvertRequest` gains `layout: str | None`, `rect_scale: str | None`, defaulting to the
settings. Invalid values are a 400, never a silent fallback. The response and the
`conversions` row gain `layout`, `pmd_version`, `canvases`.

Migration `013_pmd2.sql`:

```sql
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS pmd_version text NOT NULL DEFAULT '1.0';
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS layout      text NOT NULL DEFAULT 'linear';
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS canvases    int  NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS conversions_dedupe
    ON conversions (sha256_input, layout, pmd_version);
```

Every existing row back-fills correctly by the defaults, because every existing row *is*
linear PMD 1.0.

---

## 9. Test plan

### 9.1 Determinism

`tests/test_canvas_determinism.py`:

* **`test_input_permutation_invariance`** — for every fixture: shuffle `view.blocks`,
  `view.tables`, `view.marks`, `view.key_values` and each `block.lines` with a seeded
  `random.Random(n)` for n in 0..49, re-render, `assert out == baseline`.
  *This is the only test that actually proves the sort keys in §7.1 are total.* It is cheap and
  it is the one to run first after any change to `canvas.py`.
  (Caveat stated honestly: shuffling `blocks` also changes `source_ix`, which is a legitimate
  tiebreaker. The test therefore shuffles a *permutation-with-index-repair* view — the lists are
  reordered and every `source_ix`-carrying reference is remapped — so it tests key totality,
  not index invariance.)
* **`test_float_tripwire`** — perturb every coordinate in every fixture by ±1e-9 in a
  deterministic sign pattern; assert byte-identical output. Passes trivially today because
  `mu()` quantises at 1e-3. It exists to fire on the day someone puts a float back into the
  layout path.
* **`test_byte_determinism_across_instances`** — render a fresh `build_view()` twice and a
  `model_validate(model_dump())` round trip; all three identical. (Extends the existing test.)
* **`test_no_wall_clock`** — render with `freezegun` at two different instants; identical.
* **`test_unicode_field_present_iff_canvas`** — `("unicode:" in out) == (canvases > 0)`.

### 9.2 Information loss

`tests/test_canvas_lossless.py`. `norm(s)` collapses Unicode whitespace runs to one space and
strips.

* **`test_no_block_text_disappears`** — for every fixture and both layouts: for each
  `block` with `zone is not Zone.table` and non-empty text, assert `norm(block.text)` is a
  substring of `norm(strip_anchors(out))` **or** that every one of its lines' `norm(text)` is.
  The two-branch form is exact: a block is rendered either whole (linear) or as its lines
  (canvas), and the coverage gate (§4.2 step 5.4) guarantees those are the only two cases.
* **`test_no_block_text_duplicates`** — for each such block, `norm(out).count(norm(block.text))
  <= 1` unless the document genuinely repeats the string. Guards against a block being rendered
  in both a canvas and a linear region.
* **`test_every_table_cell_survives`** — every non-empty `cell.text` appears in the output;
  every table appears exactly once, as a pipe table.
* **`test_every_mark_survives`** — `out.count("[x]") + out.count("- [x]")` matches the selected
  mark count per page.
* **`test_kv_suppression_is_additive_only`** — for every suppressed pair, both `norm(key)` and
  `norm(value)` appear in the canvas rows of its region; `kv_in_canvas` equals the count.
* **`test_alnum_conservation`** — total alphanumeric characters in `strip_anchors(out)` ≥ total
  alphanumeric characters in `view.text()` minus the `zone=table` blocks. The coarse net that
  catches a whole region going missing.

### 9.3 Two-column fidelity

`tests/test_canvas_fidelity.py`, against `tests/fixtures/di/two_column.json` (a real recorded
payload) and a synthetic generator:

* **`test_two_column_page_produces_one_canvas`** — `canvases == 1`, `layout: band`, exactly two
  frames in the anchor. **The headline test: if this fails, the feature does not work.**
* **`test_columns_do_not_interleave`** — for every canvas row, the substring left of the gutter
  column contains only left-column source text and the substring right of it only right-column
  source text. Asserted by tagging each source line and checking membership, not by eyeball.
* **`test_left_column_starts_at_column_zero`** and
  **`test_right_column_starts_at_its_frame`** — every right-column atom's start column is
  `>= col_start_1`, and at least 80% are exactly `col_start_1` (ragged-right prose).
* **`test_no_row_exceeds_canvas_width`** — `cell_width(row) <= cols` for every row of every
  segment. Proves `cells_j`'s max-with-required-extent term.
* **`test_gutter_is_at_least_three_spaces`** — for every row with content in both frames, the
  run of spaces between them is ≥ `GUTTER_CELLS`.
* **`test_frames_invert_to_source_x`** — for every legend entry, `left_j + (c0 − col_start_j)
  · adv_j` is within one `adv_j` of the entry's rect `x0`. Both directions.
* **`test_segment_anchor_is_self_contained`** — every `seg=` anchor carries `frames=`, `em=`,
  `rows=` and a rect; no segment refers to another.
* **`test_segment_fits_a_512_token_chunk`** — every segment's anchor + legend + fence + rows
  is ≤ `CANVAS_SEG_CHARS + 400` characters.
* **`test_id_card_four_frames`** — the ID-card fixture yields four frames and seven rows with
  no blank rows.
* **`test_table_page_keeps_gfm`** — the heading+table fixture yields `^## `, `^| ` and
  `table 6x4` unchanged, and `canvases == 1` for the sign-off block only.
* **`test_cjk_widths`** — a full-width CJK fixture: no row exceeds `cols`, and the right frame
  starts at the same column on every row.
* **`test_rtl_placed_by_right_edge`** — an Arabic fixture: right-column atoms end at their
  frame's right edge, and no `U+202A`–`U+202E` appears anywhere in the output.

### 9.4 Fallback and honesty

* **`test_no_geometry_is_linear_only`** — an Office-shaped payload (no polygons) yields
  `layout: linear-only`, `canvases: 0`, no `unicode:` field, no fence, and output identical to
  `layout="linear"`.
* **`test_skewed_page_declines`** — a synthetic 3° skew yields `linear-only`.
* **`test_dense_page_declines`** — 4001 atoms yields `linear-only`, in bounded time.
* **`test_wide_table_untouched`** — a 40-column table renders as a 40-column pipe table.
* **`test_line_join_field_reports_failure`** — a payload whose `paragraphs[].spans` are
  deliberately shifted yields `line_join: 0/430` and `canvases: 0`, and the *field is present*.

### 9.5 Existing suite

`tests/test_emitter.py` is **not edited**. Traced by hand against `build_view()`:
atom heights in mu are `[12000, 18000, 18000, 30000, 30000, 30000, 40000, 60000]`, so
`em = sorted[(8-1)//4] = 18000`; the multiline threshold is `h*5 > em*8`, i.e. `h > 28800`, so
the title (40000), heading (30000) and both bodies (30000) are multiline hull atoms; the table
is a separator; every candidate region therefore contains a multiline atom and goes linear;
`canvases == 0`; `unit == "point"` so `scale == 1` and no `scale=` clause is emitted;
the whole-page linear shortcut (§4.2 step 11) then emits the page through `_linear_elements`
verbatim and the version rule yields `pmd: 1.0`. **This trace must be confirmed by running the
suite, not trusted** — it is stated so that a failure names the step that broke.
`tests/test_htmlread.py` and `tests/test_xlsxread.py` reach `no-geometry` and are likewise
untouched. `tests/test_pdfread.py` needs its scan-detection tests rewritten for the new routing
(§10) and its `MockTransport` tests kept as-is.

### 9.6 Corpus sweep

`tools/corpus_sweep.py` gains two graded columns and one histogram:

* `canvas` — the document produced ≥1 canvas (informational, not pass/fail).
* `lossless` — §9.2's `test_no_block_text_disappears` run over the returned markdown against
  an independent read. Pass/fail.
* a `reason` histogram over `no-geometry | skew | too-dense | no-gutter | multiline | coverage`.
  This is the tracked regression metric: a rise in `no-geometry` means routing broke, a rise
  in `coverage` means the span join broke, a rise in `multiline` means lines stopped being
  attached.

---

## 10. Migration

**What stays exactly as it is.**

* `layout="linear", rect_scale="legacy"` reproduces PMD 1.0 byte-for-byte, forever. That
  combination is the compatibility contract and is covered by a test.
* Any page with no canvas is emitted through the unmodified `_linear_elements`, so its element
  sequence is PMD 1.0's.
* `_table_md`, `_cell`, `_clean`, `_heading`, `_anchor`, the `seq` path, the honesty rule and
  the `zone=table` suppression are all untouched.
* Historical S3 objects are immutable and still fetchable; `GET
  /api/v1/conversions/{id}/markdown` is unchanged.
* `tests/test_emitter.py`, `tests/test_htmlread.py`, `tests/test_xlsxread.py` are untouched.

**What changes bytes and hashes.**

* Any document with ≥1 canvas. Correct: its content *ordering and layout* changed, because it
  was wrong before. A hash that did not move would be hiding a real change.
* Any Azure-read PDF, even with no canvas, because `scale=1000` fixes the inch-rect defect.
  This is broad under Azure-only routing, and it is a bug fix worth the rehash: today those
  anchors are an 8×11 integer grid and the format's central promise ("the anchors carry the
  exact rectangles") is already false for them. A deployment that must not rehash sets
  `DPC_PMD_RECT_SCALE=legacy` and gets the old bytes with the old defect.
* Nothing else. Office, HTML, plain text, DES and point-unit payloads all keep `pmd: 1.0` and
  their stored sha256.

**What breaks, named.**

1. **A text-layer PDF now costs an Azure call.** `provider` changes from `pymupdf` to
   `azure_layout` for every such document, latency goes from milliseconds to seconds, and
   per-page DI charges apply to documents that were free. This is the requirement, and it
   should be sized before rollout: `SELECT count(*), sum(pages) FROM conversions WHERE
   provider = 'pymupdf'`.
2. **`DPC_AZURE_DI_ENDPOINT` unset turns `document` conversions that used to succeed into 422.**
   Every deployment must set it (or the stub) before this ships.
3. **`.gif` and `.webp` become 415.** DI does not accept them; they were previously routed to
   `_recognize` with a made-up content type and would have failed at the service anyway, less
   legibly.
4. **`DPC_MIN_ALNUM_CHARS` becomes a no-op.** Deprecated for one release with a startup WARNING.
5. **A consumer that splits chunks on `^#` finds fewer headings** on columnar pages — only the
   column-level ones, which were never document headings (§5.4). Consumers that split on `^| `
   or grep `\[([x ])\]` are unaffected. This needs a consumer inventory before the flag flips,
   and `DPC_PMD_LAYOUT=linear` must stay flippable.
6. **The frontend "Anchors" tab** matches `<!-- @(\d+) \[([-\d,]+)\] (.+) -->`, which already
   matches canvas anchors and legend lines; the tag column will show
   `canvas 99x11 seg=1/2 …`. Informative as-is. The "Rendered" tab (react-markdown +
   remark-gfm) renders a fence as `<pre><code>`, i.e. correctly and monospaced, with no change.
   Follow-up work, not blocking: parse `frames=` and lay the canvas out as a CSS grid.

**Rollout order.** (1) models + adapters, run the suite — nothing should change.
(2) `canvas.py` + its unit tests, no emitter contact. (3) refactor `_page_elements` →
`_linear_elements`, run the suite — `GOLDEN` must still pass; **this is the checkpoint that
proves the linear path was not reimplemented**. (4) band emission + fixtures + §9.3.
(5) routing + `di_stub` + `test_pdfread` rewrite. (6) config, API, migration 013.
(7) corpus sweep in both layouts; publish the `reason` histogram. (8) flip
`DPC_PMD_LAYOUT=band` per environment.

---

## 11. The honest risk list, and the kill criteria

### 11.1 A frame that mixes type sizes gets one `adv` and both sizes are wrong

The lower median is robust to *one* outlier, not to a frame that is genuinely half 14 pt bold
labels and half 9 pt values — which is common on exactly the KYC forms this service exists for.
`cells_j`'s max-with-required-extent term guarantees no *overlap*, so the output is never
garbled, but the frame stretches to fit the widest placement and the small-print rows then sit
noticeably left of where they should. The visible symptom is a canvas 30% wider than it needs
to be with one column hugging its left edge.

The real fix is per-*band* density inside a frame. It is deliberately not in v1: a
wrong-but-monotone density degrades gracefully (everything shifts the same direction) while a
per-band density can make consecutive rows of the same column disagree, which reads as broken.
Tab snapping (§4.2 step 7b) recovers the common case where the mixed sizes still share tab
stops; it does not fix the general case.

**Kill criterion:** if, on the real corpus, more than 20% of canvases are more than 1.5× wider
than the widest row they contain, the per-frame `adv` model is wrong for this corpus and the
approach needs per-band density before it ships.

### 11.2 The line → paragraph span join can fail silently, and the failure looks like "this document just has no columns"

If Azure's `paragraphs[].spans` and `pages[].lines[].spans` disagree — and the 4.0 GA docs
already admit one such mismatch, where selection-mark field content contains `:selected:` while
the spans point at Unicode characters in the top-level `content` — lines find no claiming
paragraph, get dropped, every block falls back to its hull, hulls are multiline, multiline
forces linear, and the output is a perfectly valid, perfectly ordinary PMD file with zero
canvases. Nothing errors. Nothing warns. The user reports "the columns feature doesn't work on
our documents" and there is nothing in the artifact to look at.

That is why `line_join: <attached>/<total>` is in the front matter and why it should be treated
as a first-class metric rather than a curiosity: `line_join: 3/430` is the difference between
"this document has no columns" and "the join broke". It is the one field in this design whose
entire purpose is to make a failure legible, which is the strongest sign it is the failure I am
least able to prevent.

**Kill criterion:** if `line_join` is below 0.90 on more than 10% of the corpus, the span join
is not the right mechanism and the design must switch to a geometric line→paragraph assignment
(containment of the line rect in the paragraph hull), accepting the ambiguity that brings.

### 11.3 Band membership is stable but not continuous, and coverage may simply be too low

Two related worries.

*Stability.* The frozen-seed sweep cannot chain, which removes the catastrophic failure. But a
1-milli-unit change in one `y0` can still change which atom seeds a band and cascade that band's
membership. The determinism contract is over the `LayoutView`, so this is not a determinism
bug — but two scans of the same page can band differently, and the outputs will not diff
cleanly. There is no cheap fix; quantising `y0` to a fraction of `em` before seeding would trade
this for a different discontinuity at the quantum boundary.

*Coverage.* Every gate in §4.2 fails **closed**, to `layout: linear-only`, which is today's
known-good output. That is the right failure direction and it is also the risk: a design that
never fires is theatre. The gates that can silently swallow a genuinely columnar page are the
multiline gate (a page whose lines were not attached), the coverage gate (a page whose lines
do not reconstruct their paragraphs), the `MIN_GUTTER_ROWS = 4` floor (a genuine three-row
two-column block), and the `MAX_BLOCKING_BANDS = 3` budget (a three-column page where a
two-column-spanning subhead recurs on four bands).

**Kill criterion — the one that decides whether this approach lives.** Take 100 corpus pages
that a human labels "visually multi-column". If fewer than 60 produce a canvas, band mode is
not delivering what was asked and the right move is to reconsider the whole-page canvas with
per-band density — accepting the fence, the `#`/`|`/`- [x]` loss, and the 1.6× token cost that
come with it. If 60–85 produce a canvas, ship band mode and work the `reason` histogram. If
more than 85 produce a canvas, the gates are calibrated and the remaining work is fidelity, not
coverage.

### 11.4 Smaller, still real

* **Statement pages are mostly table**, so a bank statement produces GFM plus a small canvas
  for its header block and totals, and no canvas over the transactions. That is the correct
  trade (constraint 4) but it will read as "the feature did nothing here" to someone who
  expected a spatial statement.
* **A canvas is not a chunk-coherent unit.** Left and right column text share every row, so a
  chunk of a canvas is topically mixed. The segment cap bounds the damage to ~350 tokens and
  the legend labels it, but the underlying tension is real: spatial fidelity and topical
  coherence are different objectives, and band mode buys the first only in the bands where it
  was being lied about.
* **`DPC_OFFICE_ROUTE=render` puts LibreOffice inside the hash contract**, and Dependabot does
  not know that. Pin it in the image and say so in the deployment doc.
