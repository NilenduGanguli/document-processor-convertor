# Handoff — document-processor-convertor (DPC)

**Written:** 2026-09-02 · **Last commit at handoff:** `1464ad8` · **Branch:** `main`, pushed,
working tree clean · **Suite:** 644 passed, 2 xfailed · **Repo:**
https://github.com/NilenduGanguli/document-processor-convertor

This document exists so a new session — new account, no prior context — can be productive in
about ten minutes. It records what is built, **what is verified versus merely written**, how
to run it, what is deliberately unfinished, and the traps that cost real time.

Read §1 and §2, run §3, then go to §7 for what to do next.

---

## 1. What this service is

DPC converts documents into **positional markdown** and stores the artifacts in S3/MinIO with
rows in Postgres. Input is either a raw document (upload or base64) or a provider payload
already in hand (Azure Read / Azure Layout / DES OCR). Output is markdown that preserves
**where text sat on the page**, plus — new in this session — a **logical document tree**.

There are three artifact layers, each stored separately and each content-addressed:

| Artifact | Produced by | Notes |
|---|---|---|
| **PMD 2.0** (`.md`) | `dpc/emitter.py` | Linear GFM, except bands of genuinely parallel content, which render as fenced space-padded **canvases** with an invertible frame table on the anchor. |
| **DocTree** (`doctree.json`) | `dpc/doctree/` | Logical reading order as a node tree. **Carries zero document strings** — nodes index into the stored `LayoutView`. |
| **PMD 3.0** (`tree.md`) | `dpc/treemd.py` | The tree flattened in body pre-order. Stored *beside* PMD 2.0, never replacing it. |

Plus an optional advisory artifact: **`arrangement.json`** from an LLM pass that reviews
reading order and proposes edit operations. It never mutates the stored tree.

### Sibling repos (same machine, referenced throughout)

| Path | What it is |
|---|---|
| `~/document-classification-extraction` | **DCE** — KYC document classifier. Source of the corpus used to test DPC, and of several design lessons quoted in DPC's code comments. Also has unfinished work of its own — see §8. |
| `~/document-enrichment-services` | **DES** — OCR→chunk→embed pipeline. Provides the **local Azure DI mock** DPC tests against, and the precedent for the Gemini/Vertex client wiring. |
| `~/document_intelligence` | Docs hub. **`stellar-azure-document-processing.md` is the authority on the Stellar gateway and COIN OAuth2** — read it before touching `dpc/arrange/client.py`'s stellar branch. |

---

## 2. State of play — verified vs. written

Be precise about this distinction; it is the most useful thing in this document.

### Verified by execution ✅

- **Azure-only routing.** Every PDF and image goes to Azure DI `prebuilt-layout`. The local
  PyMuPDF *text-extraction* path is deleted (PyMuPDF survives only as a corrupt/encrypted
  pre-check). Verified on 8 real corpus PDFs → all reported `provider=azure_layout`.
- **The spatial canvas.** Two-column pages render as two columns. Pinned by tests that assert
  what a human would check: both columns on one line, in order, ≥3 spaces apart, left column
  starting at an identical offset on every row.
- **The inch-anchor fix.** Azure reports PDF geometry in **inches**; PMD 1.0 rounded to
  integers and collapsed every US-Letter page onto an 8×11 grid — distinct rows emitted
  *identical* anchors. Now milli-inches under a declared `scale=1000`. Point/pixel pages are
  byte-identical to before (golden test).
- **DocTree end to end.** 22/22 corpus files converted with `tree_status=built`. A live 6-page
  W-9 produced 112 nodes, 80/80 blocks claimed, and — visible in the console — the geometry
  audit demoting the mock's unreliable sections: `provider_sections: conflict_demoted(pages=[3,4,6])`.
- **Gemini 2.5 Flash multimodal, live.** Through Vertex with the service-account key. The
  arrange pass ran end to end: `status=ran, payload_mode=multimodal, model=gemini-2.5-flash`,
  three samples parsed, **zero ops proposed on a correctly-ordered document** (the honest
  negative result). Artifact contained no image bytes (sha only) and no document n-grams.
- **`/api/v1/process`.** Multipart upload verified locally and again through the rebuilt
  container: real W-9 → `azure_layout` → tree built.
- **The console.** QA'd by hand in a browser against the live service; all four result tabs
  render real data.

### Written, designed, NOT proven ⚠️

- **The Stellar gateway path.** Implemented per the handoff doc (COIN OAuth2, base64-encoded
  credentials, `coinscope` scope prefix, `verify=False` on the token POST only, `SSL_CERT_FILE`
  on API calls, 840 s token TTL) and tested **against mocked transports only**. The COIN
  endpoint is VDI-only; it cannot be reached from this laptop. **First real run must be inside
  the corporate network.**
