"""The doctree schema — `dpc.doctree/1` — and its one canonical serializer.

This module is the type-system half of invariant I5 (SPEC-DOCTREE-1 §2.2): **no field of the
stored tree can carry a document string**. Nodes reference content by *index* into the stored
``LayoutView``; every ``str`` field is either a closed enum or pattern-constrained to a
content-free grammar (``path``, ``figure_id``, ``prov.provider_ref``, a provider role token,
hex digests, the pass-manifest grammar). Adding an unconstrained ``str`` field fails the
schema-introspection test (§8.6), not code review — which is the point.

``dump_tree`` is the ONLY serializer allowed to feed ``sha256_tree``: ``model_dump`` with
``exclude_none``, then ``json.dumps(sort_keys=True, ensure_ascii=True, separators=(",",":"))``.
Two writers producing different bytes for one tree would make the content-address lie, so the
canonical form lives beside the models it serializes and nowhere else.

``validate_tree`` enforces invariants I1–I5 and NEVER raises: the builder's contract is that a
violation costs the structured tree (whole-doc flat fallback), never the conversion.
"""
from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # imported for validate_tree's signature only; no runtime cycle
    from dpc.models import LayoutView

#: Stored schema identifier; bumping it is a new artifact generation, never an edit.
SCHEMA = "dpc.doctree/1"

#: Builder semver (§2.4): bumps on ANY heuristic change. Stored artifacts never retro-update.
BUILDER_VERSION = "dpc-doctree/1.0.0"


class NodeKind(enum.StrEnum):
    """§2.1 verbatim — the single node taxonomy (R14). No ``canvas`` kind on purpose."""

    document = "document"    # single root; children = [body, furniture]
    body = "body"            # main-content root; pre-order = reading order
    furniture = "furniture"  # pageHeader/pageFooter/pageNumber; excluded from flow
    section = "section"      # heading + its content; nesting = heading levels
    flow_group = "flow_group"  # truly-parallel container; children are frames, read in order
    frame = "frame"          # one column/panel of a flow_group (maps to canvas.Frame)
    heading = "heading"      # leaf; level 1..4; refs one TextBlock
    paragraph = "paragraph"  # leaf; refs one TextBlock (also unknown-role landing zone)
    footnote = "footnote"    # leaf; Azure footnote role; deferred to its section's tail
    table = "table"          # leaf; refs one Table (cells stay in LayoutView)
    figure = "figure"        # image placeholder; bbox + optional caption child
    caption = "caption"      # child of figure/table; refs one TextBlock
    kv_group = "kv_group"    # spatial cluster of key-value pairs (KYC form panel)
    kv_pair = "kv_pair"      # leaf; refs one KeyValue by index
    list_group = "list_group"  # v1: detection from provider role/indent only
    list_item = "list_item"
    mark = "mark"            # selection mark leaf; refs Mark by index


class ScriptClass(enum.StrEnum):
    latin = "latin"
    cyrillic = "cyrillic"
    cjk = "cjk"
    arabic = "arabic"
    deva = "deva"
    mixed = "mixed"
    none = "none"


class DigitRatioClass(enum.StrEnum):
    none = "none"
    low = "low"
    high = "high"
    all = "all"


class Alignment(enum.StrEnum):
    left = "left"
    right = "right"
    center = "center"
    justified = "justified"
    unknown = "unknown"


class ProvSource(enum.StrEnum):
    azure_section = "azure_section"
    geometry = "geometry"
    seq_fallback = "seq_fallback"


class Evidence(enum.StrEnum):
    """The continuation rubric's evidence names (§3.3) — the closed vocabulary of ``flow``."""

    ends_hyphen = "ends_hyphen"
    no_terminal = "no_terminal"
    starts_lower = "starts_lower"
    height_match = "height_match"
    width_match = "width_match"
    adjacency = "adjacency"


