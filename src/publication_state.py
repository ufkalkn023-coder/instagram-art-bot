"""Versioned, fail-closed publication state in a dedicated durable R2 bucket."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from src.models import (
    PublicationReceipts,
    PublicationSafetyState,
    require_canonical_artwork_id,
)

SAFETY_KEY = "publication_safety_state.v2.json"
RECEIPTS_KEY = "publication_receipts.v2.json"
RECOVERY_EPOCH = "recovery-2026-09-23-22c8382f7b6e"
RECOVERY_PROTECTION_SOURCE_SHA256 = "22c8382f7b6e98d73a803a826c5000e5aeffefe68d023224a1af0c4ec829fe21"
RECOVERY_RECEIPT_SOURCE_SHA256 = "c9d8c3f4139dbc14818b63d97e75b9d6e7f73ae01f23ff60407b8038156cb5e5"
RECOVERY_SET_SHA256 = {
    "PROVEN": "26dcbd37fab8032acfe1facd2fcc3eae00a765010b65c4410a64d4413a319ebd",
    "STRONGLY_SUPPORTED": "acd28eb22c24d1e125122b4499b5c2d2e723a373fe34260f9c4f0d80b64bc0fa",
    "UNVERIFIED": "a6542bca91c1eafe2404d0c6e6e75030ec098812af8346d7d379b63528f1fdfd",
    "REUSE": "4f62b9b2929b62d9f5e4fe575f6d7e509ff64d9ac78f93aff3402b9ed4009aff",
    "INFERRED": "312645136fa90a50cb5a0ff951d70ac63796a6ab3c6afe9f63ec579b4c5ba6ba",
}
RECOVERY_RECORDS_SHA256 = "8f618dd19dfcbcdf332e621f5c81cd355f19e781e9cd4837ec7fbc40105ca70e"
STATE_VARIABLES = (
    "CLOUDFLARE_R2_ACCOUNT_ID",
    "CLOUDFLARE_STATE_R2_BUCKET_NAME",
    "CLOUDFLARE_STATE_R2_ACCESS_KEY_ID",
    "CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY",
)
_S3_CONFIG = Config(connect_timeout=10, read_timeout=30,
                    retries={"total_max_attempts": 1, "mode": "standard"})


class StateValidationError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass


class StateWriteUncertainError(RuntimeError):
    """The conditional PUT may have succeeded; reload before any further mutation."""


@dataclass(frozen=True)
class StateConfiguration:
    account_id: str
    bucket: str
    access_key: str
    secret_key: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> StateConfiguration:
        environment = os.environ if environment is None else environment
        if any(not environment.get(name, "").strip() for name in STATE_VARIABLES):
            raise StateValidationError("Dedicated durable-state R2 configuration is incomplete")
        bucket = environment["CLOUDFLARE_STATE_R2_BUCKET_NAME"].strip()
        media_bucket = environment.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
        if not media_bucket or bucket == media_bucket:
            raise StateValidationError("Durable state bucket must differ from media bucket")
        if environment["CLOUDFLARE_STATE_R2_ACCESS_KEY_ID"].strip() == environment.get(
            "CLOUDFLARE_R2_ACCESS_KEY_ID", ""
        ).strip():
            raise StateValidationError("Durable state and media credentials must be distinct")
        return cls(environment["CLOUDFLARE_R2_ACCOUNT_ID"].strip(), bucket,
                   environment["CLOUDFLARE_STATE_R2_ACCESS_KEY_ID"].strip(),
                   environment["CLOUDFLARE_STATE_R2_SECRET_ACCESS_KEY"].strip())


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def payload_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes({key: item for key, item in value.items()
                                           if key != "payload_sha256"})).hexdigest()


def seal(value: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(value)
    value["payload_sha256"] = payload_digest(value)
    return value


def _verify_digest(value: Mapping[str, Any]) -> None:
    if value.get("payload_sha256") != payload_digest(value):
        raise StateValidationError("Publication state payload digest mismatch")


def validate_safety_state(value: Any) -> PublicationSafetyState:
    try:
        if not isinstance(value, dict):
            raise StateValidationError("Publication safety state must be an object")
        _verify_digest(value)
        model = PublicationSafetyState.model_validate(value)
        protection = value["published_artwork_protection"]
        if protection.get("payload_sha256") is not None:
            _verify_digest(protection)
        if set(model.published_artwork_protection.entries) & {
            item.canonical_artwork_id for item in model.recovery_quarantine.candidate_artwork_ids
        }:
            raise StateValidationError("Quarantine candidate conflicts with protection")
        if set(model.published_artwork_protection.entries) & set(
            model.recovery_quarantine.inferred_catalog_candidates
        ):
            raise StateValidationError("Inferred candidate conflicts with protection")
        if len(model.active_publication_state.receipt_sync_pending) != len(set(
            model.active_publication_state.receipt_sync_pending
        )):
            raise StateValidationError("Duplicate pending receipt synchronization")
        # Reuse the strict live validator. Reconstructed historical receipts never enter it.
        from src.history_tracker import validate_carousel_history_for_production
        validate_carousel_history_for_production(history_view(model))
        projected = {item["id"]: item for item in model.operational_projection.publications}
        for row in model.active_publication_state.posted_artworks:
            require_canonical_artwork_id(row["id"])
            if not isinstance(row.get("publication_id"), str) or not row["publication_id"]:
                raise StateValidationError("Live artwork has no publication ID")
            if row.get("status") not in {"PENDING", "PUBLISHING", "AMBIGUOUS", "PUBLISHED", "EXPIRED"}:
                raise StateValidationError("Live artwork has no strict lifecycle status")
            if row["status"] == "PENDING" and any(row.get(field) is not None for field in (
                "container_id", "publish_started_at", "publish_response_media_id",
                "media_id", "posted_at", "ambiguous_at", "expired_at",
            )):
                raise StateValidationError("Pending live artwork contains publication outcome fields")
            if row["status"] == "PUBLISHING" and any(row.get(field) is not None for field in (
                "media_id", "posted_at", "expired_at",
            )):
                raise StateValidationError("Publishing live artwork contains final outcome fields")
            if row["status"] == "PUBLISHED":
                publication = projected.get(row["publication_id"])
                if (publication is None or row["id"] not in publication["artwork_ids"]
                        or row.get("media_id") != publication["media_id"]):
                    raise StateValidationError("Published live artwork lacks exact projection")
        for publication in model.operational_projection.publications:
            actual = [row["id"] for row in model.active_publication_state.posted_artworks
                      if row.get("publication_id") == publication["id"]]
            if actual != publication["artwork_ids"]:
                raise StateValidationError("Live publication artwork order mismatch")
            if any(artwork_id not in model.published_artwork_protection.entries
                   for artwork_id in actual):
                raise StateValidationError("Published live artwork lacks permanent protection")
        reservation_ids = {item["publication_id"] for item in model.active_publication_state.reel_reservations}
        feed_publication_ids = {
            item["publication_id"] for item in model.active_publication_state.posted_artworks
        } | set(projected)
        if feed_publication_ids & reservation_ids:
            raise StateValidationError("Feed and Reel publication IDs must be distinct")
        if any(item["id"] not in reservation_ids
               for item in model.active_publication_state.reel_publications):
            raise StateValidationError("Live Reel publication lacks reservation")
        if any(item["artwork_id"] not in model.published_artwork_protection.entries
               for item in model.active_publication_state.reel_publications):
            raise StateValidationError("Published live Reel lacks permanent protection")
        published_ids = set(projected) | {
            item["id"] for item in model.active_publication_state.reel_publications
        }
        if not set(model.active_publication_state.receipt_sync_pending).issubset(published_ids):
            raise StateValidationError("Pending receipt has no published live event")
        return model
    except (ValidationError, ValueError, KeyError, TypeError) as error:
        raise StateValidationError("Malformed publication safety state") from error


def validate_receipts(value: Any) -> PublicationReceipts:
    try:
        if not isinstance(value, dict):
            raise StateValidationError("Publication receipts must be an object")
        _verify_digest(value)
        return PublicationReceipts.model_validate(value)
    except (ValidationError, ValueError, KeyError, TypeError) as error:
        raise StateValidationError("Malformed publication receipts") from error


def _ids_digest(values: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def require_recovery_safety_baseline(state: PublicationSafetyState) -> None:
    """Fence production to the reviewed 2026-09-23 forensic recovery set."""
    protection = state.published_artwork_protection
    if (state.state_epoch != RECOVERY_EPOCH
            or protection.import_batch_id != "recovery-2026-09-23"
            or protection.source_sha256 != RECOVERY_PROTECTION_SOURCE_SHA256):
        raise StateValidationError("Production safety state has an unapproved recovery epoch")
    recovered = {
        classification: [artwork_id for artwork_id, entry in protection.entries.items()
                         if entry.origin == "RECOVERED" and entry.classification == classification]
        for classification in ("PROVEN", "STRONGLY_SUPPORTED")
    }
    recovered["UNVERIFIED"] = [
        item.canonical_artwork_id for item in state.recovery_quarantine.candidate_artwork_ids
    ]
    recovered["REUSE"] = [
        artwork_id for artwork_id, entry in protection.entries.items()
        if entry.historical_reuse_publication_ids
    ]
    recovered["INFERRED"] = state.recovery_quarantine.inferred_catalog_candidates
    if any(_ids_digest(ids) != RECOVERY_SET_SHA256[key]
           for key, ids in recovered.items()):
        raise StateValidationError("Production recovery protection set differs from reviewed evidence")
    quarantine = state.recovery_quarantine
    if (quarantine.unresolved_historic_position_count != 16
            or set(quarantine.blocked_sources) != {"met", "smithsonian"}):
        raise StateValidationError("Production recovery source embargo is incomplete")


def require_recovery_receipt_baseline(ledger: PublicationReceipts) -> None:
    """Recovered receipt events stay exact even after future events are appended."""
    if (ledger.source_artifact != "recovery-ledger-v2.json"
            or ledger.source_sha256 != RECOVERY_RECEIPT_SOURCE_SHA256):
        raise StateValidationError("Production receipt ledger has unapproved provenance")
    recovered = [record.model_dump(mode="json") for record in ledger.records
                 if record.record_origin == "RECOVERED"]
    if hashlib.sha256(canonical_bytes({"records": recovered})).hexdigest() != RECOVERY_RECORDS_SHA256:
        raise StateValidationError("Recovered publication receipts differ from reviewed evidence")


def history_view(state: PublicationSafetyState) -> dict[str, Any]:
    active = state.active_publication_state
    projection = state.operational_projection
    return {
        "posted_artworks": copy.deepcopy(active.posted_artworks),
        "reel_reservations": copy.deepcopy(active.reel_reservations),
        "reel_publications": copy.deepcopy(active.reel_publications),
        "reel_publication_count": active.reel_publication_count,
        "staging_media_cleanup_queue": copy.deepcopy(active.staging_media_cleanup_queue),
        "reel_staging_cleanup_queue": copy.deepcopy(active.reel_staging_cleanup_queue),
        "publications": copy.deepcopy(projection.publications),
        "grid_publication_count": projection.grid_publication_count,
        "active_color_tone": projection.active_color_tone,
        "_safety_state": state.model_dump(mode="json"),
    }


def validate_live_receipt_coverage(
    state: PublicationSafetyState, ledger: PublicationReceipts
) -> None:
    """All finalized live events need exact documentary identities."""
    by_id = {receipt.publication_id: receipt for receipt in ledger.records}
    pending = set(state.active_publication_state.receipt_sync_pending)
    if pending:
        raise StateValidationError("Publication receipt synchronization is incomplete")
    live = [
        (item["id"], item["media_id"], item["type"], item["artwork_ids"])
        for item in state.operational_projection.publications
    ]
    live.extend(
        (item["id"], item["media_id"], "reel", [item["artwork_id"]])
        for item in state.active_publication_state.reel_publications
    )
    for publication_id, media_id, publication_type, artwork_ids in live:
        receipt = by_id.get(publication_id)
        if (receipt is None or receipt.record_origin != "NEW"
                or receipt.instagram_media_id != media_id
                or receipt.publication_type != publication_type
                or [position.canonical_artwork_id for position in receipt.artwork_positions]
                != artwork_ids):
            raise StateValidationError("Finalized live publication lacks an exact receipt")


def blocked_ids(history: Mapping[str, Any]) -> set[str]:
    raw = history.get("_safety_state")
    if raw is None:
        return set()
    state = validate_safety_state(raw)
    return (set(state.published_artwork_protection.entries)
            | {item.canonical_artwork_id for item in state.recovery_quarantine.candidate_artwork_ids}
            | set(state.recovery_quarantine.inferred_catalog_candidates))


def blocked_sources(history: Mapping[str, Any]) -> set[str]:
    raw = history.get("_safety_state")
    return set(validate_safety_state(raw).recovery_quarantine.blocked_sources) if raw is not None else set()


def state_from_history(history: Mapping[str, Any]) -> dict[str, Any]:
    raw = history.get("_safety_state")
    if raw is None:
        raise StateValidationError("Live history has no versioned safety state")
    prior = validate_safety_state(raw)
    value = prior.model_dump(mode="json")
    active = value["active_publication_state"]
    projection = value["operational_projection"]
    for key in ("posted_artworks", "reel_reservations", "reel_publications",
                "reel_publication_count", "staging_media_cleanup_queue",
                "reel_staging_cleanup_queue"):
        active[key] = copy.deepcopy(history[key])
    for key in ("publications", "grid_publication_count", "active_color_tone"):
        projection[key] = copy.deepcopy(history[key])
    entries = value["published_artwork_protection"]["entries"]
    # A new PUBLISHED event adds protection in the same CAS as finalization.
    for publication in projection["publications"]:
        for artwork_id in publication["artwork_ids"]:
            require_canonical_artwork_id(artwork_id)
            entries.setdefault(artwork_id, {
                "canonical_artwork_id": artwork_id, "classification": "PROVEN",
                "provenance_refs": [f"new-publication:{publication['id']}"],
                "first_known_publication_reference": {
                    "publication_id": publication["id"],
                    "instagram_media_id": publication["media_id"],
                },
                "historical_reuse_publication_ids": [], "origin": "NEW",
            })
    for publication in active["reel_publications"]:
        artwork_id = require_canonical_artwork_id(publication["artwork_id"])
        entries.setdefault(artwork_id, {
            "canonical_artwork_id": artwork_id, "classification": "PROVEN",
            "provenance_refs": [f"new-publication:{publication['id']}"],
            "first_known_publication_reference": {
                "publication_id": publication["id"],
                "instagram_media_id": publication["media_id"],
            },
            "historical_reuse_publication_ids": [], "origin": "NEW",
        })
    value["published_artwork_protection"]["entry_count"] = len(entries)
    if value["published_artwork_protection"].get("payload_sha256") is not None:
        value["published_artwork_protection"] = seal(value["published_artwork_protection"])
    old_published = {item["id"] for item in prior.operational_projection.publications}
    old_published.update(item["id"] for item in prior.active_publication_state.reel_publications)
    new_published = {item["id"] for item in projection["publications"]}
    new_published.update(item["id"] for item in active["reel_publications"])
    active["receipt_sync_pending"] = sorted(
        set(active["receipt_sync_pending"]) | (new_published - old_published)
    )
    value["generation"] += 1
    result = seal(value)
    validate_safety_state(result)
    if not set(prior.published_artwork_protection.entries).issubset(entries):
        raise StateValidationError("Permanent protection cannot shrink")
    return result


class PublicationStateStore:
    def __init__(self, config: StateConfiguration | None = None, client: Any | None = None):
        self.require_recovery_baseline = config is None
        self.config = config or StateConfiguration.from_environment()
        self.client = client or boto3.client(
            "s3", endpoint_url=f"https://{self.config.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=self.config.access_key,
            aws_secret_access_key=self.config.secret_key,
            region_name="auto", config=_S3_CONFIG,
        )

    def _read(self, key: str) -> tuple[dict[str, Any], str]:
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        except (ClientError, BotoCoreError) as error:
            raise StateValidationError(f"Required durable state object is unreadable: {key}") from error
        if response.get("Expiration"):
            raise StateValidationError("Durable state object has an expiration policy")
        etag = response.get("ETag")
        if not isinstance(etag, str) or len(etag) < 3 or not etag.startswith('"') or not etag.endswith('"'):
            raise StateValidationError("Durable state response has no strong ETag")
        try:
            body = json.loads(response["Body"].read().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise StateValidationError("Durable state JSON is malformed") from error
        return body, etag

    def load_safety(self) -> tuple[PublicationSafetyState, str]:
        raw, etag = self._read(SAFETY_KEY)
        state = validate_safety_state(raw)
        if self.require_recovery_baseline:
            require_recovery_safety_baseline(state)
        return state, etag

    def load_receipts(self) -> tuple[PublicationReceipts, str]:
        raw, etag = self._read(RECEIPTS_KEY)
        ledger = validate_receipts(raw)
        if self.require_recovery_baseline:
            require_recovery_receipt_baseline(ledger)
        return ledger, etag

    def _conditional_put(self, key: str, payload: dict[str, Any], etag: str) -> None:
        if not isinstance(etag, str) or not etag.startswith('"') or not etag.endswith('"'):
            raise StateValidationError("Conditional update requires a strong ETag")
        try:
            self.client.put_object(Bucket=self.config.bucket, Key=key,
                                   Body=canonical_bytes(payload), ContentType="application/json",
                                   IfMatch=etag)
        except ClientError as error:
            response = error.response
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412 or str(
                response.get("Error", {}).get("Code")
            ) in {"412", "PreconditionFailed"}:
                raise StateConflictError("Durable state CAS conflict") from error
            raise StateWriteUncertainError("Durable state write outcome is uncertain") from error
        except (BotoCoreError, OSError) as error:
            raise StateWriteUncertainError("Durable state write outcome is uncertain") from error
        # Read back the exact committed generation and digest. A later competing
        # writer can win immediately; in that case the caller must reconcile.
        try:
            observed, _ = self._read(key)
        except StateValidationError as error:
            raise StateWriteUncertainError("Durable state read-after-write failed") from error
        if observed.get("generation") != payload["generation"] or observed.get(
            "payload_sha256"
        ) != payload["payload_sha256"]:
            raise StateWriteUncertainError("Durable state read-after-write differs")

    def create_initial(self, key: str, payload: dict[str, Any]) -> None:
        """Bootstrap only: never overwrite an existing durable object."""
        if key == SAFETY_KEY:
            state = validate_safety_state(payload)
            if self.require_recovery_baseline:
                require_recovery_safety_baseline(state)
        elif key == RECEIPTS_KEY:
            ledger = validate_receipts(payload)
            if self.require_recovery_baseline:
                require_recovery_receipt_baseline(ledger)
        else:
            raise StateValidationError("Unknown durable state object key")
        try:
            self.client.put_object(
                Bucket=self.config.bucket, Key=key, Body=canonical_bytes(payload),
                ContentType="application/json", IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412:
                raise StateConflictError("Bootstrap object already exists") from error
            raise StateWriteUncertainError("Bootstrap write outcome is uncertain") from error
        except (BotoCoreError, OSError) as error:
            raise StateWriteUncertainError("Bootstrap write outcome is uncertain") from error
        try:
            observed, _ = self._read(key)
        except StateValidationError as error:
            raise StateWriteUncertainError("Bootstrap read-after-write failed") from error
        if observed != payload:
            raise StateWriteUncertainError("Bootstrap read-after-write differs")

    def require_uninitialized(self) -> None:
        """Refuse a bootstrap when either fixed object already exists."""
        for key in (SAFETY_KEY, RECEIPTS_KEY):
            try:
                response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            except ClientError as error:
                if str(error.response.get("Error", {}).get("Code")) == "NoSuchKey":
                    continue
                raise StateValidationError("Bootstrap target cannot be verified empty") from error
            except BotoCoreError as error:
                raise StateValidationError("Bootstrap target cannot be verified empty") from error
            body = response.get("Body") if isinstance(response, dict) else None
            if body is not None and hasattr(body, "close"):
                body.close()
            raise StateConflictError("Bootstrap target already contains durable state")

    def update_safety(self, payload: dict[str, Any], etag: str) -> None:
        current, current_etag = self.load_safety()
        if etag != current_etag or payload.get("generation") != current.generation + 1:
            raise StateConflictError("Durable state generation or ETag changed")
        candidate = validate_safety_state(payload)
        if candidate.state_epoch != current.state_epoch:
            raise StateValidationError("State epoch cannot change in a normal update")
        if not set(current.published_artwork_protection.entries).issubset(
            candidate.published_artwork_protection.entries
        ):
            raise StateValidationError("Permanent protection cannot shrink")
        for artwork_id, entry in current.published_artwork_protection.entries.items():
            if candidate.published_artwork_protection.entries[artwork_id] != entry:
                raise StateValidationError("Existing protection evidence is immutable")
        if candidate.recovery_quarantine != current.recovery_quarantine:
            raise StateValidationError("Recovery quarantine requires a reviewed migration")
        old_reservation_pairs = {
            (row["publication_id"], row["id"])
            for row in current.active_publication_state.posted_artworks
        } | {
            (row["publication_id"], row["artwork_id"])
            for row in current.active_publication_state.reel_reservations
        }
        new_reservation_pairs = {
            (row["publication_id"], row["id"])
            for row in candidate.active_publication_state.posted_artworks
        } | {
            (row["publication_id"], row["artwork_id"])
            for row in candidate.active_publication_state.reel_reservations
        }
        added_reservations = {
            publication_id for publication_id, _ in new_reservation_pairs - old_reservation_pairs
        }
        if added_reservations & {publication_id for publication_id, _ in old_reservation_pairs}:
            raise StateValidationError("Publication reservation ID cannot be reused")
        if added_reservations:
            ledger, _ = self.load_receipts()
            if added_reservations & {receipt.publication_id for receipt in ledger.records}:
                raise StateValidationError("New reservation collides with an existing receipt")
        old_feed = {
            (row["publication_id"], row["id"]): row
            for row in current.active_publication_state.posted_artworks
        }
        new_feed = {
            (row["publication_id"], row["id"]): row
            for row in candidate.active_publication_state.posted_artworks
        }
        allowed_feed = {
            "PENDING": {"PENDING", "PUBLISHING", "AMBIGUOUS", "EXPIRED"},
            "PUBLISHING": {"PUBLISHING", "AMBIGUOUS", "PUBLISHED", "EXPIRED"},
            "AMBIGUOUS": {"AMBIGUOUS", "PUBLISHED"},
            "PUBLISHED": {"PUBLISHED"},
            "EXPIRED": {"EXPIRED"},
        }
        for key, old in old_feed.items():
            new = new_feed.get(key)
            if new is None:
                if old["status"] != "EXPIRED":
                    raise StateValidationError("Live feed lock cannot be removed")
            elif new["status"] not in allowed_feed[old["status"]]:
                raise StateValidationError("Live feed lock cannot move backward")
            elif (new["status"] == "EXPIRED"
                  and old.get("publish_response_media_id")):
                raise StateValidationError("Feed lock with a durable media ID cannot expire")
        old_reels = {row["publication_id"]: row for row in current.active_publication_state.reel_reservations}
        new_reels = {row["publication_id"]: row for row in candidate.active_publication_state.reel_reservations}
        allowed_reel = allowed_feed
        for publication_id, old in old_reels.items():
            new = new_reels.get(publication_id)
            if new is None:
                if old["status"] != "EXPIRED":
                    raise StateValidationError("Live Reel lock cannot be removed")
            elif new["status"] not in allowed_reel[old["status"]]:
                raise StateValidationError("Live Reel lock cannot move backward")
            elif (new["status"] == "EXPIRED"
                  and old.get("publish_response_media_id")):
                raise StateValidationError("Reel lock with a durable media ID cannot expire")
        old_publications = {row["id"]: row for row in current.operational_projection.publications}
        new_publications = {row["id"]: row for row in candidate.operational_projection.publications}
        for publication_id, old in old_publications.items():
            new = new_publications.get(publication_id)
            if new is None or any(new[field] != old[field] for field in ("media_id", "artwork_ids", "type")):
                raise StateValidationError("Live publication identity cannot be removed or rewritten")
        old_reel_publications = {row["id"]: row for row in current.active_publication_state.reel_publications}
        new_reel_publications = {row["id"]: row for row in candidate.active_publication_state.reel_publications}
        for publication_id, old in old_reel_publications.items():
            new = new_reel_publications.get(publication_id)
            if new is None or any(new[field] != old[field] for field in ("media_id", "artwork_id")):
                raise StateValidationError("Live Reel identity cannot be removed or rewritten")
        self._conditional_put(SAFETY_KEY, payload, etag)

    def append_receipt(self, receipt: dict[str, Any]) -> None:
        for _ in range(3):
            ledger, etag = self.load_receipts()
            existing = next((item for item in ledger.records if item.publication_id == receipt["publication_id"]), None)
            if existing is not None:
                if existing.model_dump(mode="json") != receipt:
                    raise StateValidationError("Receipt replay conflicts with immutable event")
                return
            value = ledger.model_dump(mode="json")
            value["records"].append(receipt)
            value["record_count"] += 1
            value["generation"] += 1
            value = seal(value)
            validate_receipts(value)
            try:
                self._conditional_put(RECEIPTS_KEY, value, etag)
            except StateConflictError:
                continue
            return
        raise StateConflictError("Receipt append lost repeated CAS conflicts")


def _new_receipt_from_state(state: PublicationSafetyState, publication_id: str) -> dict[str, Any]:
    feed = next((item for item in state.operational_projection.publications
                 if item["id"] == publication_id), None)
    reel = next((item for item in state.active_publication_state.reel_publications
                 if item["id"] == publication_id), None)
    if (feed is None) == (reel is None):
        raise StateValidationError("Pending receipt has no unique strict publication")
    publication = feed or reel
    ids = publication["artwork_ids"] if feed is not None else [publication["artwork_id"]]
    for artwork_id in ids:
        require_canonical_artwork_id(artwork_id)
    return {
        "publication_id": publication_id,
        "instagram_media_id": publication["media_id"],
        "publication_type": publication["type"] if feed is not None else "reel",
        "historical_state": None,
        "current_durable_lifecycle_state": None,
        "record_origin": "NEW",
        "identity_completeness": "COMPLETE",
        "occurred_at": publication["posted_at"],
        "permalink": publication.get("permalink"),
        "workflow_run_id": None,
        "artwork_positions": [
            {"position": index, "canonical_artwork_id": artwork_id,
             "instagram_child_media_id": None, "caption_label": None}
            for index, artwork_id in enumerate(ids, start=1)
        ],
        "evidence_ref": f"new-publication:{publication_id}",
    }


def synchronize_receipt(publication_id: str, store: PublicationStateStore | None = None) -> None:
    """Idempotently repair documentary receipts without calling Meta."""
    store = store or PublicationStateStore()
    for _ in range(3):
        state, etag = store.load_safety()
        if publication_id not in state.active_publication_state.receipt_sync_pending:
            return
        receipt = _new_receipt_from_state(state, publication_id)
        store.append_receipt(receipt)
        value = state.model_dump(mode="json")
        value["active_publication_state"]["receipt_sync_pending"].remove(publication_id)
        value["generation"] += 1
        value = seal(value)
        try:
            store.update_safety(value, etag)
        except StateConflictError:
            continue
        return
    raise StateConflictError("Receipt sync lost repeated safety-state CAS conflicts")


def validate_state_bucket_lifecycle(store: PublicationStateStore) -> None:
    """Read-only gate: reject any deletion/transition lifecycle rule or unknown result."""
    try:
        response = store.client.get_bucket_lifecycle_configuration(Bucket=store.config.bucket)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
            return
        raise StateValidationError("Durable state lifecycle cannot be verified") from error
    except (BotoCoreError, AttributeError) as error:
        raise StateValidationError("Durable state lifecycle cannot be verified") from error
    rules = response.get("Rules") if isinstance(response, dict) else None
    if not isinstance(rules, list):
        raise StateValidationError("Durable state lifecycle response is malformed")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("Status") not in {"Enabled", "Disabled"}:
            raise StateValidationError("Durable state lifecycle rule is malformed")
        if rule.get("Status") != "Enabled":
            continue
        if any(rule.get(field) for field in (
            "Expiration", "Transitions", "NoncurrentVersionExpiration",
            "NoncurrentVersionTransitions",
        )):
            raise StateValidationError("Durable state bucket has destructive lifecycle rule")


def replay_pending_receipts(store: PublicationStateStore | None = None) -> int:
    """Repair documentary gaps only; never retries an Instagram operation."""
    store = store or PublicationStateStore()
    state, _ = store.load_safety()
    pending = tuple(state.active_publication_state.receipt_sync_pending)
    for publication_id in pending:
        synchronize_receipt(publication_id, store)
    return len(pending)


def convert_legacy_snapshot(snapshot: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Offline-only conversion of a complete, strict v1 snapshot.

    A missing or partial source is an error. The local dry-run file must never be
    passed as authoritative input. This function performs no storage operation.
    """
    from src.history_tracker import validate_carousel_history_for_production
    from src.models import PublicationRecord, ReelPublicationRecord, ReelReservationRecord

    if not isinstance(snapshot, dict) or snapshot.get("schema_version", 1) != 1:
        raise StateValidationError("Legacy snapshot has an unsupported schema")
    if "posted_artworks" not in snapshot or "publications" not in snapshot:
        raise StateValidationError("Legacy snapshot is incomplete")
    if "_safety_state" in snapshot:
        raise StateValidationError("Versioned safety state cannot be legacy input")
    try:
        validate_carousel_history_for_production(snapshot)
        feed = [PublicationRecord.model_validate(item) for item in snapshot["publications"]]
        reels = [ReelPublicationRecord.model_validate(item)
                 for item in snapshot.get("reel_publications", [])]
        reservations = [ReelReservationRecord.model_validate(item)
                        for item in snapshot.get("reel_reservations", [])]
        feed_ids = {item.id: item for item in feed}
        for item in snapshot["posted_artworks"]:
            artwork_id = require_canonical_artwork_id(item["id"])
            status = item.get("status")
            if status not in {"PENDING", "PUBLISHING", "AMBIGUOUS", "PUBLISHED", "EXPIRED"}:
                raise StateValidationError("Legacy artwork has no strict lifecycle status")
            if status == "PUBLISHED":
                publication = feed_ids.get(item.get("publication_id"))
                if (publication is None or artwork_id not in publication.artwork_ids
                        or item.get("media_id") != publication.media_id):
                    raise StateValidationError("Legacy published artwork has no exact publication link")
            if str(item.get("media_id", "")).startswith("dry_run"):
                raise StateValidationError("Local dry-run artwork is not publication evidence")
        for item in reservations:
            require_canonical_artwork_id(item.artwork_id)
        if any(str(item.media_id).startswith("dry_run") for item in reels):
            raise StateValidationError("Local dry-run Reel is not publication evidence")
    except (ValidationError, ValueError, KeyError, TypeError) as error:
        raise StateValidationError("Legacy snapshot failed strict validation") from error

    source_sha = hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
    epoch = f"legacy-migration-{source_sha[:12]}"
    entries: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for publication in feed:
        ids = publication.artwork_ids
        kind = publication.type
        posted_at = publication.posted_at
        permalink = publication.permalink
        publication_id = publication.id
        media_id = publication.media_id
        for artwork_id in ids:
            require_canonical_artwork_id(artwork_id)
            if artwork_id in entries:
                raise StateValidationError("Legacy publication reuse needs manual evidence review")
            entries[artwork_id] = {
                "canonical_artwork_id": artwork_id, "classification": "PROVEN",
                "provenance_refs": [f"legacy-validated-snapshot:{publication_id}"],
                "first_known_publication_reference": {
                    "publication_id": publication_id, "instagram_media_id": media_id,
                }, "historical_reuse_publication_ids": [], "origin": "RECOVERED",
            }
        records.append({
            "publication_id": publication_id, "instagram_media_id": media_id,
            "publication_type": kind, "historical_state": "PUBLISHED_CONFIRMED",
            "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
            "identity_completeness": "COMPLETE", "occurred_at": posted_at,
            "permalink": permalink, "workflow_run_id": None,
            "artwork_positions": [
                {"position": index, "canonical_artwork_id": artwork_id,
                 "instagram_child_media_id": None, "caption_label": None}
                for index, artwork_id in enumerate(ids, start=1)
            ], "evidence_ref": f"legacy-validated-snapshot:{publication_id}",
        })
    for publication in reels:
        artwork_id = require_canonical_artwork_id(publication.artwork_id)
        if artwork_id in entries:
            raise StateValidationError("Legacy cross-format reuse needs manual evidence review")
        entries[artwork_id] = {
            "canonical_artwork_id": artwork_id, "classification": "PROVEN",
            "provenance_refs": [f"legacy-validated-snapshot:{publication.id}"],
            "first_known_publication_reference": {
                "publication_id": publication.id, "instagram_media_id": publication.media_id,
            }, "historical_reuse_publication_ids": [], "origin": "RECOVERED",
        }
        records.append({
            "publication_id": publication.id, "instagram_media_id": publication.media_id,
            "publication_type": "reel", "historical_state": "PUBLISHED_CONFIRMED",
            "current_durable_lifecycle_state": "UNKNOWN", "record_origin": "RECOVERED",
            "identity_completeness": "COMPLETE", "occurred_at": publication.posted_at,
            "permalink": publication.permalink, "workflow_run_id": None,
            "artwork_positions": [{"position": 1, "canonical_artwork_id": artwork_id,
                                   "instagram_child_media_id": None, "caption_label": None}],
            "evidence_ref": f"legacy-validated-snapshot:{publication.id}",
        })
    protection = seal({
        "schema_version": 1, "import_batch_id": epoch,
        "source_artifact": "legacy-validated-snapshot",
        "source_sha256": source_sha, "entry_count": len(entries), "entries": entries,
    })
    state = seal({
        "schema_version": 2, "state_epoch": epoch, "generation": 1,
        "published_artwork_protection": protection,
        "recovery_quarantine": {
            "schema_version": 1, "candidate_artwork_ids": [],
            "inferred_catalog_candidates": [], "unresolved_historic_position_count": 0,
            "unresolved_positions": [], "blocked_sources": [],
        },
        "active_publication_state": {
            "schema_version": 1,
            "posted_artworks": copy.deepcopy(snapshot["posted_artworks"]),
            "reel_reservations": copy.deepcopy(snapshot.get("reel_reservations", [])),
            "reel_publications": copy.deepcopy(snapshot.get("reel_publications", [])),
            "reel_publication_count": snapshot.get("reel_publication_count", 0),
            "staging_media_cleanup_queue": copy.deepcopy(snapshot.get("staging_media_cleanup_queue", [])),
            "reel_staging_cleanup_queue": copy.deepcopy(snapshot.get("reel_staging_cleanup_queue", [])),
            "receipt_sync_pending": [],
        },
        "operational_projection": {
            "publications": copy.deepcopy(snapshot["publications"]),
            "grid_publication_count": snapshot.get("grid_publication_count", 0),
            "grid_counter_epoch": epoch,
            "active_color_tone": snapshot.get("active_color_tone", "warm"),
        },
    })
    ledger = seal({
        "schema_version": 2, "generation": 1,
        "source_artifact": "legacy-validated-snapshot", "source_sha256": source_sha,
        "record_count": len(records), "records": records,
    })
    validate_safety_state(state)
    validate_receipts(ledger)
    return state, ledger
