# SPEC-DOCTREE-1 — Logical Document Tree, PII-Safe LLM Arrangement Validation, and Tree-Flattened Markdown

**Repo:** `/Users/neelu/document-processor-convertor` · **Target file:** `docs/specs/SPEC-DOCTREE-1.md`
**Status:** Approved-for-build. Welds the three slice designs (TREE / LLM / FLATTEN) with every judge-surfaced conflict resolved explicitly (§1.1).
**Inherited contracts:** deterministic sha256-stamped artifacts; integer (mu) geometry; conversion never raises; honesty counters; no log line, error, or external call carries document text.

---

## 1. DECISION SUMMARY

We build a logical document tree (`doctree.json`) between `from_azure_layout()` and the emitters: Docling-shaped flat node array + parent/children refs, two roots (`body`/`furniture`), **zero document strings by construction** — nodes reference content by index into the stored `LayoutView`. It is seeded from Azure `analyzeResult.sections`/`figures` (currently dropped by `adapters.py` — a verified gap, fixed in Workstream 0), audited by canvas geometry, and synthesized by XY-cut over the existing band/gutter machinery when sections are absent. Pre-order of `body` is the reading order; cross-column/page paragraph continuations are annotated by `continues` flow edges that never reorder the heuristic tree. Flattening produces a **new** artifact, PMD 3.0, stored beside the byte-untouched PMD 2.0, as a pure function of `(tree, view)` — both content-addressed. An async, off-by-default LLM pass reviews reading order from **structure-only features** (bucketed, edge-anchored, zero strings), emits ops verified by deterministic rules, and writes a separate provenance-stamped arrangement artifact; accepted ops deterministically derive a PMD 3.0 *variant* stamped `decided_by: heuristics+patch@{sha8}`. Models propose; the verifier decides; every artifact stamps what decided it; every skipped pass is stamped, never silent.

**Rejected:** text-in-tree and canvas-row embedding (FLATTEN §3.6) — it tripled text copies, created the admitted canvas-drift failure mode, and downgraded PII from a type-system property to a grep test. Rejected: `passes.llm_arrangement` inside the sha-stamped tree (TREE §2) — the heuristic artifact must be closed under its own inputs. Rejected: opaque `canvas` leaf nodes — they permanently cap recovery of mis-classified parallel regions. Rejected: float `confidence`, wall-clock `latency_ms` in hashed bytes, Azure's undocumented figure-id convention, LLM-generated image descriptions (Google `imageText` anti-pattern), and `merge`/`split` node mutation in v1.

### 1.1 Resolved disagreements (normative — every judge-surfaced conflict)