#: Path grammar (I5): Adobe-Extract style, 1-based per-kind instance counters, uniform for
#: every kind — content-free by construction. ``//doc/sect[1]/p[2]`` names a position, never
#: a heading's text.
PATH_TOKENS: dict[NodeKind, str] = {
    NodeKind.document: "doc",
    NodeKind.body: "body",
    NodeKind.furniture: "furn",
    NodeKind.section: "sect",
    NodeKind.flow_group: "fg",
    NodeKind.frame: "frame",
    NodeKind.heading: "h",
    NodeKind.paragraph: "p",
    NodeKind.footnote: "fn",
    NodeKind.table: "table",
    NodeKind.figure: "fig",
    NodeKind.caption: "cap",
    NodeKind.kv_group: "kvg",
    NodeKind.kv_pair: "kv",
    NodeKind.list_group: "lg",
    NodeKind.list_item: "li",
    NodeKind.mark: "mark",
}

_PATH_RE = (
    r"^//doc(/(body|furn|sect|fg|frame|h|p|fn|table|fig|cap|kvg|kv|lg|li|mark)"
    r"\[[1-9][0-9]*\])*$"
)
_FIGURE_ID_RE = r"^fig-[1-9][0-9]*-[1-9][0-9]*$"
_PROVIDER_REF_RE = r"^/(sections|paragraphs|tables|figures)/[0-9]+$"
#: A provider role is a single ASCII token (``sectionHeading``, ``formulaBlock``). One token,
#: no whitespace: a value that cannot hold a sentence cannot hold document text.
_PROVIDER_ROLE_RE = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"
_SHA256_RE = r"^[0-9a-f]{64}$"
_DOC_ID_RE = r"^[A-Za-z0-9._:-]{0,128}$"
_BUILDER_RE = r"^dpc-doctree/[0-9]+\.[0-9]+\.[0-9]+$"
#: Pass-manifest grammar: ``status`` or ``status(details)`` where details are counts, names
#: and page lists — ``used(7)``, ``conflict_demoted(pages=[2])``, ``invariant_failed(I3)``.
_PASS_RE = r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"


class Metrics(BaseModel):
    """Per-leaf content metrics — ints/bools/closed enums, derived once in ``metrics.py``.

    Exact values stay in this artifact (the trust domain); the LLM projection (§4.2) buckets
    them before anything leaves the process.
    """

    char_count: int = 0
    line_count: int = 0
    height_mu: int = 0
    ends_terminal_punct: bool = False
    #: ``None`` when the page case-profile voids it (all-caps page, non-bicameral script) —
    #: the passport/MRZ/CJK gate, applied in ``metrics.py``, never in any prompt.
    starts_lowercase: bool | None = None
    ends_hyphen: bool = False
    script_class: ScriptClass = ScriptClass.none
    digit_ratio_class: DigitRatioClass = DigitRatioClass.none
    alignment: Alignment = Alignment.unknown


class Prov(BaseModel):
    """Where a node came from and where the canvas machinery saw it."""

    source: ProvSource = ProvSource.geometry
    #: Verbatim JSON-pointer into the provider payload, when provider-seeded.
    provider_ref: str | None = Field(default=None, pattern=_PROVIDER_REF_RE)
    #: Verbatim Azure role for unknown-role paragraphs (R14) — a single token, never text.
    provider_role: str | None = Field(default=None, pattern=_PROVIDER_ROLE_RE)
    band_ix: int | None = None
    frame_ix: int | None = None
    region_ix: int | None = None


class Node(BaseModel):
    """One tree node — §2.2. Content by index only (I5)."""

    id: int
    kind: NodeKind
    path: str = Field(pattern=_PATH_RE)
    parent: int | None = None
    children: list[int] = Field(default_factory=list)
    #: First page touched.
    page: int = 1
    #: ``[x0, y0, x1, y1]`` mu ints, union rect via ``geom.rect_scale``; None when absent.
    bbox: tuple[int, int, int, int] | None = None
    #: heading/section only, 1..4.
    level: int | None = None
    #: Indices into ``LayoutView.blocks``. Text leaves: exactly one. A table node may claim
    #: the ``Zone.table`` blocks its table re-zoned, so claim exactness (I3) stays total over
    #: EVERY block index without manufacturing paragraph nodes for text the table already
    #: carries as cells.
    block_ixs: list[int] = Field(default_factory=list)
    table_ix: int | None = None
    kv_ix: int | None = None
    mark_ix: int | None = None
    #: ``fig-{page}-{n}`` (R20); Azure's undocumented id lives in ``prov.provider_ref`` only.
    figure_id: str | None = Field(default=None, pattern=_FIGURE_ID_RE)
    metrics: Metrics | None = None
    prov: Prov = Field(default_factory=Prov)


