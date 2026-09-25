"""Receipt timestamps accepted by both the recovery import and live state reader."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src import models


def recovered_receipt(occurred_at):
    return {
        "publication_id": "test-publication", "instagram_media_id": "test-media",
        "publication_type": "single", "historical_state": "PUBLISHED_CONFIRMED",
        "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
        "identity_completeness": "COMPLETE", "occurred_at": occurred_at,
        "permalink": None, "workflow_run_id": None,
        "artwork_positions": [{
            "position": 1, "canonical_artwork_id": "aic_100489",
            "instagram_child_media_id": None, "caption_label": None,
        }],
        "evidence_ref": "test-fixture:receipt",
    }


@pytest.mark.parametrize("value, expected", [
    ("2026-08-24T14:39:56+0000", datetime(2026, 8, 24, 14, 39, 56, tzinfo=timezone.utc)),
    ("2026-08-24T14:39:56+00:00", datetime(2026, 8, 24, 14, 39, 56, tzinfo=timezone.utc)),
    ("2026-08-24T14:39:56Z", datetime(2026, 8, 24, 14, 39, 56, tzinfo=timezone.utc)),
    ("2026-08-24T17:09:56+0230", datetime(2026, 8, 24, 17, 9, 56,
                                          tzinfo=timezone(timedelta(hours=2, minutes=30)))),
    ("2026-08-24T12:09:56-02:30", datetime(2026, 8, 24, 12, 9, 56,
                                           tzinfo=timezone(-timedelta(hours=2, minutes=30)))),
    ("2026-08-24T14:39:56.123456Z", datetime(2026, 8, 24, 14, 39, 56, 123456,
                                             tzinfo=timezone.utc)),
])
def test_receipt_timestamp_contract_preserves_instant(value, expected):
    parsed = models.parse_receipt_occurrence(value)
    receipt = models.PublicationReceipt.model_validate(recovered_receipt(value))
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()
    assert parsed.tzinfo is not None
    assert receipt.occurred_at == value
    assert receipt.model_dump(mode="json")["occurred_at"] == value


@pytest.mark.parametrize("value", [
    "2026-08-24T14:39:56",  # timezone required for a present timestamp
    "2026-08-24T14:39:56+2400", "2026-08-24T14:39:56+0060",
    "2026-08-24T14:39:56+0A00", "2026-08-24T14:39:56+000",
    "2026-08-24T14:39:56+24:00", "2026-08-24T14:39:56+00:60",
    "2026-08-24T14:39:56+00:0A", "2026-08-24T14:39:56+0:00",
    "2026-02-30T14:39:56Z", "2026-08-24T25:39:56Z",
    "2026-08-24T14:39:56Zgarbage", "not-a-timestamp",
])
def test_receipt_timestamp_contract_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        models.parse_receipt_occurrence(value)
    with pytest.raises(ValidationError):
        models.PublicationReceipt.model_validate(recovered_receipt(value))


def test_recovered_receipt_may_have_unknown_occurrence():
    assert models.PublicationReceipt.model_validate(recovered_receipt(None)).occurred_at is None
