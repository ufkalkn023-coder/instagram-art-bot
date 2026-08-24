"""Read-only history discovery and bounded Instagram Insights collection."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import config
from src.insights_storage import (
    InsightsConcurrencyError,
    InsightsStorage,
    InsightsStorageError,
    parse_aware_timestamp,
    utc_timestamp,
)
from src.instagram_insights import InstagramInsightsClient, InstagramInsightsError
from src.models import PublicationRecord
from pydantic import ValidationError

logger = logging.getLogger(__name__)

SLOT_HORIZONS_HOURS = {24: 24 * 7, 72: 24 * 14, 168: 24 * 30}


@dataclass
class CollectionSummary:
    publications_scanned: int = 0
    publications_eligible: int = 0
    already_complete: int = 0
    api_requests: int = 0
    snapshots_written: int = 0
    availability_pending: int = 0
    api_failures: int = 0
    write_conflicts: int = 0
    missed_slots: int = 0
    invalid_publications: int = 0
    errors: list[str] = field(default_factory=list)


def select_due_slot(age_hours: float, existing_slots: set[int]) -> tuple[int | None, int]:
    """Select the highest due, still-defensible slot and count expired gaps."""
    missed = sum(
        1
        for target, horizon in SLOT_HORIZONS_HOURS.items()
        if target not in existing_slots and age_hours >= target and age_hours > horizon
    )
    due = [
        target
        for target, horizon in SLOT_HORIZONS_HOURS.items()
        if target not in existing_slots and target <= age_hours <= horizon
    ]
    return (max(due) if due else None), missed


def _publication(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    try:
        validated = PublicationRecord.model_validate(record)
    except ValidationError:
        return None
    posted_at = parse_aware_timestamp(validated.posted_at)
    if posted_at is None:
        return None
    return {
        "id": validated.id,
        "media_id": validated.media_id,
        "type": validated.type,
        "posted_at": posted_at,
    }


class InsightsCollector:
    def __init__(self, storage: InsightsStorage, client: InstagramInsightsClient | None, now=None):
        self._storage = storage
        self._client = client
        self._now = now or datetime.now(timezone.utc)
        if self._now.tzinfo is None or self._now.utcoffset() is None:
            raise ValueError("Collector time must be timezone-aware")
        self._now = self._now.astimezone(timezone.utc)

    def run(self, dry_run: bool = False) -> CollectionSummary:
        history = self._storage.load_history()
        publications = history.get("publications", [])
        if "publications" in history and not isinstance(publications, list):
            raise InsightsStorageError("Posted history publications must be a list")

        summary = CollectionSummary()
        for raw_publication in publications:
            summary.publications_scanned += 1
            publication = _publication(raw_publication)
            if publication is None:
                summary.invalid_publications += 1
                logger.warning("Skipping malformed publication record during Insights collection")
                continue

            age_hours = (self._now - publication["posted_at"]).total_seconds() / 3600
            if age_hours < 0:
                summary.invalid_publications += 1
                logger.warning("Skipping future publication %s during Insights collection", publication["id"])
                continue

            key, partition, etag = self._storage.load_partition(publication["posted_at"])
            existing = [item for item in partition["snapshots"] if item["publication_id"] == publication["id"]]
            media_ids = {item["media_id"] for item in existing}
            if media_ids and media_ids != {publication["media_id"]}:
                raise InsightsStorageError("Conflicting analytics snapshot identity")
            existing_slots = {item["target_age_hours"] for item in existing}
            target, missed = select_due_slot(age_hours, existing_slots)
            summary.missed_slots += missed
            if target is None:
                if existing_slots == set(SLOT_HORIZONS_HOURS):
                    summary.already_complete += 1
                continue

            summary.publications_eligible += 1
            if dry_run:
                continue
            if self._client is None:
                raise RuntimeError("Insights client is required outside dry-run mode")

            summary.api_requests += 1
            try:
                response = self._client.fetch_media_insights(publication["media_id"])
            except InstagramInsightsError as exc:
                summary.api_failures += 1
                summary.errors.append(f"{publication['id']}: {exc}")
                logger.warning("Insights collection failed for publication %s: %s", publication["id"], exc)
                continue

            if not response.metrics:
                summary.availability_pending += 1
                logger.info("Insights availability pending for publication %s", publication["id"])
                continue

            snapshot = {
                "publication_id": publication["id"],
                "media_id": publication["media_id"],
                "target_age_hours": target,
                "captured_at": utc_timestamp(self._now),
                "actual_age_hours": round(age_hours, 3),
                "metrics": response.metrics,
                "requested_metrics": list(response.requested_metrics),
                "returned_metrics": list(response.returned_metrics),
                "missing_metrics": list(response.missing_metrics),
                "api_version": config.INSTAGRAM_GRAPH_API_VERSION,
            }
            try:
                self._storage.append_snapshot(key, partition, etag, snapshot)
            except InsightsConcurrencyError as exc:
                summary.write_conflicts += 1
                summary.errors.append(f"{publication['id']}: {exc}")
                logger.warning("Insights write conflict for publication %s", publication["id"])
                continue
            summary.snapshots_written += 1
        return summary
