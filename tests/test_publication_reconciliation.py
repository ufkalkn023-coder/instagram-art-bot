from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main
from src import history_tracker, publication_reconciliation, r2_media


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _single(
    status="PUBLISHING",
    *,
    publication_id="single-1",
    artwork_id="aic_1",
    container_id="container-1",
    published_media_id=None,
    started_at="2026-08-26T10:00:00Z",
):
    record = {
        "id": artwork_id,
        "publication_id": publication_id,
        "publication_type": "SINGLE",
        "status": status,
        "reserved_at": "2026-08-26T09:00:00Z",
    }
    if container_id is not None:
        record["container_id"] = container_id
    if started_at is not None:
        record["publish_started_at"] = started_at
    if published_media_id is not None:
        record["publish_response_media_id"] = published_media_id
    return record


def _carousel(status="PUBLISHING", *, container_id="parent-1", featured_count=8):
    publication_id = "carousel-1"
    records = []
    for index in range(featured_count + 1):
        records.append(
            {
                "id": "met_cover" if index == 0 else f"aic_{index}",
                "publication_id": publication_id,
                "publication_type": "CAROUSEL",
                "publication_role": "COVER" if index == 0 else "FEATURED",
                "featured_position": index if index else None,
                "status": status,
                "reserved_at": "2026-08-26T09:00:00Z",
                "publish_started_at": "2026-08-26T10:00:00Z",
                "container_id": container_id,
                "child_container_ids": [
                    f"child-{child}" for child in range(featured_count + 1)
                ],
            }
        )
    return records


def _backend(monkeypatch, records):
    history = {"posted_artworks": records}
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda value, etag: uploads.append((value, etag)),
    )
    return history, uploads


