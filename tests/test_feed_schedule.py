"""Stage B permits use real sealed state and a local CAS storage fake."""

from datetime import datetime, timedelta, timezone

import pytest

from src import feed_schedule, publication_state
from src.production_authorization import (
    ProductionAuthorization,
    ProductionAuthorizationError,
)
from tests.test_publication_state import safety_candidate, store_for

NOW = datetime(2026, 10, 4, 17, 17, tzinfo=timezone.utc)
SHA = "a" * 40
LAST = "2026-10-02T17:17:00Z"
PUB = "66a4cac3-3db7-419d-bb05-74bd16a68801"


def fixture_store():
    raw = safety_candidate()
    receipt = {
        "publication_id": PUB,
        "instagram_media_id": "18112869197091685",
        "publication_type": "carousel",
        "record_origin": "RECOVERED",
        "identity_completeness": "COMPLETE",
        "occurred_at": LAST,
        "permalink": None,
        "workflow_run_id": 1,
        "evidence_ref": "fixture:completion",
        "historical_state": "PUBLISHED_CONFIRMED",
        "current_durable_lifecycle_state": "UNKNOWN",
        "artwork_positions": [
            {
                "position": n,
                "canonical_artwork_id": art,
                "instagram_child_media_id": None,
                "caption_label": None,
            }
            for n, art in enumerate(
                list(raw["published_artwork_protection"]["entries"])[:2], 1
            )
        ],
    }
    ledger = publication_state.seal(
        {
            "schema_version": 2,
            "generation": 1,
            "source_artifact": "fixture",
            "source_sha256": "a" * 64,
            "record_count": 1,
            "records": [receipt],
        }
    )
    return store_for(raw, ledger)


def manager(store):
    return feed_schedule.FeedScheduleManager(store, owned_media=lambda **_: set())


def initialize(store):
    state, etag = store.load_safety()
    ledger, _ = store.load_receipts()
    raw = state.model_dump(mode="json")
    raw["feed_schedule_control"] = feed_schedule.initial_control(
        state, ledger
    ).model_dump(mode="json")
    raw["generation"] += 1
    store.update_safety(publication_state.seal(raw), etag)


def armed(store):
    initialize(store)
    return manager(store).arm(
        expected_generation=2,
        approved_sha=SHA,
        main_sha=SHA,
        slot=NOW,
        expires_at=NOW + timedelta(minutes=60),
        now=NOW - timedelta(hours=1),
        review_ref="operator:fixture",
    )


def auth(run_id=123, **changes):
    values = dict(
        key=f"schedule:{run_id}",
        run_id=run_id,
        created_at=NOW,
        head_sha=SHA,
        repository=feed_schedule.FEED_REPOSITORY,
        workflow_path=feed_schedule.FEED_WORKFLOW,
        run_attempt=1,
    )
    return ProductionAuthorization(**(values | changes))


def test_legacy_readable_but_cannot_admit():
    store, client = fixture_store()
    assert store.load_safety()[0].feed_schedule_control is None
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(), now=NOW)
    assert client.puts == 0


def test_one_permit_claim_pauses_and_never_reopens():
    store, _ = fixture_store()
    armed(store)
    manager(store).admit(auth(), now=NOW)
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused is True
    assert control.permits[-1].status == "ADMITTED"
    assert control.permits[-1].owner_run_id == 123
    for candidate in [auth(), auth(124)]:
        with pytest.raises(feed_schedule.FeedScheduleError):
            manager(store).admit(candidate, now=NOW)
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(125), now=NOW + timedelta(days=1))


@pytest.mark.parametrize(
    "seconds,accepted", [(172799, False), (172800, True), (172801, True)]
)
def test_cooldown_boundary(seconds, accepted):
    store, _ = fixture_store()
    initialize(store)
    # The cooldown function also serves reservation, independently of the daily slot.
    state, _ = store.load_safety()
    ledger, _ = store.load_receipts()
    completion = datetime(2026, 10, 2, 17, 17, tzinfo=timezone.utc)
    if accepted:
        feed_schedule.require_cooldown(
            state, ledger, completion + timedelta(seconds=seconds)
        )
    else:
        with pytest.raises(feed_schedule.FeedScheduleError, match="COOLDOWN"):
            feed_schedule.require_cooldown(
                state, ledger, completion + timedelta(seconds=seconds)
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_attempt": 2},
        {"head_sha": "b" * 40},
        {"repository": "other/repo"},
        {"workflow_path": ".github/workflows/instagram_reels.yml"},
        {"created_at": NOW - timedelta(seconds=1)},
        {"created_at": NOW + timedelta(hours=1)},
    ],
)
def test_invalid_identity_cannot_claim(changes):
    store, _ = fixture_store()
    armed(store)
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(**changes), now=NOW)
    assert not store.load_safety()[0].feed_schedule_control.paused