class FlowEdge(BaseModel):
    """A continuation annotation (R11): annotates, NEVER reorders."""

    src: int
    dst: int
    kind: Literal["continues"] = "continues"
    score: int = 0
    evidence: list[Evidence] = Field(default_factory=list)


class Report(BaseModel):
    """LLM-pass trigger inputs (§2.2) — the builder's own uncertainty, stated."""

    #: ``(node_a, node_b, margin_mu)`` coin-toss orderings.
    order_ties: list[tuple[int, int, int]] = Field(default_factory=list)
    coverage_fallback_pages: list[int] = Field(default_factory=list)
    declined_pages: list[int] = Field(default_factory=list)


class Passes(BaseModel):
    """Honesty manifest — construction passes ONLY (R5); LLM status never lives here."""

    provider_sections: str = Field(default="absent", pattern=_PASS_RE)
    provider_figures: str = Field(default="absent", pattern=_PASS_RE)
    geometry: str = Field(default="absent", pattern=_PASS_RE)
    interposer: str = Field(default="ran(footnotes=0)", pattern=_PASS_RE)
    heading_nesting: str = Field(default="ran(levels=0)", pattern=_PASS_RE)
    continuity: str = Field(default="ran(edges=0, candidates=0)", pattern=_PASS_RE)


class Counters(BaseModel):
    """Claim accounting (I3): every view element in exactly one node, stated not implied."""

    blocks_total: int = 0
    blocks_claimed: int = 0
    tables_claimed: int = 0
    kvs_claimed: int = 0
    marks_claimed: int = 0
    nodes: int = 0
    edges: int = 0


class PageDims(BaseModel):
    """Page dimensions as mu ints (R16) — no float anywhere in ``doctree.json``."""

    page: int
    width_mu: int = 0
    height_mu: int = 0


class DocTree(BaseModel):
    """The stored tree — schema ``dpc.doctree/1``."""

    schema_: str = Field(default=SCHEMA, alias="schema", pattern=r"^dpc\.doctree/[0-9]+$")
    doc_id: str = Field(default="", pattern=_DOC_ID_RE)
    view_sha256: str = Field(default="0" * 64, pattern=_SHA256_RE)
    builder: str = Field(default=BUILDER_VERSION, pattern=_BUILDER_RE)
    pages: list[PageDims] = Field(default_factory=list)
    body: int = 1
    furniture: int = 2
    nodes: list[Node] = Field(default_factory=list)
    flow: list[FlowEdge] = Field(default_factory=list)
    report: Report = Field(default_factory=Report)
    passes: Passes = Field(default_factory=Passes)
    counters: Counters = Field(default_factory=Counters)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------------------
def dump_tree(tree: DocTree) -> bytes:
    """THE canonical serializer — the only bytes ``sha256_tree`` may ever be computed from.

    ``exclude_none`` drops null fields (absent beats explicit-null for byte stability across
    model versions that add optional fields); ``sort_keys`` + ``ensure_ascii`` + compact
    separators make the bytes independent of insertion order, locale and writer. No wall
    clock, no floats — everything in the model is int/bool/enum/pattern-str by construction.

    Args:
        tree: The tree to serialize.

    Returns:
        Canonical UTF-8 (pure ASCII) JSON bytes.
    """
    payload = tree.model_dump(mode="json", by_alias=True, exclude_none=True)
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()


def tree_sha256(tree: DocTree) -> str:
    """Hex sha256 of :func:`dump_tree` — the tree's content address."""
    return hashlib.sha256(dump_tree(tree)).hexdigest()


def view_sha256(view: LayoutView) -> str:
    """Hex sha256 of the view's canonical JSON — what ``DocTree.view_sha256`` pins.

    Canonicalized exactly like :func:`dump_tree` (sorted keys, ASCII, compact separators) so
    the pin is a property of the view's content, not of whichever writer stored it.
    """
    payload = view.model_dump(mode="json")
    data = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


