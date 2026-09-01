"""The LLM arrangement-validation pass (SPEC-DOCTREE-1 §4, §10) — ``dpc/arrange/``.

Models propose; the verifier decides; every artifact stamps what decided it. The public
surface is deliberately small: the API background hook calls :func:`run_arrange_pass`,
tests and tooling reach the seams (features/windows/verifier/client) directly.
"""
from dpc.arrange.artifact import ARRANGEMENT_SCHEMA, SKIP_REASONS, artifact_sha256
from dpc.arrange.client import SAMPLE_TEMPS, ArrangeLlmClient
from dpc.arrange.features import NodeFeature, build_features
from dpc.arrange.ops import OpName, RawOp, Reason, parse_sample
from dpc.arrange.payload import (
    PROMPT_TEMPLATE,
    PROMPT_TEMPLATE_VERSION,
    Window,
    WindowPayload,
    make_windows,
)
from dpc.arrange.runner import ArrangementResult, run_arrange_pass, should_run
from dpc.arrange.verifier import VERIFIER_VERSION, verify_window

__all__ = [
    "ARRANGEMENT_SCHEMA",
    "PROMPT_TEMPLATE",
    "PROMPT_TEMPLATE_VERSION",
    "SAMPLE_TEMPS",
    "SKIP_REASONS",
    "VERIFIER_VERSION",
    "ArrangeLlmClient",
    "ArrangementResult",
    "NodeFeature",
    "OpName",
    "RawOp",
    "Reason",
    "Window",
    "WindowPayload",
    "artifact_sha256",
    "build_features",
    "make_windows",
    "parse_sample",
    "run_arrange_pass",
    "should_run",
    "verify_window",
]