- **The accepted-op path of the arrange pass.** The verifier's accept branch is exercised by
  fixtures, not by a live model that actually proposed a good op. The live run correctly
  proposed nothing.
- **Whether the LLM is any good at this.** There is no published precedent for geometry-only
  LLM reading-order correction. The pass ships **`arrange_mode=off`**. `SPEC-DOCTREE-1` §11
  sets the promotion gate: shadow mode over ≥50 labelled documents, ≥95% accepted-op precision,
  and a standing **kill criterion** — under 90% after iteration kills active mode permanently
  and the pass degrades to a review-queue triage tool.
- **Recursive XY-cut** exists in `dpc/doctree/build.py` but has only synthetic-fixture coverage.

### Known limitation, by design

The local DI mock is Tesseract-based and returns lower-quality structure than real Azure. On
the W-9 it attached 194/258 lines, which **trips the coverage gate** — the tree correctly
declines to render spatially when it cannot prove no text is lost. The `line_join: attached/total`
field in the front matter exists precisely so this is diagnosable rather than mysterious.
**Canvas/tree quality cannot be fully measured until real Azure DI is available.**

---

## 3. Get running in ten minutes

```bash
cd ~/document-processor-convertor

# 1. Bring the stack up (Docker Desktop must be running first)
docker compose up -d                      # app :8300, postgres :5438, minio :9004/:9005
curl -s localhost:8300/readyz             # {"ready":true,"checks":{"postgres":true,"s3":true}}

# 2. The OCR mock lives in the DES stack — DPC needs it for any PDF/image
cd ~/document-enrichment-services && docker compose up -d azure-di-mock   # :5007
```

> Containers were **stopped** at handoff time. Both `docker compose up -d` commands above are
> required. `curl localhost:5007/` returning 404 is correct — the mock only serves DI paths.

```bash
# 3. Tests (fast, no services needed — everything is stubbed)
cd ~/document-processor-convertor
.venv/bin/pytest tests -q                 # expect: 644 passed, 2 xfailed
.venv/bin/ruff check dpc/ tests/          # expect: All checks passed!

# 4. Convert something
curl -F file=@/Users/neelu/document-classification-extraction/corpus/us/us_w9.pdf \
     -F doc_id=smoke http://localhost:8300/api/v1/process
```

Open **http://localhost:8300** for the console.

### Running the full stack with tree + live Gemini

The container does not carry the Vertex credential. For arrange work, run the app from the
venv instead:

```bash
cd ~/document-processor-convertor
DPC_TREE_MODE=emit \
DPC_ARRANGE_MODE=active \
DPC_ARRANGE_PROVIDER=vertex \
DPC_ARRANGE_MODEL=gemini-2.5-flash \
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.secrets/vertex-extraction-external-key.json \
DPC_AZURE_DI_ENDPOINT=http://localhost:5007 DPC_AZURE_DI_KEY=mock \
DPC_OCR_TIMEOUT_SECONDS=600 \
.venv/bin/uvicorn dpc.api:app --host 127.0.0.1 --port 8301
```

Note `--port 8301` to avoid colliding with the container on 8300.

---

## 4. The API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/process` | **Upload face.** multipart: `file` (one file) + optional `doc_id` form field. Delegates to `convert()`. |
| POST | `/api/v1/convert` | JSON: exactly one of `content_base64` \| `azure_read_result` \| `azure_analyze_result` \| `des_ocr`. |
| GET | `/api/v1/conversions` | Index; `limit`/`offset`. |
| GET | `/api/v1/conversions/{id}` | One row. |
| GET | `/api/v1/conversions/{id}/markdown` | PMD 2.0 bytes. |
| GET | `/api/v1/conversions/{id}/tree` | `doctree.json`. |
| GET | `/api/v1/conversions/{id}/tree.md` | PMD 3.0; `?arrangement=<id>` serves an active-mode variant. |
| GET | `/api/v1/conversions/{id}/arrangement` | The advisory artifact, or 404 with a reason. |
| GET | `/api/v1/conversions/{id}/figures/{figure_id}` | Figure crop. |
| GET | `/health`, `/readyz` | `/readyz` checks Postgres **and** S3. |

**`/process` delegates to `convert()` on purpose** — one pipeline, so the two faces cannot
drift. Tests assert identical artifact shas through both. Refusals are structured and carry
remedy text: 415 `unsupported_media_type`, 422 `needs_ocr`, 413, 400.

---

## 5. Configuration that matters

Full surface with per-setting rationale is in `dpc/config.py` (read the comments — each
default is argued). The ones you will actually touch:

| Setting | Default | Meaning |
|---|---|---|
| `DPC_TREE_MODE` | `build` | `off` \| `build` (store tree) \| `emit` (also store PMD 3.0) |
| `DPC_ARRANGE_MODE` | `off` | `off` \| `shadow` (artifact only) \| `active` (derive variant) |
| `DPC_ARRANGE_PAYLOAD` | `multimodal` | `multimodal` (page images) \| `structure` (features only) |
| `DPC_ARRANGE_PROVIDER` | `""` | `stellar` \| `vertex` \| `stub` \| empty = unavailable |
| `DPC_ARRANGE_MODEL` | `gemini-2.5-flash` | |
| `DPC_OFFICE_ROUTE` | `local` | XLSX/HTML keep native readers — **see §6** |
| `DPC_TEXT_ROUTE` | `plain` | `.txt`/`.csv`/`.md` read locally, not sent to DI |
| `DPC_AZURE_DI_ENDPOINT` | `""` | **Empty ⇒ 422 refusal**, never a silent local fallback |
| `DPC_PMD_RECT_SCALE` | `auto` | `legacy` reproduces PMD 1.0 rounding for a stored hash |

**Stellar (in-network only):** `DPC_ARRANGE_PROVIDER=stellar`, `DPC_ARRANGE_ENDPOINT`,
`DPC_COIN_URL`, `DPC_COIN_CLIENT_ID`, `DPC_COIN_CLIENT_SECRET`, `DPC_COIN_SCOPE` (the three
COIN credentials are **base64-encoded** in env and decoded at use; scope gets the literal
`coinscope` prefix), `SSL_CERT_FILE`, `DPC_COIN_TOKEN_TTL_SECONDS` (840).

---

## 6. Decisions you should not silently reverse

Each of these was argued from measurement. If you change one, change it deliberately.

1. **Office/HTML are NOT sent to Azure**, despite the instruction being "send everything to
   Azure." Research established that DI returns **no geometry for Office/HTML inputs and no
   tables at all for XLSX** — routing there satisfies the letter and destroys the output.
   `DPC_OFFICE_ROUTE=azure` exists if you want the literal behaviour. This was flagged to the
   owner explicitly, not decided quietly.
2. **No endpoint configured ⇒ refuse (422), never fall back.** A local fallback produces a
   valid-*looking* file with no signal that its positions are worthless.
3. **The LLM never mutates a stored artifact.** Models propose; a deterministic verifier
   decides; the artifact stamps what decided it (`decided_by: heuristics` vs
   `heuristics+patch@<sha8>`). This preserves the frozen-rules property a control reviewer is
   promised.
4. **The DocTree carries zero document strings, enforced by type.** Nodes index into the
   `LayoutView`. PII safety is a property of the schema, not a grep test.
5. **Flow edges annotate; they never reorder.** A continuation hint cannot silently rewrite
   reading order.
6. **Determinism is a product contract.** Stored sha256 is meaningful. No wall-clock in hashed
   bytes, integer-only geometry after `mu()`, total sort orders with named tiebreakers,
   canonical JSON everywhere.

---

## 7. What to do next

**Immediately valuable, no blockers:**

1. **Run the corpus sweep with trees on.** Only 22 files were swept. `tools/corpus_sweep.py`
   plus DCE's `corpus/` (98 PDFs, 22 HTML, 2 XLSX, 2 JPG). Expect it to be slow — the Tesseract
   mock runs ~4 s/page.
2. **Reading-order fidelity at scale.** `tests/fixtures/order/` holds hand-labelled expected
   orders (`two_column`, `provider`, `footnote`) and asserts Kendall tau = 1.0 on them. The
   labelled set is small; extending it to real documents is the highest-value test work left.
3. **Measure the `conflict_demoted` rate.** `SPEC-DOCTREE-1` §11 P1-a sets a threshold: if the
   geometry audit demotes provider sections on **>20% of provider-seeded pages** across the
   corpus, stop trusting Azure's section order — geometry becomes the default rung and sections
   seed grouping only. The live W-9 already demoted 3 of 6 pages, but that is against the
   Tesseract mock, so the number means little until real DI is available. Instrument it.

**Blocked on the corporate network:**

4. **First Stellar run.** Set the COIN vars + `SSL_CERT_FILE`, start in `DPC_ARRANGE_MODE=shadow`.
5. **Real Azure DI.** Everything about canvas/tree quality is measured against a Tesseract mock
   today. Expect `line_join` to jump toward 100% and the coverage gate to stop declining.

**Deliberately deferred:**

6. **Arrange promotion to `active`** — needs the §11 gate: ≥50 labelled docs, ≥95% precision.
7. **Sub-page regions** (two documents photographed on one sheet) — designed in DCE's
   `docs/specs/2026-08-17-subpage-regions-design.md`, parked by the owner.

---

