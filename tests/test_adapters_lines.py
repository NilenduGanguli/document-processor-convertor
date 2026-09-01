"""The line -> paragraph span join in :func:`dpc.adapters._attach_lines`.

The join is the load-bearing step of spatial rendering and the one the design cannot make
fail loudly: if it breaks, every block falls back to its multi-row hull, the emitter declines
to build canvases, and the output is a perfectly ordinary file with no columns and no error.
So it is pinned here from both sides — the join it makes, and the count it reports.

The payloads are hand-built rather than recorded because the properties under test are about
*offsets*, not about any real document: a fixture would make the offsets incidental and the
shuffle test (identical join from reordered ``paragraphs[]``) impossible to state.

Offsets below index this content string, which every payload in this module shares::

    0         1         2         3         4         5         6
    0123456789012345678901234567890123456789012345678901234567890
    LEFT ONE\nLEFT TWO\nRIGHT ONE\nRIGHT TWO\nCELL A\nCELL B\n
"""
from __future__ import annotations

import random
import time
from typing import Any

import pytest

from dpc import adapters
from dpc.adapters import (
    _attach_lines,
    _block_stream,
    _map_blocks,
    _polygon_to_quad,
    _span_extent,
    _spans,
    _spans_overlap,
    from_azure_layout,
    from_azure_read,
    from_des_ocr,
)
from dpc.models import TextBlock, Zone

#: ``(offset, length)`` of each line of the shared content string, in reading order.
LEFT_ONE = (0, 8)
LEFT_TWO = (9, 8)
RIGHT_ONE = (18, 9)
RIGHT_TWO = (28, 9)
CELL_A = (38, 6)
CELL_B = (45, 6)


def _line(text: str, span: tuple[int, int], x: float, y: float) -> dict[str, Any]:
    """One ``pages[].lines[]`` entry with a 100x10 box at ``(x, y)``."""
    return {
        "content": text,
        "spans": [{"offset": span[0], "length": span[1]}],
        "polygon": [x, y, x + 100, y, x + 100, y + 10, x, y + 10],
    }


def _paragraph(
    text: str, spans: list[tuple[int, int]], y: float, role: str | None = None
) -> dict[str, Any]:
    """One ``paragraphs[]`` entry whose hull spans ``y`` to ``y + 30`` (a wrapped paragraph)."""
    node: dict[str, Any] = {
        "content": text,
        "spans": [{"offset": offset, "length": length} for offset, length in spans],
        "boundingRegions": [
            {"pageNumber": 1, "polygon": [0.0, y, 100.0, y, 100.0, y + 30, 0.0, y + 30]},
        ],
    }
    if role is not None:
        node["role"] = role
    return node


