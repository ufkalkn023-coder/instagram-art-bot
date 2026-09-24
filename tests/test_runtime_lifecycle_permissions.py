"""Runtime entrypoints use object reads without bucket-control-plane permissions."""

import pytest

import main
from src import publication_reconciliation, publication_state, reel_reconciliation
from tests.test_publication_state import safety_candidate


class StopAfterObjectReads(Exception):
    pass


@pytest.mark.parametrize(
    "reconcile",
    (
        publication_reconciliation.reconcile_publications,
        reel_reconciliation.reconcile_reel_publications,
    ),
)
def test_reconciliation_reaches_required_objects_without_lifecycle_permission(monkeypatch, reconcile):
    calls = []

    class Store:
        def load_safety(self):
            calls.append("safety")

        def load_receipts(self):
            calls.append("receipts")
            raise StopAfterObjectReads

    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", Store)
    monkeypatch.setattr(
        publication_state,
        "validate_state_bucket_lifecycle",
        lambda _store: pytest.fail("runtime requested bucket lifecycle configuration"),
    )

    with pytest.raises(StopAfterObjectReads):
        reconcile(access_token="token")
    assert calls == ["safety", "receipts"]


def test_reconciliation_preview_reads_objects_without_lifecycle_permission(monkeypatch):
    calls = []

    class Store:
        def load_safety(self):
            calls.append("safety")
            return publication_state.validate_safety_state(safety_candidate()), '"etag"'

        def load_receipts(self):
            calls.append("receipts")

    monkeypatch.setattr(publication_state, "PublicationStateStore", Store)
    monkeypatch.setattr(
        publication_state,
        "validate_state_bucket_lifecycle",
        lambda _store: pytest.fail("preview requested bucket lifecycle configuration"),
    )

    assert main.main(["--preview-publication-reconciliation"]) == 0
    assert calls == ["safety", "receipts"]