| # | Conflict | Resolution | Why |
|---|---|---|---|
| R1 | Does the tree carry document text? (TREE: never / FLATTEN: yes + canvas rows) | **TREE wins. Invariant I5 is system-wide: no `str` field in `doctree.json` can carry content.** | All three judges converged: type-enforced PII beats a grep tripwire; kills the third text copy and canvas-row drift (FLATTEN's own failure mode #2). |
| R2 | Is `view` an input to flatten? | **Yes: `flatten(tree, view, …)`.** Tree pins `view_sha256`; flatten refuses on mismatch. | Regulator replay = (view + tree [+ patch]), all content-addressed and already stored. "Pure function of tree bytes" bought nothing the sha-pin doesn't. |
| R3 | Op vocabulary (3 incompatible sets) | **LLM's vocabulary** — `move_before, move_after, reparent, merge_flow, split(advisory-only), flag_break` — **with path-string addressing** in stored artifacts. `link`/`move`/`defer` vocabularies are deleted. `reparent` gets a consumer (`apply_patch`); `defer` is expressed as `move_after` to the section tail. | Richest verifier semantics (enterprise judge); paths are content-free, deterministic, and match the front-matter audit spine. Model emits compact `n{k}` ids per window; the verifier resolves to paths before storage. |
| R4 | `confidence: float` in TREE's interface | **`confidence_pm: int` (0–1000) everywhere.** | No float ever crosses into a deterministic path. |
| R5 | `passes.llm_arrangement` inside `doctree.json` | **Evicted.** LLM status lives only in the arrangement artifact + DB row. | Either it breaks the stored tree sha or it lies forever; both judges flagged it. |
| R6 | V2/V5 deadlock kills cross-page `merge_flow` | **V2 amended:** a `context:true` node MAY be the `node` (source) of `merge_flow` when `ref` is in-window and V5's cross-page geometric gate holds. All other ops on context nodes stay rejected. | Both judges found the deadlock; cross-page continuation is the flagship value-add over Azure (which never orders across pages). |
| R7 | `x0_pm` + `alignment_class` leaks width of right-aligned values | **Send only the alignment-anchored edge** per node: `anchor_edge ∈ {left,right,center}` + `anchor_pm` (1% grid of the template-fixed edge); the free edge is never sent; extent stays `w_class`. Two-identities fixture MUST include a right-aligned filled field. | x0 of a right-aligned box = x1 − width: a 1%-grid position was a ~1-char-resolution length oracle. |
| R8 | `latency_ms` in the sha-stamped arrangement artifact | **Removed from hashed bytes.** Timings go to logs only (`arrange.done … ms=`). | Re-running the verifier on the recorded artifact must reproduce it byte-for-byte. |
| R9 | V4's unexplained 9% furniture margin | **Named constant `FURNITURE_MARGIN_PM = 90`,** justified: running headers/footers occupy ≈0.75–1 in of an 11 in page (68–91‰); 90 covers the range with OCR jitter. Versioned under `VERIFIER_VERSION`. | "All constants justified" must be true. |
| R10 | Two continuation-score rubrics (TREE §5 vs LLM V6) | **One function: `dpc/doctree/continuity.py::continuation_score`,** imported by builder AND verifier. Welded rubric in §3.3 with two named thresholds: `CONT_EDGE_MIN=5` (builder emits edge; ≡ TREE's 3 + the now-explicit +2 adjacency bonus) and `CONT_CONFIRM_MIN=4` (verifier lets the LLM confirm). V6's private formula is deleted. | "Geometrically plausible" must mean one thing in exactly one place. |
| R11 | `link` semantics (annotate vs reorder) | **Heuristic tree: `continues` edges annotate, never reorder** (TREE). **Patched variant: an accepted `merge_flow` op both adds a flow-join and repositions `ref` as immediate successor** — but only in the derived variant tree, never in the stored heuristic artifact. | Both statements were right about different artifacts; the spec now says which. |
| R12 | `apply_patch` implemented three times | **One implementation: `dpc/doctree/patch.py::apply_patch(tree, ops) -> (DocTree, flow_joins)`.** `arrange` and `treemd` import it. Refuses on `sha256_tree` mismatch (409 at API level); an op that no longer applies raises `PatchInvalid` — never partial application. | A partially applied patch with no error is unauditable. |
| R13 | Module layout (packages vs modules) | `dpc/doctree/` package (builder), `dpc/arrange/` package (LLM pass), `dpc/treemd.py` module (flattener), **`dpc/geom.py`** new shared module exporting `rect_scale`/`page_scale` (promoted from `emitter._rect`/`page_scale`; emitter imports them — byte-identical output proven by test). | canvas.py at 1716 lines argues for packages; private-helper coupling (`emitter._rect`) was the wrong mechanism for the right instinct (shared rounding). |
| R14 | Node-kind enum mismatch (lists missing; `canvas` vs `flow_group`) | **One enum in `dpc/doctree/models.py` (§2.1), TREE's taxonomy + `footnote`.** No `canvas` kind: parallel regions are `flow_group`/`frame`; the flattener decides rendering (§5.3). Unknown provider roles land as `paragraph` with the verbatim role in `prov` — never a build error, never a silent skip. | FLATTEN's "unknown kind = build error" would have detonated on every list; TREE's role-preservation degrades honestly. |
| R15 | Footnote/pull-quote/figure-interrupted flow (semantic judge's "one change") | **Interposer-demotion pass** (§3.2 step 7): Azure `footnote`-role blocks become kind `footnote`, deferred to their section's tail; continuity candidacy is relaxed to paragraph pairs separated only by non-paragraph leaves in the same frame (interposition counts as adjacency, +2). Geometric `aside` detection deferred to v2 (risk §9). | Converts three plausible-but-wrong flow cases to correct using evidence classes the design already has. |
| R16 | Float page dims in tree JSON | **Page dims stored as mu ints** (`width_mu`, `height_mu`). No float anywhere in `doctree.json`. | Canonical-bytes stability across writers. |
| R17 | `generated` timestamp vs variant replay | `generated` remains caller-supplied and included (2.0 convention). **The arrangement DB row records the exact `generated` string used for the variant**, so variant replay = pure function of recorded inputs. | Preserves 2.0 symmetry and the replay claim. |
| R18 | seq-fallback ≡ today's output (asserted, not proven) | **FLATTEN's §10.5 byte-equality test is the gate**; if the kv/mark y-splice of `emitter._linear_elements` disagrees with `(page, seq, block_ix)` fallback order, the fallback order is adjusted to match the splice — the 2.0 file is the spec, not the tree. | The claim was load-bearing and untested. |
| R19 | Coherence check audits overlap, not order | **Step 5b added:** same-frame sibling sections whose provider order inverts band order also demote the page. Cross-frame inversions are allowed (logical flow ≠ page order is the feature). | Catches the sidebar-before-body provider error without punishing legitimate reordering. |
| R20 | Figure id format (`fig-{page}-{k}` vs `{page}.{n}`) | **`fig-{page}-{n}`**, n = 1-based per page by `(y0, x0, provider_ix)`. Placeholder: `![figure fig-2-1](figure://{conversion_id}/fig-2-1)`. Azure's undocumented id preserved in `prov.provider_ref` only. | One convention; never depend on undocumented provider ids. |

---

## 2. THE DOCTREE SPEC

### 2.1 Node taxonomy (single enum, `dpc/doctree/models.py`)

```python
class NodeKind(enum.StrEnum):
    document   = "document"    # single root; children = [body, furniture]
    body       = "body"        # main-content root; pre-order = reading order
    furniture  = "furniture"   # pageHeader/pageFooter/pageNumber; excluded from flow
    section    = "section"     # heading + its content; nesting = heading levels
    flow_group = "flow_group"  # truly-parallel container; children are frames, read in order
    frame      = "frame"       # one column/panel of a flow_group (maps to canvas.Frame)
    heading    = "heading"     # leaf; level 1..4; refs one TextBlock
    paragraph  = "paragraph"   # leaf; refs one TextBlock (also unknown-role landing zone)
    footnote   = "footnote"    # leaf; Azure footnote role; deferred to its section's tail
    table      = "table"       # leaf; refs one Table (cells stay in LayoutView)
    figure     = "figure"      # image placeholder; bbox + optional caption child
    caption    = "caption"     # child of figure/table; refs one TextBlock
    kv_group   = "kv_group"    # spatial cluster of key-value pairs (KYC form panel)
    kv_pair    = "kv_pair"     # leaf; refs one KeyValue by index
    list_group = "list_group"  # v1: detection from provider role/indent only
    list_item  = "list_item"
    mark       = "mark"        # selection mark leaf; refs Mark by index
```

Deliberate v1 exclusions: no `toc`/`formula`/`aside` kinds; no block merge/split (node identity = block identity — the coverage-gate principle: we never manufacture text boundaries).

### 2.2 Stored JSON — schema `dpc.doctree/1`

Own artifact (`doctree.json`), S3 beside the PMD, sha256-stamped. **Invariant I5: no field can carry a document string** — the only `str` fields are closed enums, `path`, `figure_id`, and `prov.provider_ref` (JSON-pointer refs). Canonical bytes: `dump_tree(tree)` in `dpc/doctree/models.py` — `json.dumps(model_dump(exclude_none=True), sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()`. This is the **only** serializer allowed to feed `sha256_tree`.

```jsonc
{
  "schema": "dpc.doctree/1",
  "doc_id": "…",
  "view_sha256": "…",           // sha of the LayoutView JSON this tree indexes into
  "builder": "dpc-doctree/1.0.0",
  "pages": [{"page": 1, "width_mu": 8500, "height_mu": 11000}],   // ints only (R16)
  "body": 1, "furniture": 2,
  "nodes": [
    {
      "id": 0,                  // == index in nodes[]; final-pre-order ordinal (id order = reading order)
      "kind": "document",
      "path": "//doc",          // Adobe-Extract style, 1-based per-kind instance counters
      "parent": null,
      "children": [1, 2],
      "page": 1,                // first page touched
      "bbox": [x0, y0, x1, y1], // mu ints, union rect via geom.rect_scale; null when geometry absent
      "level": null,            // heading/section only, 1..4
      "block_ixs": [],          // indices into LayoutView.blocks (leaves: exactly one)
      "table_ix": null, "kv_ix": null, "mark_ix": null,
      "figure_id": null,        // "fig-{page}-{n}" (R20)
      "metrics": {              // ints/bools/closed enums, derived once in metrics.py;
        "char_count": 214,      //   exact values stay in this artifact (trust domain);
        "line_count": 4,        //   the LLM projection (§4.2) buckets them
        "height_mu": 310,
        "ends_terminal_punct": true,
        "starts_lowercase": false,      // null when page case-profile voids it
        "ends_hyphen": false,
        "script_class": "latin",        // latin|cyrillic|cjk|arabic|deva|mixed|none
        "digit_ratio_class": "low",     // none|low|high|all
        "alignment": "left"             // left|right|center|justified|unknown
      },
      "prov": {
        "source": "azure_section",      // azure_section | geometry | seq_fallback
        "provider_ref": "/sections/3",  // verbatim, when provider-seeded
        "provider_role": null,          // verbatim Azure role for unknown-role paragraphs (R14)
        "band_ix": 4, "frame_ix": 0, "region_ix": 2   // canvas coordinates when geometry ran
      }
    }
  ],
  "flow": [                     // continuation annotations ONLY; never reorder (R11)
    {"src": 17, "dst": 23, "kind": "continues", "score": 8,
     "evidence": ["ends_hyphen", "adjacency", "no_terminal", "height_match", "width_match"]}
  ],
  "report": {                   // LLM-pass trigger inputs (LLM slice's TreeBuildReport, merged here)
    "order_ties": [[14, 15, 120]],        // (node_a, node_b, margin_mu) coin-toss orderings
    "coverage_fallback_pages": [],
    "declined_pages": []
  },
  "passes": {                   // honesty manifest — construction passes ONLY (R5)
    "provider_sections": "used(7)",       // | "absent" | "conflict_demoted(pages=[2])"
    "provider_figures":  "used(2)",       // | "absent"
    "geometry": "ran",                    // | "declined(skew, pages=[3])" | "absent"
    "interposer": "ran(footnotes=1)",
    "heading_nesting": "ran(levels=3)",
    "continuity": "ran(edges=2, candidates=5)"
  },
  "counters": {"blocks_total": 41, "blocks_claimed": 41, "tables_claimed": 1,
               "kvs_claimed": 6, "marks_claimed": 2, "nodes": 55, "edges": 2}
}
```

### 2.3 Invariants (validated at emit; violation ⇒ whole-doc flat fallback, never a raise)

- **I1** `nodes[i].id == i`; ids are final-pre-order ordinals.
- **I2** Parent/children mutually consistent; exactly one `document`; `body`/`furniture` its only children; single tree (children assigned only to already-emitted parents ⇒ acyclic by construction).
- **I3 Claim exactness** (coverage-gate analogue): every `LayoutView` blocks/tables/kvs/marks index appears in **exactly one** node. `counters` state it. Stale-tree drift is structurally impossible: a tree validated against the wrong view fails I3.
- **I4** Flow edges: `src.id < dst.id`; both kind `paragraph`; no node is `dst` of two edges; no self/duplicate edges. (Interposers may sit between src and dst in pre-order — R15.)
- **I5** Zero document strings (§2.2). Enforced by model types, checked by test (§8.6).

### 2.4 Versioning / sha story

- `sha256_tree` = sha256 of `dump_tree` bytes; stored in DB + quoted in PMD 3.0 front matter.
- The tree pins `view_sha256`; PMD 3.0 pins `sha256_tree`; the variant pins `sha256_tree` + arrangement sha. Chain: served bytes → tree → view, all content-addressed.
- `builder` semver bumps on any heuristic change. Stored artifacts never retro-update (frozen-rules property); new conversions get the new builder.

---

## 3. CONSTRUCTION PIPELINE

### 3.1 Workstream 0 — the sections/figures adapter gap (verified; ships first, alone)

`dpc/adapters.py::from_azure_layout` currently keeps only `{provider, api_version, model_id, content_chars, _line_join}` in `raw`, dropping `analyzeResult.sections` and `analyzeResult.figures`. Fix:

```python
# dpc/doctree/harvest.py
def harvest_structure(analyze_result: dict[str, Any]) -> ProviderStructure | None:
    """Extract sections/figures refs, or None when absent.

    Runs inside from_azure_layout (the only module that sees the raw payload) and parks the
    result under view.raw["structure"], so the tree stays constructible from recorded
    payloads with no re-fetch. Defensive resolution: dangling refs dropped; an element
    claimed by two sections keeps its first (document-order) claimant; a section reachable
    twice (cycle) is cut at the revisit. Only refs and geometry are kept — caption TEXT is
    kept as a paragraph INDEX, never a string.
    """
```

`from_azure_layout` change is one line: `raw["structure"] = harvest_structure(result)` (key absent when None). `_line_join` and all existing raw keys untouched. Also in this workstream: create `dpc/geom.py` exporting `rect_scale` and `page_scale` (moved from `emitter`, which now imports them) — R13. Gate: full existing test suite passes with byte-identical PMD 2.0 output.

### 3.2 Build algorithm (`dpc/doctree/build.py::build_doctree(view) -> DocTree`; pure, never raises)

Reused verbatim from `dpc/canvas.py` (all verified to exist): `page_layout`, `atoms_for_page`, `page_em`, `page_skew_ok`, `build_bands`, `mark_separators`, `find_gutters`, `build_frames`, `build_regions` (incl. the coverage gate), `_assign_kvs` logic, `is_rtl`. New code is the recursion driver, the seed/audit/nest passes, and metrics.

1. **Per-page skeletons.** `skel[p] = page_layout(view, p)` for each page ascending. Declined pages (skew/no atoms) marked; their elements go to step 10. `passes.geometry` records them.
2. **Metrics.** `metrics.py::block_metrics(block, em)` per block — text is read here and **only** here; every output is int/bool/closed enum. `page_case_profile` computes the all-caps/non-bicameral gate for `starts_lowercase` (`ALLCAPS_RATIO`).
3. **Provider seed** (when `view.raw["structure"]` present). DFS `sections[0]` with visited-set + first-claim-wins. `/sections/M` → `section`; `/paragraphs/N` → kind by role (`title`/`sectionHeading` → `heading`; `pageHeader|pageFooter|pageNumber` → furniture leaf; `footnote` → `footnote`; anything else → `paragraph` with `prov.provider_role` set — R14); `/tables/N` → `table`; `/figures/N` → `figure`. **Sibling order = provider array order, verbatim.**
4. **Geometry tree** (fallback, and for conflicted/section-less pages). Per page: linear `Region`s → leaf runs in band order; spatial regions → `flow_group` with `frame` children (visit order `frame_ix` ascending, reversed on majority-RTL pages), atoms in `(band_ix, y0, x0, block_ix)` order. Large multi-band regions get recursive XY-cut using the already-computed `mark_separators` gaps (horizontal) and `find_gutters` gutters (vertical), bounded by `XCUT_MAX_DEPTH`/`XCUT_MIN_ATOMS`. The canvas machinery *is* the projection profile; only `_xy_cut` (the recursion driver) is new.
5. **Section coherence audit (geometry audits the provider).** Per page, per sibling-section pair with members on that page: (a) band-interval overlap `3*inter > min(len_a, len_b)` (`SECT_IOU_MAX`) ⇒ conflict; (b) **R19:** same-frame siblings whose provider order inverts band order ⇒ conflict. Conflicted pages: provider subtree discarded, rebuilt by step 4; `passes.provider_sections = "conflict_demoted(pages=[…])"`. Demotion is per-page, never per-document.
6. **Furniture split.** Furniture-zone leaves re-parent under `furniture`, ordered `(page, role_rank[header=0,number=1,footer=2], y0, x0, block_ix)`. Must run **before** continuity (a page-3 header must not sit between a page-2 tail and its page-3 continuation).
7. **Interposer demotion (R15).** `footnote`-kind leaves move to the tail of their innermost section, ordered `(page, y0, x0, block_ix)`. They remain body content (rendered at section end), never furniture.
8. **Heading nesting.** Level stack walk: `title` → 1; `sectionHeading` levels by descending rank of distinct heading height classes, capped `MAX_LEVELS`, shifted by one when a title exists. Nesting **groups, never reorders** — a wrong level distorts outline depth, not text order.
9. **Figures & captions.** `figure` nodes with `figure_id = "fig-{page}-{n}"` (R20). Azure `caption.elements` paragraph becomes a `caption` child and is claimed there (caption's strongest home is its figure — the one deliberate reversal of first-claim). No provider figures (Tesseract mock) ⇒ no figure nodes; `passes.provider_figures = "absent"`.
10. **KV groups.** Reuse `_assign_kvs` centroid assignment; cluster within region by vertical gap ≤ `KV_GAP` into `kv_group`; children `kv_pair` ordered `(page, y0_key, x0_key, kv_ix)`.
11. **Flat fallback claim.** Unclaimed elements (declined pages, bbox-less blocks, mock gaps) append under `body` in the order that makes the degenerate tree flatten to **exactly today's 2.0 output** — nominally `(page, seq, block_ix)`, adjusted if the §8.5 byte-equality test shows `emitter._linear_elements`'s kv/mark y-splice differs (R18: the 2.0 file is the spec). `prov.source = "seq_fallback"`.
12. **Continuity edges.** §3.3, over the finished pre-order.
13. **Finalize.** Assign ids by pre-order; compute paths; union bboxes bottom-up (via `geom.rect_scale` — never reimplemented, so 2.0/3.0 anchors agree to the last digit); validate I1–I5. Any failure ⇒ whole-doc step-11 flat tree, `passes.geometry = "invariant_failed(I#)"`.

### 3.3 Continuation linking — ONE rubric, ONE function (R10)

`continuity.py::continuation_score(a_metrics, b_metrics, adjacency: bool) -> int` — imported by the builder here and by the arrange verifier (V6).

**Candidate gate (builder):** both kind `paragraph`; same `script_class`; and either
(a) **frame-edge pair**: A last paragraph of its frame within bottom `GATE_TAIL` bands, B first of the next frame in pre-order (or first body leaf of the next page) within top `GATE_TAIL`; or
(b) **interposed pair (R15)**: same frame, separated in pre-order only by non-paragraph leaves (figure/mark/kv/footnote/caption).

**Score:**

| Signal | Points | Why |
|---|---|---|
| `A.ends_hyphen` | +3 | Near-sufficient alone (pdftotext++): line-final hyphen at a boundary is the split-word signature. |
| `not A.ends_terminal_punct` | +1 | Weak alone — KYC form values rarely end in periods either. |
| `B.starts_lowercase` | +2 | Strong cue; **contributes 0 when null** (all-caps page / non-bicameral script — the passport/MRZ/CJK gate, applied in `metrics.py`, not in any prompt). |
| `height_class(A) == height_class(B)` | +1 | Same paragraph ⇒ same type size. |
| frame-width match `|wA − wB| ≤ 2·em` | +1 | Continuations live in like-measured columns. |
| adjacency (frame-edge pair OR interposed pair) | +2 | Explicit form of what TREE's hard gate implied; lets the verifier score cross-page cases uniformly. |

**Thresholds:** builder emits a `continues` edge at score ≥ `CONT_EDGE_MIN = 5` (≡ TREE's 3 + always-present adjacency 2: hyphen alone still passes; no-terminal + lowercase still passes; nothing weaker does). Verifier V6 accepts an LLM-confirmed `merge_flow` at score ≥ `CONT_CONFIRM_MIN = 4` (the LLM may confirm plausible links the builder declined — e.g. adjacency absent across a window seam — never create geometrically impossible ones). Low recall is intentional: on KYC forms a false merge glues two field values, which is worse than a missed merge.

### 3.4 Constants table

| Name | Value | Justification |
|---|---|---|
| `SECT_IOU_MAX` | 1/3 (integer test `3*inter > min_len`) | Captions/pull-quotes legitimately interleave a little; beyond a third of the smaller sibling, provider hierarchy and page geometry materially disagree. Guess until measured — demotion counter makes the rate visible from day one. |
| `XCUT_MAX_DEPTH` / `XCUT_MIN_ATOMS` | 6 / 3 | 2⁶ = 64 cells exceeds any real KYC page; <3-atom sides are noise-chasing. |
| `HGT_SMALL/LARGE/DISPLAY` | 0.85/1.25/1.80 ·em (integer `100*h` vs `85*em` etc.) | Typographic scale steps ~1.2–1.5; 0.85 catches footnote-size, 1.8 separates display titles. |
| `MAX_LEVELS` | 4 | Azure gives title + flat sectionHeading; deeper inference over-fits scan noise. |
| `GATE_TAIL` | 2 bands | One band of tolerance absorbs stray noise; more admits mid-column paragraphs. |
| `CONT_EDGE_MIN` / `CONT_CONFIRM_MIN` | 5 / 4 | §3.3 (R10). Conjunction-of-signals per pdftotext++; asymmetric so the LLM confirms, never creates. |
| `ALLCAPS_RATIO` | 90% (integer `10*upper >= 9*cased`) | Passports/MRZ are all-caps with OCR noise; 90 tolerates noise while catching them. |
| `RTL_MAJ` | strict majority of atoms `is_rtl` | Frame visit order flips on majority-RTL pages; reuses existing helper. |
| `KV_GAP` | 2·em vertical | Matches visual row pitch of dense form panels. |
| `FURNITURE_MARGIN_PM` | 90‰ of page height | R9: 0.75–1 in running-header zone on an 11 in page = 68–91‰, plus jitter. |

### 3.5 Module layout

```
dpc/geom.py               # NEW (WS0): rect_scale, page_scale — shared rounding, emitter imports
dpc/doctree/__init__.py   # build_doctree, DocTree, walk_body, dump_tree, tree_sha256
dpc/doctree/models.py     # NodeKind, Metrics, Prov, TreeNode, FlowEdge, Report, Passes, DocTree, dump_tree
dpc/doctree/harvest.py    # provider sections/figures extraction (WS0)
dpc/doctree/metrics.py    # block_metrics, page_case_profile — the ONLY code that reads text
dpc/doctree/build.py      # build_doctree orchestration, _xy_cut, seed/audit/nest/attach passes
dpc/doctree/continuity.py # candidate gates + continuation_score (the one rubric)
dpc/doctree/patch.py      # apply_patch — the ONE implementation (R12)
dpc/treemd.py             # flattener (§5)
dpc/arrange/…             # LLM pass (§4)
```

---

## 4. THE LLM VALIDATION PASS (`dpc/arrange/`)

### 4.1 Posture

Async, post-store, advisory. The heuristic PMD 2.0/3.0 are stored before and independently of the LLM; their shas never depend on it. The LLM's entire output surface: one side artifact (`arrangement.json`) + optionally one *derived* PMD 3.0 variant = deterministic function of (tree, view, accepted ops). Threat model refuses "trusted network": prompts get logged somewhere we don't control; a future misconfig points at SaaS. The boundary is **the payload itself must be non-personal under combination attack** (EDPB 01/2025: attribution by any reasonably-likely means, including the blank form template the attacker trivially holds).

### 4.2 Feature schema — complete SAFE set with adversarial justification

Built by `features.py::build_features(tree, view, layouts)`. Ints/enums/bools/null only; text is reduced to buckets in-process and goes no further than that stack frame.

| Field | Form | Why safe |
|---|---|---|
| `id` | `"n{k}"` window-local, mapped to `path` by us | Assigned by us; pure tree position. |
| `path` | structural path string | Fingerprints the *template*; templates aren't people. |
| `kind`, `page`, `band`, `frame` | enum / small ints | Class labels + canvas geometry; reveal document type, not a person. |
| `anchor_edge`, `anchor_pm` | `left\|right\|center` + 1%-grid permille of the **template-fixed** edge only (R7) | Position of the printed field box is template geometry. The free edge is never sent — for right-aligned text, x0 = x1 − width was a working ~1-char length oracle; anchoring kills it. `y0_pm` sent at the same 1% grid (vertical position is template-fixed; vertical extent is `line_count_class`). |
| `w_class` | width fractions `<1/16 … ≥3/4` (ratio-≥1.5 buckets) | Extent = char_count × advance; a 1% grid would distinguish 10 vs 11 chars at 12 pt. Ordering needs where a box starts, not how far it runs. |
| `char_count_class` | log-4: `xs<8, s<32, m<128, l<512, xl` | Ratio-4 buckets ⇒ within-bucket uncertainty ≥×4; names, dates, ID numbers land in `xs`/`s` mutually indistinguishable. Kills the known-template length oracle. |
| `line_count_class` | `1, 2_3, 4plus` | Address line-count at exact resolution is a quasi-identifier; three buckets carry only "wrapped paragraph?". |
| `ends_terminal_punct`, `ends_hyphen` | bool | 1 bit each: "a sentence ended / a word wrapped", never which. |
| `starts_lowercase` | bool or **null** | Gated in `metrics.py` (non-bicameral script, all-caps page) — a gate the model applies is a gate sometimes not applied. Null is the honest value. |
| `height_class` | per-page quartile `small\|body\|large\|display` | Relative typography; exact font sizes forbidden (fingerprint + glyph-metric residue). |
| `script_class`, `digit_ratio_class` | coarse enums | Language class / "ID-shaped zone" — zone *type*, never values. Two people's ID numbers are both `all`. |
| `alignment_class` | `left\|right\|center\|justified\|unknown` | Indentation-variance geometry (Tesseract outline reasoning). Note: safe only because of R7's edge anchoring. |
| `at_frame_top`, `at_frame_bottom`, `uncertain`, `context` | bool | Pre-computed so model and verifier reason from identical booleans; zero content. |

**FORBIDDEN, with reasons:** (1) any string from the document — tokens, n-grams, prefixes, key names ("Surname" is document text and a template key pairing with a value node); zero-string is the only auditable rule. (2) Hashes of text — low-entropy PII is dictionary-attackable; the hash is the value. (3) Exact char/word counts or per-line length sequences — length-oracle / Whisper-Leak inversion. (4) Exact widths/heights/font sizes/colors/sub-percent coords — extent oracle + fingerprinting for no ordering gain. (5) Per-node OCR confidence — correlates with content properties (handwriting per field). (6) Any free-text field composed by us — the payload model has no unconstrained `str` field, so adding one fails type review (and the §8.6 schema-introspection test).

**Residual risk, stated:** the payload fingerprints the template and leaks document type + language class. It does not distinguish two people filled into the same template — falsifiable via `test_two_identities_one_payload`, which now includes a right-aligned filled field (R7).

### 4.3 Payload, windowing, prompt

One request per window; canonical JSON (sorted keys, ints/enums/bools/null), `payload_sha256` per window stamped in the artifact — the disclosable proof of what left us. `make_windows`: per-page, ≤ `arrange_max_window=48` nodes (LayoutGPT-class degradation past ~15 objects argues small; 48 ≈ 1.5K tokens), band-atomic splits, 4 `context:true` carry-over nodes. Payload carries `nodes`, `order` (heuristic order in-window), `succ_uncertain` (tie pairs from `tree.report.order_ties`). Prompt = frozen system template (`PROMPT_TEMPLATE_VERSION="ap1"`: task, one-line feature glossary, op grammar, "prefer no ops on a plausible order", context-node rules incl. the R6 merge_flow exception) + the payload, nothing else. Decoding: JSON-schema-constrained where the gateway supports it, strict post-parse otherwise — the grammar is not the security boundary; the verifier is. Sampling: k=3 (temp 0, 0.7, 0.7).

### 4.4 Op language

```json
{"schema": "dpc-arrange-ops/1", "ops": [
  {"op": "move_before", "node": "n15", "ref": "n13", "reason": "ORDER_INVERSION"},
  {"op": "move_after",  "node": "n7",  "ref": "n9",  "reason": "SIDEBAR_DEFERRED"},
  {"op": "reparent",    "node": "n21", "ref": "n4",  "reason": "HEADING_SCOPE"},
  {"op": "merge_flow",  "node": "n14", "ref": "n18", "reason": "COLUMN_CONTINUATION"},
  {"op": "split",       "node": "n30",               "reason": "TABLE_FRAGMENT"},
  {"op": "flag_break",  "node": "n11", "confidence_pm": 800, "reason": "INTERRUPTED_FLOW"}
]}
```

`move_*`: node + subtree repositioned among ref's siblings. `reparent`: node becomes last child of ref (heading/section only). `merge_flow`: adds `(node, ref)` to flow_joins AND makes ref immediate successor **in the patched variant only** (R11) — never concatenates text; a join is rendering adjacency. `split`: **ADVISORY_ONLY** in v1 (mutating node identity needs a coverage-gate-style reconstruction proof we aren't building). `flag_break`: pure advisory → review queue. Closed reason enum: `COLUMN_CONTINUATION, PAGE_CONTINUATION, INTERRUPTED_FLOW, ORDER_INVERSION, SIDEBAR_DEFERRED, FURNITURE_MISPLACED, CAPTION_DETACHED, TABLE_FRAGMENT, LIST_CONTINUATION, HEADING_SCOPE, OTHER_STRUCTURAL`. Model emits `n{k}` ids; verifier resolves to canonical `path` strings before storage (R3).

### 4.5 Verifier (`verifier.py`, `VERIFIER_VERSION="av1"`) — rules in order, first failure names the verdict

1. **V1 REJECT_UNKNOWN_ID** — id not in the window.
2. **V2 REJECT_CONTEXT_TARGET** — `node` is `context:true`, **except** (R6): a context node may be `node` of `merge_flow` when `ref` is in-window and V5's cross-page gate holds. Context nodes may otherwise appear only as `ref` of `merge_flow`.
3. **V3 REJECT_ORPHAN** — simulate on a copy: every body node exactly once in pre-order; no self-ancestry; `reparent` ref must be `kind ∈ {heading, section}`. Checked against the evolving simulated state.
4. **V4 REJECT_TYPE** — moves may not cross table/figure subtree boundaries (a cell/caption never leaves its parent); `merge_flow` requires `kind(node)==kind(ref) ∈ {paragraph, list_item}`; `reparent` of furniture into body requires reason `FURNITURE_MISPLACED` **and** the node's rect outside the top/bottom `FURNITURE_MARGIN_PM=90‰` bands (R9).
5. **V5 REJECT_PAGE_CROSS** — no cross-page relations except `merge_flow` with `node.at_frame_bottom ∧ ref.at_frame_top ∧ ref.page == node.page + 1` (the one legitimate cross-page relation; Azure never orders across pages, so this is exactly where a validator adds value).
6. **V6 REJECT_GEOMETRY** — confirm-plausible-never-create-impossible: `merge_flow` requires `continuity.continuation_score(node, ref, adjacency) >= CONT_CONFIRM_MIN (=4)` — **imported from `dpc/doctree/continuity.py`; the verifier owns no formula** (R10). `move_*` rejected if it inverts a strict geometric order: same frame, vertical gap > 2·em (`page_em`), neither node `uncertain` — clearly stacked same-frame text has one true order; only ties are arguable. Geometry-null nodes fail V6 for all mutating ops by default.
7. **V7 REJECT_LOW_CONFIDENCE** — `flag_break` with `confidence_pm < 700` drops from the review queue (still recorded). Mutating ops carry no self-reported confidence — self-assessment is not evidence; V8 is.
8. **V8 REJECT_NO_MAJORITY** — edit-level self-consistency: canonical op identity `(op, node, ref)` (reasons/confidence excluded); accept at ≥2 of k=3.
9. **V9 REJECT_RUNAWAY** — a sample proposing >12 mutating ops in one window (25% of the node cap) is discarded before voting; a heuristic order that wrong is a bug report, not a bulk silent rewrite.

Canonical application order: `(page, op_rank[reparent=0, move_before=1, move_after=2, merge_flow=3], node_ordinal, ref_ordinal)`; V3/V6 re-checked per op against the simulated state. Determinism claim, precisely: *every stored byte is either produced deterministically or recorded verbatim as the input that produced it* — re-running the verifier on the recorded artifact reproduces verdicts byte-for-byte.

### 4.6 Artifact (`arrangement.json`) — canonical JSON, sha256 stored, **no wall-clock fields** (R8)

```json
{"schema": "dpc-arrangement/1",
 "doc_id": "…", "pmd_sha256": "…", "sha256_tree": "…",
 "status": "ran",
 "model_id": "…", "prompt_template_version": "ap1", "verifier_version": "av1", "samples": 3,
 "windows": [{"window_ix": 0, "page": 1, "node_span": [0, 41], "payload_sha256": "…",
   "raw": [{"sample_ix": 0, "ops": ["…verbatim…"], "discarded": null}],
   "verdicts": [{"op": {"op": "merge_flow", "node": "/pg[1]/…/p[2]", "ref": "/pg[1]/…/p[1]",
                 "reason": "COLUMN_CONTINUATION"}, "votes": 3, "verdict": "ACCEPTED", "rule": null}]}],
 "accepted_ops": ["…path-addressed, canonical application order…"],
 "review_queue": [{"after": "/pg[1]/…/p[3]", "confidence_pm": 800, "reason": "INTERRUPTED_FLOW"}]}
```

Skips are artifacts too: `{"status":"skipped","reason":"no_llm_configured|timeout|clean_single_column|no_geometry|budget_exhausted"}`. Latency/timings go to logs only (`arrange.done doc=… windows=… accepted=… rejected=… ms=…` — counts, never content).

### 4.7 Run modes, triggers, unavailability

- `DPC_ARRANGE_MODE`: `off` (default — ships dark) | `shadow` (full pass, artifact written, **no variant derived** — the measurement mode) | `active` (variant derived on accepted ops). Precondition: `tree_mode=emit`.
- **Trigger** `should_run(tree, layouts) -> (bool, reason)`: run iff (a) any page ≥2 frames; or (b) coverage_fallback_pages non-empty; or (c) order_ties non-empty; or (d) no provider sections while any page has >1 band and >10 body nodes. Clean single-column letters skip with `skipped(clean_single_column)`.
- **Budgets**: 20 s per window call, 120 s wall per doc; exhaustion records completed windows + `skipped(budget_exhausted)` for the rest. Unreachable/unparseable ⇒ sample `discarded: reason`; <2 usable samples ⇒ all ops REJECT_NO_MAJORITY by construction. `run_arrange_pass` catches everything at its boundary — an advisory pass that can break ingestion isn't advisory.
- **Client seam**: `ArrangeLlmClient` mirrors `OcrClient` — OpenAI-compatible `POST {endpoint}/v1/chat/completions`, settings-driven, injectable httpx transport, and **fixture replay**: with `arrange_fixture_dir` set, responses come from `{dir}/{payload_sha256}.{sample_ix}.json` and the network is never touched — the DI stub's exact posture, so the whole pass runs offline from recorded corpus payloads.
- If accepted ops exist (active mode): `patch.apply_patch(tree, accepted_ops)` → `treemd.flatten(patched_tree, view, decided_by=f"heuristics+patch@{sha8}", …)` → stored as a separate object; DB row updated (incl. the exact `generated` string — R17).

---

## 5. FLATTENING — PMD 3.0 (`dpc/treemd.py`)

### 5.1 Contract

```python
def flatten(tree: DocTree, view: LayoutView, *, generated: str = "",
            extra: dict[str, Any] | None = None, decided_by: str = "heuristics",
            flow_joins: frozenset[tuple[int, int]] = frozenset()) -> tuple[str, FlattenReport]:
    """DocTree + LayoutView -> PMD 3.0. Pure function of its arguments (R2).

    Refuses (TreeInvalid) when view sha != tree.view_sha256, when I1-I5 fail, or when the
    element census mismatches — fail closed on the NEW artifact, never on PMD 2.0.
    Text joins happen here via block_ixs/table_ix/kv_ix; the tree carries no text (R1).
    """
```

PMD 2.0 remains the frozen provider-order artifact and the default serving bytes; `sha256_markdown` (a dedupe key — `dpc/api.py:214`) is never touched. Two claims, two files, two hashes.

### 5.2 Semantics

- **Order**: pre-order of `body` in children order (flow: continues-edges render the pair without an intervening blank line and MAY dehyphenate iff `ends_hyphen` — a rendering adjacency, never a text mutation; dehyphenation ships **off** until §8.4 corpus measurement); then `<!-- furniture -->`; then furniture grouped `(page, pre-order)` — demoted, not lost (anchors keep true page/rect). `footnote` nodes render at their section tail with anchor tag `footnote`.
- **Page markers** at first body visit of each page — a flow annotation in 3.0, not a partition (declared semantic difference from 2.0, front-matter versioned); per-element anchors are ground truth.
- **Per-kind rendering** reuses emitter functions verbatim (`_heading` semantics, `_table_md` GFM, `_clean`); anchors are the existing `<!-- @page [rect] tag -->` **with one appended clause ` path=<node-path>`** — appending keeps every 2.0 anchor parser working on 3.0 bytes, and the path is the artifact-to-artifact audit link. Heading depth: `#` reserved for `title`; section depth d → `'#'*(1+min(d,5))`, so a flat tree reproduces 2.0's `title→#`, `sectionHeading→##` exactly (enables the §8.5 equivalence test).
- **Figures**: `![figure fig-2-1](figure://{conversion_id}/fig-2-1)` + optional italic caption line (caption is document text, searchable, visually subordinate). Never a generated description — a described signature/photo is manufactured PII. The `/figures/{fid}` endpoint 404s (`figure_extraction: not_stored`) until crops are persisted; the URI scheme is stable now so MD bytes never change when it lights up.
- **KV/mark**: already tree leaves — no y-splicing in this path (`_linear_elements` stays 2.0-only). One surviving rule: a `kv_pair` whose key and value both appear in a covering canvas fence is suppressed and counted (`kv_in_canvas`, same `_norm` containment test).

### 5.3 flow_group rendering — order from the tree, looks from the canvas (R14)

Deterministic type-based rule, v1: a `flow_group` **linearizes** (frames rendered in visit order as normal linear markdown — the user's ask for multi-column prose) iff every leaf under it is of kind `paragraph|heading|footnote|list_group|list_item|figure|caption`. Otherwise (kv_group/mark/table present — form panels like "FOR BANK USE ONLY") it renders as the existing space-padded **canvas fence** by re-running `page_layout(view, page)` and selecting the region via `prov.region_ix` — possible precisely because `view` is an input (R2), so no rows are embedded and no drift exists. Ops may target paragraphs/frames inside a linearized flow_group; fence-rendered regions are opaque to ops (verifier V4 boundary).

### 5.4 Front matter (fixed order)

```
pmd: 3.0 / generator / order: tree / tree_source: provider_sections|geometry|flat
decided_by: heuristics | heuristics+patch@{sha8}
sha256_tree: <64 hex>
doc_id / source / provider / pages / blocks / tables / marks / key_values / chars (as 2.0)
figures: N (when >0) / furniture_nodes: N (when >0)
passes: sections,geometry[,interposer,continuity]        ← construction passes; LLM never named here
unicode: <ver> (only when a fence exists, 2.0 rule) / generated / sha256_input
```

`decided_by` + `sha256_tree` are the audit spine: from the served bytes alone a regulator names the tree and whether a model influenced order (EU AI Act Art. 12 shape).

---

## 6. API + STORAGE + UI

### 6.1 Endpoints (additive only)

```
POST /api/v1/convert                     — unchanged request; optional "tree": true|false|null
                                           (null = settings.tree_mode decides). Response gains
                                           tree_source, sha256_tree, tree_nodes, tree_status,
                                           sha256_tree_markdown, passes when the tree ran.
                                           Tree failure => 200, PMD 2.0 stored, tree_status=error:<Name>.
GET  /api/v1/conversions/{id}            — row includes new nullable columns
GET  /api/v1/conversions/{id}/markdown   — BYTE-IDENTICAL to today, always PMD 2.0
GET  /api/v1/conversions/{id}/tree       — stored doctree.json; 404 {"error":"no_tree","tree_status":…}
GET  /api/v1/conversions/{id}/tree.md    — PMD 3.0; ?arrangement={suggestion_id} serves the variant
                                           (404 arrangement_not_found / 409 arrangement_rejected)
GET  /api/v1/conversions/{id}/figures/{fid} — reserved; 404 figure_extraction:not_stored in v1
```

Distinct artifacts get distinct addresses — no `?output=` on `/markdown` (its contract is "the stored primary artifact, byte-exact"; a parameter returning different bytes from one URL breaks caches and recorded addresses).

### 6.2 Storage

S3: `tree/{yyyy}/{mm}/{id}.tree.json`, `treemd/{yyyy}/{mm}/{id}.md`, `treemd/{yyyy}/{mm}/{id}.{sha8}.md` (variant), `arr/{yyyy}/{mm}/{id}.{n}.arr.json` — new prefixes so lifecycle rules on `pmd/` are untouched. `storage.py` gains `put_tree/get_tree/put_tree_markdown/get_tree_markdown/put_arrangement/get_arrangement` mirroring the existing pair.

`schema.sql` (idempotent, all nullable — old rows valid, old readers ignore):

```sql
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_s3_key text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS sha256_tree text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_source text;      -- provider_sections|geometry|flat
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_nodes int;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_status text;      -- ok|skipped:*|invalid:*|error:*
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS tree_md_s3_key text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS sha256_tree_markdown text;
ALTER TABLE conversions ADD COLUMN IF NOT EXISTS passes text;

CREATE TABLE IF NOT EXISTS arrangements (
  suggestion_id uuid PRIMARY KEY,
  conversion_id uuid NOT NULL REFERENCES conversions(id),
  artifact_sha256 text NOT NULL, s3_key text NOT NULL,
  status text NOT NULL,                    -- ran|skipped:<reason>
  model_id text, prompt_template_version text, verifier_version text,
  n_accepted int, n_rejected int,
  variant_s3_key text, variant_sha256 text, variant_generated text,   -- R17
  created_at timestamptz NOT NULL DEFAULT now()
);
```

### 6.3 Config (env vars, `config.py` validator pattern)

| Var | Values | Default | Note |
|---|---|---|---|
| `DPC_TREE_MODE` | `off\|build\|emit` | `build` | Tree becomes real data on day one; `emit` waits for §8 gates. |
| `DPC_ARRANGE_MODE` | `off\|shadow\|active` | `off` | Requires `tree_mode=emit`. |
| `DPC_ARRANGE_ENDPOINT/_API_KEY/_MODEL` | — | unset | Unset ⇒ `skipped(no_llm_configured)`. |
| `DPC_ARRANGE_TIMEOUT_SECONDS` / `_DOC_BUDGET_SECONDS` | float | 20 / 120 | §4.7. |
| `DPC_ARRANGE_SAMPLES` / `_MAX_WINDOW` | int | 3 / 48 | §4.3/§4.5. |
| `DPC_ARRANGE_FIXTURE_DIR` | path | unset | Offline replay. |

### 6.4 Console (read-only, deferrable)

`frontend/src/ResultViewer.tsx`: one "Tree" tab (fetch `/tree`; collapsible nested list: kind icon, path, page/rect chip, `builder` version surfaced — the stale-artifact tell; node text shown by resolving `block_ixs` against the already-fetched view, since the tree carries none); a "Flow" toggle on the markdown tab switching `/markdown` ↔ `/tree.md`; clicking a tree node scrolls to the matching ` path=` anchor. Out of scope v1: tree editing, drag-reorder, arrangement review UI, figure thumbnails.

---

## 7. PHASED WORKSTREAM PLAN (builder-agent grade; sequential phases, exclusive file ownership within each)

**Common verification (every phase):** `pytest --tb=short -q && ruff check dpc/ && mypy dpc/ --ignore-missing-imports`. No phase may change PMD 2.0 bytes: golden-sha test `test_pmd2_byte_stability` runs in all phases.

### Phase 0 — Shared geometry + provider harvest (the verified adapter gap). NOT deferrable.
- **Owns:** `dpc/geom.py` (new), `dpc/emitter.py` (import-only change), `dpc/adapters.py` (one line in `from_azure_layout`), `dpc/doctree/harvest.py` + minimal `dpc/doctree/__init__.py`, `tests/test_geom.py`, `tests/test_harvest.py`, fixture additions under `tests/fixtures/di/`.
- **Contracts:** `geom.rect_scale`/`geom.page_scale` byte-equivalent to the old private helpers (test asserts identical output over the existing corpus); `harvest_structure(analyze_result) -> ProviderStructure | None` per §3.1 (dangling-ref drop, first-claim, cycle cut); `raw["structure"]` key absent when None; `_line_join` untouched.
- **DONE-WHEN:** existing suite green; PMD 2.0 golden shas unchanged; harvest resolves the recorded Azure fixtures' sections/figures with zero dangling refs surviving; Tesseract-mock payload yields None without error.

### Phase 1 — Tree construction + storage (`tree_mode=build`). NOT deferrable.
- **Owns:** `dpc/doctree/{models,metrics,build,continuity,patch}.py`, `dpc/storage.py` (put/get_tree), `dpc/db.py` + `schema.sql` (conversions columns), `dpc/api.py` (convert wiring + `/tree`), `dpc/config.py` (`tree_mode`), `tests/test_doctree_*.py`.
- **Contracts:** §2 schema + I1–I5; `build_doctree(view) -> DocTree` pure, never raises; `dump_tree` canonical bytes; `walk_body` pre-order iterator; `continuation_score` per §3.3; `apply_patch` per R12 (implemented now, exercised by Phase 3).
- **DONE-WHEN:** determinism test (twice in-process + subprocess with varied `PYTHONHASHSEED` ⇒ byte-equal tree, sha match) green on all fixture rungs (provider_sections / geometry / flat); I3 claim-exactness on every fixture; degradation matrix (§8.7) green; `POST /convert` returns 200 with `tree_status=error:*` when the builder is monkeypatched to raise; `conflict_demoted` rate over the recorded corpus reported (report-only).
- **Deferrable within phase:** list_group detection (may land as paragraphs with roles preserved).

### Phase 2 — Flattener + PMD 3.0 (`tree_mode=emit` capability; default stays `build` until gates pass). NOT deferrable.
- **Owns:** `dpc/treemd.py`, `dpc/api.py` (`/tree.md`), `dpc/storage.py` (tree_markdown), `tests/test_treemd.py`, `tests/test_tree_api.py`, `tests/fixtures/**/*.order.json` (hand-labelled expected orders).
- **Contracts:** §5.1 `flatten` signature; §5.4 front matter; anchor ` path=` append rule; §5.3 linearize-vs-fence rule; `FlattenReport(elements_emitted, figures, kv_in_canvas, furniture_nodes, pages_visited)`.
- **Measurement gates (flip default to `emit` only when ALL pass on the recorded corpus):** (1) `/markdown` byte-equality `tree_mode=emit` vs `off` — 100%; (2) no-text-loss exactly-once multiset — 100%; (3) flat-tree ≡ 2.0 equivalence (R18) — 100%; (4) Kendall tau = 1.0 on labelled small fixtures; corpus-sweep tau report generated per Azure `api_version`.
- **Deferrable:** dehyphenating render of continues-edges (ships off; needs §8.4 data), figures crop endpoint, console Tree tab.

### Phase 3 — Arrange pass in shadow. DEFERRABLE as a whole (Phases 0–2 ship the user's stages 1+2 with no LLM).
- **Owns:** `dpc/arrange/{__init__,features,payload,ops,verifier,client,runner,artifact}.py`, `schema.sql` (arrangements table), `dpc/config.py` (arrange_*), `dpc/api.py` (background hook + `?arrangement=`), `tests/test_arrange_*.py`, `tests/fixtures/arrange/` (recorded LLM responses keyed `{payload_sha256}.{sample_ix}.json`).
- **Contracts:** §4.2 feature schema; §4.4 op schema; §4.5 verifier (importing `continuity.continuation_score` — a test asserts `verifier.py` defines no score formula); §4.6 artifact; `should_run` per §4.7.
- **DONE-WHEN:** entire pass runs offline from fixtures (no network in tests, asserted); verifier replay byte-for-byte from recorded artifact; PII suite (§8.6) green incl. `test_two_identities_one_payload` with the right-aligned field; cross-page merge_flow fixture proves R6 (the previously-deadlocked case is accepted end-to-end); skip artifacts written for every §4.7 reason.
- **Gate to Phase 4:** shadow over ≥50 real corpus docs: accepted-op precision ≥95% against hand-labelled order; zero V6-escaped geometric violations; degenerate-sample (V9) rate <10%.

### Phase 4 — Active mode + console. DEFERRABLE.
- **Owns:** `dpc/arrange/runner.py` (variant derivation path), `frontend/src/ResultViewer.tsx`, promotion checklist doc.
- **DONE-WHEN:** variant derivation is byte-reproducible from (tree + view + accepted_ops + recorded `generated`); `?arrangement=` serves it; default serving remains heuristic; console Tree tab renders the three fixture rungs.

---

## 8. TEST PLAN

1. **Fixtures.** Recorded real Azure DI 2024-11-30 payloads **with** `sections`/`figures` (synthetic filled templates — no real PII in the repo), the existing Tesseract-mock payloads (no sections), one seq-only view (HTML/XLSX path). Every tree test runs the full honesty ladder. Two-column, cross-page-continuation, figure+caption, footnote-role, all-caps passport/MRZ, and 200-block dense pages are each represented.
2. **Determinism.** Per fixture: build + flatten twice in-process and once in a subprocess with varied `PYTHONHASHSEED`; assert byte-equal `doctree.json`, byte-equal PMD 3.0, stored shas match recomputation. Golden shas committed.
3. **No-text-loss (exactly-once).** Multiset of `_norm` text over all non-table-zone blocks + table cells + kv keys/values + mark states == multiset extracted from the flattened MD (anchors/fence-metadata stripped); *exactly once* each — catches drops AND duplicates (double-parenting class). Canvas fence rows count as covering text for their atoms, same accounting as the coverage gate. Backed at build time by invariant I3.
4. **Reading-order fidelity.** Hand-labelled `*.order.json` (expected node-path order) per small fixture: normalized Kendall tau between label and body pre-order **= 1.0** (fixtures are small and unambiguous by construction). Corpus sweep: tau report per Azure `api_version`, report-only until the corpus stabilizes — also the tripwire for Azure silently reshuffling section order.
5. **Backward compat.** `/markdown` bytes byte-equal under `tree_mode=emit` vs `off` (sha assert). Flat-source single-column tree flattens element-for-element equal to the 2.0 file (anchors differ only by ` path=`). R18's fallback-order equivalence.
6. **PII leak suite — real mechanisms, named:**
   - `test_payload_model_closed`: introspects the Pydantic JSON schema of the payload models and asserts **every string-typed field carries an `enum` or `pattern` constraint** — no unconstrained `str` can exist; adding one fails this test, not code review.
   - `test_no_document_ngrams_in_request`: builds payloads from fixtures with known synthetic text; asserts no case-folded, whitespace-normalized **4-gram of any block/cell/kv/mark/caption text** appears in the serialized window bytes; then, via the injectable httpx transport, captures the **actual outgoing request body** (template + payload) and asserts the same over the wire — the transport-level tripwire behind the type-level boundary.
   - `test_two_identities_one_payload`: same form template filled with two synthetic identities, **including a right-aligned filled field** (R7); asserts the two serialized payloads are byte-identical.
   - `test_tree_has_no_strings`: walks `dump_tree` output and asserts every string value matches the closed set (enum values, `path` grammar, JSON-pointer `provider_ref`, `fig-\d+-\d+`).
7. **Degradation matrix.** (no sections)→`geometry`; (no geometry)→`flat`; (builder raises)→200 + `tree_status=error:*` + PMD 2.0 stored; (invalid tree)→`invalid:*`, no 3.0 object; (patch sha mismatch)→409; (no LLM)→`skipped(no_llm_configured)` artifact; (budget)→partial windows recorded — each asserted via API tests with the DI stub.
8. **Adversarial pages.** 200-block dense page: flatten <1 s, explicit-stack walk (no recursion limit); skewed scan: `page_skew_ok` declines → flat claim, `passes` says so; all-caps page: `starts_lowercase` null throughout, continuity still links on hyphen evidence.
9. **Verifier replay.** Re-run `verify` over the recorded `arrangement.json` raw samples: verdict list byte-identical to stored.

---

## 9. HONEST RISK LIST + KILL CRITERIA

| # | Risk | Mitigation | Kill criterion |
|---|---|---|---|
| P1-a | Azure sibling order is implied, never contractually guaranteed, as reading order; `SECT_IOU_MAX=1/3` and the R19 order audit are guesses until measured. | Per-page demotion counter visible from day one; tau sweep per `api_version`. | If `conflict_demoted` >20% of provider-seeded pages on the corpus, stop trusting provider order: geometry tree becomes the default rung and sections seed grouping only. |
| P1-b | Continuity rubric tuned on prose logic; KYC forms are punctuation-poor and all-caps — recall low by design, and hyphen-lookalike OCR noise can still cause a false merge hint. | Paragraph-kind gate + adjacency gate + edges-annotate-never-reorder: worst case is a declined hint. Dehyphenation ships off. | If false-merge-hint rate >1% on labelled corpus, raise `CONT_EDGE_MIN` before enabling any dehyphenation; if recall ≈0, the feature stays (edges are optional annotations). |
| P1-c | Heading levels from height quantiles misrank on mixed-DPI scans → wrong outline depth. | Nesting groups, never reorders — wrong indent, not wrong text order; per-page em-relative re-normalization is the v2 fix. | None (contained by construction); track distortion in the tau sweep. |
| P2-a | The 2.0/3.0 anchor-rect equality quietly breaks if anyone reimplements rounding. | `geom.py` is the single implementation (R13); explicit cross-file anchor-equality test. | Gate 100% on the anchor test before `emit`. |
| P2-b | Linearize-vs-fence type rule (§5.3) mis-renders some region (linearized form panel or fenced prose). | Rule is deterministic and per-flow_group; fence remains available; corpus sweep eyeballs flagged pages. | If >5% of corpus flow_groups render "wrong" per manual review, keep default `build`, revise the rule (versioned builder bump). |
| P2-c | Page-spanning tables: two adjacent table leaves, structure lost (acknowledged, deferred). | Order is still correct; adjacency recorded in prov. | v2 scope; not a gate. |
| P3-a | A general LLM may simply be mediocre at geometry-only ordering — no published precedent (closest: Layout2Pos, LayoutGPT; suggestive, not proof). | Ships `off`; shadow accumulates artifacts; promotion needs measured precision. | If shadow precision <90% after prompt/threshold iteration, **kill active mode permanently**; the pass degrades to a `flag_break` review-queue triage tool (still useful) or is removed. |
| P3-b | Window seams: a break straddling two mid-page windows on a dense page is invisible to both. | 4 context nodes + R6 cross-page carve-out cover the common case; stated cost of bounded payloads. | If shadow data shows >10% of labelled breaks lost at seams, build the deferred seam-stitching pass before promotion. |
| P3-c | Verifier thresholds (`CONT_CONFIRM_MIN=4`, 2·em inversion, 12-op cap, `FURNITURE_MARGIN_PM=90`) are literature-anchored guesses. | All named constants under `VERIFIER_VERSION`; every rejection recorded with rule id; retunes are visible version bumps. | If tuning oscillates without converging on the corpus (precision and yield can't both clear gates), stay in shadow indefinitely. |
| P3-d | Residual payload leakage: template fingerprint + document type/language class are disclosed to the LLM by design. | Stated openly; the two-identities test defines and enforces the boundary ("cannot distinguish two people on one template"). | If any future field addition fails `test_two_identities_one_payload` or the n-gram tripwire, that field is dead on arrival — the tests are the kill switch. |
| ALL | Stored artifacts never retro-update: a builder fix ships and old trees look "wrong" in triage. | `builder` semver in tree header + surfaced in console; two-files-two-hashes doctrine documented. | None — this is the frozen-rules property working as intended. |
---

## 10. ADDENDUM (2026-09-01) — multimodal payload mode, per explicit owner authorization

The owner has authorized sending **document page images** to the arrange LLM — Gemini 2.5
Flash via the Stellar gateway (COIN OAuth2, corporate SSL, VDI-only), with Vertex Gemini as
the off-network fallback — the same approved path DES already uses for contextual enrichment.
This amends §4.2's structure-only posture as follows, and ONLY as follows:

- `DPC_ARRANGE_PAYLOAD` = `multimodal` (default) | `structure`. In `multimodal`, each window's
  request carries the structural feature payload **plus PNG crops of the window's page
  region(s)** (rasterized at `DPC_ARRANGE_RASTER_DPI`, default 144 — DES's value). In
  `structure`, the request is exactly §4.2 and the PII suite applies unchanged.
- Multimodal requires source bytes (a `document` input). Provider-JSON inputs
  (`azure_*_result`, `des_ocr`) have no pixels; the pass falls back to `structure`
  automatically and the artifact records `payload_mode: structure(fallback_no_images)`.
- The arrangement artifact always records `payload_mode`, and image bytes are NEVER stored
  in it — only `image_sha256` per window, so the disclosure is auditable without a copy.
- The client (`dpc/arrange/client.py`) is provider-agnostic, mirroring the org pattern:
  - `stellar`: OpenAI-compatible `/chat/completions`; COIN OAuth2 client-credentials token
    (client id/secret/scope arrive base64-encoded in env, scope prefixed `coinscope`; token
    POST intentionally `verify=False`; API calls verify against `SSL_CERT_FILE`); token
    refreshed every `DPC_COIN_TOKEN_TTL_SECONDS` (default 840). Images as `image_url`
    data: URIs.
  - `vertex`: `google-genai` with `GOOGLE_APPLICATION_CREDENTIALS`; images as inline parts.
    The local-dev and laptop path.
  - `stub`: fixture replay by `{payload_sha256}.{sample_ix}.json`, never any network.
- The structure-only mode remains fully implemented and tested — it is the posture for any
  deployment whose gateway approval does not extend to document pixels.
