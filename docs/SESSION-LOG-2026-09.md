# Session log — 2026-08/09

Chronological record of what was asked, what was decided, and why. `HANDOFF.md` says *where
things stand*; this says *how they got there*, because the reasoning behind several decisions
is not recoverable from the code alone.

Requests are the owner's, paraphrased faithfully. Outcomes are what actually happened,
including the parts that did not work.

---

## Part 1 — DCE (`~/document-classification-extraction`)

### Request: "Look at DCE and suggest improvements… also a strategy for the optical channel using convolution… give me state-of-the-art document classification"

Three research threads plus a first-hand measurement.

**The measurement that organised everything.** Rather than theorise about abstentions, every
corpus abstention was tallied by its *failing gate*:

```
concurrence 9 · separation 4 · coverage 1
```

Those 14 split into three populations wanting three different remedies: **anchor-gap (~5)**
where the leader is already correct but uncorroborated (`ca_aif` leads at 0.889); **registry
defects (2)** (`mx_cif` is a template-identical twin; `us_1099` shares an IRS cover sheet where
`us_w9` anchors fire 7.20 bits against the 1099's own 4.90); and **card-evidence starvation
(~5)** (`mx_ine` coverage **0.111**, green card/EAD margins of 0.015) — the last being the
visual channel's named target list.

**Key findings.**

- **A wire defect blocks the highest-value work.** `runners_up` still ships the
  registry-normalised softmax — the N-dependent quantity the evidence-in-bits rewrite removed
  from decisions. Any fork mining confusions from the wire is standing on the fixed defect.
  Exposing `lead_bits` (audit-only, provable by a byte-identical corpus run) is prerequisite.
- **`confusable_with` is not a taxonomy.** Its transitive closure is one 87-doctype component
  spanning three countries; only the winner↔runner-up *edge* is usable.
- **L3 BERT is decision-inert** — scores enter only the audit trail, never `_verdict`. The docs
  claim it remedies family confusion. That claim is currently false.
- **The margin floor should not move on this corpus.** Sweeping it costs 7 correct answers and
  buys no *measured* precision; one wrong answer has no statistical power.

**Optical channel — reconciled with the closed programme.** A prior visual effort was measured
at 0.08 precision and shut down, concluding *"a layout descriptor cannot see a title word."*
The new proposal survives that autopsy by being different in three ways: **scoped to rectified
cards only** (manufactured artefacts with fixed per-version geometry); **region-restricted**
rather than whole-card (0.9907 was a whole-card score on cards genuinely ~99% identical by
area — the signal is in the ~1%, at the DHS-mandated compliance corner, which is *glyph-adjacent
by design*); and **calibrated to log₂-LR bits feeding the existing gates**, never a competing
classifier with a veto (the MRZ-veto contamination lesson).

**SOTA verdict, licences read not assumed.** Conformal/LTT calibration over the existing bits
scorer ranks first (no new model, distribution-free guarantee). LayoutLMv3 is **licence-dead**
(CC BY-NC-SA, verified in Microsoft's own README). Zero-shot VLMs rejected on numbers (best
published 69.9%). RVL-CDIP deltas rejected as noise (8.1% measured label error). Azure DI
custom classification **verified cloud-only** — cannot satisfy residency.

**Deliverables.** `docs/specs/2026-08-25-core-and-visual-roadmap.md` (`e6bf07e`, main) and
`docs/specs/2026-08-25-implementation-plan.md` on branch **`improvement/core-visual-roadmap`**
(`d54193f`) — seven builder-agent work orders. **Neither is implemented.**

---

## Part 2 — DPC: PMD 2.0

### Request: "Relative positions are not preserved… no internal OCR, everything to Azure Layout… two columns should look similar in the md file, use spaces"

**The complaint reproduced first.** A two-column statement through the old emitter collapsed
into one vertical list — and worse, it read as *sequential*, so an LLM would bind "Closing
Balance" to the account-number block above it. The old emitter did this **deliberately**: its
docstring rejected geometry ordering because "a naive y-sort interleaves columns line by line."
So this was a design replacement, not a bug fix.

**A prerequisite defect found on the way.** Azure reports PDF geometry in **inches**; the
adapter stored it verbatim; `_rect` rounded to integers. Every Azure-read PDF's coordinates
collapsed onto an 8×11 grid — a heading and the paragraph below it emitted the *identical*
anchor `[1,2,4,2]`, and a title 0.35 in tall emitted a zero-height rectangle. A spatial emitter
computing columns from those numbers would have found nothing. Fixed via milli-inches under a
declared `scale=1000`; point/pixel pages byte-identical.

**A prototype flaw caught before it shipped.** The naive `pdftotext -layout` band approach
discards vertical position *within* a band: a tall paragraph beside four short fee lines
collapsed all four onto one row. Fixed by making each band a true 2-D canvas with row placement
computed from y.

**The Office/HTML tension, surfaced not buried.** Research established DI returns no geometry
for Office/HTML and **no tables at all for XLSX**. Routing them to Azure satisfies the letter of
"send everything to Azure" and destroys the output. Decision: PDFs and images always to Azure
(the actual complaint, fixed unconditionally); XLSX/HTML keep native readers with
`DPC_OFFICE_ROUTE=azure` available. **This was flagged to the owner explicitly as cutting
against the instruction.**

**Review found what tests missed.** Two CRITICAL (a two-byte `BM` magic classifying
`"BMW Finance Ltd…"` as a bitmap; every new refusal returning 500 instead of 415) and a
**determinism** defect — the line-join keyed on `spans[0][0]` instead of the minimum offset, so
reordering a semantically meaningless array moved a line to a different block and changed the
sha256. Plus an O(lines×blocks) join measured at **49 s** on a 400-page document.

---

## Part 3 — DPC: the DocTree

### Request: "Build a tree from the OCR data… nodes are paragraphs, images as placeholders, tables… heuristic flattening, then an LLM that rearranges without seeing PII… bounding-box mathematics should guide the heuristics"

**The decisive discovery.** Azure DI v4 returns `analyzeResult.sections` (hierarchical, with
element references) and `figures` — *a provider-computed document tree* — and the adapter
**dropped both**. So the architecture became *seed from provider structure, audit with geometry,
fall back to pure geometry*, not invent-a-tree-from-scratch. That made Phase 1 much stronger and
needed no LLM at all.

**The PII boundary, prototyped before designing.** A throwaway script proved the complete LLM
request body could contain **zero document strings** while the derived booleans still carried
the continuity signal — a paragraph flowing column 1 → column 2 flags on
`ends_hyphen ∧ ¬ends_terminal ∧ starts_lower`, while label→value correctly does not. The
prototype also exposed a bug fed straight into the design: floor-bucketing `chars` sent a
14-character label as `chars: 0`, indistinguishable from empty.

**The design panel's sharpest catch.** For a **right-aligned** form field, `x0 = x1 − width`, so
sending a 1%-grid left edge was a ~1-character-resolution **length oracle** on a known template
— an attacker with the blank form could read how long the filled name is. Resolution: send only
the template-fixed *anchor edge*. Enforced by `test_two_identities_one_payload` (same template,
two synthetic people → byte-identical payloads) and a transport-level 4-gram tripwire.

**The determinism/LLM tension, resolved structurally.** Stored artifacts are sha256-stamped; an
LLM is neither deterministic nor always available. So: the tree carries **zero document strings
by type**, the LLM proposes ops in a closed language, a deterministic verifier (V1–V9) decides,
and accepted ops derive a *separate variant* stamped `heuristics+patch@<sha8>`. The heuristic
artifact is never mutated.

### Request: "Use multimodal Gemini 2.5 Flash via Stellar gateway like we used"

Authorization to send **page images** — recorded as `SPEC-DOCTREE-1` §10 before implementing,
with `payload_mode` in every artifact, image **sha only** (never bytes) stored, automatic
fallback to structure-only when the input has no pixels, and the strict structure-only mode
retained for deployments whose gateway approval does not extend to pixels.

**Three defects only the live run found**, after 640 tests were already green:

1. `genai.Client()` bare hunts for `GOOGLE_API_KEY` and dies `ApiKeyMissing` despite a valid
   service account — needs `vertexai=True, project=…, location=…`.
2. `should_run` consulted only the tree. Frame nodes exist only when the *geometry* rung built
   it, so a provider-seeded two-column page — **the flagship case a reading-order validator
   exists for** — skipped as `clean_single_column`.
3. `n_rejected` counted the literal `"REJECTED"`, which the verifier never emits (its vocabulary
   is `ACCEPTED | ADVISORY | REJECT_<RULE>`), so it counted zero forever.

**Live result:** `status=ran, payload_mode=multimodal, model=gemini-2.5-flash`, three samples
parsed, **zero ops proposed** on a correctly-ordered document — the honest negative.

---

## Part 4 — DPC: `/process` and the console

### Request: "Make the UI better, and an endpoint /api/v1/process that takes one file plus an optional doc_id"

`/process` **delegates to `convert()`** rather than duplicating the pipeline — one code path, so
the upload face inherits routing, tree/arrange wiring, and the whole refusal surface, and the
two faces cannot drift. Pinned by a test asserting identical artifact shas through both.

Console rebuilt upload-first with four result tabs. **Visual QA was done by hand in a browser
against the live service**, which caught stale copy still claiming "a PDF with a text layer is
read locally" — false since Azure-only routing. Code review caught `image/*` in the accept list
re-admitting exactly the formats routing refuses, and a conversion that took minutes of OCR
being **discarded** when the follow-up markdown fetch blipped.

A satisfying moment from that QA: the Tree tab on a real W-9 showed
`provider_sections: conflict_demoted(pages=[3,4,6])` — the geometry audit rejecting the mock's
unreliable sections on three pages, visible in the UI. The honesty machinery is not only in the
artifact; a reviewer can see it.

---

## Corrections made to my own claims during this session

Recorded because a handoff that hides them is less trustworthy, not more.

- Asserted "21 title-gated decisive anchors" in DCE; the real number is **4**. Corrected.
- Cited a 76.7% recall figure from a one-off script; the committed harness says **35.3%**.
  Corrected in `ARCHITECTURE.md` and `RUNBOOK.md`.
- Claimed `__pycache__` files were still tracked in this handoff's first draft; they had already
  been untracked. Corrected before committing.
- Wrote a `should_run` regression test whose premise was wrong (the fixture also produced order
  ties, so the gate fired for a different reason). Isolated the branch under test rather than
  letting the test pass for the wrong reason.
