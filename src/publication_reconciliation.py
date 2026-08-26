"""Bounded, conservative reconciliation for Instagram publication units."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging

from src import history_tracker, instagram_poster


logger = logging.getLogger(__name__)

STARTUP_RECONCILIATION_LIMIT = 20
MANUAL_RECONCILIATION_LIMIT = 100
STARTUP_RECONCILIATION_MAX_AGE = timedelta(days=30)
PUBLISHING_RECONCILIATION_GRACE = timedelta(minutes=50)
MAX_LOGICAL_NETWORK_CALLS_PER_UNIT = 1


class ReconciliationOutcome(str, Enum):
    CONFIRMED_PUBLISHED = "CONFIRMED_PUBLISHED"
    CONFIRMED_NOT_PUBLISHED = "CONFIRMED_NOT_PUBLISHED"
    STILL_AMBIGUOUS = "STILL_AMBIGUOUS"
    RECONCILIATION_ERROR = "RECONCILIATION_ERROR"


@dataclass(frozen=True)
class PublicationReconciliationResult:
    publication_id: str
    previous_status: str
    outcome: ReconciliationOutcome
    evidence: str


@dataclass(frozen=True)
class ReconciliationSummary:
    inspected: int
    confirmed_published: int
    confirmed_not_published: int
    still_ambiguous: int
    errors: int
    results: tuple[PublicationReconciliationResult, ...]


def _result(
    unit: history_tracker.PublicationUnit,
    outcome: ReconciliationOutcome,
    evidence: str,
) -> PublicationReconciliationResult:
    previous = unit.status.value if unit.status is not None else ",".join(unit.record_statuses)
    logger.info(
        "publication_reconciliation publication_id=%s previous=%s result=%s evidence=%s",
        unit.publication_id,
        previous,
        outcome.value,
        evidence,
    )
    return PublicationReconciliationResult(
        publication_id=unit.publication_id,
        previous_status=previous,
        outcome=outcome,
        evidence=evidence,
    )


def _reconcile_unit(
    unit: history_tracker.PublicationUnit,
    *,
    access_token: str,
    now: datetime,
) -> PublicationReconciliationResult | None:
    if unit.status is None:
        return _result(
            unit,
            ReconciliationOutcome.RECONCILIATION_ERROR,
            "inconsistent_publication_unit_metadata_or_state",
        )

    if unit.status is history_tracker.PublicationStatus.PENDING:
        if unit.reserved_at is None:
            return _result(
                unit,
                ReconciliationOutcome.RECONCILIATION_ERROR,
                "pending_reservation_timestamp_missing",
            )
        if now - unit.reserved_at < history_tracker.PENDING_RESERVATION_TTL:
            return None
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=history_tracker.PublicationStatus.EXPIRED,
            result=ReconciliationOutcome.CONFIRMED_NOT_PUBLISHED.value,
            evidence="pending_ttl_expired_before_publish_boundary",
            authoritative=True,
            expected_status=unit.status,
            now=now,
        )
        return _result(
            unit,
            ReconciliationOutcome.CONFIRMED_NOT_PUBLISHED,
            "pending_ttl_expired_before_publish_boundary",
        )

    if (
        unit.status is history_tracker.PublicationStatus.PUBLISHING
        and unit.publish_started_at is not None
        and now - unit.publish_started_at < PUBLISHING_RECONCILIATION_GRACE
    ):
        return None

    if unit.publish_response_media_id:
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=history_tracker.PublicationStatus.PUBLISHED,
            result=ReconciliationOutcome.CONFIRMED_PUBLISHED.value,
            evidence="durable_media_publish_response_id",
            media_id=unit.publish_response_media_id,
            authoritative=True,
            expected_status=unit.status,
            now=now,
        )
        return _result(
            unit,
            ReconciliationOutcome.CONFIRMED_PUBLISHED,
            "durable_media_publish_response_id",
        )

    if not unit.container_id:
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=history_tracker.PublicationStatus.AMBIGUOUS,
            result=ReconciliationOutcome.STILL_AMBIGUOUS.value,
            evidence="creation_container_id_missing",
            expected_status=unit.status,
            now=now,
        )
        return _result(
            unit,
            ReconciliationOutcome.STILL_AMBIGUOUS,
            "creation_container_id_missing",
        )

    try:
        container_status = instagram_poster.get_container_status(
            unit.container_id, access_token
        )
    except Exception as error:
        evidence = f"container_status_error:{type(error).__name__}"
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=None,
            result=ReconciliationOutcome.RECONCILIATION_ERROR.value,
            evidence=evidence,
            expected_status=unit.status,
            now=now,
        )
        return _result(unit, ReconciliationOutcome.RECONCILIATION_ERROR, evidence)

    if container_status == "PUBLISHED":
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=history_tracker.PublicationStatus.PUBLISHED,
            result=ReconciliationOutcome.CONFIRMED_PUBLISHED.value,
            evidence="container_status:PUBLISHED",
            authoritative=True,
            expected_status=unit.status,
            now=now,
        )
        return _result(
            unit,
            ReconciliationOutcome.CONFIRMED_PUBLISHED,
            "container_status:PUBLISHED",
        )

    if container_status in {"ERROR", "EXPIRED"}:
        evidence = f"container_status:{container_status}"
        history_tracker.record_reconciliation_result(
            unit.artwork_ids,
            target_status=history_tracker.PublicationStatus.EXPIRED,
            result=ReconciliationOutcome.CONFIRMED_NOT_PUBLISHED.value,
            evidence=evidence,
            authoritative=True,
            expected_status=unit.status,
            now=now,
        )
        return _result(
            unit, ReconciliationOutcome.CONFIRMED_NOT_PUBLISHED, evidence
        )

    evidence = f"container_status:{container_status}"
    history_tracker.record_reconciliation_result(
        unit.artwork_ids,
        target_status=history_tracker.PublicationStatus.AMBIGUOUS,
        result=ReconciliationOutcome.STILL_AMBIGUOUS.value,
        evidence=evidence,
        expected_status=unit.status,
        now=now,
    )
    return _result(unit, ReconciliationOutcome.STILL_AMBIGUOUS, evidence)


def reconcile_publications(
    *,
    access_token: str,
    limit: int = STARTUP_RECONCILIATION_LIMIT,
    max_age: timedelta | None = STARTUP_RECONCILIATION_MAX_AGE,
    now: datetime | None = None,
) -> ReconciliationSummary:
    """Reconcile bounded units without allowing one failure to abort the scan."""
    if not access_token:
        raise ValueError("Instagram access token is required for reconciliation")
    reconciliation_time = now or datetime.now(timezone.utc)
    if reconciliation_time.tzinfo is None or reconciliation_time.utcoffset() is None:
        raise ValueError("Reconciliation time must be timezone-aware")
    reconciliation_time = reconciliation_time.astimezone(timezone.utc)
    units = history_tracker.list_unresolved_publication_units(
        limit=limit,
        now=reconciliation_time,
        max_age=max_age,
    )
    results: list[PublicationReconciliationResult] = []
    for unit in units:
        try:
            result = _reconcile_unit(
                unit,
                access_token=access_token,
                now=reconciliation_time,
            )
        except Exception as error:
            logger.exception(
                "publication_reconciliation publication_id=%s result=%s error=%s",
                unit.publication_id,
                ReconciliationOutcome.RECONCILIATION_ERROR.value,
                type(error).__name__,
            )
            result = _result(
                unit,
                ReconciliationOutcome.RECONCILIATION_ERROR,
                f"history_update_error:{type(error).__name__}",
            )
        if result is not None:
            results.append(result)

    return ReconciliationSummary(
        inspected=len(units),
        confirmed_published=sum(
            result.outcome is ReconciliationOutcome.CONFIRMED_PUBLISHED
            for result in results
        ),
        confirmed_not_published=sum(
            result.outcome is ReconciliationOutcome.CONFIRMED_NOT_PUBLISHED
            for result in results
        ),
        still_ambiguous=sum(
            result.outcome is ReconciliationOutcome.STILL_AMBIGUOUS
            for result in results
        ),
        errors=sum(
            result.outcome is ReconciliationOutcome.RECONCILIATION_ERROR
            for result in results
        ),
        results=tuple(results),
    )
