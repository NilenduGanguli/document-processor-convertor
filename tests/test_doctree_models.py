"""The doctree schema: canonical bytes, traversal, invariant validation, and I5.

The one test that matters most here is ``test_tree_has_no_strings``: it walks the CANONICAL
BYTES (not the models) of every fixture tree and asserts each string value belongs to the
closed set the spec names — enum values, the path grammar, JSON-pointer provider refs,
``fig-N-N`` ids and the hex/manifest scalars. Invariant I5 is a type-system property, but
the test is the tripwire that survives a refactor of the types.

Negative validation cases use ``model_construct`` on purpose: pydantic's own field patterns
make invalid trees unrepresentable through the front door, and the validator must still
catch a tree that arrived through the back one (a hand-edited artifact, a version skew).
"""
from __future__ import annotations

import json
import re

import pytest
from test_doctree_build import ALL_VIEWS, provider_view, two_column_view

from dpc.doctree.build import build_doctree
from dpc.doctree.models import (
    Alignment,
    DigitRatioClass,
    DocTree,
    Evidence,
    FlowEdge,
    NodeKind,
    ProvSource,
    ScriptClass,
    dump_tree,
    tree_sha256,
    validate_tree,
    walk_body,
)


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------
def test_dump_tree_is_canonical_json() -> None:
    """Sorted keys, compact separators, pure ASCII — the only bytes sha256 may see."""
    raw = dump_tree(build_doctree(two_column_view()))
    assert raw == raw.decode("ascii").encode()  # ASCII round-trip: no non-ASCII bytes
    text = raw.decode()
    assert '": ' not in text, "separators must be compact"
    payload = json.loads(text)
    assert json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ) == text, "re-serializing canonically must be the identity"


def test_dump_tree_excludes_nulls() -> None:
    raw = dump_tree(build_doctree(two_column_view()))
    assert b"null" not in raw, "exclude_none: absent beats explicit null"


def test_tree_sha_is_the_sha_of_the_canonical_bytes() -> None:
    import hashlib

    tree = build_doctree(provider_view())
    assert tree_sha256(tree) == hashlib.sha256(dump_tree(tree)).hexdigest()


def test_model_round_trip_preserves_bytes() -> None:
    tree = build_doctree(provider_view())
    reloaded = DocTree.model_validate_json(dump_tree(tree))
    assert dump_tree(reloaded) == dump_tree(tree)


# ---------------------------------------------------------------------------
# walk_body
# ---------------------------------------------------------------------------
def test_walk_body_is_preorder_and_stack_based() -> None:
    tree = build_doctree(provider_view())
    ids = [n.id for n in walk_body(tree)]
    assert ids[0] == tree.body
    assert ids == sorted(ids), "final ids ARE the pre-order, so the walk ascends"
    # Every body-subtree node exactly once, no furniture.
    furn_ids = {tree.furniture} | set(tree.nodes[tree.furniture].children)
    assert set(ids).isdisjoint(furn_ids)


def test_walk_body_survives_a_dense_flat_tree_without_recursion() -> None:
    """200-block page: the explicit stack must not care about interpreter limits (§8.8)."""
    from dpc.models import LayoutView, TextBlock

    view = LayoutView(
        blocks=[TextBlock(text=f"row {i}", seq=i) for i in range(200)]
    )
    tree = build_doctree(view)
    assert sum(1 for _ in walk_body(tree)) == 201  # body + 200 leaves
    assert validate_tree(tree, view).ok


def test_walk_body_tolerates_malformed_ids() -> None:
    tree = build_doctree(two_column_view())
    tree.nodes[tree.body].children.append(10_000)  # dangling child ref
    assert [n.id for n in walk_body(tree)]  # walks, skips, never raises


# ---------------------------------------------------------------------------
# validate_tree — each invariant caught, never a raise
# ---------------------------------------------------------------------------
def _valid_tree() -> tuple[DocTree, object]:
    view = provider_view()
    return build_doctree(view), view


def test_validator_accepts_the_builder_output() -> None:
    tree, view = _valid_tree()
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert check.ok and check.violations == ()


def test_i1_catches_id_index_mismatch() -> None:
    tree, view = _valid_tree()
    tree.nodes[3] = tree.nodes[3].model_copy(update={"id": 99})
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert not check.ok
    assert any(v.startswith("I1") for v in check.violations)


def test_i2_catches_broken_parent_child_refs() -> None:
    tree, view = _valid_tree()
    tree.nodes[3] = tree.nodes[3].model_copy(update={"parent": None})
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert any(v.startswith("I2") for v in check.violations)


def test_i3_catches_an_unclaimed_block() -> None:
    tree, view = _valid_tree()
    victim = next(n for n in tree.nodes if n.block_ixs)
    tree.nodes[victim.id] = victim.model_copy(update={"block_ixs": []})
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert "I3:blocks" in check.violations


def test_i3_catches_a_double_claimed_block() -> None:
    tree, view = _valid_tree()
    victim = next(n for n in tree.nodes if n.block_ixs)
    doubled = victim.block_ixs + victim.block_ixs
    tree.nodes[victim.id] = victim.model_copy(update={"block_ixs": doubled})
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert "I3:blocks" in check.violations


def test_i3_fails_against_the_wrong_view() -> None:
    """Stale-tree drift is structurally impossible: wrong view => I3 mismatch."""
    tree, _ = _valid_tree()
    check = validate_tree(tree, two_column_view())
    assert "I3:blocks" in check.violations


def test_i4_catches_bad_flow_edges() -> None:
    tree, view = _valid_tree()
    paragraphs = [n.id for n in tree.nodes if n.kind is NodeKind.paragraph]
    a, b = paragraphs[0], paragraphs[0]
    tree.flow.append(FlowEdge(src=b, dst=a))  # self edge, src == dst
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert "I4:flow" in check.violations


