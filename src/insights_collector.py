"""Read-only media association and bounded Instagram Insights collection."""

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
from src.insights_snapshot import inferred_snapshot_status, learning_snapshot_components
from src.instagram_insights import InstagramInsightsClient, InstagramInsightsError
from src.models import PublicationRecord
from src.reel_analytics import LocalReel, derive_engagement_rates, match_recent_media
from pydantic import ValidationError

logger = logging.getLogger(__name__)
LEGACY_UTC_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Each slot is collected from its target until the next target. A delayed run
# captures the newest defensible slot and reports earlier windows as missed.
SLOT_WINDOWS_HOURS = {1: 6, 6: 24, 24: 72, 72: 168, 168: 24 * 30}


@dataclass
class CollectionSummary:
    media_discovered: int = 0
    matched_new: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    mapped_media: int = 0
    publications_scanned: int = 0
    publications_eligible: int = 0
    already_complete: int = 0
    api_calls: int = 0
    snapshots_written: int = 0
    missed_slot_markers_written: int = 0
    learning_complete_snapshots: int = 0
    partial_snapshots: int = 0
    permanently_unavailable: int = 0
    availability_pending: int = 0
    api_failures: int = 0
    write_conflicts: int = 0
    missed_slots: int = 0
    invalid_publications: int = 0
    rate_limit_usage: dict[str, int | float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def api_requests(self) -> int:
        """Backward-compatible summary name."""
        return self.api_calls


def _merge_usage(target: dict[str, int | float], update: dict[str, int | float]) -> None:
    for key, value in update.items():
        target[key] = max(target.get(key, 0), value)


def _compact_ids(values: tuple[str, ...], limit: int = 10) -> str:
    visible = ",".join(values[:limit])
    remaining = len(values) - limit
    return f"{visible},+{remaining}" if remaining > 0 else visible


def select_due_slot(age_hours: float, existing_slots: set[int]) -> tuple[int | None, int]:
    """Select the newest open target window and count expired gaps."""
    if age_hours < 0 or not isinstance(existing_slots, set):
        return None, 0
    missed = sum(
        1
        for target, deadline in SLOT_WINDOWS_HOURS.items()
        if target not in existing_slots and age_hours >= deadline
    )
    due = [
        target
        for target, deadline in SLOT_WINDOWS_HOURS.items()
        if target not in existing_slots and target <= age_hours < deadline
    ]
    return (max(due) if due else None), missed


def _legacy_publication(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    normalized = dict(record)
    raw_posted_at = normalized.get("posted_at")
    if parse_aware_timestamp(raw_posted_at) is None and isinstance(raw_posted_at, str):
        try:
            legacy_timestamp = datetime.strptime(raw_posted_at, LEGACY_UTC_TIMESTAMP_FORMAT)
        except ValueError:
            pass
        else:
            # The legacy writer used time.strftime in GitHub's UTC ubuntu-latest
            # environment. Normalize only its exact format; other naive values
            # remain invalid rather than having a timezone guessed for them.
            normalized["posted_at"] = utc_timestamp(legacy_timestamp.replace(tzinfo=timezone.utc))
    try:
        validated = PublicationRecord.model_validate(normalized)
    except ValidationError:
        return None
    posted_at = parse_aware_timestamp(validated.posted_at)
    if posted_at is None:
        return None
    canonical_id = validated.artwork_ids[0]
    return {
        "canonical_artwork_id": canonical_id,
        "reel_id": validated.id,
        "instagram_media_id": validated.media_id,
        "published_at": utc_timestamp(posted_at),
        "matched_at": utc_timestamp(posted_at),
        "match_method": "bot_publication",
    }


def merge_associations(existing: list[dict[str, Any]], additions: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Add new one-to-one associations without changing an existing mapping."""
    output = list(existing)
    reels = {item.get("reel_id") for item in output}
    media = {item.get("instagram_media_id") for item in output}
    for item in additions:
        if item["reel_id"] in reels or item["instagram_media_id"] in media:
            raise InsightsStorageError("Duplicate media association")
        output.append(item)
        reels.add(item["reel_id"])
        media.add(item["instagram_media_id"])
    return output


def manual_association(
    storage: InsightsStorage,
    client: InstagramInsightsClient,
    local_reels: tuple[LocalReel, ...],
    reel_id: str,
    media_id: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist an explicit link, replacing only a prior automatic link."""
    local = next((item for item in local_reels if item.reel_id == reel_id), None)
    if local is None:
        raise InsightsStorageError(f"Unknown local Reel ID: {reel_id}")
    media = client.fetch_media(media_id)
    data, etag = storage.load_associations()
    associations = list(data["associations"])
    media_owner = next((item for item in associations if item["instagram_media_id"] == media.id), None)
    if media_owner is not None and media_owner["reel_id"] != reel_id:
        raise InsightsStorageError("Instagram media ID is already linked to another local Reel")
    existing = next((item for item in associations if item["reel_id"] == reel_id), None)
    if existing is not None and existing["instagram_media_id"] == media.id and existing["match_method"] == "manual":
        return existing, False
    if existing is not None and existing["match_method"] == "manual":
        raise InsightsStorageError("Local Reel already has a different manual association")
    if existing is not None:
        associations.remove(existing)
    matched_at = now or datetime.now(timezone.utc)
    association = {
        "canonical_artwork_id": local.canonical_artwork_id,
        "reel_id": local.reel_id,
        "instagram_media_id": media.id,
        **({"permalink": media.permalink} if media.permalink else {}),
        "published_at": media.timestamp,
        "matched_at": utc_timestamp(matched_at),
        "match_method": "manual",
    }
    associations.append(association)
    storage.write_associations({"schema_version": 1, "associations": associations}, etag)
    return association, True


class InsightsCollector:
    def __init__(self, storage: InsightsStorage, client: InstagramInsightsClient | None, now=None):
        self._storage = storage
        self._client = client
        self._now = now or datetime.now(timezone.utc)
        if self._now.tzinfo is None or self._now.utcoffset() is None:
            raise ValueError("Collector time must be timezone-aware")
        self._now = self._now.astimezone(timezone.utc)

    def _append_attempts(
        self,
        key: str,
        partition: dict[str, Any],
        etag: str | None,
        snapshots: list[dict[str, Any]],
        summary: CollectionSummary,
        reel_id: str,
    ) -> bool:
        if not snapshots:
            return True
        try:
            append_many = getattr(self._storage, "append_snapshots", None)
            if callable(append_many):
                append_many(key, partition, etag, snapshots)
            else:
                for snapshot in snapshots:
                    self._storage.append_snapshot(key, partition, etag, snapshot)
        except InsightsConcurrencyError as exc:
            summary.write_conflicts += 1
            summary.errors.append(f"{reel_id}: {exc}")
            logger.warning("Insights write conflict for local Reel %s", reel_id)
            return False
        for snapshot in snapshots:
            if snapshot.get("failure_category") == "missed_collection_window":
                summary.missed_slot_markers_written += 1
                continue
            summary.snapshots_written += 1
            status = inferred_snapshot_status(snapshot)
            if status == "learning_complete":
                summary.learning_complete_snapshots += 1
            elif status == "partial":
                summary.partial_snapshots += 1
            else:
                summary.permanently_unavailable += 1
        return True

    def _snapshot_record(
        self,
        target_record: dict[str, Any],
        target_slot: int,
        age_seconds: float,
        *,
        metrics: dict[str, int | float],
        missing_metrics: list[str],
        completion_status: str,
        requested_metrics: list[str] | None = None,
        returned_metrics: list[str] | None = None,
        failure_category: str | None = None,
        diagnostics: list[str] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "publication_id": target_record["reel_id"],
            "reel_id": target_record["reel_id"],
            "canonical_artwork_id": target_record["canonical_artwork_id"],
            "media_id": target_record["instagram_media_id"],
            "target_age_hours": target_slot,
            "captured_at": utc_timestamp(self._now),
            "age_seconds": round(age_seconds),
            "actual_age_hours": round(age_seconds / 3600, 3),
            "metrics": metrics,
            "derived_metrics": derive_engagement_rates(metrics),
            "requested_metrics": requested_metrics or [],
            "returned_metrics": returned_metrics or [],
            "missing_metrics": missing_metrics,
            "api_version": config.INSTAGRAM_GRAPH_API_VERSION,
            "completion_status": completion_status,
        }
        if failure_category:
            snapshot["failure_category"] = failure_category
        if diagnostics:
            snapshot["diagnostics"] = diagnostics
        return snapshot

    def _legacy_targets(self, summary: CollectionSummary) -> list[dict[str, Any]]:
        history = self._storage.load_history()
        publications = history.get("publications", [])
        if "publications" in history and not isinstance(publications, list):
            raise InsightsStorageError("Posted history publications must be a list")
        targets: list[dict[str, Any]] = []
        for index, raw in enumerate(publications):
            summary.publications_scanned += 1
            parsed = _legacy_publication(raw)
            if parsed is None:
                summary.invalid_publications += 1
                identifier = raw.get("id") if isinstance(raw, dict) else None
                logger.warning(
                    "Skipping malformed legacy publication at index %d%s",
                    index,
                    f" (id={identifier})" if isinstance(identifier, str) and identifier else "",
                )
            else:
                targets.append(parsed)
        return targets

    def _targets(
        self,
        local_reels: tuple[LocalReel, ...] | None,
        dry_run: bool,
        summary: CollectionSummary,
        association_mode: bool,
    ) -> list[dict[str, Any]]:
        if not association_mode:
            return self._legacy_targets(summary)
        associations_data, etag = self._storage.load_associations()
        associations = list(associations_data["associations"])
        if not dry_run and local_reels is not None:
            if self._client is None:
                raise RuntimeError("Insights client is required outside dry-run mode")
            try:
                discovery = self._client.discover_recent_media()
            except InstagramInsightsError as exc:
                summary.api_calls += 1
                summary.api_failures += 1
                summary.errors.append(f"discovery: {exc}")
                logger.warning("Instagram Reel discovery failed; persisted mappings remain collectable: %s", exc)
            else:
                summary.media_discovered = len(discovery.media)
                summary.api_calls += discovery.api_calls
                _merge_usage(summary.rate_limit_usage, discovery.rate_limit_usage)
                result = match_recent_media(local_reels, discovery.media, associations, self._now)
                summary.matched_new = len(result.associations)
                summary.ambiguous = len(result.ambiguous_media_ids)
                summary.unmatched = len(result.unmatched_media_ids)
                if result.ambiguous_media_ids:
                    logger.warning(
                        "[insights] ambiguous=%d media_ids=%s",
                        summary.ambiguous,
                        _compact_ids(result.ambiguous_media_ids),
                    )
                if result.unmatched_media_ids:
                    logger.info(
                        "[insights] unmatched=%d media_ids=%s",
                        summary.unmatched,
                        _compact_ids(result.unmatched_media_ids),
                    )
                if result.associations:
                    associations = merge_associations(associations, result.associations)
                    try:
                        self._storage.write_associations(
                            {"schema_version": 1, "associations": associations}, etag
                        )
                    except InsightsConcurrencyError as exc:
                        summary.write_conflicts += 1
                        summary.errors.append(str(exc))
                        logger.warning("Media association write conflict; new links will be retried next run")
                        associations = list(associations_data["associations"])
                        summary.matched_new = 0
        summary.mapped_media = len(associations)
        targets = list(associations)
        linked_reels = {item["reel_id"] for item in associations}
        linked_media = {item["instagram_media_id"] for item in associations}
        for item in self._legacy_targets(summary):
            if item["reel_id"] not in linked_reels and item["instagram_media_id"] not in linked_media:
                targets.append(item)
        summary.publications_scanned = len(targets)
        return targets

    def run(
        self,
        dry_run: bool = False,
        local_reels: tuple[LocalReel, ...] | None = None,
        association_mode: bool | None = None,
    ) -> CollectionSummary:
        summary = CollectionSummary()
        use_associations = local_reels is not None if association_mode is None else association_mode
        targets = self._targets(local_reels, dry_run, summary, use_associations)
        for target_record in targets:
            published_at = parse_aware_timestamp(target_record.get("published_at"))
            reel_id = target_record.get("reel_id")
            media_id = target_record.get("instagram_media_id")
            if published_at is None or not isinstance(reel_id, str) or not isinstance(media_id, str):
                summary.invalid_publications += 1
                logger.warning("Skipping malformed media association during Insights collection")
                continue
            age_seconds = (self._now - published_at).total_seconds()
            if age_seconds < 0:
                summary.invalid_publications += 1
                logger.warning("Skipping future media association for local Reel %s", reel_id)
                continue

            key, partition, etag = self._storage.load_partition(published_at)
            for item in partition["snapshots"]:
                same_publication = item["publication_id"] == reel_id
                same_media = item["media_id"] == media_id
                if same_publication != same_media:
                    raise InsightsStorageError("Conflicting analytics snapshot identity")
            existing = [
                item for item in partition["snapshots"]
                if item["media_id"] == media_id and item["publication_id"] == reel_id
            ]
            completed_slots = {
                item["target_age_hours"]
                for item in existing
                if inferred_snapshot_status(item)
                in {"learning_complete", "permanently_unavailable"}
            }
            target_slot, missed = select_due_slot(age_seconds / 3600, completed_slots)
            summary.missed_slots += missed
            missed_targets = [
                target
                for target, deadline in SLOT_WINDOWS_HOURS.items()
                if target not in completed_slots and age_seconds / 3600 >= deadline
            ]
            pending_snapshots = [
                self._snapshot_record(
                    target_record,
                    target,
                    age_seconds,
                    metrics={},
                    missing_metrics=[],
                    completion_status="permanently_unavailable",
                    failure_category="missed_collection_window",
                )
                for target in missed_targets
            ]
            if target_slot is None:
                if completed_slots == set(SLOT_WINDOWS_HOURS):
                    summary.already_complete += 1
                if not dry_run:
                    self._append_attempts(
                        key,
                        partition,
                        etag,
                        pending_snapshots,
                        summary,
                        reel_id,
                    )
                continue

            summary.publications_eligible += 1
            if dry_run:
                continue
            if self._client is None:
                raise RuntimeError("Insights client is required outside dry-run mode")

            try:
                response = self._client.fetch_media_insights(media_id)
                summary.api_calls += response.api_calls
                _merge_usage(summary.rate_limit_usage, response.rate_limit_usage)
            except InstagramInsightsError as exc:
                summary.api_calls += 1
                summary.api_failures += 1
                summary.errors.append(f"{reel_id}: {exc}")
                logger.warning("Insights collection failed for local Reel %s: %s", reel_id, exc)
                self._append_attempts(
                    key,
                    partition,
                    etag,
                    pending_snapshots,
                    summary,
                    reel_id,
                )
                continue

            if not response.metrics and not response.permanently_unavailable:
                summary.availability_pending += 1
                logger.info("Insights availability pending for local Reel %s", reel_id)
                self._append_attempts(
                    key,
                    partition,
                    etag,
                    pending_snapshots,
                    summary,
                    reel_id,
                )
                continue

            completion_status = (
                "permanently_unavailable"
                if response.permanently_unavailable
                else (
                    "learning_complete"
                    if learning_snapshot_components(response.metrics) is not None
                    else "partial"
                )
            )
            snapshot = self._snapshot_record(
                target_record,
                target_slot,
                age_seconds,
                metrics=response.metrics,
                missing_metrics=list(response.missing_metrics),
                completion_status=completion_status,
                requested_metrics=list(response.requested_metrics),
                returned_metrics=list(response.returned_metrics),
                failure_category=response.permanent_failure_category,
                diagnostics=list(response.diagnostics),
            )
            self._append_attempts(
                key,
                partition,
                etag,
                [*pending_snapshots, snapshot],
                summary,
                reel_id,
            )
        return summary


def format_summary(summary: CollectionSummary) -> str:
    usage = ",".join(f"{key}={value:g}" for key, value in sorted(summary.rate_limit_usage.items())) or "unavailable"
    return "\n".join((
        f"[insights] media_discovered={summary.media_discovered}",
        f"[insights] matched_new={summary.matched_new}",
        f"[insights] ambiguous={summary.ambiguous}",
        f"[insights] unmatched={summary.unmatched}",
        f"[insights] mapped_media={summary.mapped_media}",
        f"[insights] snapshots_written={summary.snapshots_written}",
        f"[insights] learning_complete_snapshots={summary.learning_complete_snapshots}",
        f"[insights] partial_snapshots={summary.partial_snapshots}",
        f"[insights] permanently_unavailable={summary.permanently_unavailable}",
        f"[insights] missed_slots={summary.missed_slots}",
        f"[insights] missed_slot_markers_written={summary.missed_slot_markers_written}",
        f"[insights] api_calls={summary.api_calls}",
        f"[insights] api_failures={summary.api_failures}",
        f"[insights] rate_limit_usage={usage}",
    ))