def test_dependency_failure_expires_instead_of_replacing_next_day():
    store, _ = fixture_store()
    armed(store)
    # No application admission: setup failed or the concurrency pending run disappeared.
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(
            auth(124, created_at=NOW + timedelta(days=1)), now=NOW + timedelta(days=1)
        )
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).arm(
            expected_generation=3,
            approved_sha=SHA,
            main_sha=SHA,
            slot=NOW + timedelta(days=1),
            expires_at=NOW + timedelta(days=1, minutes=60),
            now=NOW + timedelta(hours=2),
            review_ref="operator:replacement",
        )
    assert store.load_safety()[0].feed_schedule_control.permits[-1].owner_run_id is None


def setup_runtime(monkeypatch):
    store, client = fixture_store()
    armed(store)
    monkeypatch.setenv("CLOUDFLARE_STATE_R2_BUCKET_NAME", "state")
    monkeypatch.setattr(publication_state, "PublicationStateStore", lambda: store)
    from src import r2_media, history_tracker

    monkeypatch.setattr(r2_media, "list_owned_publication_ids", lambda **_: set())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(history_tracker, "datetime", Clock)
    manager(store).admit(auth(), now=NOW)
    return store, client


def reserve():
    from src import history_tracker

    def art(identifier):
        return {"id": identifier, "title": "X", "artist": "Y", "museum": "Museum"}

    return history_tracker.reserve_carousel(
        art("cleveland_990001"),
        [art(f"aic_{990002 + n}") for n in range(5)],
        authorization=auth(),
    )


def ids(store):
    return [
        r["id"] for r in store.load_safety()[0].active_publication_state.posted_artworks
    ]


def test_reservation_and_publish_boundary_use_same_owner_cas(monkeypatch):
    from src import history_tracker

    store, client = setup_runtime(monkeypatch)
    publication_id = reserve()
    state = store.load_safety()[0]
    permit = state.feed_schedule_control.permits[-1]
    assert permit.status == "RESERVED"
    assert permit.publication_id == publication_id
    assert (
        state.active_publication_state.consumed_authorizations[0]["key"]
        == "schedule:123"
    )
    with pytest.raises(ProductionAuthorizationError):
        reserve()
    history_tracker.start_publication_attempt(
        ids(store),
        "parent",
        ["child"],
        expected_publication_id=publication_id,
        authorization=auth(),
    )
    assert (
        store.load_safety()[0].feed_schedule_control.permits[-1].status == "PUBLISHING"
    )
    before = client.puts
    # Even an idempotent boundary replay cannot authorize another HTTP publication.
    with pytest.raises(feed_schedule.FeedScheduleError):
        history_tracker.start_publication_attempt(
            ids(store), "parent", ["child"], expected_publication_id=publication_id
        )
    assert client.puts == before


@pytest.mark.parametrize(
    "operation", ["pause", "wrong_run", "wrong_sha", "missing_owner", "expired"]
)
def test_pre_publish_owner_rechecked_on_current_state(monkeypatch, operation):
    from src import history_tracker

    store, client = setup_runtime(monkeypatch)
    publication_id = reserve()
    authorization = auth()
    if operation == "pause":
        manager(store).pause(expected_generation=5, reason="emergency", now=NOW)
    elif operation == "wrong_run":
        authorization = auth(124)
    elif operation == "wrong_sha":
        authorization = auth(head_sha="b" * 40)
    elif operation == "missing_owner":
        authorization = None
    else:

        class LateClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(hours=1)

        monkeypatch.setattr(history_tracker, "datetime", LateClock)
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        history_tracker.start_publication_attempt(
            ids(store),
            "parent",
            expected_publication_id=publication_id,
            authorization=authorization,
        )
    assert client.puts == before
    assert {
        r["status"]
        for r in store.load_safety()[0].active_publication_state.posted_artworks
    } == {"PENDING"}


