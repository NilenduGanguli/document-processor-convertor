"""Tests for :mod:`dpc.geom` — the R13 promotion of the emitter's shared rounding.

The promotion's whole contract is "nothing changed": ``geom.page_scale``/``geom.rect_scale``
must be the exact functions the emitter used privately, the emitter must keep serving them
under its historical names, and the emitted bytes over the existing fixtures must be
byte-identical to the pre-promotion goldens. The golden string in ``test_emitter.py`` was
committed before the promotion, so comparing a fresh render against it IS the byte-identity
proof — no snapshot juggling required.
"""
from __future__ import annotations

from test_emitter import GOLDEN, build_view, render

from dpc import emitter, geom
from dpc.geom import page_scale, rect_scale


# ---------------------------------------------------------------------------
# The promotion itself
# ---------------------------------------------------------------------------
def test_emitter_serves_the_promoted_functions() -> None:
    """The emitter's names are aliases, not copies — one implementation (R13, risk P2-a)."""
    assert emitter.page_scale is geom.page_scale
    assert emitter._rect is geom.rect_scale


def test_emitter_bytes_identical_after_promotion() -> None:
    """The full golden render is unchanged by moving the helpers to geom.py."""
    assert render(build_view()) == GOLDEN


def test_emitter_bytes_identical_on_inch_pages() -> None:
    """The scale=1000 path (the one with real arithmetic) also survives the move.

    The golden view is all point-unit, so this re-renders it on inch pages and pins the
    milli-inch rectangles to values computed by hand from the quads — independent of any
    implementation, promoted or not.
    """
    view = build_view()
    for page in view.pages:
        page.unit = "inch"
    out = render(view)
    # Title quad [72, 40, 540, 80] in "inches" -> milli-inches, exactly.
    assert "<!-- @1 [72000,40000,540000,80000] title -->" in out
    assert "scale=1000" in out


# ---------------------------------------------------------------------------
# page_scale
# ---------------------------------------------------------------------------
def test_page_scale_units() -> None:
    assert page_scale("inch") == 1000
    assert page_scale("point") == 1
    assert page_scale("pixel") == 1
    assert page_scale("") == 1


def test_page_scale_legacy_pins_pmd_1_0_rounding() -> None:
    assert page_scale("inch", "legacy") == 1
    assert page_scale("point", "legacy") == 1


# ---------------------------------------------------------------------------
# rect_scale
# ---------------------------------------------------------------------------
def test_rect_scale_rejects_missing_or_short_quads() -> None:
    assert rect_scale(None) is None
    assert rect_scale([]) is None
    assert rect_scale([1.0, 2.0, 3.0, 4.0]) is None  # 4 numbers is not a quad here


def test_rect_scale_takes_the_axis_aligned_extent() -> None:
    # A rotated quad: the rectangle is min/max per axis, not the first/third corner.
    quad = [2.0, 1.0, 5.0, 2.0, 4.0, 6.0, 1.0, 5.0]
    assert rect_scale(quad) == (1, 1, 5, 6)


def test_rect_scale_at_scale_1_keeps_historic_rounding() -> None:
    """scale=1 keeps ``round()`` (banker's) verbatim — those bytes are already stored."""
    quad = [0.5, 1.5, 2.5, 3.5, 2.5, 3.5, 0.5, 1.5]
    assert rect_scale(quad, 1) == (round(0.5), round(1.5), round(2.5), round(3.5))
    assert rect_scale(quad, 1) == (0, 2, 2, 4)


def test_rect_scale_scaled_path_is_half_up_not_bankers() -> None:
    """At scale=1000 a .5 milli-unit boundary always rounds UP, whatever its parity.

    Banker's rounding would give 2 for 2.5 and 4 for 3.5 — a 1-ULP polygon change could flip
    a stored rectangle. Half-up is total and monotone; this pins it.
    """
    quad = [0.0025, 0.0025, 0.0035, 0.0035, 0.0035, 0.0035, 0.0025, 0.0025]
    assert rect_scale(quad, 1000) == (3, 3, 4, 4)


def test_rect_scale_determinism_across_calls() -> None:
    quad = [1.0001, 2.0002, 3.0003, 2.0002, 3.0003, 4.0004, 1.0001, 4.0004]
    assert rect_scale(quad, 1000) == rect_scale(list(quad), 1000) == (1000, 2000, 3000, 4000)
