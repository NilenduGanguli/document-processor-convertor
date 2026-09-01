"""The op language (SPEC-DOCTREE-1 §4.4) and strict parsing of model output.

The grammar is NOT the security boundary — the verifier is (§4.3). Parsing is strict anyway
because a sloppily-parsed sample is an unauditable sample: the artifact stores either a
schema-valid ops array verbatim, or a discard reason, never free text. A model response that
fails any check below is DISCARDED WITH A NAMED REASON — its raw text is not stored, not
logged, and not partially salvaged, so arbitrary model prose can never reach a stored
artifact through the "verbatim sample" channel.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from dpc.arrange.features import NId

#: The op envelope's schema id — the model must echo it (§4.4).
OPS_SCHEMA = "dpc-arrange-ops/1"


class OpName(enum.StrEnum):
    """§4.4 verbatim. ``split`` is ADVISORY_ONLY in v1; ``flag_break`` is pure advisory."""

    move_before = "move_before"
    move_after = "move_after"
    reparent = "reparent"
    merge_flow = "merge_flow"
    split = "split"
    flag_break = "flag_break"


#: Ops that change the derived variant; the only ones ``apply_patch`` will accept, and the
#: only ones V8 majority voting and V9's runaway cap count.
MUTATING_OPS = frozenset({
    OpName.move_before, OpName.move_after, OpName.reparent, OpName.merge_flow,
})

#: Ops that take a ``ref``. Advisory ops must NOT carry one — a ref on a split would imply
#: apply semantics v1 does not have.
REF_OPS = MUTATING_OPS


class Reason(enum.StrEnum):
    """§4.4's closed reason enum, verbatim."""

    COLUMN_CONTINUATION = "COLUMN_CONTINUATION"
    PAGE_CONTINUATION = "PAGE_CONTINUATION"
    INTERRUPTED_FLOW = "INTERRUPTED_FLOW"
    ORDER_INVERSION = "ORDER_INVERSION"
    SIDEBAR_DEFERRED = "SIDEBAR_DEFERRED"
    FURNITURE_MISPLACED = "FURNITURE_MISPLACED"
    CAPTION_DETACHED = "CAPTION_DETACHED"
    TABLE_FRAGMENT = "TABLE_FRAGMENT"
    LIST_CONTINUATION = "LIST_CONTINUATION"
    HEADING_SCOPE = "HEADING_SCOPE"
    OTHER_STRUCTURAL = "OTHER_STRUCTURAL"


class RawOp(BaseModel):
    """One model-proposed op, schema-checked. ``extra="forbid"``: an unexpected key is a
    malformed sample, not a tolerated passenger — unknown keys are where free text hides."""

    model_config = {"extra": "forbid"}

    op: OpName
    node: NId
    ref: NId | None = None
    reason: Reason
    confidence_pm: int | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _ref_shape(self) -> RawOp:
        if self.op in REF_OPS and self.ref is None:
            raise ValueError("mutating op requires ref")
        if self.op not in REF_OPS and self.ref is not None:
            raise ValueError("advisory op must not carry ref")
        return self

    def identity(self) -> tuple[str, str, str]:
        """V8's canonical op identity: ``(op, node, ref)`` — reasons/confidence excluded."""
        return (self.op.value, self.node, self.ref or "")

    def dump(self) -> dict[str, Any]:
        """The op as a canonical JSON-ready dict (what the artifact stores verbatim)."""
        return self.model_dump(mode="json", exclude_none=True)


class OpsEnvelope(BaseModel):
    """The whole response document: ``{"schema": "dpc-arrange-ops/1", "ops": [...]}``."""

    model_config = {"populate_by_name": True, "extra": "forbid"}

    schema_: str = Field(alias="schema", pattern=r"^dpc-arrange-ops/1$")
    ops: list[RawOp] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ParsedSample:
    """One sample's parse outcome: a schema-valid ops list XOR a named discard reason."""

    ops: tuple[RawOp, ...] | None
    discarded: str | None

    @property
    def usable(self) -> bool:
        return self.ops is not None


def _strip_fence(text: str) -> str:
    """Drop one surrounding markdown code fence, if present.

    Normalisation, not laxity: chat models wrap JSON in ``` fences habitually, and refusing
    the fence would discard otherwise schema-perfect samples for a formatting tic. Anything
    beyond one clean fence still fails ``json.loads`` and is discarded.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1 and stripped.endswith("```"):
            stripped = stripped[first_newline + 1 : -3].strip()
    return stripped


def parse_sample(text: str) -> ParsedSample:
    """Strict parse of one model response into ops, or a discard with a named reason.

    Args:
        text: The raw model response text.

    Returns:
        A :class:`ParsedSample`. Discard reasons are a closed set: ``not_json`` (the text is
        not a JSON object) or ``bad_schema`` (JSON, but not the §4.4 envelope). The raw text
        of a discarded sample goes nowhere.
    """
    try:
        document = json.loads(_strip_fence(text))
    except (ValueError, TypeError):
        return ParsedSample(ops=None, discarded="not_json")
    if not isinstance(document, dict):
        return ParsedSample(ops=None, discarded="not_json")
    try:
        envelope = OpsEnvelope.model_validate(document)
    except ValidationError:
        return ParsedSample(ops=None, discarded="bad_schema")
    return ParsedSample(ops=tuple(envelope.ops), discarded=None)


__all__ = [
    "MUTATING_OPS",
    "OPS_SCHEMA",
    "REF_OPS",
    "OpName",
    "OpsEnvelope",
    "ParsedSample",
    "RawOp",
    "Reason",
    "parse_sample",
]