## 8. Unfinished work in the sibling DCE repo

Also from this session, and **not implemented** — plans only:

- `~/document-classification-extraction`, branch **`improvement/core-visual-roadmap`**
  (commit `d54193f`), file `docs/specs/2026-08-25-implementation-plan.md`: seven builder-agent
  work orders with exclusive file ownership and measurement gates.
- On `main` (`e6bf07e`): `docs/specs/2026-08-25-core-and-visual-roadmap.md` — the reasoning
  behind them.

Headline findings there, measured first-hand: DCE's abstentions split by failing gate into
**concurrence 9 / separation 4 / coverage 1**, forming three populations (anchor-gap ~5,
registry defects 2, card-evidence starvation ~5). A wire-format defect blocks the highest-value
work — `runners_up` still ships a **registry-normalised softmax**, the N-dependent quantity the
evidence-in-bits rewrite removed from decisions, so any fork mining confusions from the wire is
standing on the fixed defect. Fix that first (`WS1`, audit-only, provable by a byte-identical
corpus run).

---

## 9. Traps that cost real time

- **A hidden Browser pane screenshots black.** Not an app bug. Front the tab, or use
  `read_page` / `get_page_text` / `javascript_tool`, or emulate a tall viewport so content sits
  at scroll 0.
- **Gemini fences its JSON in ` ```json ` even at temperature 0.** `dpc/arrange/ops.py::_strip_fence`
  handles one fence; anything worse is discarded as malformed.
- **`genai.Client()` bare hunts for `GOOGLE_API_KEY`** and dies `ApiKeyMissing` even with a
  perfectly good service account. Must be `genai.Client(vertexai=True, project=..., location=...)`.
  `dpc/arrange/client.py` reads `project_id` from the SA file when `arrange_vertex_project` is empty.
- **Under Azure-only routing, a PDF fixture in a test needs a DI stub.** Use `.txt` (routes
  locally via `text_route=plain`) when the endpoint's *face* is what you are testing.
- **`TestClient` re-raises server exceptions.** Refusal tests need
  `TestClient(app, raise_server_exceptions=False)`.
- **Python's `uuid.UUID` grammar is wider than Postgres's** — `urn:uuid:…` parses in Python and
  500s on a `uuid` column. One shared gate lives in `dpc/db.py::_valid_uuid`; `api.py`
  re-exports it. Do not write a second copy.
- **The container mounts no source.** Any code change needs
  `docker compose build app && docker compose up -d --force-recreate app`, then verify the
  rebuild actually took before believing a result.

---

## 10. How this work was produced

Worth continuing, because it repeatedly caught defects that would have shipped.

Multi-agent workflows with **exclusive file ownership** (no two agents ever write the same
file), each build wave followed by an **independent reviewer** that verifies against disk
rather than trusting the builder's report, then **fixers**, then **recheckers** that reproduce
each original failure and confirm every new regression test goes red when the fix is reverted
in a scratch copy.

What that discipline actually found: a **PII-carrying exception** escaping `build_doctree`; a
verifier accepting moves into fence-rendered regions where the moved text **silently
vanished**; a two-byte `BM` magic check classifying `"BMW Finance Ltd…"` as a bitmap and
shipping it to Azure as an image; every new refusal returning **500 instead of 415**; a
**determinism** defect where the placement sort fell through to the provider's *arrival index*
(5 of 800 fuzzed pages changed bytes under permutation); and an O(lines×blocks) line join
measured at **49 seconds** on a 400-page document.

Three further defects were found only by **running the thing live** — the bare `genai.Client()`,
a `should_run` gate that skipped the exact multi-column page a reading-order validator exists
for, and `n_rejected` counting a verdict string the verifier never emits. Fixtures alone would
not have caught any of them. **Run it live before believing it.**

---

## 11. Specs — read these before changing behaviour

| File | What it settles |
|---|---|
| `docs/SPEC-PMD-2.md` | The band/canvas emitter. Every constant sourced to poppler/pdfminer/Tesseract line numbers. |
| `docs/SPEC-DOCTREE-1.md` | The tree, the LLM protocol, the verifier rules V1–V9, the phased plan, the kill criteria. **§10 records the owner's authorization for multimodal payloads** and its limits. |
| `docs/SPEC-DOCTREE-1-research.md` | The evidence: enterprise document-tree systems (Docling, Textract LAYOUT, Google DocAI, PDF/UA) and PII-safe LLM-over-geometry prior art. |
| `docs/SPEC-PMD.md` | PMD 1.0, with a section explaining exactly what 2.0 changed and why. |
| `docs/SESSION-LOG-2026-09.md` | **Why** things are the way they are: what was asked, what was decided, what was measured, and the corrections made along the way. Read this when a decision looks arbitrary. |