def two_column_payload(paragraphs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A one-page payload: two paragraphs of two lines each, side by side."""
    return {
        "content": "LEFT ONE\nLEFT TWO\nRIGHT ONE\nRIGHT TWO\n",
        "paragraphs": paragraphs
        if paragraphs is not None
        else [
            _paragraph("LEFT ONE LEFT TWO", [LEFT_ONE, LEFT_TWO], 40.0),
            _paragraph("RIGHT ONE RIGHT TWO", [RIGHT_ONE, RIGHT_TWO], 40.0),
        ],
        "pages": [
            {
                "pageNumber": 1,
                "width": 612.0,
                "height": 792.0,
                "unit": "point",
                "lines": [
                    _line("LEFT ONE", LEFT_ONE, 40.0, 100.0),
                    _line("LEFT TWO", LEFT_TWO, 40.0, 112.0),
                    _line("RIGHT ONE", RIGHT_ONE, 320.0, 100.0),
                    _line("RIGHT TWO", RIGHT_TWO, 320.0, 112.0),
                ],
            },
        ],
    }


def _joined(view_blocks: list[TextBlock]) -> list[tuple[str, list[str]]]:
    """``(block text, [line texts])`` — the whole join, in one comparable value."""
    return [(block.text, [line.text for line in block.lines]) for block in view_blocks]


# ---------------------------------------------------------------------------
# The join itself
# ---------------------------------------------------------------------------
def test_lines_join_their_paragraph_by_span() -> None:
    """Each line lands on the paragraph whose spans cover it, in provider order."""
    view = from_azure_layout(two_column_payload())
    assert _joined(view.blocks) == [
        ("LEFT ONE LEFT TWO", ["LEFT ONE", "LEFT TWO"]),
        ("RIGHT ONE RIGHT TWO", ["RIGHT ONE", "RIGHT TWO"]),
    ]


def test_join_is_geometry_free() -> None:
    """A line joins by span even when its box sits inside the *other* paragraph's hull.

    This is the point of doing it by span: the right column's lines are given boxes on the
    left, and the join must not notice. Geometric assignment would get this backwards.
    """
    payload = two_column_payload()
    for line in payload["pages"][0]["lines"]:
        line["polygon"] = [40.0, 100.0, 140.0, 100.0, 140.0, 110.0, 40.0, 110.0]
    view = from_azure_layout(payload)
    assert _joined(view.blocks) == [
        ("LEFT ONE LEFT TWO", ["LEFT ONE", "LEFT TWO"]),
        ("RIGHT ONE RIGHT TWO", ["RIGHT ONE", "RIGHT TWO"]),
    ]


def test_line_bbox_is_the_line_polygon_not_the_hull() -> None:
    """The attached box is the line's own single-row rectangle."""
    view = from_azure_layout(two_column_payload())
    right = view.blocks[1]
    assert [line.bbox for line in right.lines] == [
        [320.0, 100.0, 420.0, 100.0, 420.0, 110.0, 320.0, 110.0],
        [320.0, 112.0, 420.0, 112.0, 420.0, 122.0, 320.0, 122.0],
    ]


def test_a_line_is_claimed_exactly_once() -> None:
    """Overlapping paragraphs do not both get the line: the smaller key wins, alone."""
    payload = two_column_payload(
        paragraphs=[
            # Both paragraphs' spans cover LEFT ONE; the smaller first offset decides.
            _paragraph("LEFT ONE LEFT TWO", [LEFT_ONE, LEFT_TWO], 40.0),
            _paragraph("FT O", [(2, 4)], 80.0),
        ]
    )
    view = from_azure_layout(payload)
    counts = [len(block.lines) for block in view.blocks]
    assert counts == [2, 0]  # LEFT ONE went to the first block only
    # The two right-hand lines are covered by neither paragraph and are dropped, not shared.
    assert view.raw["_line_join"] == [2, 4]


def test_lowest_first_offset_wins_regardless_of_block_index() -> None:
    """The claim key is ``(first_span_offset, block_index)``, not the payload's order."""
    payload = two_column_payload(
        paragraphs=[
            _paragraph("LATER", [(4, 40)], 80.0),  # index 0, first offset 4
            _paragraph("EARLIER", [(0, 44)], 40.0),  # index 1, first offset 0 -> wins
        ]
    )
    view = from_azure_layout(payload)
    assert [len(block.lines) for block in view.blocks] == [0, 4]


def test_shuffled_paragraphs_produce_the_identical_join() -> None:
    """Reordering ``paragraphs[]`` reorders the blocks and nothing else.

    Microsoft does not guarantee ``paragraphs[]`` is sorted by span, so the join is compared
    as a *set* of (block, lines) pairs: same pairs, different order.
    """
    ordered = from_azure_layout(two_column_payload())
    shuffled_payload = two_column_payload(
        paragraphs=[
            _paragraph("RIGHT ONE RIGHT TWO", [RIGHT_ONE, RIGHT_TWO], 40.0),
            _paragraph("LEFT ONE LEFT TWO", [LEFT_ONE, LEFT_TWO], 40.0),
        ]
    )
    shuffled = from_azure_layout(shuffled_payload)
    assert _joined(shuffled.blocks) == list(reversed(_joined(ordered.blocks)))
    assert sorted(_joined(shuffled.blocks)) == sorted(_joined(ordered.blocks))


def test_permuting_a_nodes_spans_array_changes_nothing() -> None:
    """DETERMINISM. The claim key is ``min(offset)``, never ``spans[0].offset``.

    Nothing in Microsoft's docs orders the entries *inside* one node's ``spans[]`` array, so it
    carries no semantics — but a key read off ``spans[0]`` makes it decide the join. Here the
    winning paragraph's spans are ``[LEFT_TWO, LEFT_ONE]``, i.e. its lowest offset is not first,
    and a ``spans[0]``-keyed join hands both lines to the decoy instead. Losing the line moves
    it to another block, hence another canvas, hence another sha256 from the same bytes.
    """
    sorted_spans = two_column_payload(
        paragraphs=[
            _paragraph("LEFT ONE LEFT TWO", [LEFT_ONE, LEFT_TWO], 40.0),
            _paragraph("DECOY", [(1, 20)], 80.0),  # min offset 1 > 0: must lose both left lines
        ]
    )
    permuted = two_column_payload(
        paragraphs=[
            _paragraph("LEFT ONE LEFT TWO", [LEFT_TWO, LEFT_ONE], 40.0),
            _paragraph("DECOY", [(1, 20)], 80.0),
        ]
    )
    expected = [("LEFT ONE LEFT TWO", ["LEFT ONE", "LEFT TWO"]), ("DECOY", ["RIGHT ONE"])]
    assert _joined(from_azure_layout(sorted_spans).blocks) == expected
    assert _joined(from_azure_layout(permuted).blocks) == expected


def test_an_exact_tie_is_broken_by_paragraphs_order_deterministically() -> None:
    """The tie-break the key genuinely has, stated rather than wished away.

    Two blocks with the SAME ``min(offset)`` are separated by ``block_index`` — their position
    in ``paragraphs[]``. That is §3.2's rule, so the join is deterministic for a given payload
    but is not order-*independent* in a tie, and the docstring must not claim otherwise. Pinned
    both ways round so the tiebreaker is the payload's order and not, say, the smaller hull.
    """
    aaa = _paragraph("AAA", [(0, 8)], 40.0)  # min offset 0, covers LEFT ONE only
    bbb = _paragraph("BBB", [(0, 20)], 80.0)  # min offset 0 too — the tie
    forward = dict(_joined(from_azure_layout(two_column_payload(paragraphs=[aaa, bbb])).blocks))
    reverse = dict(_joined(from_azure_layout(two_column_payload(paragraphs=[bbb, aaa])).blocks))
    # LEFT ONE is the contested line: both cover it and both key on offset 0.
    assert forward["AAA"] == ["LEFT ONE"]  # AAA is paragraphs[0], so AAA takes it
    assert "LEFT ONE" not in forward["BBB"]
    assert reverse["BBB"][0] == "LEFT ONE"  # now BBB is paragraphs[0], so BBB takes it
    assert reverse["AAA"] == []
    # Uncontested lines land the same way regardless: only the TIE moved.
    assert forward["BBB"] == ["LEFT TWO", "RIGHT ONE"]


def test_a_second_call_does_not_duplicate_the_lines() -> None:
    """IDEMPOTENCE. ``_attach_lines`` replaces a joined block's lines; it does not append.

    The old version appended on every call while still returning the same counts, so a re-run
    silently doubled the text on the canvas and ``line_join`` said nothing about it.
    """
    payload = two_column_payload()
    blocks = [
        TextBlock(text="LEFT ONE LEFT TWO", page=1),
        TextBlock(text="RIGHT ONE RIGHT TWO", page=1),
    ]
    assert _attach_lines(blocks, payload) == (4, 4)
    first = _joined(blocks)
    assert _attach_lines(blocks, payload) == (4, 4)
    assert _joined(blocks) == first
    assert [len(block.lines) for block in blocks] == [2, 2]


def test_two_mappings_of_one_payload_agree() -> None:
    """Two independent mappings produce the same join — no dict/set-iteration dependence."""
    first = _joined(from_azure_layout(two_column_payload()).blocks)
    second = _joined(from_azure_layout(two_column_payload()).blocks)
    assert first == second


# ---------------------------------------------------------------------------
# What is deliberately NOT joined
# ---------------------------------------------------------------------------
def test_unclaimed_lines_are_dropped_not_promoted() -> None:
    """A line no paragraph covers vanishes: it is table content ``tables[]`` already carries."""
    payload = two_column_payload()
    payload["pages"][0]["lines"].extend(
        [
            _line("CELL A", CELL_A, 40.0, 300.0),
            _line("CELL B", CELL_B, 320.0, 300.0),
        ]
    )
    view = from_azure_layout(payload)
    assert len(view.blocks) == 2  # nothing was promoted to a block
    assert sum(len(block.lines) for block in view.blocks) == 4
    assert "CELL A" not in [line.text for block in view.blocks for line in block.lines]


def test_table_zone_blocks_are_skipped() -> None:
    """A paragraph re-zoned to ``Zone.table`` claims nothing, so its lines are dropped.

    The emitter suppresses table-zone blocks from the body, so a line given to one would be
    placed nowhere while ``tables[]`` emitted the same text again.
    """
    payload = two_column_payload()
    payload["content"] += "CELL A\nCELL B\n"
    payload["paragraphs"].append(_paragraph("CELL A CELL B", [CELL_A, CELL_B], 300.0))
    payload["pages"][0]["lines"].extend(
        [
            _line("CELL A", CELL_A, 40.0, 300.0),
            _line("CELL B", CELL_B, 320.0, 300.0),
        ]
    )
    payload["tables"] = [
        {
            "rowCount": 1,
            "columnCount": 2,
            "boundingRegions": [{"pageNumber": 1, "polygon": [0, 300, 400, 300, 400, 320, 0, 320]}],
            "cells": [
                {
                    "rowIndex": 0,
                    "columnIndex": 0,
                    "content": "CELL A",
                    "spans": [{"offset": CELL_A[0], "length": CELL_A[1]}],
                },
                {
                    "rowIndex": 0,
                    "columnIndex": 1,
                    "content": "CELL B",
                    "spans": [{"offset": CELL_B[0], "length": CELL_B[1]}],
                },
            ],
        }
    ]
    view = from_azure_layout(payload)
    table_blocks = [block for block in view.blocks if block.zone is Zone.table]
    assert len(table_blocks) == 1  # the re-zoning happened, so the skip is the thing under test
    assert table_blocks[0].lines == []
    assert view.raw["_line_join"] == [4, 6]


def test_a_paragraph_with_no_spans_claims_nothing() -> None:
    """No spans is not "matches everything" — an unspanned block stays empty."""
    payload = two_column_payload(
        paragraphs=[
            {
                "content": "LEFT ONE LEFT TWO",
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [0.0, 40.0, 100.0, 40.0, 100.0, 70.0, 0.0, 70.0]}
                ],
            },
        ]
    )
    view = from_azure_layout(payload)
    assert view.blocks[0].lines == []
    assert view.raw["_line_join"] == [0, 4]


