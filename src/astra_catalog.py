"""Artfolio unified design catalog registry (Astra 001-100 + Neon 001-180 = 280 designs)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
ASTRA_CATALOG_PATH = REPO_ROOT / "assets" / "astra" / "catalog.json"
NEON_CATALOG_PATH = REPO_ROOT / "assets" / "neon" / "catalog.json"

DESIGN_ID_PATTERN = r"^[a-z][a-z0-9]{1,15}-[0-9]{3}$"

READY_ASTRA_IDS: frozenset[str] = frozenset(f"astra-{n:03d}" for n in range(1, 101))
READY_NEON_IDS: frozenset[str] = frozenset(f"neon-{n:03d}" for n in range(1, 181))
READY_DESIGN_IDS: frozenset[str] = READY_ASTRA_IDS | READY_NEON_IDS

FAMILY_SLUGS: tuple[str, ...] = (
    "gallery",
    "detail",
    "duet",
    "poster",
    "archive",
    "cinema",
    "colors",
    "typography",
    "story",
    "experimental",
)


class _StrictCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DesignFamily(_StrictCatalogModel):
    """One of the shared visual composition families."""

    slug: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)


class DesignCatalogEntry(_StrictCatalogModel):
    """A registered design entry with separate catalog and template readiness status."""

    id: str = Field(pattern=DESIGN_ID_PATTERN)
    collection: str = Field(pattern=r"^[a-z][a-z0-9]{1,15}$")
    number: int = Field(ge=1, le=999, strict=True)
    name: str = Field(min_length=1, max_length=80)
    family: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    family_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=320)
    sample_image: str = Field(min_length=1, max_length=160)
    source_keys: tuple[str, ...] = Field(min_length=1, max_length=6)
    required_sources: int = Field(ge=1, le=6, strict=True)
    sample_head: str = Field(default="", max_length=120)
    catalog_status: Literal["registered"] = "registered"
    template_status: Literal["ready", "catalog_only"] = "catalog_only"

    @model_validator(mode="after")
    def _validate_consistency(self) -> "DesignCatalogEntry":
        expected_id = f"{self.collection}-{self.number:03d}"
        if self.id != expected_id:
            raise ValueError(
                f"Design id '{self.id}' does not match collection/number '{expected_id}'"
            )
        if self.required_sources != len(self.source_keys):
            raise ValueError(
                f"Design '{self.id}' required_sources ({self.required_sources}) "
                f"must match len(source_keys) ({len(self.source_keys)})"
            )
        return self

    @property
    def is_usable(self) -> bool:
        """Whether this design is ported and safe to render with real artworks."""
        return self.template_status == "ready"


class DesignCatalog(_StrictCatalogModel):
    """Validated collection of design families and unique design entries."""

    schema_version: Literal["artfolio-design-catalog-v1"] = "artfolio-design-catalog-v1"
    families: tuple[DesignFamily, ...] = Field(min_length=1)
    designs: tuple[DesignCatalogEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> "DesignCatalog":
        family_slugs = [family.slug for family in self.families]
        if len(set(family_slugs)) != len(family_slugs):
            raise ValueError("Design family slugs must be unique")
        known_families = set(family_slugs)

        design_ids = [entry.id for entry in self.designs]
        if len(set(design_ids)) != len(design_ids):
            raise ValueError("Design catalog IDs must be unique")

        for entry in self.designs:
            if entry.family not in known_families:
                raise ValueError(
                    f"Design '{entry.id}' references unknown family '{entry.family}'"
                )
        return self

    def by_id(self, design_id: str) -> DesignCatalogEntry:
        for entry in self.designs:
            if entry.id == design_id:
                return entry
        raise KeyError(f"Unknown design id: {design_id}")

    def usable_designs(
        self,
        *,
        source_count: int | None = None,
        family: str | None = None,
        collection: str | None = None,
    ) -> tuple[DesignCatalogEntry, ...]:
        """Return only ported designs that are ready for real artwork rendering."""
        matches = [entry for entry in self.designs if entry.is_usable]
        if source_count is not None:
            matches = [entry for entry in matches if entry.required_sources == source_count]
        if family is not None:
            matches = [entry for entry in matches if entry.family == family]
        if collection is not None:
            matches = [entry for entry in matches if entry.collection == collection]
        return tuple(matches)

    def select_auto_design(
        self,
        *,
        source_count: int = 1,
        family: str | None = None,
        collection: str | None = None,
        exclude_ids: set[str] | frozenset[str] = frozenset(),
    ) -> DesignCatalogEntry:
        """Select a ready template for automatic workflows; never picks unported entries."""
        candidates = [
            entry
            for entry in self.usable_designs(
                source_count=source_count, family=family, collection=collection
            )
            if entry.id not in exclude_ids
        ]
        if not candidates and family is not None:
            candidates = [
                entry
                for entry in self.usable_designs(
                    source_count=source_count, collection=collection
                )
                if entry.id not in exclude_ids
            ]
        if not candidates and collection is not None:
            candidates = [
                entry
                for entry in self.usable_designs(source_count=source_count)
                if entry.id not in exclude_ids
            ]
        if not candidates:
            raise ValueError(
                f"No ready design template available for source_count={source_count}"
            )
        return candidates[0]

    def with_collection_entries(
        self,
        entries: list[dict] | tuple[DesignCatalogEntry, ...],
        *,
        extra_families: list[dict] | tuple[DesignFamily, ...] = (),
    ) -> "DesignCatalog":
        """Return a new validated catalog with additional collections without ID collision."""
        merged_families = list(self.families)
        existing_slugs = {family.slug for family in merged_families}
        for raw_family in extra_families:
            family = (
                raw_family
                if isinstance(raw_family, DesignFamily)
                else DesignFamily.model_validate(raw_family)
            )
            if family.slug not in existing_slugs:
                merged_families.append(family)
                existing_slugs.add(family.slug)

        merged_designs = list(self.designs)
        for raw_entry in entries:
            entry = (
                raw_entry
                if isinstance(raw_entry, DesignCatalogEntry)
                else DesignCatalogEntry.model_validate(raw_entry)
            )
            merged_designs.append(entry)

        return DesignCatalog(
            families=tuple(merged_families),
            designs=tuple(merged_designs),
        )


@lru_cache(maxsize=8)
def load_design_catalog(
    catalog_path: str | Path = ASTRA_CATALOG_PATH,
    ready_ids: frozenset[str] = READY_DESIGN_IDS,
    *,
    include_neon: bool = True,
    neon_catalog_path: str | Path = NEON_CATALOG_PATH,
) -> DesignCatalog:
    """Load and validate Astra (100) + Neon (180) design catalogs from disk."""
    path = Path(catalog_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    families = tuple(DesignFamily.model_validate(item) for item in payload["families"])
    raw_designs = list(payload["designs"])

    if include_neon:
        neon_path = Path(neon_catalog_path)
        if neon_path.is_file():
            neon_payload = json.loads(neon_path.read_text(encoding="utf-8"))
            raw_designs.extend(neon_payload["designs"])

    designs = []
    for raw in raw_designs:
        design_id = raw["id"]
        entry_data = {
            **raw,
            "catalog_status": "registered",
            "template_status": "ready" if design_id in ready_ids else "catalog_only",
        }
        designs.append(DesignCatalogEntry.model_validate(entry_data))
    return DesignCatalog(families=families, designs=tuple(designs))


def get_design(design_id: str) -> DesignCatalogEntry:
    """Look up any registered Astra or Neon design by canonical ID."""
    return load_design_catalog().by_id(design_id)


def list_usable_designs(
    *,
    source_count: int | None = None,
    family: str | None = None,
    collection: str | None = None,
) -> tuple[DesignCatalogEntry, ...]:
    """Return only designs with template_status == 'ready'."""
    return load_design_catalog().usable_designs(
        source_count=source_count,
        family=family,
        collection=collection,
    )


def select_auto_design(
    *,
    source_count: int = 1,
    family: str | None = None,
    collection: str | None = None,
    exclude_ids: set[str] | frozenset[str] = frozenset(),
) -> DesignCatalogEntry:
    """Select an automatic design strictly from ready templates."""
    return load_design_catalog().select_auto_design(
        source_count=source_count,
        family=family,
        collection=collection,
        exclude_ids=exclude_ids,
    )
