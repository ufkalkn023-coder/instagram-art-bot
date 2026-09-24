#!/usr/bin/env python3
"""Deterministic offline recovery candidate; no production write by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import require_canonical_artwork_id  # noqa: E402
from src.publication_state import (  # noqa: E402
    PublicationStateStore, StateValidationError, canonical_bytes, seal,
    validate_receipts, validate_safety_state, validate_state_bucket_lifecycle,
    SAFETY_KEY, RECEIPTS_KEY,
)

EXPECTED_DIGESTS = {
    "published-artwork-protection-set-v2.json": "22c8382f7b6e98d73a803a826c5000e5aeffefe68d023224a1af0c4ec829fe21",
    "recovery-ledger-v2.json": "c9d8c3f4139dbc14818b63d97e75b9d6e7f73ae01f23ff60407b8038156cb5e5",
    "legacy-candidate-audit.json": "447bf82b258d05e6126d3cf0e56360dd39ff6b51c926e177b16ec2f294d7797f",
    "unknown-artwork-resolution-v2.md": "a9890af9e0a817cebe1502e540450ea993e0173279b70a3c4454ad240b6a6e67",
}
INFERRED_MET_IDS = ("met_437896", "met_45779", "met_471349", "met_10459",
                    "met_36133", "met_40020")


def _read_evidence(directory: Path, name: str) -> Any:
    raw = (directory / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_DIGESTS[name]:
        raise StateValidationError(f"Evidence digest mismatch: {name}")
    return raw.decode("utf-8") if name.endswith(".md") else json.loads(raw)


def _value(record: dict[str, Any], field: str) -> Any:
    return record["fields"][field]["value"]


def build_candidate(directory: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    protection = _read_evidence(directory, "published-artwork-protection-set-v2.json")
    ledger = _read_evidence(directory, "recovery-ledger-v2.json")
    audit = _read_evidence(directory, "legacy-candidate-audit.json")
    unknown_review = _read_evidence(directory, "unknown-artwork-resolution-v2.md")
    if (protection["schema_version"] != 2 or ledger["schema_version"] != 2
            or audit["schema_version"] != 1
            or not all(item["upload_prohibited"] for item in (protection, ledger, audit))):
        raise StateValidationError("Forensic input schema or safety marker mismatch")
    counts = protection["counts"]
    expected = (624, 10, 18, 634, 5, 16)
    actual = (counts["A_PROVEN_PUBLISHED_ARTWORK_IDS"],
              counts["B_STRONGLY_SUPPORTED_PUBLISHED_ARTWORK_IDS"],
              counts["C_LEGACY_UNVERIFIED_CANDIDATES"],
              counts["known_protection_set_A_union_B"],
              counts["D_CONFLICTING_IDS"],
              counts["unknown_carousel_artwork_positions_not_counted"])
    if actual != expected or ledger["summary"]["expected_publications"] != 68:
        raise StateValidationError("Forensic evidence count mismatch")
    sets = protection["sets"]
    proven = set(sets["A_PROVEN_PUBLISHED_ARTWORK_IDS"])
    supported = set(sets["B_STRONGLY_SUPPORTED_PUBLISHED_ARTWORK_IDS"])
    unverified = set(sets["C_LEGACY_UNVERIFIED_CANDIDATES"])
    reuse = set(sets["D_CONFLICTING_IDS"])
    if (len(proven), len(supported), len(unverified), len(reuse)) != (624, 10, 18, 5):
        raise StateValidationError("Forensic evidence set cardinality mismatch")
    if (proven & supported) or (proven & unverified) or (supported & unverified):
        raise StateValidationError("Evidence classifications overlap")
    if set(sets["CONSERVATIVE_PROTECTION_SET"]) != proven | supported:
        raise StateValidationError("Protection union mismatch")
    if {item["canonical_artwork_id"] for item in audit["candidates"]} != unverified:
        raise StateValidationError("Legacy audit candidate mismatch")
    audit_indices = {item["canonical_artwork_id"]: index
                     for index, item in enumerate(audit["candidates"])}
    for item in audit["candidates"]:
        if item["classification"] != "UNVERIFIED" or item["protect_in_evidence_based_set"]:
            raise StateValidationError("Unverified legacy candidate was promoted")
    for artwork_id in proven | supported | unverified | reuse:
        require_canonical_artwork_id(artwork_id)
    if not reuse.issubset(proven):
        raise StateValidationError("Reuse annotation is outside proven protection")
    if set(protection["entries"]) != proven | supported | unverified:
        raise StateValidationError("Evidence entry keys mismatch")

    receipts = []
    occurrences: dict[str, list[str]] = {}
    unknown_positions = 0
    unresolved_descriptors = []
    for index, record in enumerate(ledger["publications"]):
        if record["chronological_index"] != index + 1:
            raise StateValidationError("Chronological ledger index mismatch")
        publication_id = _value(record, "publication_id")
        media_id = _value(record, "instagram_media_id")
        publication_type = _value(record, "publication_type")
        if (record["fields"]["publication_id"]["confidence"] != "EXACT"
                or record["fields"]["instagram_media_id"]["confidence"] != "EXACT"):
            raise StateValidationError("Publication/media pair lacks exact evidence")
        artwork_ids = _value(record, "artwork_ids_in_order")
        children = _value(record, "carousel_children_in_order") or []
        labels = _value(record, "caption_artworks_in_order") or []
        if artwork_ids is None:
            if publication_type != "carousel" or len(children) != 8 or len(labels) != 8:
                raise StateValidationError("Incomplete carousel evidence shape mismatch")
            artwork_ids = [None] * 8
        elif record["fields"]["artwork_ids_in_order"]["confidence"] != "EXACT":
            raise StateValidationError("Artwork membership is not exact")
        positions = []
        for position, artwork_id in enumerate(artwork_ids, start=1):
            if artwork_id is not None:
                require_canonical_artwork_id(artwork_id)
                occurrences.setdefault(artwork_id, []).append(publication_id)
            else:
                unknown_positions += 1
                unresolved_descriptors.append({
                    "publication_id": publication_id, "position": position,
                    "instagram_child_media_id": children[position - 1]["id"] if children else None,
                    "caption_label": labels[position - 1] if labels else None,
                    "evidence_ref": f"recovery-ledger-v2.json#/publications/{index}/artwork_positions/{position - 1}",
                })
            positions.append({
                "position": position, "canonical_artwork_id": artwork_id,
                "instagram_child_media_id": children[position - 1]["id"] if children else None,
                "caption_label": labels[position - 1] if labels else None,
            })
        receipts.append({
            "publication_id": publication_id, "instagram_media_id": media_id,
            "publication_type": publication_type,
            "historical_state": "PUBLISHED_CONFIRMED",
            "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
            "identity_completeness": "INCOMPLETE" if None in artwork_ids else "COMPLETE",
            "occurred_at": _value(record, "publication_timestamp"),
            "permalink": _value(record, "instagram_permalink"),
            "workflow_run_id": _value(record, "run_id"),
            "artwork_positions": positions,
            "evidence_ref": f"recovery-ledger-v2.json#/publications/{index}",
        })
    if len(receipts) != 68 or unknown_positions != 16:
        raise StateValidationError("Recovered receipt/unresolved position mismatch")
    if {key for key, values in occurrences.items() if len(values) > 1} != reuse:
        raise StateValidationError("Historical reuse evidence mismatch")
    if set(ledger["summary"]["duplicate_artwork_ids_across_publications"]) != reuse:
        raise StateValidationError("Ledger reuse summary mismatch")
    if any(
        set(occurrences[key]) != set(
            ledger["summary"]["duplicate_artwork_ids_across_publications"][key]
        )
        for key in reuse
    ):
        raise StateValidationError("Historical reuse publication references mismatch")
    if len(occurrences) != 395 or len(proven - set(occurrences)) != 229:
        raise StateValidationError("Exact membership or older lock count mismatch")
    if sum(item["identity_completeness"] == "COMPLETE" for item in receipts) != 66:
        raise StateValidationError("Exact membership count mismatch")
    if not all(re.search(rf"\b{artwork_id}\b", unknown_review)
               for artwork_id in INFERRED_MET_IDS):
        raise StateValidationError("Inferred candidate review mismatch")

    entries: dict[str, Any] = {}
    for artwork_id in sorted(proven | supported):
        evidence = protection["entries"][artwork_id]
        expected_class = ("PROVEN_PUBLISHED_ARTWORK_IDS" if artwork_id in proven
                          else "STRONGLY_SUPPORTED_PUBLISHED_ARTWORK_IDS")
        if evidence["classification"] != expected_class or not evidence["protect_in_conservative_set"]:
            raise StateValidationError("Protection entry classification mismatch")
        provenance = evidence["provenance"]
        if not provenance:
            raise StateValidationError("Protection entry lacks provenance")
        first = next((item for item in provenance if item.get("publication_id")
                      or item.get("instagram_media_id")), None)
        entries[artwork_id] = {
            "canonical_artwork_id": artwork_id,
            "classification": "PROVEN" if artwork_id in proven else "STRONGLY_SUPPORTED",
            "provenance_refs": [f"published-artwork-protection-set-v2.json#/entries/{artwork_id}/provenance/{i}"
                                for i in range(len(provenance))],
            "first_known_publication_reference": {
                "publication_id": first.get("publication_id"),
                "instagram_media_id": first.get("instagram_media_id"),
            } if first else None,
            "historical_reuse_publication_ids": sorted(set(occurrences.get(artwork_id, [])))
            if artwork_id in reuse else [],
            "origin": "RECOVERED",
        }
    protection_section = seal({
        "schema_version": 1, "import_batch_id": "recovery-2026-09-23",
        "source_artifact": "published-artwork-protection-set-v2.json",
        "source_sha256": EXPECTED_DIGESTS["published-artwork-protection-set-v2.json"],
        "entry_count": len(entries), "entries": entries,
    })
    epoch = "recovery-2026-09-23-" + EXPECTED_DIGESTS["published-artwork-protection-set-v2.json"][:12]
    safety = seal({
        "schema_version": 2, "state_epoch": epoch, "generation": 1,
        "published_artwork_protection": protection_section,
        "recovery_quarantine": {
            "schema_version": 1,
            "candidate_artwork_ids": [
                {"canonical_artwork_id": artwork_id, "classification": "UNVERIFIED",
                 "evidence_ref": f"legacy-candidate-audit.json#/candidates/{audit_indices[artwork_id]}"}
                for artwork_id in sorted(unverified)
            ],
            "inferred_catalog_candidates": sorted(INFERRED_MET_IDS),
            "unresolved_historic_position_count": unknown_positions,
            "unresolved_positions": unresolved_descriptors,
            "blocked_sources": ["met", "smithsonian"],
        },
        "active_publication_state": {
            "schema_version": 1, "posted_artworks": [], "reel_reservations": [],
            "reel_publications": [], "reel_publication_count": 0,
            "staging_media_cleanup_queue": [], "reel_staging_cleanup_queue": [],
            "receipt_sync_pending": [],
        },
        "operational_projection": {
            "publications": [], "grid_publication_count": 0,
            "grid_counter_epoch": epoch, "active_color_tone": "warm",
        },
    })
    receipt_ledger = seal({
        "schema_version": 2, "generation": 1,
        "source_artifact": "recovery-ledger-v2.json",
        "source_sha256": EXPECTED_DIGESTS["recovery-ledger-v2.json"],
        "record_count": len(receipts), "records": receipts,
    })
    validate_safety_state(safety)
    validate_receipts(receipt_ledger)
    report = {
        "protected_ids": len(entries), "proven_ids": len(proven),
        "strongly_supported_ids": len(supported),
        "publication_receipts": len(receipts), "unresolved_positions": unknown_positions,
        "quarantine_ids": len(unverified), "inferred_candidates": len(INFERRED_MET_IDS),
        "historical_reuse_ids": len(reuse),
        "safety_sha256": safety["payload_sha256"],
        "receipts_sha256": receipt_ledger["payload_sha256"],
    }
    return safety, receipt_ledger, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path,
                        help="Write local candidate JSON files; never uploads")
    parser.add_argument("--write", action="store_true",
                        help="Create production objects only with explicit confirmation")
    parser.add_argument("--confirm-production-write", default="")
    parser.add_argument("--target-bucket", default="",
                        help="Explicit durable-state bucket name required with --write")
    args = parser.parse_args(argv)
    safety, receipts, report = build_candidate(args.evidence_dir)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / SAFETY_KEY).write_bytes(canonical_bytes(safety) + b"\n")
        (args.output_dir / RECEIPTS_KEY).write_bytes(canonical_bytes(receipts) + b"\n")
    if args.write:
        if args.confirm_production_write != "I_AUTHORIZE_PRODUCTION_RECOVERY_BOOTSTRAP":
            raise StateValidationError("Production write requires exact affirmative confirmation")
        if not args.target_bucket.strip():
            raise StateValidationError("Production write requires --target-bucket")
        store = PublicationStateStore()
        if args.target_bucket.strip() != store.config.bucket:
            raise StateValidationError("Explicit bootstrap target differs from configured state bucket")
        validate_state_bucket_lifecycle(store)
        store.require_uninitialized()
        # Activate safety last. A failure after the receipt write leaves a
        # receipts-only target, which production cannot read as active state.
        store.create_initial(RECEIPTS_KEY, receipts)
        store.create_initial(SAFETY_KEY, safety)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