# ---------------------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------------------
def walk_body(tree: DocTree) -> Iterator[Node]:
    """Pre-order iterator over the ``body`` subtree — the reading order, by definition.

    Explicit stack, no recursion: a 200-block dense page must not be able to hit the
    interpreter's recursion limit (§8.8). Malformed ids are skipped rather than raised on —
    traversal is used by validators, and a validator that raises on the malformed input it
    exists to detect would be useless.
    """
    if not (0 <= tree.body < len(tree.nodes)):
        return
    stack = [tree.body]
    seen: set[int] = set()
    while stack:
        node_id = stack.pop()
        if node_id in seen or not (0 <= node_id < len(tree.nodes)):
            continue
        seen.add(node_id)
        node = tree.nodes[node_id]
        yield node
        stack.extend(reversed(node.children))


def _preorder_ids(tree: DocTree, root: int) -> list[int]:
    """Pre-order id sequence from ``root`` with cycle/range protection (validator helper)."""
    out: list[int] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        node_id = stack.pop()
        if node_id in seen or not (0 <= node_id < len(tree.nodes)):
            continue
        seen.add(node_id)
        out.append(node_id)
        stack.extend(reversed(tree.nodes[node_id].children))
    return out


# ---------------------------------------------------------------------------------------
# Validation — I1..I5, typed result, never raises
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TreeValidation:
    """The typed result of :func:`validate_tree`. ``violations`` name the invariant first."""

    ok: bool
    violations: tuple[str, ...]


def validate_tree(tree: DocTree, view: LayoutView) -> TreeValidation:
    """Check invariants I1–I5 against the view the tree claims to index.

    Never raises: an internal error is itself reported as a violation (``error:<Name>``),
    because the caller's contract (§2.3) is "violation => whole-doc flat fallback", and an
    exception here would turn a data defect into an ingestion outage.

    Args:
        tree: The tree to validate.
        view: The ``LayoutView`` the tree indexes into (I3 is meaningless without it).

    Returns:
        A :class:`TreeValidation`; ``ok`` iff no violation was found.
    """
    try:
        violations = _validate(tree, view)
    except Exception as exc:  # noqa: BLE001 - the never-raises contract; name only, no text.
        violations = [f"error:{type(exc).__name__}"]
    return TreeValidation(ok=not violations, violations=tuple(violations))


def _validate(tree: DocTree, view: LayoutView) -> list[str]:
    out: list[str] = []
    nodes = tree.nodes

    # I1 — ids are final-pre-order ordinals and equal their index.
    if any(node.id != i for i, node in enumerate(nodes)):
        out.append("I1:id_index")
    if nodes and _preorder_ids(tree, 0) != list(range(len(nodes))):
        out.append("I1:preorder")

    # I2 — one document root; body/furniture its only children; mutual parent/children refs.
    doc_ids = [n.id for n in nodes if n.kind is NodeKind.document]
    if doc_ids != [0]:
        out.append("I2:document_root")
    else:
        doc = nodes[0]
        if doc.children != [tree.body, tree.furniture]:
            out.append("I2:root_children")
        if not (0 <= tree.body < len(nodes)) or nodes[tree.body].kind is not NodeKind.body:
            out.append("I2:body_ref")
        if (
            not (0 <= tree.furniture < len(nodes))
            or nodes[tree.furniture].kind is not NodeKind.furniture
        ):
            out.append("I2:furniture_ref")
    for node in nodes:
        for child_id in node.children:
            if not (0 <= child_id < len(nodes)) or nodes[child_id].parent != node.id:
                out.append("I2:child_backref")
                break
        if node.id != 0:
            parent = node.parent
            if (
                parent is None
                or not (0 <= parent < len(nodes))
                or node.id not in nodes[parent].children
            ):
                out.append("I2:parent_ref")
                break

    # I3 — claim exactness: every blocks/tables/kvs/marks index in exactly one node.
    claims: dict[str, list[int]] = {"blocks": [], "tables": [], "kvs": [], "marks": []}
    for node in nodes:
        claims["blocks"].extend(node.block_ixs)
        if node.table_ix is not None:
            claims["tables"].append(node.table_ix)
        if node.kv_ix is not None:
            claims["kvs"].append(node.kv_ix)
        if node.mark_ix is not None:
            claims["marks"].append(node.mark_ix)
    universe = {
        "blocks": len(view.blocks),
        "tables": len(view.tables),
        "kvs": len(view.key_values),
        "marks": len(view.marks),
    }
    for name, got in claims.items():
        if sorted(got) != list(range(universe[name])):
            out.append(f"I3:{name}")

    # I4 — flow edges: src < dst, both paragraphs, no double-dst, no self/duplicate edges.
    seen_pairs: set[tuple[int, int]] = set()
    seen_dst: set[int] = set()
    for edge in tree.flow:
        pair = (edge.src, edge.dst)
        bad = (
            edge.src >= edge.dst
            or pair in seen_pairs
            or edge.dst in seen_dst
            or not (0 <= edge.src < len(nodes))
            or not (0 <= edge.dst < len(nodes))
            or nodes[edge.src].kind is not NodeKind.paragraph
            or nodes[edge.dst].kind is not NodeKind.paragraph
        )
        if bad:
            out.append("I4:flow")
            break
        seen_pairs.add(pair)
        seen_dst.add(edge.dst)

    # I5 — zero document strings: every string in the canonical bytes matches the closed set.
    out.extend(_string_violations(json.loads(dump_tree(tree))))
    return out