def test_blank_lines_are_not_counted_in_total() -> None:
    """``total`` is lines with text: a blank line cannot be placed and must not skew the ratio."""
    payload = two_column_payload()
    payload["pages"][0]["lines"].append(_line("   ", (60, 3), 40.0, 400.0))
    view = from_azure_layout(payload)
    assert view.raw["_line_join"] == [4, 4]


# ---------------------------------------------------------------------------
# The (attached, total) counts
# ---------------------------------------------------------------------------
def test_counts_are_reported_on_the_view() -> None:
    view = from_azure_layout(two_column_payload())
    assert view.raw["_line_join"] == [4, 4]


def test_counts_are_returned_by_the_function() -> None:
    """The helper's own contract, independent of where the view stashes it."""
    payload = two_column_payload()
    blocks = [
        TextBlock(text="LEFT ONE LEFT TWO", page=1),
        TextBlock(text="RIGHT ONE RIGHT TWO", page=1),
    ]
    assert _attach_lines(blocks, payload) == (4, 4)
    assert [len(block.lines) for block in blocks] == [2, 2]


def test_blocks_that_did_not_come_from_this_payload_claim_nothing() -> None:
    """The length guard, tested head-on rather than through the ``content`` path.

    ``zip(sources, blocks)`` pairs a block with the node it was mapped from *by position*. If
    the two lengths disagree the pairing is meaningless — these blocks came from somewhere else
    — so nothing is claimed and the count says ``0/N`` rather than joining by guesswork. The
    counterpart matters as much: ``total`` still counts the lines that were offered, because
    "no block would take them" is exactly the health signal §11.2 asks for.
    """
    payload = two_column_payload()  # two paragraph sources, four lines
    blocks = [TextBlock(text="A BLOCK FROM ANOTHER DOCUMENT", page=1)]
    assert _attach_lines(blocks, payload) == (0, 4)
    assert blocks[0].lines == []


