# DocTree research appendix — the evidence behind SPEC-DOCTREE-1

Two survey passes feeding the spec. Facts below were gathered from official docs and
primary sources; each survey flags what it could not verify. Read the spec first;
come here for the provenance of a design choice.

---

# Survey #1 — Enterprise Document-Tree Systems

## 1. Azure Document Intelligence prebuilt-layout v4 (2024-11-30)

**Tree representation.** `analyzeResult.sections` is an array of section objects: `{ "spans": [...], "elements": ["/paragraphs/0", "/sections/1", "/sections/2", ...] }`. The tree is encoded by JSON-pointer-style references: a section's `elements` array mixes leaf refs (`/paragraphs/N`, `/tables/N`, `/figures/N`) and child-section refs (`/sections/M`) — so nesting is by reference into the flat top-level arrays, with `sections[0]` as the implicit root. `analyzeResult.figures` is a sibling flat array: `{ "id": "{page}.{index}", "boundingRegions", "spans", "elements": ["/paragraphs/15"], "caption": { "content", "boundingRegions", "spans", "elements" } }`. The figure `id` follows an *undocumented* convention `{pageNumber}.{figureIndex}` with figureIndex resetting to 1 per page (Microsoft's own docs call it undocumented). For v4.0 GA, figure/table bounding regions cover only core content and exclude caption and footnotes.

**Reading order.** Content elements are "sorted by reading order that arranges semantically contiguous elements together, even if they cross line or column boundaries" — i.e., a learned model, algorithm unpublished. When ambiguous, it falls back to left-to-right, top-to-bottom. Documented limits: **no reading order across page boundaries**, and selection marks are not positioned within surrounding words. Reading order is materialized as spans into the top-level `content` string; within a section's `elements` array the order is the reading order (implied, not contractually guaranteed in the docs — flag below). Paragraph roles (per-paragraph `role` field): `title`, `sectionHeading`, `footnote`, `pageHeader`, `pageFooter`, `pageNumber` (unroled = body).

**Figures as placeholders.** With `output=figures` on the analyze call, the service crops each figure; retrieve via `GET /analyzeResults/{resultId}/figures/{figureId}`. In markdown mode the placeholder is `![](figures/0)` inside a `<figure>` element with `<figcaption>` and an inline `FigureContent="..."` text dump of OCR'd text inside the figure. Caveat: if a sectionHeading is detected inside a figure, markdown mode suppresses the figure entirely — you must enumerate the JSON `figures` array to see all of them.

**Markdown flattening** (`outputContentFormat=markdown`): headings from section hierarchy as `#`–`######`; tables as full **HTML** `<table>/<tr>/<th>/<td>` with rowspan/colspan and `<caption>` (not GFM — deliberate, for merged cells); selection marks as Unicode ☒/☐ (below 0.1 confidence filtered out); formulas as `$...$` / `$$...$$` LaTeX; barcodes as `![type](barcodes/{page}.{n} "value")`; pageHeader/pageFooter/pageNumber as HTML comments `<!-- PageHeader="..." -->`; page delimiter `<!-- PageBreak -->`; keyValuePairs/language/style never appear in markdown. So Microsoft's own answer to "what tree do we build" = sections tree + roles, and its own flattener is exactly the user's stage 3 done server-side.

**License.** Proprietary Azure service; SDKs MIT.

