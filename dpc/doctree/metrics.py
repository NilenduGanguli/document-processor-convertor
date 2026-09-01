"""Per-block content metrics — the ONLY doctree code that reads document text.

Text enters this module and leaves it as ints, bools and closed enums (SPEC-DOCTREE-1 §3.2
step 2). Everything downstream — the builder, the continuity rubric, the stored tree, the
LLM feature projection — reasons over these outputs, so the PII boundary is a property of
this module's return types rather than of every consumer's discipline.

All decisions are integer decisions. Ratio thresholds are expressed as cross-multiplied
integer tests (``10 * upper >= 9 * cased``), never divisions, because a float ratio that a
1-ULP change can move across a threshold is the determinism trap this codebase exists to
avoid (see ``canvas.mu``).
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from dpc.canvas import mu
from dpc.doctree.models import Alignment, DigitRatioClass, Metrics, ScriptClass
from dpc.models import TextBlock

#: §3.4: pages whose cased letters are >= 90% uppercase void ``starts_lowercase`` — passports
#: and MRZ pages are all-caps with OCR noise; 90 tolerates the noise while catching them.
#: Integer test: ``10 * upper >= 9 * cased``.
ALLCAPS_RATIO = (9, 10)

#: §3.4 height classes, as ``100 * h`` vs ``N * em`` integer tests. Typographic scale steps
#: run ~1.2–1.5x; 0.85 catches footnote-size, 1.8 separates display titles.
HGT_SMALL = 85
HGT_LARGE = 125
HGT_DISPLAY = 180

#: Terminal punctuation across the scripts this corpus actually contains (Latin, CJK,
#: Arabic). Closing quotes/brackets are stripped first: a sentence ending «."» still ended.
_TERMINAL = frozenset(".!?…。！？؟۔")

#: ASCII hyphen-minus plus the dedicated hyphens (U+2010/U+2011) and the soft hyphen a
#: hyphenating OCR pass can leave behind. En/em dashes are excluded on purpose — a dash ends
#: a clause, a hyphen splits a word, and only the second is continuation evidence.
_HYPHENS = frozenset("-‐‑­")

#: Code-point ranges per script class. Ranges, not ``unicodedata.name`` lookups: a name
#: lookup per char on a 200-block page is measurable cost for identical information.
_SCRIPT_RANGES: tuple[tuple[ScriptClass, tuple[tuple[int, int], ...]], ...] = (
    (ScriptClass.latin, ((0x0041, 0x024F), (0x1E00, 0x1EFF))),
    (ScriptClass.cyrillic, ((0x0400, 0x052F),)),
    (ScriptClass.arabic, ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                          (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    (ScriptClass.deva, ((0x0900, 0x097F),)),
    (ScriptClass.cjk, ((0x2E80, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF),
                       (0xFF66, 0xFF9D))),
)


def page_case_profile(blocks: Sequence[TextBlock]) -> bool:
    """True when the page's case profile VOIDS ``starts_lowercase`` (§3.2 step 2).

    Voided when at least 90% of the page's cased letters are uppercase (``ALLCAPS_RATIO`` —
    the passport/MRZ gate) or when the page has no cased letters at all (non-bicameral
    scripts: CJK, Arabic, Devanagari). On such a page a capital initial is a property of the
    page, not of the block, so the signal is honestly ``None`` rather than a fabricated
    ``False``.

    Args:
        blocks: The page's text blocks (the caller filters by page and zone).

    Returns:
        True when ``starts_lowercase`` must be ``None`` for every block on this page.
    """
    upper = cased = 0
    for block in blocks:
        for char in block.text:
            if char.isupper():
                upper += 1
                cased += 1
            elif char.islower():
                cased += 1
    if cased == 0:
        return True
    return ALLCAPS_RATIO[1] * upper >= ALLCAPS_RATIO[0] * cased


def height_class(height_mu: int, em: int) -> str:
    """``small | body | large | display`` from a height against the page em (§3.4).

    Integer tests: ``100 * h < HGT_SMALL * em`` is small, ``>= HGT_DISPLAY * em`` is display,
    ``>= HGT_LARGE * em`` is large, else body. ``em <= 0`` (no geometry) degrades to body —
    the class that carries no signal either way.

    Args:
        height_mu: The element's height in milli-units.
        em: The page em in milli-units.

    Returns:
        The class name — one of the four closed values.
    """
    if em <= 0 or height_mu <= 0:
        return "body"
    scaled = 100 * height_mu
    if scaled < HGT_SMALL * em:
        return "small"
    if scaled >= HGT_DISPLAY * em:
        return "display"
    if scaled >= HGT_LARGE * em:
        return "large"
    return "body"


def _strip_closers(text: str) -> str:
    """Drop trailing closing punctuation (quotes, brackets) and whitespace.

    ``Pe``/``Pf`` categories plus the ASCII quotes that Unicode classifies as ambiguous
    ``Po``. What remains last is the character that actually terminated the sentence.
    """
    end = len(text)
    while end > 0:
        char = text[end - 1]
        if char.isspace() or char in "\"'" or unicodedata.category(char) in ("Pe", "Pf"):
            end -= 1
            continue
        break
    return text[:end]


def _script_of(char: str) -> ScriptClass | None:
    point = ord(char)
    for script, ranges in _SCRIPT_RANGES:
        for lo, hi in ranges:
            if lo <= point <= hi:
                return script
    return None


def _script_class(text: str) -> ScriptClass:
    """Dominant script at >= 90% of classified letters; ``mixed`` below that; ``none`` empty.

    The 90% majority (same integer form as ``ALLCAPS_RATIO``) keeps a Latin paragraph with
    one Cyrillic surname honest — it is still a Latin paragraph — while a genuinely bilingual
    block lands in ``mixed`` rather than whichever script happened to be counted first.
    """
    counts: dict[ScriptClass, int] = {}
    total = 0
    for char in text:
        if not char.isalpha():
            continue
        script = _script_of(char)
        if script is None:
            continue
        counts[script] = counts.get(script, 0) + 1
        total += 1
    if total == 0:
        return ScriptClass.none
    # Max by (count, enum value): the value tiebreak keeps the choice total when two scripts
    # tie exactly — never dict insertion order.
    best = max(counts.items(), key=lambda item: (item[1], item[0].value))
    if 10 * best[1] >= 9 * total:
        return best[0]
    return ScriptClass.mixed


def _digit_ratio_class(text: str) -> DigitRatioClass:
    digits = alnum = 0
    for char in text:
        if char.isdigit():
            digits += 1
            alnum += 1
        elif char.isalpha():
            alnum += 1
    if digits == 0:
        return DigitRatioClass.none
    if digits == alnum:
        return DigitRatioClass.all
    if 2 * digits >= alnum:
        return DigitRatioClass.high
    return DigitRatioClass.low


def _alignment(
    rect: tuple[int, int, int, int] | None,
    em: int,
    content_x0: int | None,
    content_x1: int | None,
) -> Alignment:
    """Alignment of a block within the page's content extent, in whole-em tolerances.

    Both margins within one em => ``justified`` (the block spans the measure); one flush edge
    => ``left``/``right``; balanced margins => ``center``; anything else — including missing
    geometry or an unknown extent — is honestly ``unknown``. The em tolerance absorbs OCR
    jitter without letting a genuinely indented block claim a flush edge.
    """
    if rect is None or em <= 0 or content_x0 is None or content_x1 is None:
        return Alignment.unknown
    left_margin = rect[0] - content_x0
    right_margin = content_x1 - rect[2]
    if left_margin < 0 or right_margin < 0:
        return Alignment.unknown
    flush_left = left_margin <= em
    flush_right = right_margin <= em
    if flush_left and flush_right:
        return Alignment.justified
    if flush_left:
        return Alignment.left
    if flush_right:
        return Alignment.right
    if abs(left_margin - right_margin) <= em:
        return Alignment.center
    return Alignment.unknown


def _mu_rect(quad: Sequence[float] | None) -> tuple[int, int, int, int] | None:
    if not quad or len(quad) < 8:
        return None
    xs = [mu(v) for v in quad[0:8:2]]
    ys = [mu(v) for v in quad[1:8:2]]
    return (min(xs), min(ys), max(xs), max(ys))


def block_metrics(
    block: TextBlock,
    em: int,
    *,
    case_voided: bool = False,
    content_x0: int | None = None,
    content_x1: int | None = None,
) -> Metrics:
    """One block's metrics — pure function of (block text, bbox, page stats).

    Args:
        block: The text block. Its text is read HERE and only here (§3.2 step 2).
        em: The page em in milli-units (0 when the page has no geometry).
        case_voided: The page's :func:`page_case_profile` — voids ``starts_lowercase``.
        content_x0: Page content extent left, milli-units, for alignment; None when unknown.
        content_x1: Page content extent right, as above.

    Returns:
        A :class:`~dpc.doctree.models.Metrics` — ints, bools and closed enums only.
    """
    text = block.text
    rect = _mu_rect(block.bbox)
    stripped = _strip_closers(text)
    last = stripped[-1] if stripped else ""

    starts_lowercase: bool | None
    if case_voided:
        starts_lowercase = None
    else:
        first_cased = next((c for c in text if c.isupper() or c.islower()), None)
        starts_lowercase = first_cased.islower() if first_cased is not None else None

    return Metrics(
        char_count=len(text),
        line_count=len(block.lines) if block.lines else (1 if text.strip() else 0),
        height_mu=(rect[3] - rect[1]) if rect else 0,
        ends_terminal_punct=last in _TERMINAL,
        starts_lowercase=starts_lowercase,
        ends_hyphen=bool(last) and last in _HYPHENS,
        script_class=_script_class(text),
        digit_ratio_class=_digit_ratio_class(text),
        alignment=_alignment(rect, em, content_x0, content_x1),
    )


__all__ = [
    "ALLCAPS_RATIO",
    "HGT_DISPLAY",
    "HGT_LARGE",
    "HGT_SMALL",
    "block_metrics",
    "height_class",
    "page_case_profile",
]