def test_a_claimed_line_with_no_polygon_counts_as_attached() -> None:
    """``line_join`` measures the JOIN, not the geometry — and says so.

    A line the provider gave no ``polygon`` is claimed, counted, and attached with
    ``bbox=None``: §4.2 then declines to make an atom of it and the ``reason`` histogram's
    ``no-geometry`` bucket names it. Folding missing boxes into ``attached`` would fire §11.2's
    kill criterion — "the span join is not the right mechanism, switch to geometric assignment"
    — on a document whose join was perfect, which is the one conclusion that evidence cannot
    support. Geometric assignment needs the boxes this document does not have.
    """
    payload = two_column_payload()
    payload["pages"][0]["lines"][0].pop("polygon")
    view = from_azure_layout(payload)
    assert view.raw["_line_join"] == [4, 4]
    first_line = view.blocks[0].lines[0]
    assert (first_line.text, first_line.bbox) == ("LEFT ONE", None)


def test_shifted_spans_report_a_broken_join_rather_than_failing() -> None:
    """§11.2's silent failure, made legible: every line dropped, and the count says so."""
    payload = two_column_payload()
    for paragraph in payload["paragraphs"]:
        for span in paragraph["spans"]:
            span["offset"] += 10_000
    view = from_azure_layout(payload)
    assert view.raw["_line_join"] == [0, 4]
    assert all(block.lines == [] for block in view.blocks)
    assert [block.text for block in view.blocks] == [
        "LEFT ONE LEFT TWO",
        "RIGHT ONE RIGHT TWO",
    ]


def test_payload_with_no_lines_reports_zero_of_zero() -> None:
    """No line stream is not a failed join; it is no join, and the count is honest about it."""
    payload = two_column_payload()
    payload["pages"][0].pop("lines")
    view = from_azure_layout(payload)
    assert view.raw["_line_join"] == [0, 0]
    assert all(block.lines == [] for block in view.blocks)


