"""Logical document tree (SPEC-DOCTREE-1) — the artifact between the adapters and emitters.

Phase 0 ships only the provider-structure harvest (§3.1): ``from_azure_layout`` parks the
sections/figures skeleton under ``LayoutView.raw["structure"]`` so the tree stays
constructible from recorded payloads with no re-fetch. The builder, flattener and arrange
pass land in later phases (§7) and will widen this namespace to ``build_doctree``,
``DocTree``, ``walk_body``, ``dump_tree`` and ``tree_sha256``.
"""
from __future__ import annotations

from dpc.doctree.harvest import FigureRef, ProviderStructure, SectionRef, harvest_structure

__all__ = [
    "FigureRef",
    "ProviderStructure",
    "SectionRef",
    "harvest_structure",
]
