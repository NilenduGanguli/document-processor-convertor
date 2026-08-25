# document-processor-convertor — design

**Date:** 2026-08-17 · **Status:** v1, implemented

One sentence: any supported reading of a document — the document itself, an Azure Read or
Azure Layout result, or a DES output — becomes one deterministic, position-preserving
markdown file in S3, with its record in Postgres, so every downstream AI workflow consumes
documents in exactly one shape.

## Why markdown at all

The grand pipeline (DES enriches, DCE classifies and extracts, DAS orchestrates) keeps
needing the same thing: a representation of a document that an LLM can read natively, that
survives chunking for retrieval, that a human can diff, and that does not lose where things
sat on the page. JSON layouts fail the first three; plain text fails the last. Markdown with
inline positional anchors is the only shape that passes all four, which is why this service
exists as a separate, single-purpose unit rather than a feature inside one of the others.

## The format decision that everything else follows

**PMD: GitHub-flavoured markdown plus one HTML-comment anchor per element** —
`<!-- @2 [93,319,434,347] title -->` on the line above the element it describes.
Normative spec: `docs/SPEC-PMD.md`.

Alternatives considered and rejected:

| Alternative | Why not |
|---|---|
| Geometry appendix at file end, keyed by block id | dies at CHUNKING — the first thing every RAG pipeline does. An appendix is never in the same chunk as its block. Inline anchors travel with any chunk containing their element. |
| Per-word coordinates | ~10x the tokens for precision nothing downstream uses; block rectangles answer "where on the page" |
| HTML/hOCR output | faithful, unreadable to humans, and hostile to LLM prompting; markdown-native structure (headings, GFM tables, task-list checkboxes) is the point |
| Invent bboxes for blocks that lack them | a consumer cannot tell measured from made-up; the rule is **no bbox, no anchor** |

Two more load-bearing choices, argued in `dpc/emitter.py`'s docstrings: **provider order is
reading order** (a naive y-sort interleaves columns line by line; Azure and PyMuPDF both order
better than rectangles can reconstruct), with tables/marks/key-values spliced in by
y-position; and **determinism** — same input, same bytes, so the stored sha256 means something
and re-conversions are comparable.

## Input matrix

| Input | Mapper | Provider recorded |
|---|---|---|
| document bytes: PDF with text layer | PyMuPDF `get_text("dict")` → blocks WITH rects | `pymupdf` |
| document bytes: image, or PDF with scanned pages | Azure DI (in-network endpoint) → `from_azure` | `azure_layout` |
| `azure_analyze_result` (Read or Layout payload) | `from_azure` — shape auto-detected | as detected |
| `azure_read_result` (declared Read v3.2) | `from_azure_read` | `azure_read_v3.2` |
| `des_ocr` | `from_des_ocr` | `des` |

The adapters are ported from the sibling DCE service verbatim (same author, same semantics):
battle-tested mapping beats a rewrite. A scanned document with no DI endpoint configured is a
structured `needs_ocr` refusal, never a guess — the posture every service in this fleet takes.

A mixed PDF (text pages + scanned pages) goes to DI **whole**: one document, one reading, one
provider recorded — the same one-document-one-reading rule DCE arrived at by measurement.

## Storage

- **S3** (MinIO locally): bucket `docmd`, key `pmd/{yyyy}/{mm}/{uuid}.md`. The markdown is the
  artifact; S3 is where artifacts live so any future consumer needs no service call to read one.
- **Postgres**: one `conversions` row per artifact — ids, source, provider, counts, both
  sha256es (input and markdown), S3 coordinates, status, timing. The table is the index and
  audit trail; the file is the truth.

## Parallelism

Conversion is CPU-light; the service is async end to end (FastAPI + thread-pooled PyMuPDF and
boto3), so concurrent requests overlap I/O. The heavier parallelism was spent where it pays:
the repo was built by four concurrent workstreams against a written contract (`CONTRACTS.md`),
and bulk backfills parallelise trivially — the converter is a pure function per document, so N
workers need share nothing but the bucket and the table.

## Security posture

Inherited from the fleet, deliberately: no document text in any log line (counts and hashes
only), `doc_id` treated as caller data, optional `X-API-Key` gate, keys from the environment
and never in the image, and the only outbound call the optional in-network DI endpoint.

## Fit in the pipeline

DES (enrich) · DCE (classify/extract) · DAS (orchestrate) · **DPC (this): normalise to PMD**.
Anything that wants "the document, as text, with structure and positions" — RAG ingestion,
agent tool-use, review UIs, diffing — reads the PMD from S3 and never re-parses a PDF again.
