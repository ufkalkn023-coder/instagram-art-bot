"""Forensic input is transformed without inventing publication facts."""

import json
import shutil
from pathlib import Path

import pytest

from scripts.bootstrap_publication_recovery import EXPECTED_DIGESTS, build_candidate
from src import publication_state

EVIDENCE = Path.home() / "Documents/UfukOS/900-Archive/Agent-Logs/Codex/carousel-history-recovery"
pytestmark = pytest.mark.skipif(not EVIDENCE.exists(), reason="external forensic artifacts unavailable")


def test_recovery_candidate_is_deterministic_and_preserves_uncertainty():
    safety, ledger, report = build_candidate(EVIDENCE)
    safety2, ledger2, report2 = build_candidate(EVIDENCE)
    assert publication_state.canonical_bytes(safety) == publication_state.canonical_bytes(safety2)
    assert publication_state.canonical_bytes(ledger) == publication_state.canonical_bytes(ledger2)
    assert report == report2
    publication_state.require_recovery_safety_baseline(
        publication_state.validate_safety_state(safety)
    )
    publication_state.require_recovery_receipt_baseline(
        publication_state.validate_receipts(ledger)
    )
    assert report["protected_ids"] == 634
    assert report["publication_receipts"] == 68
    assert report["quarantine_ids"] == 18
    assert report["unresolved_positions"] == 16
    assert len(safety["recovery_quarantine"]["unresolved_positions"]) == 16
    assert all(item["canonical_artwork_id"] is None
               for receipt in ledger["records"]
               for item in receipt["artwork_positions"]
               if receipt["identity_completeness"] == "INCOMPLETE")
    reused = {key: entry["historical_reuse_publication_ids"]
              for key, entry in safety["published_artwork_protection"]["entries"].items()
              if entry["historical_reuse_publication_ids"]}
    assert len(reused) == 5
    assert all(len(publications) == 2 for publications in reused.values())
    assert all(key in safety["published_artwork_protection"]["entries"] for key in reused)
    assert len({item["publication_id"] for item in ledger["records"]}) == 68
    assert sum(item["occurred_at"] is None for item in ledger["records"]) == 1
    assert sum(item["occurred_at"] is not None and item["occurred_at"].endswith("+0000")
               for item in ledger["records"]) == 67


def test_recovery_import_rejects_modified_evidence(tmp_path):
    for name in EXPECTED_DIGESTS:
        shutil.copyfile(EVIDENCE / name, tmp_path / name)
    path = tmp_path / "legacy-candidate-audit.json"
    value = json.loads(path.read_text())
    value["candidates"][0]["classification"] = "PROVEN"
    path.write_text(json.dumps(value))
    with pytest.raises(publication_state.StateValidationError, match="digest mismatch"):
        build_candidate(tmp_path)