def test_stale_pending_expires_without_instagram_lookup(monkeypatch):
    record = _single(
        "PENDING", container_id=None, started_at=None
    )
    _backend(monkeypatch, [record])
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: pytest.fail("PENDING must not query Instagram"),
    )
    cleaned = []
    monkeypatch.setattr(
        publication_reconciliation.r2_media,
        "cleanup_publication_media",
        lambda publication_id, **kwargs: cleaned.append(publication_id)
        or r2_media.MediaCleanupSummary(
            publication_id, 1, 1, 0, True, kwargs["reason"]
        ),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert record["status"] == "EXPIRED"
    assert summary.confirmed_not_published == 1
    assert cleaned == ["single-1"]


@pytest.mark.parametrize("featured_count", [5, 6, 8])
def test_parent_published_status_without_media_identity_keeps_whole_carousel_ambiguous(
    monkeypatch, featured_count
):
    records = _carousel(featured_count=featured_count)
    _, uploads = _backend(monkeypatch, records)
    calls = []
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda container_id, token: calls.append((container_id, token)) or "PUBLISHED",
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert calls == [("parent-1", "token")]
    assert {record["status"] for record in records} == {"AMBIGUOUS"}
    assert len(uploads) == 1
    assert summary.still_ambiguous == 1


@pytest.mark.parametrize("featured_count", [5, 6, 8])
def test_carousel_boundary_persists_parent_and_children_on_every_row(
    monkeypatch, featured_count
):
    records = _carousel("PENDING", container_id=None, featured_count=featured_count)
    for record in records:
        record.pop("publish_started_at", None)
        record.pop("child_container_ids", None)
    _backend(monkeypatch, records)
    ids = [record["id"] for record in records]
    children = tuple(f"child-{index}" for index in range(featured_count + 1))

    assert history_tracker.start_publication_attempt(
        ids, "parent-1", children
    ) == featured_count + 1

    assert {record["status"] for record in records} == {"PUBLISHING"}
    assert {record["container_id"] for record in records} == {"parent-1"}
    assert {
        tuple(record["child_container_ids"]) for record in records
    } == {children}
    assert all(record["publish_started_at"] for record in records)


@pytest.mark.parametrize("featured_count", [5, 6, 8])
def test_child_finished_status_is_never_used_as_publication_evidence(
    monkeypatch, featured_count
):
    records = _carousel(featured_count=featured_count)
    _backend(monkeypatch, records)
    queried = []
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda container_id, token: queried.append(container_id) or "FINISHED",
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert queried == ["parent-1"]
    assert {record["status"] for record in records} == {"AMBIGUOUS"}
    assert summary.still_ambiguous == 1


@pytest.mark.parametrize("container_status", ["ERROR", "EXPIRED"])
def test_authoritative_non_publication_status_releases_whole_unit(
    monkeypatch, container_status
):
    records = _carousel("AMBIGUOUS")
    _backend(monkeypatch, records)
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: container_status,
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert {record["status"] for record in records} == {"EXPIRED"}
    assert summary.confirmed_not_published == 1


def test_confirmed_not_published_cleans_only_after_expired_cas_persists(
    monkeypatch,
):
    record = _single("AMBIGUOUS")
    history, uploads = _backend(monkeypatch, [record])
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: "ERROR",
    )
    cleanup_observations = []

    def cleanup(publication_id, **kwargs):
        cleanup_observations.append(
            (
                publication_id,
                record["status"],
                len(uploads),
                history[history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY][0][
                    "publication_id"
                ],
            )
        )
        return r2_media.MediaCleanupSummary(
            publication_id, 1, 1, 0, True, kwargs["reason"]
        )

    monkeypatch.setattr(
        publication_reconciliation.r2_media,
        "cleanup_publication_media",
        cleanup,
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert cleanup_observations == [("single-1", "EXPIRED", 1, "single-1")]
    assert history[history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY] == []
    assert summary.confirmed_not_published == 1
    assert summary.cleanup_deleted == 1
    assert summary.cleanup_failures == 0


def test_reconciliation_status_only_published_ambiguous_and_error_outcomes_retain_media(
    monkeypatch,
):
    records = [
        _single(
            publication_id="single-published",
            artwork_id="aic_published",
            container_id="published",
        ),
        _single(
            publication_id="single-ambiguous",
            artwork_id="aic_ambiguous",
            container_id="ambiguous",
        ),
        _single(
            publication_id="single-error",
            artwork_id="aic_error",
            container_id="error",
        ),
    ]
    _backend(monkeypatch, records)

    def status(container_id, token):
        if container_id == "published":
            return "PUBLISHED"
        if container_id == "ambiguous":
            return "IN_PROGRESS"
        raise OSError("network")

    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        status,
    )
    monkeypatch.setattr(
        publication_reconciliation.r2_media,
        "cleanup_publication_media",
        lambda *args, **kwargs: pytest.fail("non-expired media was cleaned"),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert records[0]["status"] == "AMBIGUOUS"
    assert records[1]["status"] == "AMBIGUOUS"
    assert records[2]["status"] == "PUBLISHING"
    assert summary.cleanup_inspected == 0


def test_failed_expired_history_cas_never_reaches_media_cleanup(monkeypatch):
    record = _single("AMBIGUOUS")
    history = {"posted_artworks": [record]}
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, "etag")
    )
    monkeypatch.setattr(
        history_tracker,
        "_upload_history",
        lambda *args: (_ for _ in ()).throw(OSError("CAS write failed")),
    )
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: "ERROR",
    )
    monkeypatch.setattr(
        publication_reconciliation.r2_media,
        "cleanup_publication_media",
        lambda *args, **kwargs: pytest.fail("cleanup ran before durable EXPIRED"),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert record["status"] == "AMBIGUOUS"
    assert history.get(history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY, []) == []
    assert summary.errors == 1
    assert summary.cleanup_inspected == 0


def test_expired_cleanup_failure_is_retryable_without_reopening_lifecycle(
    monkeypatch,
):
    record = _single("EXPIRED")
    record["expired_at"] = "2026-08-26T11:00:00Z"
    history = {
        "posted_artworks": [record],
        history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY: [
            {
                "publication_id": "single-1",
                "eligible_at": "2026-08-26T11:00:00Z",
                "reason": "container_status:ERROR",
            }
        ],
    }
    monkeypatch.setattr(
        history_tracker, "load_history_with_etag", lambda: (history, "etag")
    )
    monkeypatch.setattr(history_tracker, "_upload_history", lambda *args: None)
    attempts = []

    def cleanup(publication_id, **kwargs):
        attempts.append(publication_id)
        complete = len(attempts) == 2
        return r2_media.MediaCleanupSummary(
            publication_id,
            1,
            1 if complete else 0,
            0 if complete else 1,
            complete,
            kwargs["reason"],
        )

    monkeypatch.setattr(
        publication_reconciliation.r2_media,
        "cleanup_publication_media",
        cleanup,
    )
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: pytest.fail("EXPIRED cleanup queried Instagram"),
    )

    first = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )
    assert record["status"] == "EXPIRED"
    assert first.cleanup_failures == 1
    assert history[history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY]

    second = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )
    assert record["status"] == "EXPIRED"
    assert second.cleanup_failures == 0
    assert history[history_tracker.STAGING_MEDIA_CLEANUP_QUEUE_KEY] == []
    assert attempts == ["single-1", "single-1"]