@pytest.mark.parametrize(
    "classification",
    ["SUCCESS", "DEFINITIVE_FAILURE", "AMBIGUOUS", "INCOMPLETE", "CANCELLED"],
)
def test_outcomes_pause_and_preserve_success_clock(monkeypatch, classification):
    from src import history_tracker

    store, _ = setup_runtime(monkeypatch)
    publication_id = reserve()
    artwork_ids = ids(store)
    if classification in {"SUCCESS", "AMBIGUOUS"}:
        history_tracker.start_publication_attempt(
            artwork_ids,
            "parent",
            expected_publication_id=publication_id,
            authorization=auth(),
        )
    if classification == "SUCCESS":
        history_tracker.record_publish_response(
            artwork_ids, "new-media", expected_publication_id=publication_id
        )
        history_tracker.confirm_carousel_publication(
            artwork_ids[0], artwork_ids[1:], "new-media", publication_id=publication_id
        )
    elif classification == "DEFINITIVE_FAILURE":
        history_tracker.mark_publication_not_published(
            artwork_ids,
            "staging_failed",
            authoritative=True,
            expected_publication_id=publication_id,
        )
    elif classification == "AMBIGUOUS":
        history_tracker.mark_artworks_ambiguous(
            artwork_ids, expected_publication_id=publication_id
        )
    manager(store).record_outcome(
        auth(),
        interrupted="CANCELLED" if classification == "CANCELLED" else None,
        now=NOW,
    )
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused
    assert control.permits[-1].status == classification
    assert control.permits[-1].outcome.classification == classification
    assert control.permits[-1].outcome.publication_id == publication_id
    assert control.latest_successful_feed_at == (
        "2026-10-04T17:17:00Z" if classification == "SUCCESS" else LAST
    )
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(
            auth(124, created_at=NOW + timedelta(days=1)), now=NOW + timedelta(days=1)
        )


@pytest.mark.parametrize("point", ["admission", "reservation", "boundary", "outcome"])
def test_uncertain_state_write_never_retries(monkeypatch, point):
    from src import history_tracker

    store, client = fixture_store()
    armed(store)
    if point == "admission":

        def operation():
            return manager(store).admit(auth(), now=NOW)
    else:
        store, client = setup_runtime(monkeypatch)
        if point == "reservation":
            operation = reserve
        else:
            publication_id = reserve()
            if point == "boundary":

                def operation():
                    return history_tracker.start_publication_attempt(
                        ids(store),
                        "parent",
                        expected_publication_id=publication_id,
                        authorization=auth(),
                    )
            else:

                def operation():
                    return manager(store).record_outcome(auth(), now=NOW)

    client.uncertain = True
    before = client.puts
    with pytest.raises(publication_state.StateWriteUncertainError):
        operation()
    assert client.puts == before + 1
    if point != "admission":
        assert store.load_safety()[0].feed_schedule_control.paused


def test_cancellation_before_reservation_is_paused(monkeypatch):
    store, _ = setup_runtime(monkeypatch)
    manager(store).record_outcome(auth(), interrupted="CANCELLED", now=NOW)
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert permit.status == "CANCELLED"
    assert permit.publication_id is None


def test_crash_leaves_owner_paused_with_incomplete_evidence(monkeypatch):
    store, _ = setup_runtime(monkeypatch)
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused and control.permits[-1].outcome is None
    assert control.permits[-1].status == "ADMITTED"
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(124), now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("paused", "false"),
        ("next_eligible_at", "2026-10-02T17:17:00Z"),
        ("unknown", "unsafe"),
    ],
)
def test_malformed_control_cannot_load(field, value):
    store, client = fixture_store()
    initialize(store)
    raw = client.objects[publication_state.SAFETY_KEY]
    raw["feed_schedule_control"][field] = value
    client.objects[publication_state.SAFETY_KEY] = publication_state.seal(raw)
    with pytest.raises(publication_state.StateValidationError):
        store.load_safety()


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_run_attempt", True),
        ("approved_sha", "bad"),
        ("authorization_key", "schedule:999"),
        ("owner_run_id", 124),
        ("expires_at", "2026-10-05T17:17:00Z"),
        ("slot_at", "2026-10-04T17:18:00Z"),
        ("status", "ARMED"),
    ],
)
def test_malformed_or_rewritten_owner_cannot_be_saved(monkeypatch, field, value):
    store, _ = setup_runtime(monkeypatch)
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["feed_schedule_control"]["permits"][-1][field] = value
    raw["generation"] += 1
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(raw), etag)


