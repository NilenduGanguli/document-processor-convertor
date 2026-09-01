"""Shared page-geometry rounding — the ONE implementation of anchor-rectangle math.

Promoted verbatim from ``dpc/emitter.py`` (SPEC-DOCTREE-1 R13): the doctree builder must
union node bboxes with *exactly* the rounding the emitter uses for its anchors, or the 2.0
and 3.0 artifacts drift apart by one milli-unit and the cross-artifact anchor-equality gate
(risk P2-a) fails in the least debuggable way possible. Private-helper coupling
(``from dpc.emitter import _rect``) was the wrong mechanism for the right instinct, so the
two functions live here and the emitter imports them under its historical names.

Nothing in this module may grow provider- or artifact-specific behaviour. It is arithmetic
only: unit -> scale factor, quad -> integer rectangle. Both functions are pure, total over
their accepted inputs, and float-free past the single half-up rounding step (SPEC-PMD-2 §4.1:
after quantisation the pipeline performs no floating-point arithmetic).
"""
from __future__ import annotations

import math

from dpc.models import Quad


def page_scale(unit: str, rect_scale: str = "auto") -> int:
    """Anchor scale factor for a page in ``unit``: 1000 for inches, 1 otherwise.

    **This exists because PMD 1.0's anchors were a total loss on every Azure-read PDF.**
    Azure Document Intelligence reports PDF geometry with ``"unit": "inch"`` and polygons in
    inches, the adapters store those coordinates verbatim in the page's own unit, and
    :func:`rect_scale` rounded them to integers — so a US-Letter page collapsed onto an 8x11
    integer grid. Measured on a real payload: a heading at y 1.53-1.78 in and the paragraph
    below it at y 1.88-2.10 in both emitted ``[1,2,4,2]``, and a title 0.35 in tall emitted a
    zero-height rectangle. Distinct rows became indistinguishable, which is loss, not
    rounding.

    Scaling inches to milli-inches restores 0.072 pt of resolution while keeping every
    rectangle an integer — integers being what makes the output byte-deterministic across
    float formatting. Point and pixel pages already have adequate integer resolution
    (1/72 in and one device pixel), so they are untouched and their bytes are unchanged.

    Args:
        unit: The page's own unit, as the provider reported it.
        rect_scale: ``"auto"`` scales inch pages; ``"legacy"`` pins PMD 1.0's rounding
            everywhere, for a caller regenerating a stored sha256.
    """
    if rect_scale == "legacy":
        return 1
    return 1000 if unit == "inch" else 1


def rect_scale(quad: Quad | None, scale: int = 1) -> tuple[int, int, int, int] | None:
    """A quad's axis-aligned bounding rectangle as integers in ``scale``-ths of the page unit.

    Rounded because sub-unit precision below the scale is provider noise, and stable integers
    are what make the output byte-deterministic across float formatting differences. See
    :func:`page_scale` for why ``scale`` is not always 1.

    Half-up rather than :func:`round` when scaling: Python's ``round`` is banker's rounding,
    so a coordinate landing exactly on .5 resolves by the parity of its neighbour and a
    1-ULP change in a polygon can flip it. Half-up is total and monotone.
    """
    if not quad or len(quad) < 8:
        return None
    xs, ys = quad[0::2], quad[1::2]
    if scale == 1:
        return (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys)))
    return (
        math.floor(min(xs) * scale + 0.5), math.floor(min(ys) * scale + 0.5),
        math.floor(max(xs) * scale + 0.5), math.floor(max(ys) * scale + 0.5),
    )


__all__ = [
    "page_scale",
    "rect_scale",
]
