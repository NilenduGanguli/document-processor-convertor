"""Continuation linking — ONE rubric, ONE function (SPEC-DOCTREE-1 §3.3, R10).

The builder imports :func:`continuation_score` to decide whether to EMIT a ``continues``
edge, and the arrange verifier (V6) imports the very same function to decide whether an
LLM-proposed ``merge_flow`` is geometrically plausible. Two rubrics for one question is how
"plausible" comes to mean two different things, which is the exact defect R10 exists to
prevent — so the verifier owns no formula and this module owns exactly one.

Edges ANNOTATE, they never reorder (R11): the worst case of a wrong score in the stored tree
is a wrong hint. Low recall is intentional — on KYC forms a false merge glues two field
values together, which is strictly worse than a missed merge.
"""
from __future__ import annotations

from dataclasses import dataclass

from dpc.doctree.metrics import height_class
from dpc.doctree.models import Evidence, Metrics

#: §3.3/§3.4 (R10): the builder emits a ``continues`` edge at score >= 5. This is TREE's
#: original threshold of 3 plus the now-explicit +2 adjacency bonus (every builder candidate
#: is adjacent by gate): hyphen alone (3+2) still passes; no-terminal + lowercase (1+2+2)
#: still passes; nothing weaker does. Conjunction-of-signals per pdftotext++.
CONT_EDGE_MIN = 5

#: §3.3 (R10): the verifier accepts an LLM-CONFIRMED ``merge_flow`` at score >= 4 — one point
#: laxer, asymmetric on purpose: the model may confirm plausible links the builder declined
#: (e.g. adjacency invisible across a window seam), never create geometrically impossible
#: ones. The LLM confirms; it does not create.
CONT_CONFIRM_MIN = 4

#: §3.4: a candidate's source must sit within the bottom GATE_TAIL bands of its column and
#: its target within the top GATE_TAIL of the next. One band of tolerance absorbs stray
#: noise; more would admit mid-column paragraphs.
GATE_TAIL = 2

#: Frame-width match tolerance: continuations live in like-measured columns; two ems of
#: difference is jitter, more is a different measure (§3.3).
_WIDTH_EM = 2


@dataclass(frozen=True, slots=True)
class ContinuityFeatures:
    """What the rubric needs to know about one node — built from stored metrics + geometry.

    A tiny projection rather than the whole node, so the verifier can build it from the LLM
    feature payload (which carries classes, not raw values) and the builder from the tree,
    and both mean the same thing by construction.
    """

    ends_terminal_punct: bool
    ends_hyphen: bool
    #: None when the page case-profile voided it — contributes 0, never a fabricated False.
    starts_lowercase: bool | None
    #: ``small|body|large|display`` — pre-classed so both callers compare like with like.
    height_class: str
    #: The node's frame/column width in milli-units; 0 when unknown.
    width_mu: int
    #: The page em in milli-units; 0 when unknown (voids the width test honestly).
    em: int
    #: Script class VALUE (``Metrics.script_class``); the candidate gate requires equality.
    script_class: str


def features_from_metrics(
    metrics: Metrics, *, width_mu: int, em: int
) -> ContinuityFeatures:
    """The builder-side constructor: stored :class:`Metrics` + column geometry -> features."""
    return ContinuityFeatures(
        ends_terminal_punct=metrics.ends_terminal_punct,
        ends_hyphen=metrics.ends_hyphen,
        starts_lowercase=metrics.starts_lowercase,
        height_class=height_class(metrics.height_mu, em),
        width_mu=width_mu,
        em=em,
        script_class=metrics.script_class.value,
    )


def score_with_evidence(
    src: ContinuityFeatures, dst: ContinuityFeatures, adjacency: bool
) -> tuple[int, list[Evidence]]:
    """The §3.3 rubric with its evidence trail — what the stored ``flow`` edge records.

    | Signal                          | Points | Why                                        |
    |---------------------------------|--------|--------------------------------------------|
    | ``src.ends_hyphen``             | +3     | Near-sufficient alone: a line-final hyphen |
    |                                 |        | at a boundary is the split-word signature. |
    | ``not src.ends_terminal_punct`` | +1     | Weak alone — KYC values rarely end in      |
    |                                 |        | periods either.                            |
    | ``dst.starts_lowercase``        | +2     | Strong cue; 0 when None (all-caps page /   |
    |                                 |        | non-bicameral script — the metrics gate).  |
    | height classes equal            | +1     | Same paragraph => same type size.          |
    | widths within 2 em              | +1     | Continuations live in like-measured        |
    |                                 |        | columns.                                   |
    | adjacency                       | +2     | Frame-edge OR interposed pair (R15) —      |
    |                                 |        | TREE's hard gate made explicit, so the     |
    |                                 |        | verifier scores cross-page cases uniformly.|

    Args:
        src: The candidate continuation's SOURCE (the paragraph that may be cut short).
        dst: The candidate TARGET (the paragraph that may continue it).
        adjacency: True when the pair is a frame-edge pair or an interposed pair.

    Returns:
        ``(score, evidence)`` — evidence in fixed rubric order, so the stored list is a
        deterministic function of the inputs, never of evaluation order.
    """
    score = 0
    evidence: list[Evidence] = []
    if src.ends_hyphen:
        score += 3
        evidence.append(Evidence.ends_hyphen)
    if not src.ends_terminal_punct:
        score += 1
        evidence.append(Evidence.no_terminal)
    if dst.starts_lowercase:  # None contributes 0 — the honest all-caps/CJK gate.
        score += 2
        evidence.append(Evidence.starts_lower)
    if src.height_class == dst.height_class:
        score += 1
        evidence.append(Evidence.height_match)
    if (
        src.em > 0
        and src.width_mu > 0
        and dst.width_mu > 0
        and abs(src.width_mu - dst.width_mu) <= _WIDTH_EM * src.em
    ):
        score += 1
        evidence.append(Evidence.width_match)
    if adjacency:
        score += 2
        evidence.append(Evidence.adjacency)
    return score, evidence


def continuation_score(
    src: ContinuityFeatures, dst: ContinuityFeatures, adjacency: bool
) -> int:
    """§3.3's single scoring function — imported by the builder AND the arrange verifier.

    Args:
        src: Source-node features.
        dst: Target-node features.
        adjacency: True for a frame-edge pair or an interposed pair (R15).

    Returns:
        The integer evidence score. The builder emits an edge at >= :data:`CONT_EDGE_MIN`;
        the verifier confirms at >= :data:`CONT_CONFIRM_MIN`.
    """
    return score_with_evidence(src, dst, adjacency)[0]


__all__ = [
    "CONT_CONFIRM_MIN",
    "CONT_EDGE_MIN",
    "GATE_TAIL",
    "ContinuityFeatures",
    "continuation_score",
    "features_from_metrics",
    "score_with_evidence",
]