def test_content_only_payload_joins_nothing() -> None:
    """Blocks split out of the flat ``content`` string have no provider node and no spans."""
    view = from_azure_layout({"content": "ALPHA\nBRAVO\n"})
    assert [block.text for block in view.blocks] == ["ALPHA", "BRAVO"]
    assert view.raw["_line_join"] == [0, 0]
    assert all(block.lines == [] for block in view.blocks)


def test_line_fallback_blocks_get_their_own_line() -> None:
    """With no ``paragraphs[]`` the blocks ARE the lines, and each keeps its own rectangle."""
    payload = two_column_payload()
    payload.pop("paragraphs")
    view = from_azure_layout(payload)
    assert _joined(view.blocks) == [
        ("LEFT ONE", ["LEFT ONE"]),
        ("LEFT TWO", ["LEFT TWO"]),
        ("RIGHT ONE", ["RIGHT ONE"]),
        ("RIGHT TWO", ["RIGHT TWO"]),
    ]
    assert view.raw["_line_join"] == [4, 4]


def test_line_fallback_blocks_keep_their_geometry_when_the_lines_have_no_spans() -> None:
    """A ``prebuilt-read``-shaped payload whose lines carry no ``spans`` loses NOTHING.

    ``from_azure_layout`` promises to tolerate lines-without-paragraphs, and Azure omits
    ``spans`` on a ``prebuilt-read`` result. There is no join to do here — each block IS one
    provider line and already owns that line's single-row quad, exactly the case
    ``from_azure_read`` handles — so the old span-only path threw the geometry away (no quad
    reaches ``lines``, so the page can never produce a canvas) and then reported ``0/N``, a
    false alarm on the one field §11.2 makes a kill criterion out of.
    """
    payload = two_column_payload()
    payload.pop("paragraphs")
    for line in payload["pages"][0]["lines"]:
        line.pop("spans")
    view = from_azure_layout(payload)
    assert _joined(view.blocks) == [
        ("LEFT ONE", ["LEFT ONE"]),
        ("LEFT TWO", ["LEFT TWO"]),
        ("RIGHT ONE", ["RIGHT ONE"]),
        ("RIGHT TWO", ["RIGHT TWO"]),
    ]
    assert [line.bbox for block in view.blocks for line in block.lines] == [
        block.bbox for block in view.blocks
    ]
    assert view.raw["_line_join"] == [4, 4]  # NOT 0/4: nothing failed, so nothing is reported


def test_line_fallback_still_withholds_lines_from_table_zone_blocks() -> None:
    """The lines fallback keeps the span join's one exclusion: a table-zone block stays empty.

    The emitter suppresses table-zone blocks from the body, so a line given to one is placed
    nowhere while ``tables[]`` emits the same text again. Pinned with spans absent, so the
    exclusion is proven on the path that no longer consults spans at all.
    """
    payload = two_column_payload()
    payload.pop("paragraphs")
    for line in payload["pages"][0]["lines"]:
        line.pop("spans")
    payload["tables"] = [
        {
            "rowCount": 1,
            "columnCount": 1,
            "boundingRegions": [{"pageNumber": 1, "polygon": [0, 90, 500, 90, 500, 111, 0, 111]}],
            "cells": [
                {
                    "rowIndex": 0,
                    "columnIndex": 0,
                    "content": "LEFT ONE",
                    "boundingRegions": [
                        {"pageNumber": 1, "polygon": [0, 90, 500, 90, 500, 111, 0, 111]}
                    ],
                }
            ],
        }
    ]
    view = from_azure_layout(payload)
    table_blocks = [block for block in view.blocks if block.zone is Zone.table]
    assert table_blocks, "the re-zoning must happen, or this proves nothing"
    assert all(block.lines == [] for block in table_blocks)
    assert view.raw["_line_join"] == [len(view.blocks) - len(table_blocks), 4]


def test_multi_page_lines_join_across_pages() -> None:
    """Spans are document-global, so a second page's lines find their paragraphs the same way."""
    payload = two_column_payload()
    payload["content"] += "CELL A\nCELL B\n"
    payload["paragraphs"].append(
        {
            "content": "CELL A CELL B",
            "spans": [
                {"offset": CELL_A[0], "length": CELL_A[1]},
                {"offset": CELL_B[0], "length": CELL_B[1]},
            ],
            "boundingRegions": [
                {"pageNumber": 2, "polygon": [0.0, 40.0, 100.0, 40.0, 100.0, 70.0, 0.0, 70.0]}
            ],
        }
    )
    payload["pages"].append(
        {
            "pageNumber": 2,
            "width": 612.0,
            "height": 792.0,
            "unit": "point",
            "lines": [_line("CELL A", CELL_A, 40.0, 100.0), _line("CELL B", CELL_B, 40.0, 112.0)],
        }
    )
    view = from_azure_layout(payload)
    assert _joined(view.blocks)[2] == ("CELL A CELL B", ["CELL A", "CELL B"])
    assert view.raw["_line_join"] == [6, 6]


