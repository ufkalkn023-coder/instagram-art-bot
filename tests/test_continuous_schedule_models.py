"""Model invariants for continuous feed schedule approvals and permits."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.models import FeedScheduleControl


SHA = "a" * 40
APPROVAL_ID = str(uuid4())
PERMIT_ID = str(uuid4())
RUN_ID = 123


def stamp(day, hour=17, minute=17):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def approval(**changes):
    return {
        "approval_id": APPROVAL_ID,
        "approved_sha": SHA,
        "created_at": stamp(1, 12, 0),
        "approval_ref": "operator:continuous",
        "revoked_at": None,
    } | changes


def permit(day=2, **changes):
    slot = stamp(day)
    created = stamp(day)
    return {
        "permit_id": PERMIT_ID,
        "slot_at": slot,
        "expires_at": stamp(day, 18, 17),
        "approved_sha": SHA,
        "created_at": created,
        "approval_ref": "continuous:operator",
        "status": "ADMITTED",
        "continuous_approval_id": APPROVAL_ID,
        "owner_run_id": RUN_ID,
        "owner_run_attempt": 1,
        "authorization_key": f"schedule:{RUN_ID}",
        "admitted_at": created,
    } | changes


def control(permits=None, approvals=None, paused=True):
    return {
        "schema_version": 1,
        "paused": paused,
        "pause_reason": "operator",
        "latest_successful_feed_at": stamp(1),
        "latest_successful_feed_id": "feed-1",
        "next_eligible_at": (datetime.fromisoformat(stamp(1).replace("Z", "+00:00"))
                              + timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
        "permits": permits or [],
        "continuous_approvals": approvals or [],
    }


def test_continuous_permit_accepts_slot_creation_and_matching_live_approval():
    parsed = FeedScheduleControl.model_validate(
        control([permit()], [approval()])
    )
    assert parsed.permits[0].continuous_approval_id == APPROVAL_ID


@pytest.mark.parametrize(
    "changes, approvals",
    [
        ({"approved_sha": "b" * 40}, [approval()]),
        ({}, [approval(revoked_at=stamp(2, 17, 16))]),
        ({"created_at": stamp(2, 17, 17, )}, [approval(created_at=stamp(2, 17, 18))]),
        ({"admitted_at": stamp(2, 17, 18)}, [approval()]),
    ],
)
def test_continuous_permit_must_be_covered_at_admission(changes, approvals):
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(control([permit(**changes)], approvals))


def test_continuous_mode_unpauses_with_live_approval_and_no_permit():
    parsed = FeedScheduleControl.model_validate(control(approvals=[approval()], paused=False))
    assert parsed.paused is False


def test_historical_permit_created_at_revocation_timestamp_remains_readable():
    parsed = FeedScheduleControl.model_validate(
        control([permit()], [approval(revoked_at=stamp(2))])
    )
    assert parsed.permits[0].continuous_approval_id == APPROVAL_ID


def test_replacement_approval_can_share_revocation_timestamp():
    replaced = approval(revoked_at=stamp(2, 12, 0))
    replacement = approval(approval_id=str(uuid4()), created_at=stamp(2, 12, 0))
    parsed = FeedScheduleControl.model_validate(
        control(approvals=[replaced, replacement])
    )
    assert len(parsed.continuous_approvals) == 2


def test_continuous_permit_created_after_revocation_is_rejected():
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(
            control(
                [permit(created_at=stamp(2, 17, 18), admitted_at=stamp(2, 17, 18))],
                [approval(revoked_at=stamp(2))],
            )
        )


def test_continuous_next_slot_can_follow_closed_success_without_operator_ack():
    first = permit(day=2, status="SUCCESS", publication_id="66a4cac3-3db7-419d-bb05-74bd16a68801",
                   reservation_generation=2,
                   outcome={"classification": "SUCCESS", "recorded_at": stamp(2, 17, 30),
                            "safety_generation": 4, "receipt_generation": 2,
                            "publication_id": "66a4cac3-3db7-419d-bb05-74bd16a68801",
                            "instagram_media_id": "media-1", "cleanup_pending": False,
                            "reconciliation_pending": False})
    second = permit(day=3, permit_id=str(uuid4()), owner_run_id=124,
                    authorization_key="schedule:124")
    parsed = FeedScheduleControl.model_validate(control([first, second], [approval()]))
    assert len(parsed.permits) == 2


def test_continuous_next_slot_rejects_unreviewed_ambiguous_predecessor():
    first = permit(day=2, status="AMBIGUOUS", publication_id="66a4cac3-3db7-419d-bb05-74bd16a68801",
                   reservation_generation=2,
                   outcome={"classification": "AMBIGUOUS", "recorded_at": stamp(2, 17, 30),
                            "safety_generation": 4, "receipt_generation": 2,
                            "publication_id": "66a4cac3-3db7-419d-bb05-74bd16a68801",
                            "instagram_media_id": None, "cleanup_pending": False,
                            "reconciliation_pending": False})
    second = permit(day=3, permit_id=str(uuid4()), owner_run_id=124,
                    authorization_key="schedule:124")
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(control([first, second], [approval()]))


@pytest.mark.parametrize("status", ["AMBIGUOUS", "CANCELLED"])
def test_reviewed_ambiguous_or_cancelled_latest_permit_can_unpause(status):
    publication_id = "66a4cac3-3db7-419d-bb05-74bd16a68801"
    latest = permit(
        status=status,
        publication_id=publication_id,
        reservation_generation=2,
        outcome={"classification": status, "recorded_at": stamp(2, 17, 30),
                 "safety_generation": 4, "receipt_generation": 2,
                 "publication_id": publication_id, "instagram_media_id": None,
                 "cleanup_pending": False, "reconciliation_pending": False},
        acknowledgement={"reviewed_at": stamp(2, 18, 18),
                         "evidence_ref": "operator:review", "run_ids": [123],
                         "conclusions": ["failure"]},
    )
    parsed = FeedScheduleControl.model_validate(
        control([latest], [approval()], paused=False)
    )
    assert parsed.permits[-1].acknowledgement is not None


def test_acknowledged_definitive_failure_can_follow_stale_cleanup_flag():
    first = permit(
        status="DEFINITIVE_FAILURE",
        outcome={"classification": "DEFINITIVE_FAILURE", "recorded_at": stamp(2, 17, 30),
                 "safety_generation": 4, "receipt_generation": 2,
                 "publication_id": None, "instagram_media_id": None,
                 "cleanup_pending": True, "reconciliation_pending": False},
        acknowledgement={"reviewed_at": stamp(2, 18, 18),
                         "evidence_ref": "operator:review", "run_ids": [123],
                         "conclusions": ["failure"]},
    )
    following = permit(day=3, permit_id=str(uuid4()), owner_run_id=124,
                       authorization_key="schedule:124")
    parsed = FeedScheduleControl.model_validate(
        control([first, following], [approval()])
    )
    assert len(parsed.permits) == 2


def test_definitive_failure_with_stale_cleanup_flag_still_blocks_without_ack():
    first = permit(
        status="DEFINITIVE_FAILURE",
        outcome={"classification": "DEFINITIVE_FAILURE", "recorded_at": stamp(2, 17, 30),
                 "safety_generation": 4, "receipt_generation": 2,
                 "publication_id": None, "instagram_media_id": None,
                 "cleanup_pending": True, "reconciliation_pending": False},
    )
    following = permit(day=3, permit_id=str(uuid4()), owner_run_id=124,
                       authorization_key="schedule:124")
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(
            control([first, following], [approval()])
        )


def test_continuous_mode_replaces_revoked_unowned_future_slot():
    old = permit(day=2, status="REVOKED", owner_run_id=None,
                 owner_run_attempt=None, authorization_key=None, admitted_at=None,
                 continuous_approval_id=None, created_at=stamp(1, 12, 0),
                 revoked_at=stamp(1, 12, 1))
    following = permit(day=2, permit_id=str(uuid4()), created_at=stamp(2, 17, 18),
                       owner_run_id=124, authorization_key="schedule:124",
                       admitted_at=stamp(2, 17, 18))
    parsed = FeedScheduleControl.model_validate(
        control([old, following], [approval()])
    )
    assert parsed.permits[-1].owner_run_id == 124


def test_continuous_mode_rejects_second_owned_attempt_for_same_slot():
    first = permit()
    second = permit(permit_id=str(uuid4()), owner_run_id=124,
                    authorization_key="schedule:124")
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(control([first, second], [approval()]))


def test_legacy_future_permit_still_requires_review_to_follow():
    legacy = permit(day=2)
    legacy.pop("continuous_approval_id")
    legacy["created_at"] = stamp(1, 12, 0)
    legacy["status"] = "ARMED"
    legacy["owner_run_id"] = None
    legacy["owner_run_attempt"] = None
    legacy["authorization_key"] = None
    legacy["admitted_at"] = None
    second = dict(legacy)
    second.update({"permit_id": str(uuid4()), "slot_at": stamp(3),
                   "expires_at": stamp(3, 18, 17), "created_at": stamp(2, 19, 0)})
    with pytest.raises(ValidationError):
        FeedScheduleControl.model_validate(control([legacy, second]))
