"""Tests for the unified 280-design catalog (100 Astra + 180 Neon) and readiness guards."""

from __future__ import annotations

import pytest

from src.astra_catalog import (
    ASTRA_CATALOG_PATH,
    FAMILY_SLUGS,
    READY_ASTRA_IDS,
    READY_DESIGN_IDS,
    READY_NEON_IDS,
    get_design,
    list_usable_designs,
    load_design_catalog,
    select_auto_design,
)


def test_unified_catalog_registers_280_unique_designs_across_10_families():
    catalog = load_design_catalog()
    assert len(catalog.families) == 10
    assert tuple(f.slug for f in catalog.families) == FAMILY_SLUGS
    assert len(catalog.designs) == 280

    astra = [d for d in catalog.designs if d.collection == "astra"]
    neon = [d for d in catalog.designs if d.collection == "neon"]
    assert len(astra) == 100
    assert len(neon) == 180
    assert [d.id for d in astra] == [f"astra-{n:03d}" for n in range(1, 101)]
    assert [d.id for d in neon] == [f"neon-{n:03d}" for n in range(1, 181)]
    assert len({d.id for d in catalog.designs}) == 280

    for entry in catalog.designs:
        assert entry.catalog_status == "registered"
        assert entry.name and entry.description and entry.sample_image
        assert len(entry.source_keys) == entry.required_sources


def test_all_280_astra_and_neon_templates_are_ready_while_preserving_status_separation():
    usable = list_usable_designs()
    assert {d.id for d in usable} == READY_DESIGN_IDS
    assert len(READY_ASTRA_IDS) == 100
    assert len(READY_NEON_IDS) == 180
    assert len(usable) == 280

    # Verify strict separation when only a subset is marked ready.
    partial = load_design_catalog(
        ASTRA_CATALOG_PATH,
        ready_ids=frozenset({"astra-001", "neon-180"}),
    )
    assert len(partial.designs) == 280
    assert {d.id for d in partial.usable_designs()} == {"astra-001", "neon-180"}
    assert sum(d.template_status == "catalog_only" for d in partial.designs) == 278


def test_auto_selection_never_picks_unported_catalog_only_templates():
    partial = load_design_catalog(
        ASTRA_CATALOG_PATH,
        ready_ids=frozenset({"astra-001", "neon-180"}),
    )
    chosen = partial.select_auto_design(source_count=1)
    assert chosen.is_usable and chosen.id in {"astra-001", "neon-180"}

    # No 3-source template is in the restricted ready set -> must raise, never leak catalog_only.
    with pytest.raises(ValueError, match="No ready design template"):
        partial.select_auto_design(source_count=3)

    # Full catalog can select across 1, 2, 3, and 4 source counts cleanly.
    assert select_auto_design(source_count=1, collection="astra").collection == "astra"
    assert select_auto_design(source_count=1, collection="neon").collection == "neon"
    assert select_auto_design(source_count=2).required_sources == 2
    assert select_auto_design(source_count=3).required_sources == 3
    assert select_auto_design(source_count=4).required_sources == 4


def test_future_collections_register_cleanly_without_id_collision():
    catalog = load_design_catalog()
    extended = catalog.with_collection_entries(
        [
            {
                "id": "prism-001",
                "collection": "prism",
                "number": 1,
                "name": "Prism Study",
                "family": "gallery",
                "family_name": "Galeri duvarı",
                "description": "Örnek gelecek koleksiyon kaydı.",
                "sample_image": "prism-001.jpg",
                "source_keys": ["artwork_1"],
                "required_sources": 1,
                "sample_head": "Prism",
                "catalog_status": "registered",
                "template_status": "catalog_only",
            }
        ]
    )
    assert len(extended.designs) == 281
    assert not extended.by_id("prism-001").is_usable
    assert get_design("astra-001").name == "Quiet Gallery"
    assert get_design("neon-180").name == "Museum Room Number"