# ---------------------------------------------------------------------------
# Read v3.2 — one line per block, for free
# ---------------------------------------------------------------------------
def read_payload() -> dict[str, Any]:
    return {
        "status": "succeeded",
        "analyzeResult": {
            "version": "3.2.0",
            "readResults": [
                {
                    "page": 1,
                    "width": 1700,
                    "height": 2200,
                    "unit": "pixel",
                    "lines": [
                        {"text": "LEFT ONE", "boundingBox": [40, 100, 140, 100, 140, 110, 40, 110]},
                        {"text": "LEFT TWO", "boundingBox": [40, 112, 140, 112, 140, 122, 40, 122]},
                    ],
                }
            ],
        },
    }


def test_read_blocks_each_get_one_line() -> None:
    """In Read v3.2 a block IS a line, so the block's own rectangle is a real single-row box."""
    view = from_azure_read(read_payload())
    assert _joined(view.blocks) == [
        ("LEFT ONE", ["LEFT ONE"]),
        ("LEFT TWO", ["LEFT TWO"]),
    ]
    assert [line.bbox for block in view.blocks for line in block.lines] == [
        [40.0, 100.0, 140.0, 100.0, 140.0, 110.0, 40.0, 110.0],
        [40.0, 112.0, 140.0, 112.0, 140.0, 122.0, 40.0, 122.0],
    ]
    assert [block.bbox for block in view.blocks] == [
        line.bbox for block in view.blocks for line in block.lines
    ]


def test_read_block_without_geometry_has_no_line() -> None:
    """No box, no line: an invented rectangle is worse than an absent one."""
    payload = read_payload()
    payload["analyzeResult"]["readResults"][0]["lines"][1].pop("boundingBox")
    view = from_azure_read(payload)
    assert view.blocks[1].bbox is None
    assert view.blocks[1].lines == []
    assert len(view.blocks[0].lines) == 1


def test_read_view_is_otherwise_unchanged() -> None:
    """The Read mapper's own contract is untouched by the addition."""
    view = from_azure_read(read_payload())
    assert view.raw["provider"] == "azure-read-v3.2"
    assert all(block.zone is Zone.body for block in view.blocks)
    assert [page.page for page in view.pages] == [1]


# ---------------------------------------------------------------------------
# The field reaches the front matter on every path that runs the join
# ---------------------------------------------------------------------------
def des_payload(pages: int = 2) -> dict[str, Any]:
    """A DES OCR envelope carrying Azure's verbatim per-page payload, one paragraph per page."""
    entries = []
    for index in range(pages):
        offset = index * 20
        entries.append(
            {
                "page": {"page_number": index + 1},
                "raw": {
                    "pageNumber": index + 1,
                    "lines": [
                        {
                            "content": "LEFT ONE",
                            "spans": [{"offset": offset, "length": 8}],
                            "polygon": [40.0, 100.0, 140.0, 100.0, 140.0, 110.0, 40.0, 110.0],
                        },
                        {
                            "content": "LEFT TWO",
                            "spans": [{"offset": offset + 9, "length": 8}],
                            "polygon": [40.0, 112.0, 140.0, 112.0, 140.0, 122.0, 40.0, 122.0],
                        },
                    ],
                    "paragraphs": [
                        {
                            "content": "LEFT ONE LEFT TWO",
                            "spans": [
                                {"offset": offset, "length": 8},
                                {"offset": offset + 9, "length": 8},
                            ],
                            "boundingRegions": [
                                {
                                    "pageNumber": index + 1,
                                    "polygon": [0.0, 40.0, 100.0, 40.0, 100.0, 70.0, 0.0, 70.0],
                                }
                            ],
                        }
                    ],
                },
            }
        )
    return {"pages": entries}


def test_des_carries_the_line_join_it_actually_ran() -> None:
    """DES runs the FULL span join and then used to throw the diagnostic away.

    ``from_des_ocr`` maps each Azure-shaped page through ``from_azure_layout`` — join included —
    and then replaced ``raw`` wholesale with its own provenance, so the one path that runs the
    join in production shipped with §11.2's failure mode completely dark. The counts survive the
    replacement now, and are summed across pages so the field is a document-level number.
    """
    view = from_des_ocr(des_payload(pages=2))
    assert view.raw["provider"] == "des-ocr"
    assert view.raw["_line_join"] == [4, 4]
    assert [[line.text for line in block.lines] for block in view.blocks] == [
        ["LEFT ONE", "LEFT TWO"],
        ["LEFT ONE", "LEFT TWO"],
    ]