def test_i4_catches_a_double_destination() -> None:
    tree, view = _valid_tree()
    heads = [n.id for n in tree.nodes if n.kind is NodeKind.paragraph]
    assert len(heads) >= 2
    # Two edges sharing one destination — the second must trip I4's double-dst rule.
    tree.flow.extend([
        FlowEdge(src=min(heads), dst=max(heads)),
        FlowEdge(src=min(heads), dst=max(heads)),
    ])
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert "I4:flow" in check.violations


def test_i5_catches_a_smuggled_document_string() -> None:
    """model_construct bypasses field patterns — exactly how a hand-edited artifact would."""
    tree, view = _valid_tree()
    tree.nodes[3] = tree.nodes[3].model_copy(
        update={"path": "Jane Q. Public"}  # document text where a path belongs
    )
    check = validate_tree(tree, view)  # type: ignore[arg-type]
    assert any(v.startswith("I5") for v in check.violations)


def test_validator_never_raises_on_garbage() -> None:
    tree = DocTree.model_construct(nodes="not a list")  # type: ignore[arg-type]
    check = validate_tree(tree, two_column_view())
    assert not check.ok  # reported, not raised


# ---------------------------------------------------------------------------
# I5 — the closed-set walk over canonical bytes (§8.6 test_tree_has_no_strings)
# ---------------------------------------------------------------------------
_PATH = re.compile(
    r"^//doc(/(body|furn|sect|fg|frame|h|p|fn|table|fig|cap|kvg|kv|lg|li|mark)"
    r"\[[1-9][0-9]*\])*$"
)
_ALLOWED_BY_KEY: dict[str, object] = {
    "schema": re.compile(r"^dpc\.doctree/[0-9]+$"),
    "doc_id": re.compile(r"^[A-Za-z0-9._:-]{0,128}$"),
    "view_sha256": re.compile(r"^[0-9a-f]{64}$"),
    "builder": re.compile(r"^dpc-doctree/[0-9]+\.[0-9]+\.[0-9]+$"),
    "kind": {k.value for k in NodeKind} | {"continues"},
    "path": _PATH,
    "figure_id": re.compile(r"^fig-[1-9][0-9]*-[1-9][0-9]*$"),
    "provider_ref": re.compile(r"^/(sections|paragraphs|tables|figures)/[0-9]+$"),
    "provider_role": re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$"),
    "source": {s.value for s in ProvSource},
    "script_class": {s.value for s in ScriptClass},
    "digit_ratio_class": {d.value for d in DigitRatioClass},
    "alignment": {a.value for a in Alignment},
    "evidence": {e.value for e in Evidence},
    "provider_sections": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
    "provider_figures": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
    "geometry": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
    "interposer": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
    "heading_nesting": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
    "continuity": re.compile(r"^[a-z_]+(\([A-Za-z0-9_=,\[\] .:-]*\))?$"),
}


def _collect_strings(payload: object, key: str = "") -> list[tuple[str, str]]:
    if isinstance(payload, str):
        return [(key, payload)]
    if isinstance(payload, dict):
        return [
            pair for child_key in sorted(payload)
            for pair in _collect_strings(payload[child_key], child_key)
        ]
    if isinstance(payload, list):
        return [pair for item in payload for pair in _collect_strings(item, key)]
    return []


@pytest.mark.parametrize("name", sorted(ALL_VIEWS))
def test_tree_has_no_strings(name: str) -> None:
    """Every string in the stored bytes belongs to the closed set — invariant I5.

    Document text in these fixtures includes names, addresses and account fragments; NONE of
    it may survive into the artifact, and the assertion is per-key, so an unexpected key
    holding any string at all also fails.
    """
    tree = build_doctree(ALL_VIEWS[name]())
    payload = json.loads(dump_tree(tree))
    for key, value in _collect_strings(payload):
        rule = _ALLOWED_BY_KEY.get(key)
        assert rule is not None, f"string under unexpected key {key!r}: {value!r}"
        if isinstance(rule, set):
            assert value in rule, f"{key}={value!r} outside its closed enum"
        else:
            assert rule.fullmatch(value), f"{key}={value!r} breaks its grammar"  # type: ignore[union-attr]


#: The artifact's own legitimate vocabulary — JSON keys, enum values, grammar tokens. A
#: document word that happens to collide with it ("report", "table") proves nothing either
#: way, so the leak test only asserts over words OUTSIDE this set.
_SCHEMA_VOCAB = (
    {k.value for k in NodeKind}
    | {s.value for s in ProvSource} | {s.value for s in ScriptClass}
    | {a.value for a in Alignment} | {d.value for d in DigitRatioClass}
    | {e.value for e in Evidence}
    | {
        "schema", "body", "furniture", "nodes", "flow", "report", "passes", "counters",
        "pages", "builder", "children", "parent", "kind", "path", "page", "bbox", "level",
        "metrics", "prov", "source", "continues", "used", "absent", "declined", "error",
    }
)


@pytest.mark.parametrize("name", sorted(ALL_VIEWS))
def test_no_block_text_fragment_reaches_the_artifact(name: str) -> None:
    """Belt over braces: no distinctive word of any block's text appears in the bytes."""
    view = ALL_VIEWS[name]()
    raw = dump_tree(build_doctree(view)).decode().lower()
    checked = 0
    for block in view.blocks:
        for word in block.text.lower().split():
            if len(word) >= 4 and word.isalpha() and word not in _SCHEMA_VOCAB:
                assert word not in raw, f"document word {word!r} leaked into the tree"
                checked += 1
    assert checked, f"{name}: fixture carried no distinctive words to check"
