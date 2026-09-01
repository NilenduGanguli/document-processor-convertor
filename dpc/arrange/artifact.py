"""``arrangement.json`` assembly (SPEC-DOCTREE-1 §4.6) — canonical bytes, no wall clock.

R8 is the whole point of this module's shape: the artifact's bytes contain NO wall-clock
field, no latency, no timestamp — every byte is either produced deterministically from
(tree, view, recorded samples) or IS a recorded input — so re-running the verifier over a
stored artifact's raw samples reproduces it byte-for-byte. Timings go to logs only.

Skips are artifacts too (§4.7): a pass that did not run writes ``{"status": "skipped",
"reason": ...}`` from a CLOSED reason set, and a pass that blew up writes
``{"status": "error:<Type>"}`` — stamped, never silent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from dpc.arrange.payload import PROMPT_TEMPLATE_VERSION
from dpc.arrange.verifier import VERIFIER_VERSION

#: Stored artifact schema id.
ARRANGEMENT_SCHEMA = "dpc-arrangement/1"

#: §4.7's closed skip-reason set. A reason outside this set is a programming error the
#: tests catch, not a new vocabulary entry.
SKIP_REASONS = frozenset({
    "no_llm_configured",
    "timeout",
    "clean_single_column",
    "no_geometry",
    "budget_exhausted",
})


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """The one serialization ``artifact_sha256`` may hash — same canonical form as
    :func:`dpc.doctree.models.dump_tree` (sorted keys, ASCII, compact)."""
    return json.dumps(
        document, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def artifact_sha256(document: dict[str, Any]) -> str:
    """Hex sha256 of :func:`canonical_bytes` — the artifact's content address."""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def ran_artifact(
    *,
    doc_id: str,
    pmd_sha256: str,
    sha256_tree: str,
    model_id: str,
    payload_mode: str,
    samples: int,
    windows: list[dict[str, Any]],
    accepted_ops: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    """The §4.6 artifact for a pass that ran (fully or to budget exhaustion).

    Args:
        doc_id: The conversion's document id.
        pmd_sha256: The stored PMD's sha (the serving-bytes link).
        sha256_tree: The heuristic tree's sha — what the ops were verified against.
        model_id: The model actually called (``arrange_model``).
        payload_mode: ``multimodal`` | ``structure`` | ``structure(fallback_no_images)``
            (§10 — always recorded, so the disclosure surface is auditable per document).
        samples: k (samples per window).
        windows: Per-window records: ``window_ix, page, node_span, payload_sha256``, plus
            ``image_sha256`` when a page image was sent (NEVER the image bytes), ``raw``
            (verbatim schema-valid ops per sample, or a discard reason) and ``verdicts`` —
            or ``skipped: budget_exhausted`` for windows the budget cut off.
        accepted_ops: Path-addressed accepted ops in canonical application order.
        review_queue: Accepted ``flag_break`` advisories.

    Returns:
        The artifact document, ready for :func:`canonical_bytes`.
    """
    return {
        "schema": ARRANGEMENT_SCHEMA,
        "doc_id": doc_id,
        "pmd_sha256": pmd_sha256,
        "sha256_tree": sha256_tree,
        "status": "ran",
        "model_id": model_id,
        "payload_mode": payload_mode,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "samples": samples,
        "windows": windows,
        "accepted_ops": accepted_ops,
        "review_queue": review_queue,
    }


def skip_artifact(
    *, doc_id: str, sha256_tree: str, reason: str, payload_mode: str | None = None
) -> dict[str, Any]:
    """A §4.7 skip — stamped, never silent. ``reason`` must come from the closed set."""
    if reason not in SKIP_REASONS:
        # A programming error, surfaced honestly rather than minted into the vocabulary.
        reason = "no_llm_configured"
    document: dict[str, Any] = {
        "schema": ARRANGEMENT_SCHEMA,
        "doc_id": doc_id,
        "sha256_tree": sha256_tree,
        "status": "skipped",
        "reason": reason,
        "verifier_version": VERIFIER_VERSION,
    }
    if payload_mode is not None:
        document["payload_mode"] = payload_mode
    return document


def error_artifact(*, doc_id: str, sha256_tree: str, exc_name: str) -> dict[str, Any]:
    """The boundary-failure artifact: the exception's CLASS NAME only, never its message —
    an exception message can quote anything, including document text (the PII rule)."""
    return {
        "schema": ARRANGEMENT_SCHEMA,
        "doc_id": doc_id,
        "sha256_tree": sha256_tree,
        "status": f"error:{exc_name}",
        "verifier_version": VERIFIER_VERSION,
    }


__all__ = [
    "ARRANGEMENT_SCHEMA",
    "SKIP_REASONS",
    "artifact_sha256",
    "canonical_bytes",
    "error_artifact",
    "ran_artifact",
    "skip_artifact",
]