def test_des_reports_a_broken_join_instead_of_hiding_it() -> None:
    """§11.2 on the DES path: shifted paragraph spans, every line dropped, and the field says so."""
    payload = des_payload(pages=2)
    for entry in payload["pages"]:
        for paragraph in entry["raw"]["paragraphs"]:
            for span in paragraph["spans"]:
                span["offset"] += 10_000
    view = from_des_ocr(payload)
    assert view.raw["_line_join"] == [0, 4]
    assert all(block.lines == [] for block in view.blocks)


def test_a_des_page_that_runs_no_join_omits_the_field() -> None:
    """A normalized DES row has no line stream, so there is no join and no ``0/0`` to misread.

    The emitter must tolerate the key being ABSENT — an honest silence beats a ``0/0`` that a
    corpus sweep would have to special-case out of the ratio.
    """
    view = from_des_ocr(
        {"page_number": 1, "paragraphs": [{"content": "ALPHA", "bbox": [0, 0, 10, 10]}]}
    )
    assert "_line_join" not in view.raw
    assert [block.text for block in view.blocks] == ["ALPHA"]


def test_read_reports_a_line_join_of_its_own() -> None:
    """Read runs no span join, but the corpus sweep still needs a number from it.

    A Read page that carried no field at all would be invisible in exactly the sweep §11.2
    exists to support. Here it means what it can mean: lines that ended up placeable, over
    lines offered. The second line has no ``boundingBox``, so it is offered and not placeable.
    """
    payload = read_payload()
    payload["analyzeResult"]["readResults"][0]["lines"][1].pop("boundingBox")
    assert from_azure_read(payload).raw["_line_join"] == [1, 2]
    assert from_azure_read(read_payload()).raw["_line_join"] == [2, 2]


# ---------------------------------------------------------------------------
# Scale: the join must stay linear, and stay the same join
# ---------------------------------------------------------------------------
def _brute_force_join(payload: dict[str, Any]) -> tuple[list[tuple[int, str]], tuple[int, int]]:
    """The join stated as its definition: no bisect, no prefix maxima, no cleverness.

    Every candidate is tested against every line and the minimum ``(min_span_offset,
    block_index)`` wins. This is the specification the fast scan has to reproduce; keeping it
    here is what makes the optimisation checkable rather than merely believed.
    """
    blocks = _map_blocks(payload, {}, {})
    _, sources = _block_stream(payload)
    candidates: list[tuple[int, int, list[tuple[int, int]]]] = []
    if len(sources) == len(blocks):
        for index, (node, block) in enumerate(zip(sources, blocks, strict=True)):
            spans = _spans(node)
            if block.zone is Zone.table or not spans:
                continue
            candidates.append((_span_extent(spans)[0], index, spans))
    claims: list[tuple[int, str]] = []
    attached = 0
    total = 0
    for page in payload["pages"]:
        for line in page["lines"]:
            text = str(line.get("content") or "").strip()
            if not text:
                continue
            total += 1
            line_spans = _spans(line)
            if not line_spans:
                continue
            best: tuple[int, int] | None = None
            for low, index, spans in candidates:
                if _spans_overlap(line_spans, spans) and (best is None or (low, index) < best):
                    best = (low, index)
            if best is None:
                continue
            claims.append((best[1], text))
            attached += 1
    return claims, (attached, total)