@pytest.mark.parametrize(
    "blocker",
    [
        "cleanup_feed",
        "cleanup_reel",
        "receipt_pending",
        "owned_feed",
        "owned_reel",
        "recovery",
        "expiration",
        "digest",
    ],
)
def test_state_blockers_prevent_admission(blocker):
    store, client = fixture_store()
    armed(store)
    raw = client.objects[publication_state.SAFETY_KEY]
    active = raw["active_publication_state"]
    if blocker == "cleanup_feed":
        active["staging_media_cleanup_queue"] = [
            {
                "publication_id": PUB,
                "eligible_at": "2026-10-04T17:17:00Z",
                "reason": "fixture",
            }
        ]
    elif blocker == "cleanup_reel":
        active["reel_staging_cleanup_queue"] = [
            {
                "publication_id": PUB,
                "eligible_at": "2026-10-04T17:17:00Z",
                "reason": "fixture",
            }
        ]
    elif blocker == "receipt_pending":
        active["receipt_sync_pending"] = [PUB]
    elif blocker == "recovery":
        store.require_recovery_baseline = True
    elif blocker == "expiration":
        client.expiration_metadata[publication_state.SAFETY_KEY] = {
            "Expiration": "tomorrow"
        }
    client.objects[publication_state.SAFETY_KEY] = publication_state.seal(raw)
    if blocker == "digest":
        client.objects[publication_state.SAFETY_KEY]["payload_sha256"] = "a" * 64
    target = feed_schedule.FeedScheduleManager(
        store,
        owned_media=lambda **kw: {PUB}
        if blocker == ("owned_reel" if kw.get("reel") else "owned_feed")
        else set(),
    )
    before = client.puts
    with pytest.raises(
        (
            feed_schedule.FeedScheduleError,
            publication_state.StateValidationError,
            RuntimeError,
        )
    ):
        target.admit(auth(), now=NOW)
    assert client.puts == before


@pytest.mark.parametrize("status", ["PENDING", "PUBLISHING", "AMBIGUOUS"])
def test_unresolved_transaction_prevents_new_permit_and_ack(monkeypatch, status):
    from src import history_tracker

    store, client = setup_runtime(monkeypatch)
    publication_id = reserve()
    if status != "PENDING":
        history_tracker.start_publication_attempt(
            ids(store),
            "parent",
            expected_publication_id=publication_id,
            authorization=auth(),
        )
    if status == "AMBIGUOUS":
        history_tracker.mark_artworks_ambiguous(
            ids(store), expected_publication_id=publication_id
        )
    before = client.puts
    with pytest.raises(RuntimeError):
        manager(store).acknowledge(
            expected_generation=store.load_safety()[0].generation,
            evidence_ref="operator:review",
            audit_runs=lambda _: [],
            now=NOW + timedelta(hours=2),
        )
    assert client.puts == before


def test_reviewed_failure_allows_only_one_new_explicit_permit():
    from tests.test_manage_feed_schedule import GitHub

    store, _ = fixture_store()
    armed(store)
    manager(store).admit(auth(), now=NOW)
    manager(store).record_outcome(auth(), now=NOW)
    manager(store).acknowledge(
        expected_generation=5,
        evidence_ref="operator:failed-run",
        audit_runs=GitHub().audit_runs,
        now=NOW + timedelta(hours=2),
    )
    manager(store).arm(
        expected_generation=6,
        approved_sha=SHA,
        main_sha=SHA,
        slot=NOW + timedelta(days=1),
        expires_at=NOW + timedelta(days=1, minutes=60),
        review_ref="operator:new-approval",
        now=NOW + timedelta(hours=3),
    )
    manager(store).admit(
        auth(124, created_at=NOW + timedelta(days=1)), now=NOW + timedelta(days=1)
    )
    assert len(store.load_safety()[0].feed_schedule_control.permits) == 2
    assert store.load_safety()[0].feed_schedule_control.paused


def test_pre_admission_failed_run_must_be_reviewed_after_expiry():
    from tests.test_manage_feed_schedule import GitHub

    store, _ = fixture_store()
    armed(store)
    manager(store).acknowledge(
        expected_generation=3,
        evidence_ref="operator:setup-failure",
        audit_runs=GitHub().audit_runs,
        now=NOW + timedelta(hours=2),
    )
    permit = store.load_safety()[0].feed_schedule_control.permits[-1]
    assert permit.status == "EXPIRED"
    assert permit.owner_run_id is None
    assert permit.acknowledgement.run_ids == [123]
    assert permit.acknowledgement.conclusions == ["failure"]


def test_cas_conflict_does_not_claim_duplicate_permit(monkeypatch):
    store, client = fixture_store()
    armed(store)
    original = store.update_safety

    def competing_claim(value, etag):
        monkeypatch.setattr(store, "update_safety", original)
        manager(store).admit(auth(124), now=NOW)
        original(value, etag)

    monkeypatch.setattr(store, "update_safety", competing_claim)
    with pytest.raises(publication_state.StateConflictError):
        manager(store).admit(auth(), now=NOW)
    assert store.load_safety()[0].feed_schedule_control.permits[-1].owner_run_id == 124
    assert client.puts == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "workflow_dispatch"),
        ("head_branch", "other"),
        ("head_sha", "b" * 40),
        ("path", ".github/workflows/instagram_reels.yml"),
        ("run_attempt", 2),
        ("status", "queued"),
        ("conclusion", None),
    ],
)
def test_nonterminal_or_wrong_identity_run_cannot_be_acknowledged(field, value):
    from tests.test_manage_feed_schedule import GitHub

    store, client = fixture_store()
    armed(store)
    runs = GitHub().audit_runs(None)
    runs[0][field] = value
    before = client.puts
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).acknowledge(
            expected_generation=3,
            evidence_ref="operator:review",
            audit_runs=lambda _: runs,
            now=NOW + timedelta(hours=2),
        )
    assert client.puts == before