**Direct DPC relevance:** the sections+figures tree the user wants is literally in the payload `dpc/adapters.py` already receives and drops. Consuming `sections` (with a cycle/dangling-ref guard, since it's refs not embedded nesting) gives a provider tree for free when real Azure DI is the backend; the Tesseract mock won't have it, so it must be an optional enrichment pass.

## 2. AWS Textract LAYOUT

**Tree representation.** Layout elements are `Block` objects: `LAYOUT_TITLE`, `LAYOUT_HEADER`, `LAYOUT_FOOTER`, `LAYOUT_SECTION_HEADER`, `LAYOUT_PAGE_NUMBER`, `LAYOUT_LIST`, `LAYOUT_FIGURE`, `LAYOUT_TABLE`, `LAYOUT_KEY_VALUE`, `LAYOUT_TEXT`. Tree is a flat block list wired by `Relationships: [{Type: "CHILD", Ids: [...]}]`: PAGE → LAYOUT_* → LINE (except LAYOUT_LIST → LAYOUT_TEXT → LINE, one extra level; LAYOUT_TABLE points at TABLE structures). Max depth ~3; there is **no section nesting** — LAYOUT_SECTION_HEADER is a sibling label, not a parent of following content. Shallower than Azure/Google.

**Reading order.** Explicitly documented: "Elements are returned in implied reading order... left to right, top to bottom. For multicolumn pages, elements are returned from the top of the leftmost column ... until the bottom of the column is reached. Then, the elements from the next leftmost column are returned in the same way." So: array order = reading order, column-major, guaranteed in docs. Algorithm unpublished (learned layout model + ordering).

**Figures.** `LAYOUT_FIGURE` gives the bounding box of an image region only — **no cropped-image retrieval API and no caption association**. You crop from the source yourself.

**Markdown.** No native markdown mode. AWS's open-source `amazon-textract-textractor` library (Apache-2.0) provides "linearization" with a large config object (placeholder text for figures, table→markdown rendering, header prefixes) — flattening is client-side and configurable. (Config specifics from memory, not re-verified this session.)

**License.** Proprietary service; Textractor library Apache-2.0.

## 3. Google Document AI Layout Parser

**Tree representation.** The deepest explicit tree of the three clouds: `document.documentLayout.blocks[]` where each `DocumentLayoutBlock` = `{ blockId, pageSpan, boundingBox, <union: textBlock | tableBlock | listBlock | imageBlock> }`. `LayoutTextBlock` = `{ text, type, blocks[], annotations }` — the nested `blocks` field is child blocks, so **blocks contain blocks**: a `heading-1` text block literally contains the paragraphs and sub-headings under it. `LayoutListBlock.listEntries[].blocks`, `LayoutTableBlock.bodyRows[].cells[].blocks` — tables and list entries recurse too. `type` values: `paragraph`, `subtitle`, `heading-1`…`heading-5`, `header`, `footer` (no `title`). Built expressly for RAG: the companion `chunkedDocument` output emits chunks augmented with ancestral headings ("context-aware chunks that include content from ancestral headings and table headers").

**Reading order.** Not explicitly guaranteed in the docs I could fetch — block array order appears to be reading order but I could not verify a written guarantee (flagged below). Algorithm unpublished.

**Figures.** `LayoutImageBlock` = `{ mimeType, imageText, annotations, blobAssetId | gcsUri | dataUri }` — notable: the image can be *described as text* (`imageText`, Gemini-generated description) for retrieval, i.e., Google's placeholder is a generated description rather than a link. (PII-relevant: that behavior is exactly what DPC must NOT do with an external LLM.)

**Markdown.** Chunks are emitted as markdown-ish text with heading context; there is no full-document markdown export equivalent to Azure's. Proprietary service.

## 4. IBM Docling (open source, MIT) — closest analogue

**Tree representation — `DoclingDocument`** (in `docling-core`): flat typed arrays `texts` (TextItem subclasses: paragraph, `SectionHeaderItem` with integer `level`, TitleItem, CodeItem, FormulaItem, ListItem…), `tables` (TableItem), `pictures` (PictureItem), `key_value_items`, plus pure-structure `groups` (GroupItem: list groups, inline groups, chapters…) and **two roots**: `body` (main-content tree) and `furniture` (headers/footers/page furniture — excluded from body flow). All nodes reference `children` and `parent` via JSON pointers into the flat arrays — same flat-arrays-plus-ref-tree shape as Azure sections, but with an explicit furniture/body split and typed groups. **Reading order is the tree**: "the reading order of the document is encapsulated through the `body` tree and the order of children in each item" — no separate order field.

**Reading-order computation.** Pipeline: `docling-parse`/OCR → per-page layout model (RT-DETR-family: `docling-layout-heron` etc., trained on DocLayNet; outputs labeled bboxes: TEXT, TABLE, PICTURE, SECTION_HEADER…) → TableFormer for table structure → **assembly with a dedicated reading-order step**. That step lives in `docling-ibm-models` as a **deterministic rule-based predictor** (`reading_order_rb`): input = bounding boxes + labels + text snippets; computes spatial/geometric features (positions, sizes, alignments) to sort elements, groups lists/headers/footnotes, and predicts element relations — merges, caption-to-figure/table matching. Recent changelog entries confirm "deterministic page sorting in reading-order model." It is not a neural model (the model catalog lists no reading-order network). This is the strongest precedent for the user's requirement that heuristics decide deterministically: Docling ships a rules-only reading-order stage at production quality, MIT licensed, and the newer "Advanced Layout Analysis Models for Docling" (arXiv 2509.11720) keeps it as post-processing over better detectors.

**Figures.** `PictureItem` node in the tree, optional classification (DocumentFigureClassifier) and caption child. Markdown serializer default placeholder: **`image_placeholder: str = "<!-- image -->"`**, with `ImageRefMode` = PLACEHOLDER | EMBEDDED (base64 data URI) | REFERENCED (external file link).

**Markdown flattening.** A composable serializer framework: `MarkdownDocSerializer` walks the `body` tree in child order, delegating per node type to `BaseTextSerializer` / `BaseTableSerializer` / picture / list / inline serializers; `serialize()` also returns *which components contributed* (provenance — matches DPC's honesty-counter culture). Headings = `item.level + 1` hashes; GFM tables (span cells written at origin only, covered cells empty — lossy, unlike Azure's HTML tables); `page_break_placeholder` default None; markdown documented as lossy vs the lossless JSON.

**License.** MIT (code, models, and docling-core). Safe to copy algorithms and even vendored code.

## 5. unstructured.io

**Tree representation.** A *flattened* tree: a flat list of typed elements — `Title`, `NarrativeText`, `ListItem`, `Table`, `Image`, `Header`, `Footer`, `Formula`, `CodeSnippet`, `CompositeElement` (post-chunking) — with hierarchy pushed into metadata: `parent_id` (element's parent, e.g. NarrativeText → its Title) and `category_depth` (depth among same-category elements, e.g. H1 vs H3, list nesting), letting a "hierarchy post-processor" reconstruct the tree. Notable as the minimal-viable tree encoding: DPC could add exactly two fields to TextBlock and have a tree without changing storage shape.

**Reading order.** No dedicated model; inherits order from the partitioner (for PDF hi-res: a detectron2/yolox-family layout model then top-to-bottom sort). Weak on multi-column relative to the others; no published guarantee.

**Figures.** `Image` elements; `metadata.image_base64` + `image_mime_type` (hi-res partitioning), or `image_url`/`image_path` — i.e., placeholder element with the payload in metadata.

**Flattening/chunking.** `chunk_by_title`: hard rule "a single chunk will never contain text that occurred in two different sections"; a `Title` element always closes the prior chunk even if it would fit; params `max_characters` (hard), `new_after_n_chars` (soft), `combine_text_under_n_chars`, `multipage_sections`; `Table` elements are never merged with text and oversize tables split into `TableChunk`s. This is a *tree-flattening policy* worth stealing for PMD sectioning: section boundary supremacy over size packing.

**License.** Apache-2.0 (open-source library; hosted API proprietary).

## 6. marker and MinerU

**marker (datalab-to/marker).** Pipeline: text extraction → OCR if needed → **Surya** layout detection → Surya reading-order prediction → per-block cleanup → postprocess merge. Reading order is *learned*: historically an encoder-decoder (Donut-style encoder, MBart-style decoder) emitting a reading-order index per text region; current Surya folds order prediction into its layout stack (exact current architecture not verified — flagged). Handles multi-column via the learned model, not projection cuts. Output: markdown (image links, GFM tables, LaTeX), HTML, **JSON tree** — pages → blocks with `id`, `block_type`, `html`, `polygon`, `children`, recursive nesting plus `section_hierarchy` per block (each block knows its ancestor headings — a good idea for PMD provenance), and a flattened "chunks" mode for RAG. Images: extracted and saved next to the markdown, or base64 in JSON keyed by block id; with `--disable_image_extraction` + LLM mode they become text descriptions. **License: code Apache-2.0; model weights modified AI Pubs Open RAIL-M — free under $5M funding/revenue, paid above.** Weights license makes marker's *models* unusable for most enterprises without a datalab contract; the *pipeline shape* is freely copyable.

**MinerU (opendatalab).** Historical pipeline (paper 2409.18839): PDF-Extract-Kit layout detection (DocLayout-YOLO) → **LayoutReader** for reading order → formula/table models → markdown. v3.0.0 **removed** doclayoutyolo + mfd_yolov8 (AGPLv3) and layoutreader (**CC-BY-NC-SA 4.0** — note: the popular LayoutLMv3-based fast LayoutReader reimplementation carries a non-commercial license; a licensing landmine for anyone tempted to "just use LayoutReader"). Current: pipeline backend (PP-OCRv6-era models, 86.47 OmniDocBench v1.6) or VLM backend (MinerU2.5 1.2B VLM, ~95.3 OmniDocBench) where the VLM emits ordered structured output directly — reading order becomes an emergent property of the VLM decode, docs admit it "may be out of order in some areas under extremely complex layouts." Markdown output: human-reading-order MD with images saved to a local dir and linked (dir convention not re-verified). **License: moved from AGPLv3 to a custom "MinerU Open Source License" based on Apache 2.0** (read the custom terms before depending on it).

## 7. The standards answer — PDF logical structure and Adobe Extract

**ISO 32000 / Tagged PDF / PDF/UA.** The canonical document tree: document catalog → `StructTreeRoot` → structure element hierarchy, typically rooted at a `Document` element, using standard structure types — grouping (`Document`, `Part`, `Sect`, `Art`, `Div`, `Aside`), headings `H1`–`H6`/`H`, `P`, lists (`L` → `LI` → `Lbl` + `LBody`), tables (`Table` → `TR` → `TH`/`TD`), `Figure` (with mandatory `/Alt` text in PDF/UA — the standards world's image placeholder is *required alt text*), `Caption`, `TOC`/`TOCI`, `Reference`, `Footnote`. PDF/UA-1/2 (and pdf/A's WTPDF profile) requires all "real content" tagged **in logical reading order** with artifacts (running headers/footers, decorations) explicitly excluded from the structure tree — i.e., the standards world independently arrived at Docling's body/furniture split ("artifact" = furniture). Depth-first pre-order traversal of the structure tree *is* the normative reading order.

**Adobe PDF Extract API** is this tree serialized: JSON `elements[]` "ordered list of semantic elements ... on the basis of position in the structure tree"; each element has a `Path` like `//Document/Sect[2]/P[3]` (structure-tree path with instance numbers), `Bounds` in PDF user space (origin bottom-left; left,bottom,right,top), types H1/P/L/Li/Table/TR/TD/Figure/Sect/Aside/Footnote/TOC. Reading order: "the reading order of content within columns, across page breaks, and inclusive of asides is represented by the order of the elements in the Elements array." Figures → PNG renditions in a `figures/` folder referenced by `filePaths`; tables → CSV/XLSX/PNG in `tables/`. For untagged PDFs Adobe infers the tree with its own (unpublished, Sensei-derived) models. Proprietary API.

**Takeaway for DPC:** the Path-string idea (`//Document/Sect[2]/P[3]`) is a compact, PII-free, deterministic node address — ideal for the LLM-advisory payload and for arrangement-patch provenance.

## 8. Reading-order literature with numbers

- **LayoutReader + ReadingBank** (Microsoft, EMNLP 2021): ReadingBank = 500k document pages with ground-truth reading order harvested from DOCX XML. LayoutReader = seq2seq with LayoutLM encoder generating the order permutation; **98.2 average page-level BLEU**, materially improves ordering of open-source and commercial OCR line output. Licensing caution: ReadingBank/weights carry research-oriented terms and the widely used fast reimplementation (LayoutLMv3-based) is CC-BY-NC-SA — the reason MinerU dropped it.
- **XY-Cut** (classic): recursive projection-profile cuts — alternate horizontal/vertical whitespace splits building a cut tree; pre-order traversal = reading order. Deterministic, fast, fails on overlapping/L-shaped regions and some multi-column cases. **XY-Cut++** (arXiv 2504.10258, 2025): pre-mask processing + multi-granularity segmentation + cross-modal matching on top of XY-Cut; **98.8 overall BLEU on DocBench-100** (their new benchmark), "up to 24%" over baselines, retains rule-based speed/simplicity. This is the single most relevant algorithm for DPC's heuristic stage: state-of-the-art-competitive reading order with **no model, no training, deterministic** — compatible with the sha256 byte-identity contract, and DPC's canvas.py band/gutter machinery is already most of an XY-Cut implementation.
- **Detect-Order-Construct** (arXiv 2401.11874, Alibaba/USTC): tree construction as three explicit stages — detect page objects, order them (reading-order prediction), construct hierarchy via relation prediction (child-of/sibling between headings and content); introduces Comp-HRDoc benchmark; SOTA on PubLayNet, DocLayNet, HRDoc. Its successor **UniHDSA** (arXiv 2503.15893, 2025) unifies order+hierarchy as one relation-prediction task. Academic, but it validates the user's exact decomposition (tree build ≡ detect → order → construct) as the field-standard formulation.
- **Deployed 2024-25 practice:** the deployed systems have bifurcated — deterministic-geometric (Docling's rule-based predictor, MinerU-pipeline post XY-Cut-style sorting, Textract's documented column-major sweep) vs end-to-end learned/VLM (Surya/marker, MinerU2.5 VLM, Azure's internal model). Nobody deployed puts an LLM in the ordering hot path; VLM-based systems make it the whole parser instead.

---

## Synthesis — what to steal per stage

| Stage | Best-proven source | Concrete algorithm/mechanism to steal | Why it fits DPC |
|---|---|---|---|
| **1. Tree construction** | **Azure DI `sections` (already in DPC's payload)** + **DoclingDocument shape** | Consume `analyzeResult.sections` element-refs (`/paragraphs/N`, `/sections/M`) as the provider tree when present; store the tree Docling-style: flat typed node arrays + parent/children refs, **`body` vs `furniture` roots** (ISO 32000 "artifact" concept — pageHeader/pageFooter/pageNumber roles already map to furniture); when sections are absent (Tesseract mock), synthesize the tree from canvas.py's page→band→region→frame hierarchy + XY-Cut recursion, with heading-level stack (Docling `SectionHeaderItem.level`, Google `heading-N`) nesting body content under headings | Zero new inference; degrades honestly (provider tree → geometric tree → flat list), and the flat-arrays+refs shape serializes deterministically |
| **1b. Node addressing** | **Adobe Extract `Path`** | `//Document/Sect[2]/P[3]`-style deterministic path strings per node | Compact, content-free node IDs for patches, audits, and the LLM payload |
| **2. Heuristic ordering (the math)** | **XY-Cut++** (arXiv 2504.10258) + **Docling `reading_order_rb`** | Recursive projection-profile cuts with pre-masking of tall/spanning elements and multi-granularity passes (98.8 BLEU, rule-based, deterministic); plus Docling's deterministic feature-sort (position/size/alignment) and rule-based caption↔figure matching — all MIT, readable in `docling-ibm-models` | Only SOTA-competitive approach that preserves byte-identical artifacts; canvas.py's bands/gutters/separators are already the projection profiles XY-Cut needs |
| **3. LLM validation** | **No deployed precedent — by design** (deployed systems are either fully deterministic or fully VLM); closest formal frame: **Detect-Order-Construct / UniHDSA** (order-as-relation-prediction between element pairs) | Frame the LLM task as UniHDSA-style *relation judgment over structure-only features*: given node type, Path, bbox/centroid classes, page, column, char/line-count classes, boolean continuity cues — "flag edges in the succession chain that break continuity," output = advisory list of (path_a, path_b, suspected_break, confidence). Persist as a **separate versioned advisory artifact** (provenance-stamped), never mutating the deterministic PMD — models propose, deterministic code decides | The pair-relation formulation is exactly expressible with the PII-safe derived features already enumerated in the constraints; being off the hot path preserves determinism and availability |
| **4. Flattening to MD** | **Docling `MarkdownDocSerializer`** + **Azure markdown-elements** + **unstructured `chunk_by_title`** | Docling: pre-order body-tree walk with per-node-type serializers that report which nodes contributed (provenance); Azure: HTML tables for rowspan/colspan fidelity, `<!-- PageBreak -->`/`<!-- PageHeader="..." -->` comment furniture, `<figure>`+placeholder figures; unstructured: section-boundary supremacy (a heading always breaks a chunk) if PMD ever chunks | Docling serializer is MIT (copyable), tree-walk-deterministic, and its contributed-components return value matches PMD's line_join honesty-counter pattern |
| **Image placeholders** | Docling `<!-- image -->` / Azure `![](figures/{id})` inside `<figure>` | Emit `<figure>` wrapper + deterministic ref placeholder (`![](figures/{page}.{n})` mirroring Azure's id convention) + optional caption; never a generated description (Google's `imageText` approach is the anti-pattern for KYC PII) | Invertible, deterministic, PII-free |

## Could not verify / flags

1. **Azure**: whether order *within* a section's `elements` array and across sibling sections is contractually guaranteed as reading order — docs imply it (spans sort into `content`) but never state a guarantee; cross-page reading order explicitly NOT supported. Figure-id convention explicitly "undocumented" by Microsoft — don't hard-depend on `{page}.{index}`.
2. **Google**: no written reading-order guarantee found for `documentLayout.blocks` array order; JSON examples page was thin — the REST reference (fields quoted above) is solid, the guide page was summarized by the fetcher without raw JSON.
3. **marker**: current Surya reading-order architecture (standalone Donut/MBart order model vs order folded into the newer layout model) not pinned down; the pipeline description mixes eras.
4. **MinerU**: post-v3 pipeline-backend reading-order replacement for LayoutReader (presumably XY-Cut-style geometric sort) not explicitly documented; markdown image-dir convention not re-verified; "MinerU Open Source License" is custom Apache-2.0-based — needs legal read before adoption.
5. **Textract**: markdown-linearization details of `amazon-textract-textractor` (config field names) stated from prior knowledge, not re-fetched this session.
6. **Docling** `reading_order_rb` exact rule set (feature weights, tie-breaks) not read line-by-line — but the code is MIT at `github.com/docling-project/docling-ibm-models` and should be read directly before implementation.
7. LayoutReader/ReadingBank license terms summarized from ecosystem behavior (MinerU's removal citing CC-BY-NC-SA) — verify the exact weight license if it ever matters.

Sources:
- [Azure DI analyze response (sections/figures/roles)](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/analyze-document-response)
- [Azure DI prebuilt-layout](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout)
- [Azure DI markdown elements](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/markdown-elements)
- [AWS Textract Layout response](https://docs.aws.amazon.com/textract/latest/dg/layoutresponse.html)
- [Google Document AI Layout Parser](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)
- [Google Document AI Document reference (DocumentLayout)](https://docs.cloud.google.com/document-ai/docs/reference/rest/v1/Document#DocumentLayout)
- [DoclingDocument concepts](https://docling-project.github.io/docling/concepts/docling_document/)
- [Docling serialization](https://docling-project.github.io/docling/concepts/serialization/)
- [Docling model catalog](https://docling-project.github.io/docling/usage/model_catalog/)
- [Docling technical report (arXiv 2501.17887)](https://arxiv.org/html/2501.17887v1)
- [docling-core markdown serializer source](https://github.com/docling-project/docling-core/blob/main/docling_core/transforms/serializer/markdown.py)
- [docling-ibm-models repo](https://github.com/docling-project/docling-ibm-models/blob/main/CHANGELOG.md)
- [unstructured document elements](https://docs.unstructured.io/open-source/concepts/document-elements)
- [unstructured chunking](https://docs.unstructured.io/open-source/core-functionality/chunking)
- [marker repo](https://github.com/datalab-to/marker)
- [surya repo](https://github.com/datalab-to/surya)
- [How marker works (Kevin Hu)](https://kevinhu.io/notes/how-marker-works/)
- [MinerU repo](https://github.com/opendatalab/MinerU)
- [MinerU paper (arXiv 2409.18839)](https://ar5iv.labs.arxiv.org/html/2409.18839)
- [Adobe PDF Extract API](https://developer.adobe.com/document-services/docs/overview/pdf-extract-api/howtos/extract-api/)
- [Well-Tagged PDF (PDF Association)](https://pdfa.org/wp-content/uploads/2024/02/Well-Tagged-PDF-WTPDF-1.0.pdf)
- [PDF/UA (Wikipedia)](https://en.wikipedia.org/wiki/PDF/UA)
- [Tagged PDF Best Practice Guide](https://pdfa.org/wp-content/uploads/2019/06/TaggedPDFBestPracticeGuideSyntax.pdf)
- [LayoutReader (Microsoft Research)](https://www.microsoft.com/en-us/research/publication/layoutreader-pre-training-of-text-and-layout-for-reading-order-detection/)
- [ReadingBank repo](https://github.com/doc-analysis/ReadingBank)
- [unilm/layoutreader](https://github.com/microsoft/unilm/tree/master/layoutreader)
- [Fast LayoutReader reimplementation](https://github.com/ppaanngggg/layoutreader)
- [XY-Cut++ (arXiv 2504.10258)](https://arxiv.org/abs/2504.10258)
- [Detect-Order-Construct (arXiv 2401.11874)](https://arxiv.org/abs/2401.11874)
- [UniHDSA (arXiv 2503.15893)](https://arxiv.org/pdf/2503.15893)
- [Advanced Layout Analysis Models for Docling (arXiv 2509.11720)](https://arxiv.org/abs/2509.11720)
---

# Survey #2 — LLM-Validated Structure Without Content, and the PII Boundary

## 1. Prior art: LLMs reasoning over layout geometry only

**Direct finding: no published system was found that feeds bbox-only (zero text) input to a general-purpose LLM specifically for reading-order correction.** That exact combination appears to be unclaimed territory — flag it as such. But the two halves are each independently proven:

**(a) Reading order is learnable from geometry alone (specialized models):**
- **Layout2Pos** (TPDL 2024, Springer/OpenReview) is the strongest evidence: a shallow Transformer that generates position embeddings **from layout only**, built on the explicit thesis that "layout contains the information for correct reading order." Competitive with reading-order-dependent models on three IE benchmarks — i.e., text is not required to recover order.
- **Google's sparse graph segmentation** ([arXiv 2305.02577](https://arxiv.org/abs/2305.02577)): GCN over a sparse layout graph of text-line boxes, explicitly **language-agnostic** — designed to work without reading the text. (Exact feature list unverified from the abstract; the language-agnostic claim is verified.)
- **LayoutReader** ([arXiv 2108.11591](https://ar5iv.labs.arxiv.org/html/2108.11591)) uses text+layout (LayoutLM encoder, seq2seq over ReadingBank), but established the reading-order-as-permutation formulation; its known limitations (autoregressive decoding cost, word-level granularity mismatch) argue for **block-level, relation-based** formulations instead — see [Modeling Layout Reading Order as Ordering Relations (arXiv 2409.19672)](https://arxiv.org/html/2409.19672), which replaces total permutation with **pairwise ordering relations** — a better match for "find the break in continuity" than "emit the whole order."
- Non-LLM detectors (RT-DocLayout, DLAFormer) now predict reading order jointly with layout from vision — geometry suffices in production systems too.

**(b) General LLMs can reason over pure numeric geometry:**
- **LayoutGPT** ([NeurIPS 2023](https://proceedings.neurips.cc/paper_files/paper/2023/file/3a7f9e485845dac27423375c934cb4db-Paper-Conference.pdf)) has GPT-3.5/4 emit CSS-style numeric bounding-box layouts from in-context examples, matching human users on numerical/spatial correctness; **LayoutNUWA** renders layouts as HTML/SVG code for LLM completion. Both are generation, not ordering, but they prove the competence your helper needs: an LLM given `{type, x, y, w, h}` sequences reasons usefully about them. Known failure mode: quality degrades past ~15 objects or under strict constraints — so **chunk the tree per page/band and cap node counts per call**.
- **LayTextLLM** ([arXiv 2407.01976](https://arxiv.org/html/2407.01976v2)) and DocLLM interleave bbox tokens with text — evidence bboxes are LLM-consumable as first-class tokens, but both need text, so they are *not* usable inside your PII boundary.

**Implication:** your design (geometry+type features → LLM → advisory break-flags) is plausible per LayoutGPT-class evidence but unbenchmarked; that is exactly why the verifier layer in §3 must carry the correctness burden, not the model.

## 2. What non-content features are safe — the adversarial view

**The core adversarial result: length sequences are quasi-content.**
- **Whisper Leak** (Microsoft 2025, [arXiv 2511.03675](https://arxiv.org/html/2511.03675v1)): encrypted **packet sizes + timings alone** classify the topic of LLM conversations. No plaintext needed.
- **"What Was Your Prompt?"** ([arXiv 2403.09751](https://arxiv.org/pdf/2403.09751)) and the [USENIX Security '26 token-length paper](https://www.usenix.org/conference/usenixsecurity26/presentation/li-sijia): a **sequence of token lengths** (e.g. [4,5,3,1,6]) is enough for a trained model to reconstruct plausible — often actual — sentences. Fine-grained length sequences are partially invertible to text.
- **Unicity literature** (de Montjoye, *Unique in the Crowd*): 4 spatiotemporal points re-identify 95% of individuals; the credit-card follow-up showed the same for purchase metadata. Lesson: a handful of exact quasi-identifiers uniquely fingerprints a person; "it's only metadata" is not a defense.
- **EDPB Guidelines 01/2025 on pseudonymisation**: effectiveness is judged against attribution by any reasonably-likely means *including combination with other data held by the recipient*; unauthorized re-identification of pseudonymized data is itself a **personal data breach**. Data minimization requires sending only what the purpose (order validation) needs.

**Applying that to a KYC form:** the attacker's best move is exactly the one you named — on a known template, the value block next to the "Surname" label position has char_count = length of the surname. Exact `char_count` of a short block on a known template is a **direct length oracle** on a PII value. Length of an ID number reveals format/country (acceptable — document type is not personal data) but combined with name-length, DOB-block digit pattern, and address line-count it starts building a fingerprint. Per-line or per-word length sequences are worse — that is the Whisper-Leak regime.

### Proposed SAFE schema (per node)

| Feature | Form | Why safe |
|---|---|---|
| `node_id` | opaque ordinal | assigned by us, no content |
| `type` | enum: paragraph/heading/table/figure/kv/furniture | class label, provider-derived, no content |
| `page`, `band`, `frame` (column) | small ints | pure geometry from canvas.py |
| `bbox` | mu-quantized quad, **rounded to a coarse grid (e.g. 1% of page)** | geometry; coarse grid kills any residual glyph-level length signal |
| `char_count_class` | log-bucket enum: `xs(<8)/s(<32)/m(<128)/l(<512)/xl` | order validation only needs "is this a fragment or a wall of text"; bucketing destroys the length oracle (a 5-char vs 7-char name both → `xs`) |
| `line_count_class` | 1 / 2–3 / 4+ | same reasoning |
| `ends_terminal_punct` | bool | 1 bit; reveals "sentence ended", not what it said |
| `starts_lowercase` | bool | 1 bit; continuation signal |
| `ends_hyphen` | bool | 1 bit; classic continuation signal |
| `height_class` | font-height quantile class per page (small/body/large/display) | relative typography, no absolute metrics |
| `script_class`, `digit_ratio_class` | enums (latin/cjk/…; none/low/high/all-digits) | coarse classes; "all-digits" flags an ID/MRZ *zone type*, not its value |
| `alignment_class` | left/right/center/justified (from indentation variance) | Tesseract-style derived geometry |

### FORBIDDEN, with reasons

- **Any string from the document** — tokens, n-grams, prefixes, first-letters, key/field names. Field labels ("Surname") are document text and template fingerprints; zero-string policy is the only auditable rule.
- **Hashes of text** — low-entropy PII (names, dates, ID numbers) is dictionary-attackable; a hash is the value.
- **Exact char/word counts, per-line or per-word length sequences** — the length-oracle and Whisper-Leak inversion results above.
- **Exact font sizes / colors / precise sub-percent coordinates** — device fingerprinting of the template and residual glyph-metric leakage; classes and coarse grids carry all ordering signal needed.
- **Free-text "reason" fields from us to the model** — nothing composed from document content may ride along in the prompt.

One residual risk to state honestly in the design doc: even the bucketed schema fingerprints the **template** (which is fine — templates aren't people) and leaks **document type**; it does not, under the schema above, distinguish two different people filled into the same template — which is the test the schema must pass.

## 3. Propose-verify: keeping the model advisory

- **Constrained decoding** (Outlines, llguidance, vLLM structured output; OpenAI/Anthropic JSON-schema modes) guarantees **syntactic** validity by masking illegal tokens at every step — use it so parsing never fails ([zeroentropy explainer](https://zeroentropy.dev/concepts/constrained-decoding/)). Two caveats from the literature: over-tight formats can degrade reasoning quality (["Let Me Speak Freely?", arXiv 2408.02442](https://arxiv.org/pdf/2408.02442)) — keep the op schema flat and small; and grammar constraints are not a security boundary ([arXiv 2503.24191](https://arxiv.org/pdf/2503.24191)) — semantic checking still belongs to the verifier.
- **LLM-Modulo** (Kambhampati et al.): LLMs cannot self-verify; correctness comes from a generate-test-critique loop where **sound external critics** accept/reject candidates. This is the canonical statement of "models propose, deterministic code decides."
- **Code-repair practice** ([Self-Debugging, arXiv 2304.05128](https://arxiv.org/pdf/2304.05128); generate-run-revise surveys): a patch is *never* trusted — it is accepted only when an executable oracle (tests) passes. Your executable oracle is the geometric invariant checker.
- **Self-consistency** (Wang et al. 2022) — sample k, majority vote. The refinement that matters for you: **edit-level majority voting** ([arXiv 2605.13624](https://arxiv.org/pdf/2605.13624), for grammatical error correction) votes on **individual edit operations across samples rather than whole outputs**, which mitigates over-correction. Vote per `link`/`move` op across k=3 samples; accept an op only with ≥2/3 agreement.

## 4. Determinism + audit provenance

- **EU AI Act Article 12** ([text](https://artificialintelligenceact.eu/article/12/)): automatic event logging over the system lifetime, traceability of each outcome to the data and **model version** that influenced it, retention ≥6 months. Assessor practice: "if you can't trace a result, compliance becomes problematic."
- **Decision provenance** (Singh, Cobbe & Norval, IEEE Access 2019, [arXiv 1804.05741](https://arxiv.org/abs/1804.05741)): record the chain of inputs to, nature of, and flow-on effects from every decision point — the academic template for your per-op accepted/rejected ledger.
- **Model cards** (Mitchell et al. 2019) cover the model side; what you need per *document* is the decision-trace side: emerging work on decision-trace schemas for governance evidence (arXiv 2604.09296) points the same way — a typed, append-only record per decision.

**Concrete pattern for DPC** (matching the sibling-service rule): the heuristic PMD remains the stored, sha256-stamped artifact, byte-identical regardless of LLM availability. The LLM pass writes a **separate versioned artifact**: `arrangement_suggestion` = {suggestion_id, document_version_ref, model_id, prompt_template_version, **prompt_hash** (sha256 of the exact structural payload — provable no-PII: the payload can be disclosed to a regulator), raw ops, per-op verdict accepted/rejected + verifier rule id, verifier_version}. If any ops are accepted, a **second PMD variant** is derived by deterministic re-application of the accepted patch, stamped `decided_by: heuristics+patch@suggestion_id`; the heuristic-only PMD stamps `decided_by: heuristics`. A regulator can distinguish the two on any single document by the stamp alone, and re-derive the patched artifact deterministically from (heuristic PMD + accepted ops).

## 5. Continuity-break signals without text (published heuristics)

- **Tesseract** (`paragraphs.cpp`/`paragraphs.h`, [source](https://github.com/tesseract-ocr/tesseract/blob/main/src/ccmain/paragraphs.h)): classifies each line as **start-line vs body-line**. Start evidence: capital-letter first word, list markers ("2.", bullets), first-line-indent differing from body-indent. End evidence: terminal punctuation. When textual clues fail it falls back to **outline-only** reasoning: left/right indentation **variance** infers alignment (small left variance + large right variance ⇒ left-aligned), and un-indented openers become "crown" paragraphs matched to the body model later. Every one of these is computable from your TextLine geometry + two booleans.
- **GROBID/pdfalto** ([docs](https://grobid.readthedocs.io/en/latest/Principles/)): XY-projection + heuristics for token/line/block boundaries, text-order recovery and column detection at the parser layer.
- **pdftotext++ / Bast & Korzen** ([benchmark paper](https://ad-publications.cs.uni-freiburg.de/benchmark.pdf), [repo](https://github.com/ad-freiburg/pdftotext-plus-plus)): rejoins words hyphenated at line/column breaks (hyphen + whitespace continuation pattern, keeping compound-word hyphens); treats a paragraph interrupted by a formula/figure, or ending at the bottom of a column/page and continuing on the next, as one paragraph — while noting the same interruption can be a real break, i.e. the decision needs the conjunction of signals, not any one.
- **Docling** ([arXiv 2408.09869](https://arxiv.org/html/2408.09869v5)): a deterministic post-processing model *corrects reading order* and matches figures with captions after ML layout analysis — precedent for "order correction as a distinct deterministic pass."

**Consolidated continuation rule** (block A end-of-column/page → block B top-of-next): score = `ends_hyphen(A)` (strongest) + `¬ends_terminal_punct(A)` + `starts_lowercase(B)` + `height_class(A)==height_class(B)` + `alignment/width model match` + `A bottom-of-frame ∧ B top-of-frame` + `type(A)==type(B)==paragraph`. **KYC caveat (flag): `starts_lowercase` is void for all-caps documents (passports, MRZ) and non-bicameral scripts (CJK, Devanagari) — gate it on `script_class` and a per-page all-caps detector, else it silently biases against merging.**

---

## Recommended LLM-validation protocol (one page)

**Position in pipeline:** heuristic tree build (canvas.py order) → derive structural features → **LLM advisory pass** → deterministic verifier → flatten. The stored heuristic PMD is produced unconditionally, before and independent of the LLM.

**Input schema (per call, one page or one band-group, ≤40 nodes):** ordered list of nodes exactly per the SAFE table in §2 — `{id, type, page, band, frame, coarse_bbox, char_count_class, line_count_class, ends_terminal_punct, starts_lowercase, ends_hyphen, height_class, script_class, digit_ratio_class, alignment_class}` plus the heuristic successor order as id pairs. **No strings from the document, ever; the payload is hashable and disclosable.**

**Output op language (JSON-schema-constrained decoding):**
- `{"op":"flag_break","after":id,"confidence":0..1}` — continuity break in heuristic order
- `{"op":"link","from":id,"to":id}` — assert logical succession (e.g. cross-column continuation)
- `{"op":"move","node":id,"before":id}` — reorder within page
- `{"op":"defer","node":id}` — demote furniture/sidebar to end
No merge/split in v1 (they mutate node identity; add only with block-text-reconstruction gating like the canvas coverage gate).

**Verifier (deterministic, versioned, the only writer):** reject any op that (1) references unknown ids; (2) breaks permutation validity — after applying all accepted ops every node appears exactly once; (3) crosses type constraints (can't `link` into furniture, can't move a table row out of its table); (4) violates geometric plausibility — `link` allowed only when the §5 continuation score ≥ threshold (LLM can *confirm* a geometrically plausible link, never *create* a geometrically impossible one); (5) moves a node across a page except column-continuation cases; (6) arrives without 2-of-3 self-consistency agreement across k=3 samples (edit-level voting) or below confidence 0.7. Accepted ops become the versioned `arrangement_suggestion` artifact of §4; the patched PMD variant is derived deterministically and stamped `decided_by`.

**When it runs:** async, post-heuristic, only when the heuristic itself signals uncertainty (multi-frame pages, low continuation-score ties, coverage-gate fallbacks) — not on clean single-column documents. Cost: k=3 samples × ~1–2K structural tokens per page-group ≈ trivially small vs OCR cost; the real budget is latency, which async absorbs.

**When the LLM is unavailable / unconfigured / times out:** nothing blocks — heuristic PMD is already stored and remains the default serving artifact; the artifact's pass manifest records `llm_pass: skipped(reason)` vs `ran(suggestion_id, n_accepted, n_rejected)`, so absence is stated, not silent.

**Flagged as unverified:** (a) no published bbox-only-LLM reading-order system found — the closest are Layout2Pos (specialized, layout-only) and LayoutGPT (LLM, geometry, but generation) — so quality of a general LLM at this task must be measured on your recorded Azure payloads before enabling by default; (b) Layout2Pos internals summarized from abstract/reviews only (Springer full text paywalled); (c) exact feature list of Google's sparse-graph model unverified beyond its language-agnostic claim; (d) LayoutReader layout-only ablation numbers not verified first-hand.

Sources:
- [Layout2Pos (Springer)](https://link.springer.com/chapter/10.1007/978-3-031-72437-4_1) / [OpenReview](https://openreview.net/forum?id=hxI3il74u5I)
- [Ordering Relations for Reading Order — arXiv 2409.19672](https://arxiv.org/html/2409.19672)
- [Sparse Graph Segmentation reading order — arXiv 2305.02577](https://arxiv.org/abs/2305.02577)
- [LayoutReader — arXiv 2108.11591](https://ar5iv.labs.arxiv.org/html/2108.11591)
- [LayoutGPT — NeurIPS 2023](https://proceedings.neurips.cc/paper_files/paper/2023/file/3a7f9e485845dac27423375c934cb4db-Paper-Conference.pdf) / [site](https://layoutgpt.github.io/)
- [LayTextLLM — arXiv 2407.01976](https://arxiv.org/html/2407.01976v2)
- [Whisper Leak — Microsoft](https://www.microsoft.com/en-us/security/blog/2025/11/07/whisper-leak-a-novel-side-channel-cyberattack-on-remote-language-models/) / [arXiv 2511.03675](https://arxiv.org/html/2511.03675v1)
- [Token-length side channel — USENIX Sec '26](https://www.usenix.org/conference/usenixsecurity26/presentation/li-sijia)
- [Remote keylogging on AI assistants — arXiv 2403.09751](https://arxiv.org/pdf/2403.09751)
- [EDPB pseudonymisation guidelines coverage — Hunton](https://www.hunton.com/insights/publications/edpb-advises-on-pseudonymisation-for-gdpr-compliance) / [McCann FitzGerald](https://www.mccannfitzgerald.com/knowledge/technology-and-innovation/pseudonymisation-under-gdpr-edpbs-latest-guidelines)
- [Unique in the Crowd (de Montjoye)](https://www.researchgate.net/publication/236076438_Unique_in_the_Crowd_The_Privacy_Bounds_of_Human_Mobility) / [credit-card metadata](https://dspace.mit.edu/bitstream/handle/1721.1/96321/UniqueInTheShoppingMall_draft.pdf?sequence=1)
- [Constrained decoding — zeroentropy](https://zeroentropy.dev/concepts/constrained-decoding/) / ["Let Me Speak Freely?" — arXiv 2408.02442](https://arxiv.org/pdf/2408.02442) / [grammar attack surface — arXiv 2503.24191](https://arxiv.org/pdf/2503.24191)
- [LLM-Modulo — emergentmind overview](https://www.emergentmind.com/topics/llm-modulo-framework)
- [Self-Debugging — arXiv 2304.05128](https://arxiv.org/pdf/2304.05128) / [edit-level majority voting — arXiv 2605.13624](https://arxiv.org/pdf/2605.13624)
- [EU AI Act Article 12](https://artificialintelligenceact.eu/article/12/) / [ISMS Article 12 guide](https://www.isms.online/iso-42001/eu-ai-act/article-12/)
- [Decision Provenance — arXiv 1804.05741](https://arxiv.org/abs/1804.05741)
- [Tesseract paragraphs.h](https://github.com/tesseract-ocr/tesseract/blob/main/src/ccmain/paragraphs.h) / [paragraphs.cpp](https://zdenop.github.io/tesseract-doc/paragraphs_8cpp_source.html)
- [GROBID principles](https://grobid.readthedocs.io/en/latest/Principles/)
- [pdftotext++](https://github.com/ad-freiburg/pdftotext-plus-plus/blob/master/project.description) / [Bast & Korzen benchmark](https://ad-publications.cs.uni-freiburg.de/benchmark.pdf)
- [Docling Technical Report — arXiv 2408.09869](https://arxiv.org/html/2408.09869v5)