def ragged_payload(seed: int, lines: int = 90) -> dict[str, Any]:
    """A deterministic corpus built to break a lazy join: ragged, overlapping, shuffled.

    Zero-length spans, gaps, paragraphs whose spans are reversed or shuffled, paragraphs that
    overlap each other, and a ``paragraphs[]`` array in no particular order — every property
    the fast scan's two cuts (bisect on the tail, prefix maxima on the head) could get wrong.
    """
    rng = random.Random(seed)
    offset = 0
    line_nodes: list[dict[str, Any]] = []
    for index in range(lines):
        length = rng.choice([0, 1, 4, 9, 25])
        line_nodes.append(
            {
                "content": f"L{index}",
                "spans": [{"offset": offset, "length": length}],
                "polygon": [0.0, float(index), 10.0, float(index), 10.0, index + 1.0, 0.0, index + 1.0],
            }
        )
        offset += length + rng.randint(0, 4)
    paragraphs: list[dict[str, Any]] = []
    for _ in range(rng.randint(1, lines // 2)):
        picks = [rng.randrange(lines) for _ in range(rng.randint(1, 5))]
        spans = [
            {
                "offset": max(0, line_nodes[p]["spans"][0]["offset"] + rng.randint(-3, 3)),
                "length": rng.choice([0, 1, 6, 40]),
            }
            for p in picks
        ]
        rng.shuffle(spans)  # spans[] in no offset order — finding 2's shape, at scale
        paragraphs.append(
            {
                "content": "P",
                "spans": spans,
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
            }
        )
    rng.shuffle(paragraphs)
    pages = rng.randint(1, 4)
    per = max(1, lines // pages)
    page_nodes = [
        {"pageNumber": p + 1, "lines": line_nodes[p * per : (p + 1) * per]} for p in range(pages)
    ]
    page_nodes[-1]["lines"] = line_nodes[(pages - 1) * per :]
    return {"content": "x" * (offset + 64), "paragraphs": paragraphs, "pages": page_nodes}


@pytest.mark.parametrize("seed", range(30))
def test_the_fast_scan_makes_the_brute_force_join(seed: int) -> None:
    """The optimisation is an optimisation: same claims, same boxes, same counts.

    Thirty ragged/overlapping/shuffled corpora, each compared claim-for-claim against the
    definition above. A performance fix to a determinism-critical function is worth nothing if
    it quietly changes which block a line lands on.
    """
    payload = ragged_payload(seed)
    expected_claims, expected_counts = _brute_force_join(payload)
    view = from_azure_layout(payload)
    actual = [
        (index, line.text)
        for index, block in enumerate(view.blocks)
        for line in block.lines
    ]
    assert sorted(actual) == sorted(expected_claims)
    assert view.raw["_line_join"] == [expected_counts[0], expected_counts[1]]
    # The boxes travel with the claims: a line keeps its own single-row rectangle.
    by_text = {
        line["content"]: _polygon_to_quad(line["polygon"])
        for page in payload["pages"]
        for line in page["lines"]
    }
    assert all(
        line.bbox == by_text[line.text] for block in view.blocks for line in block.lines
    )


def dense_payload(pages: int, per_page: int = 100, per_paragraph: int = 4) -> dict[str, Any]:
    """A realistic multi-page document: every line claimed, one paragraph per four lines."""
    offset = 0
    line_nodes: list[dict[str, Any]] = []
    page_nodes: list[dict[str, Any]] = []
    for page in range(pages):
        rows = []
        for index in range(per_page):
            text = f"line {page}-{index} with a few words on it"
            rows.append(
                {
                    "content": text,
                    "spans": [{"offset": offset, "length": len(text)}],
                    "polygon": [0.0, float(index), 100.0, float(index), 100.0, index + 9.0, 0.0, index + 9.0],
                }
            )
            offset += len(text) + 1
        page_nodes.append({"pageNumber": page + 1, "lines": rows})
        line_nodes.extend(rows)
    paragraphs = [
        {
            "content": " ".join(row["content"] for row in line_nodes[start : start + per_paragraph]),
            "spans": [row["spans"][0] for row in line_nodes[start : start + per_paragraph]],
            "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 100, 0, 100, 30, 0, 30]}],
        }
        for start in range(0, len(line_nodes), per_paragraph)
    ]
    return {"content": "x" * offset, "paragraphs": paragraphs, "pages": page_nodes}


def test_the_join_does_not_test_every_block_against_every_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost guard, counted rather than timed — so it cannot flake on a busy machine.

    The old scan cut only the TAIL, so a line at offset X still tested every candidate starting
    before it: O(lines x blocks), which on this 40-page payload is ~2 million overlap tests and
    on a 400-page one was 49 s of blocking CPU inside a synchronous mapper, on a provider that
    accepts 2000 pages. The prefix-maximum cut makes the head bounded too. Ten tests per line is
    a deliberately loose ceiling; the real figure here is close to one.
    """
    payload = dense_payload(pages=40)
    calls = 0
    real = adapters._spans_overlap

    def counting(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
        nonlocal calls
        calls += 1
        return real(a, b)

    monkeypatch.setattr(adapters, "_spans_overlap", counting)
    view = from_azure_layout(payload)
    assert view.raw["_line_join"] == [4000, 4000]
    assert calls <= 10 * 4000, f"{calls} overlap tests for 4000 lines is not a linear scan"


def test_a_four_hundred_page_document_joins_in_seconds_not_minutes() -> None:
    """The coarse wall-clock guard on the number the finding was actually about.

    Measured 48.3 s before the fix and 0.14 s after, on the same machine. The ceiling is set two
    orders of magnitude above the fixed cost and one below the broken one, so it catches a
    return to quadratic without failing on a slow CI box.
    """
    payload = dense_payload(pages=400)
    started = time.perf_counter()
    view = from_azure_layout(payload)
    elapsed = time.perf_counter() - started
    assert view.raw["_line_join"] == [40000, 40000]
    assert elapsed < 10.0, f"400-page join took {elapsed:.1f}s"