def test_reconciliation_error_preserves_safe_state_and_scan_continues(monkeypatch):
    first = _single(publication_id="single-1", artwork_id="aic_1", container_id="bad")
    second = _single(publication_id="single-2", artwork_id="aic_2", container_id="good")
    _backend(monkeypatch, [first, second])

    def status(container_id, token):
        if container_id == "bad":
            raise OSError("network")
        return "PUBLISHED"

    monkeypatch.setattr(
        publication_reconciliation.instagram_poster, "get_container_status", status
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert first["status"] == "PUBLISHING"
    assert second["status"] == "AMBIGUOUS"
    assert summary.errors == 1
    assert summary.still_ambiguous == 1


def test_inconsistent_carousel_container_metadata_is_not_reconciled(monkeypatch):
    records = _carousel()
    records[-1]["container_id"] = "different-parent"
    _backend(monkeypatch, records)
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: pytest.fail("inconsistent publication must not query Instagram"),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert {record["status"] for record in records} == {"PUBLISHING"}
    assert summary.errors == 1


def test_durable_publish_response_confirms_without_network_call(monkeypatch):
    record = _single(published_media_id="media-1")
    history, uploads = _backend(monkeypatch, [record])
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: pytest.fail("durable media response is already authoritative"),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert record["status"] == "PUBLISHED"
    assert record["media_id"] == "media-1"
    assert history["publications"][0]["media_id"] == "media-1"
    assert history["publications"][0]["artwork_ids"] == ["aic_1"]
    assert history["grid_publication_count"] == 1
    assert len(uploads) == 1
    assert summary.confirmed_published == 1


def test_published_container_without_media_identity_stays_ambiguous(monkeypatch):
    reconciled = _single()
    assert "media_id" not in reconciled
    assert "publish_response_media_id" not in reconciled
    history, _ = _backend(monkeypatch, [reconciled])
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda *args: "PUBLISHED",
    )
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "_publish_container",
        lambda *args: pytest.fail("reconciliation must not call media_publish"),
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW
    )

    assert reconciled["status"] == "AMBIGUOUS"
    assert "media_id" not in reconciled
    assert "publish_response_media_id" not in reconciled
    assert history.get("publications", []) == []
    assert history_tracker.get_posted_ids() == {"aic_1"}
    assert summary.confirmed_published == 0
    assert summary.still_ambiguous == 1
    assert summary.results == (
        publication_reconciliation.PublicationReconciliationResult(
            publication_id="single-1",
            previous_status="PUBLISHING",
            outcome=publication_reconciliation.ReconciliationOutcome.STILL_AMBIGUOUS,
            evidence="container_status:PUBLISHED_media_identity_missing",
        ),
    )


def test_publish_response_survives_final_confirmation_failure(monkeypatch):
    record = _single()
    history = {"posted_artworks": [record]}
    uploads = 0
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: (history, "etag"))

    def upload(value, etag):
        nonlocal uploads
        uploads += 1
        if uploads == 2:
            raise OSError("R2 confirmation failed")

    monkeypatch.setattr(history_tracker, "_upload_history", upload)

    assert history_tracker.record_publish_response(["aic_1"], "media-1") == 1
    with pytest.raises(OSError, match="confirmation failed"):
        history_tracker.confirm_artwork("aic_1", "media-1")

    assert record["status"] == "PUBLISHING"
    assert record["publish_response_media_id"] == "media-1"