#: Per-key closed sets for the I5 walk. A key not listed here must not hold a string at all.
_STRING_RULES: dict[str, Any] = {
    "schema": re.compile(r"^dpc\.doctree/[0-9]+$"),
    "doc_id": re.compile(_DOC_ID_RE),
    "view_sha256": re.compile(_SHA256_RE),
    "builder": re.compile(_BUILDER_RE),
    "kind": frozenset({k.value for k in NodeKind} | {"continues"}),
    "path": re.compile(_PATH_RE),
    "figure_id": re.compile(_FIGURE_ID_RE),
    "provider_ref": re.compile(_PROVIDER_REF_RE),
    "provider_role": re.compile(_PROVIDER_ROLE_RE),
    "source": frozenset(s.value for s in ProvSource),
    "script_class": frozenset(s.value for s in ScriptClass),
    "digit_ratio_class": frozenset(d.value for d in DigitRatioClass),
    "alignment": frozenset(a.value for a in Alignment),
    "evidence": frozenset(e.value for e in Evidence),
    "provider_sections": re.compile(_PASS_RE),
    "provider_figures": re.compile(_PASS_RE),
    "geometry": re.compile(_PASS_RE),
    "interposer": re.compile(_PASS_RE),
    "heading_nesting": re.compile(_PASS_RE),
    "continuity": re.compile(_PASS_RE),
}


def _string_violations(payload: Any, key: str = "") -> list[str]:
    """Walk a dumped tree and flag every string outside the closed set (I5's runtime check).

    The walk carries the owning key down through lists, so an ``evidence`` entry is checked
    against the evidence vocabulary, not against "any string anywhere" — per-key sets are a
    materially tighter net than one union set.
    """
    if isinstance(payload, str):
        rule = _STRING_RULES.get(key)
        if rule is None:
            return [f"I5:{key or 'value'}"]
        if isinstance(rule, frozenset):
            return [] if payload in rule else [f"I5:{key}"]
        return [] if rule.fullmatch(payload) else [f"I5:{key}"]
    if isinstance(payload, dict):
        out: list[str] = []
        for child_key in sorted(payload):
            out.extend(_string_violations(payload[child_key], child_key))
        return out
    if isinstance(payload, list):
        out = []
        for item in payload:
            out.extend(_string_violations(item, key))
        return out
    return []


__all__ = [
    "BUILDER_VERSION",
    "PATH_TOKENS",
    "SCHEMA",
    "Alignment",
    "Counters",
    "DigitRatioClass",
    "DocTree",
    "Evidence",
    "FlowEdge",
    "Metrics",
    "Node",
    "NodeKind",
    "PageDims",
    "Passes",
    "Prov",
    "ProvSource",
    "Report",
    "ScriptClass",
    "TreeValidation",
    "dump_tree",
    "tree_sha256",
    "validate_tree",
    "view_sha256",
    "walk_body",
]