def test_success_clock_cannot_be_forged_in_normal_write():
    store, _ = fixture_store()
    initialize(store)
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["feed_schedule_control"]["latest_successful_feed_at"] = "2026-10-03T17:17:00Z"
    raw["feed_schedule_control"]["next_eligible_at"] = "2026-10-05T17:17:00Z"
    raw["generation"] += 1
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(raw), etag)


def test_old_recovered_unknown_time_does_not_hide_verified_live_completion(monkeypatch):
    from src import history_tracker

    store, client = setup_runtime(monkeypatch)
    publication_id = reserve()
    artwork_ids = ids(store)
    history_tracker.start_publication_attempt(
        artwork_ids,
        "parent",
        expected_publication_id=publication_id,
        authorization=auth(),
    )
    history_tracker.record_publish_response(
        artwork_ids, "new-media", expected_publication_id=publication_id
    )
    history_tracker.confirm_carousel_publication(
        artwork_ids[0], artwork_ids[1:], "new-media", publication_id=publication_id
    )
    state, _ = store.load_safety()
    raw = client.objects[publication_state.RECEIPTS_KEY]
    import copy

    unknown = copy.deepcopy(raw["records"][0])
    unknown.update(
        publication_id="old-recovered", instagram_media_id="old-media", occurred_at=None
    )
    raw["records"].append(unknown)
    raw["record_count"] += 1
    client.objects[publication_state.RECEIPTS_KEY] = publication_state.seal(raw)
    ledger, _ = store.load_receipts()
    feed_schedule.require_cooldown(state, ledger, NOW + timedelta(hours=48))


@pytest.mark.parametrize("point", ["admission", "reservation", "boundary", "outcome"])
def test_write_committed_but_response_lost_stays_consumed_and_paused(
    monkeypatch, point
):
    from botocore.exceptions import EndpointConnectionError
    from src import history_tracker

    store, client = fixture_store()
    armed(store)
    if point == "admission":

        def operation():
            manager(store).admit(auth(), now=NOW)
    else:
        store, client = setup_runtime(monkeypatch)
        if point == "reservation":
            operation = reserve
        else:
            publication_id = reserve()
            if point == "boundary":

                def operation():
                    history_tracker.start_publication_attempt(
                        ids(store),
                        "parent",
                        expected_publication_id=publication_id,
                        authorization=auth(),
                    )
            else:

                def operation():
                    manager(store).record_outcome(auth(), now=NOW)

    original = client.put_object

    def lost_response(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://fixture.invalid")

    monkeypatch.setattr(client, "put_object", lost_response)
    before = client.puts
    with pytest.raises(publication_state.StateWriteUncertainError):
        operation()
    assert client.puts == before + 1
    control = store.load_safety()[0].feed_schedule_control
    assert control.paused
    assert control.permits[-1].owner_run_id == 123
    with pytest.raises(feed_schedule.FeedScheduleError):
        manager(store).admit(auth(124), now=NOW)
    assert client.puts == before + 1


def test_false_success_without_finalized_publication_is_rejected(monkeypatch):
    from src import history_tracker

    store, _ = setup_runtime(monkeypatch)
    publication_id = reserve()
    history_tracker.start_publication_attempt(
        ids(store),
        "parent",
        expected_publication_id=publication_id,
        authorization=auth(),
    )
    state, etag = store.load_safety()
    raw = state.model_dump(mode="json")
    raw["generation"] += 1
    permit = raw["feed_schedule_control"]["permits"][-1]
    permit["status"] = "SUCCESS"
    permit["outcome"] = {
        "classification": "SUCCESS",
        "recorded_at": "2026-10-04T17:17:00Z",
        "safety_generation": raw["generation"],
        "receipt_generation": 1,
        "publication_id": publication_id,
        "instagram_media_id": "fabricated",
        "cleanup_pending": False,
        "reconciliation_pending": False,
    }
    with pytest.raises(publication_state.StateValidationError):
        store.update_safety(publication_state.seal(raw), etag)