def test_reconciliation_is_bounded_and_unrelated_ambiguous_art_stays_quarantined(monkeypatch):
    records = [
        _single(
            "AMBIGUOUS",
            publication_id=f"single-{index}",
            artwork_id=f"aic_{index}",
            container_id=f"container-{index}",
        )
        for index in range(6)
    ]
    _backend(monkeypatch, records)
    calls = []
    monkeypatch.setattr(
        publication_reconciliation.instagram_poster,
        "get_container_status",
        lambda container_id, token: calls.append(container_id) or "FINISHED",
    )

    summary = publication_reconciliation.reconcile_publications(
        access_token="token", now=NOW, limit=3
    )

    assert summary.inspected == 3
    assert len(calls) == 3
    assert history_tracker.get_posted_ids() == {f"aic_{index}" for index in range(6)}


def test_conditional_conflict_reloads_and_re_evaluates_boundary_write(monkeypatch):
    first = {"posted_artworks": [_single("PENDING", container_id=None, started_at=None)]}
    second = {"posted_artworks": [_single("PENDING", container_id=None, started_at=None)]}
    loads = iter([(first, "etag-1"), (second, "etag-2")])
    uploads = []
    monkeypatch.setattr(history_tracker, "load_history_with_etag", lambda: next(loads))

    def upload(history, etag):
        uploads.append(etag)
        if etag == "etag-1":
            raise history_tracker.ConcurrentWriteError("conflict")

    monkeypatch.setattr(history_tracker, "_upload_history", upload)

    assert history_tracker.start_publication_attempt(
        ["aic_1"], "container-1"
    ) == 1

    assert first["posted_artworks"][0]["status"] == "PENDING"
    assert second["posted_artworks"][0]["status"] == "PUBLISHING"
    assert second["posted_artworks"][0]["container_id"] == "container-1"
    assert uploads == ["etag-1", "etag-2"]


def test_public_lifecycle_api_rejects_partial_carousel_transition(monkeypatch):
    records = _carousel("PENDING")
    _backend(monkeypatch, records)

    with pytest.raises(RuntimeError, match="whole publication unit"):
        history_tracker.start_publication_attempt(
            [record["id"] for record in records[:-1]], "parent-1"
        )

    assert {record["status"] for record in records} == {"PENDING"}


@pytest.mark.parametrize(
    ("status", "operation"),
    [
        (
            "PUBLISHED",
            lambda: history_tracker.mark_publication_not_published(
                ["aic_1"], "invalid", authoritative=True
            ),
        ),
        (
            "PUBLISHED",
            lambda: history_tracker.start_publication_attempt(
                ["aic_1"], "container-2"
            ),
        ),
        (
            "EXPIRED",
            lambda: history_tracker.confirm_publication(
                ["aic_1"], "media-1", authoritative=True
            ),
        ),
    ],
)
def test_terminal_lifecycle_transitions_are_rejected(monkeypatch, status, operation):
    record = _single(status)
    _backend(monkeypatch, [record])

    with pytest.raises(RuntimeError, match="Illegal|Cannot confirm"):
        operation()

    assert record["status"] == status


def test_reconcile_only_cli_performs_no_acquisition_or_publish(monkeypatch):
    monkeypatch.setattr(main, "validate_reconciliation_configuration", lambda: None)
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **kwargs: SimpleNamespace(
            inspected=1,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=1,
            errors=0,
        ),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda args: pytest.fail("reconcile-only acquired carousel artworks"),
    )

    assert main.main(["--reconcile-publications"]) == 0


def test_reconcile_only_cli_reports_cleanup_failure_separately(monkeypatch):
    monkeypatch.setattr(main, "validate_reconciliation_configuration", lambda: None)
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **kwargs: SimpleNamespace(
            inspected=0,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=0,
            errors=0,
            cleanup_inspected=1,
            cleanup_deleted=0,
            cleanup_failures=1,
        ),
    )

    assert main.main(["--reconcile-publications"]) == 1


def test_startup_reconciliation_runs_before_new_acquisition(monkeypatch):
    events = []
    monkeypatch.setattr(main, "validate_production_configuration", lambda: {})
    monkeypatch.setattr(
        main.publication_reconciliation,
        "reconcile_publications",
        lambda **kwargs: events.append("reconcile")
        or SimpleNamespace(
            inspected=0,
            confirmed_published=0,
            confirmed_not_published=0,
            still_ambiguous=1,
            errors=0,
        ),
    )
    monkeypatch.setattr(
        main,
        "run_carousel_post",
        lambda args: events.append("acquire"),
    )

    assert main.main(["--mode", "carousel"]) == 0
    assert events == ["reconcile", "acquire"]
