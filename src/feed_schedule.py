"""Single-slot and continuously approved Feed attempts use the same safety CAS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from src.models import (
    GITHUB_TERMINAL_CONCLUSIONS,
    FeedScheduleControl,
    FeedScheduleOutcome,
    FeedSchedulePermit,
    FeedScheduleReview,
    FeedContinuousApproval,
    PublicationReceipts,
    PublicationSafetyState,
    parse_receipt_occurrence,
)
from src import publication_state, r2_media
from src.production_authorization import (
    ProductionAuthorization,
    ProductionAuthorizationError,
)
from src.production_config import require_clear_publication_state

FEED_REPOSITORY = "ufkalkn023-coder/instagram-art-bot"
FEED_WORKFLOW = ".github/workflows/instagram_bot.yml"
COOLDOWN = timedelta(hours=48)


class FeedScheduleError(RuntimeError):
    """No scheduled production may follow a rejected or uncertain state transition."""


def require_fresh(authorization: ProductionAuthorization, now: datetime) -> None:
    try:
        authorization.require_fresh(now)
    except ProductionAuthorizationError as error:
        raise FeedScheduleError("SCHEDULE_RUN_NOT_FRESH") from error


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FeedScheduleError("SCHEDULE_TIME_INVALID")
    return value.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def latest_success(
    state: PublicationSafetyState, ledger: PublicationReceipts
) -> tuple[datetime, str]:
    publication_state.validate_live_receipt_coverage(state, ledger)
    completed = [
        r for r in ledger.records if r.publication_type in {"single", "carousel"}
    ]
    live = {event["id"]: event for event in state.operational_projection.publications}
    new_receipts = [r for r in completed if r.record_origin == "NEW"]
    for receipt in new_receipts:
        event = live.get(receipt.publication_id)
        if (
            receipt.occurred_at is None
            or event is None
            or parse_receipt_occurrence(receipt.occurred_at)
            != parse_receipt_occurrence(event["posted_at"])
        ):
            raise FeedScheduleError("SCHEDULE_COMPLETION_EVIDENCE_MISMATCH")
    # Recovered history predates the reviewed recovery baseline. Once a strict
    # post-recovery Feed completion exists, old unknown times do not replace it.
    if not completed or (
        not new_receipts and any(r.occurred_at is None for r in completed)
    ):
        raise FeedScheduleError("SCHEDULE_COMPLETION_EVIDENCE_MISSING")
    dated = [r for r in completed if r.occurred_at is not None]
    latest = max(dated, key=lambda r: parse_receipt_occurrence(r.occurred_at))
    return utc(parse_receipt_occurrence(latest.occurred_at)), latest.publication_id


def initial_control(
    state: PublicationSafetyState, ledger: PublicationReceipts
) -> FeedScheduleControl:
    """Pure migration builder; never initializes storage or fabricates a permit."""
    completed, publication_id = latest_success(state, ledger)
    return FeedScheduleControl(
        schema_version=1,
        paused=True,
        pause_reason="STAGE_B_NOT_ARMED",
        latest_successful_feed_at=stamp(completed),
        latest_successful_feed_id=publication_id,
        next_eligible_at=stamp(completed + COOLDOWN),
    )


def control_for(state: PublicationSafetyState) -> FeedScheduleControl:
    if state.feed_schedule_control is None:
        raise FeedScheduleError("STAGE_B_SCHEDULE_NOT_INITIALIZED")
    return state.feed_schedule_control


def continuous_approval(control: FeedScheduleControl) -> FeedContinuousApproval | None:
    if control.continuous_approvals and control.continuous_approvals[-1].revoked_at is None:
        return control.continuous_approvals[-1]
    return None


def safe_completed_attempt(permit: FeedSchedulePermit) -> bool:
    return bool(
        permit.outcome is not None
        and permit.status in {"SUCCESS", "DEFINITIVE_FAILURE"}
        and not permit.outcome.cleanup_pending
        and not permit.outcome.reconciliation_pending
    )


def require_cooldown(
    state: PublicationSafetyState, ledger: PublicationReceipts, at: datetime
) -> None:
    completed, publication_id = latest_success(state, ledger)
    control = control_for(state)
    if (
        parse_receipt_occurrence(control.latest_successful_feed_at) != completed
        or control.latest_successful_feed_id != publication_id
    ):
        raise FeedScheduleError("SCHEDULE_COMPLETION_BASELINE_MISMATCH")
    if utc(at) < completed + COOLDOWN:
        raise FeedScheduleError("SCHEDULE_COOLDOWN_NOT_SATISFIED")


def require_owner(
    state: PublicationSafetyState,
    authorization: ProductionAuthorization,
    *,
    status: str,
    now: datetime,
) -> FeedSchedulePermit:
    require_fresh(authorization, now)
    control = control_for(state)
    if not control.permits:
        raise FeedScheduleError("SCHEDULE_NOT_ARMED")
    permit = control.permits[-1]
    if permit.continuous_approval_id is not None:
        approval = continuous_approval(control)
        if (control.paused or approval is None
                or approval.approval_id != permit.continuous_approval_id
                or approval.approved_sha != authorization.head_sha):
            raise FeedScheduleError("CONTINUOUS_APPROVAL_REVOKED")
    if (
        not authorization.is_scheduled_feed
        or permit.status != status
        or permit.revoked_at is not None
        or permit.acknowledgement is not None
        or authorization.repository != FEED_REPOSITORY
        or authorization.workflow_path != FEED_WORKFLOW
        or type(authorization.run_attempt) is not int
        or authorization.run_attempt != 1
        or permit.owner_run_id != authorization.run_id
        or permit.owner_run_attempt != authorization.run_attempt
        or permit.authorization_key != authorization.key
        or permit.approved_sha != authorization.head_sha
        or not parse_receipt_occurrence(permit.slot_at)
        <= utc(now)
        < parse_receipt_occurrence(permit.expires_at)
    ):
        raise FeedScheduleError("SCHEDULE_OWNER_OR_WINDOW_INVALID")
    return permit


def require_control_update(
    old: FeedScheduleControl | None, new: FeedScheduleControl | None
) -> None:
    """Normal state writes cannot erase permits, outcomes, ownership or acknowledgements."""
    if old is None:
        if new is not None and (not new.paused or new.permits):
            raise publication_state.StateValidationError(
                "Schedule initialization must be paused"
            )
        return
    if new is None or len(new.permits) < len(old.permits):
        raise publication_state.StateValidationError(
            "Schedule evidence cannot disappear"
        )
    if (len(new.continuous_approvals) < len(old.continuous_approvals)
            or len(new.continuous_approvals) > len(old.continuous_approvals) + 1):
        raise publication_state.StateValidationError("Continuous approvals are append-only")
    for prior, candidate in zip(old.continuous_approvals, new.continuous_approvals):
        if any(getattr(prior, field) != getattr(candidate, field) for field in (
            "approval_id", "approved_sha", "created_at", "approval_ref",
        )) or (prior.revoked_at is not None and prior.revoked_at != candidate.revoked_at):
            raise publication_state.StateValidationError("Continuous approval evidence is immutable")
    if parse_receipt_occurrence(
        new.latest_successful_feed_at
    ) < parse_receipt_occurrence(old.latest_successful_feed_at):
        raise publication_state.StateValidationError(
            "Successful Feed clock cannot move backward"
        )
    allowed = {
        "ARMED": {"ARMED", "ADMITTED", "EXPIRED", "REVOKED"},
        "ADMITTED": {
            "ADMITTED",
            "RESERVED",
            "DEFINITIVE_FAILURE",
            "CANCELLED",
            "INCOMPLETE",
        },
        "RESERVED": {
            "RESERVED",
            "PUBLISHING",
            "DEFINITIVE_FAILURE",
            "CANCELLED",
            "INCOMPLETE",
        },
        "PUBLISHING": {
            "PUBLISHING",
            "SUCCESS",
            "DEFINITIVE_FAILURE",
            "AMBIGUOUS",
            "CANCELLED",
            "INCOMPLETE",
        },
    }
    for prior, candidate in zip(old.permits, new.permits):
        for field in (
            "permit_id",
            "slot_at",
            "expires_at",
            "approved_sha",
            "created_at",
            "approval_ref",
            "continuous_approval_id",
        ):
            if getattr(prior, field) != getattr(candidate, field):
                raise publication_state.StateValidationError(
                    "Permit approval is immutable"
                )
        for field in (
            "owner_run_id",
            "owner_run_attempt",
            "authorization_key",
            "admitted_at",
            "publication_id",
            "reservation_generation",
            "outcome",
            "acknowledgement",
            "revoked_at",
        ):
            before = getattr(prior, field)
            if before is not None and before != getattr(candidate, field):
                raise publication_state.StateValidationError(
                    "Scheduled attempt evidence is immutable"
                )
        if candidate.status not in allowed.get(prior.status, {prior.status}):
            raise publication_state.StateValidationError(
                "Schedule permit cannot reopen or move backward"
            )
        if prior.acknowledgement is not None and prior != candidate:
            raise publication_state.StateValidationError(
                "Reviewed schedule evidence is immutable"
            )
    if len(new.permits) > len(old.permits) + 1:
        raise publication_state.StateValidationError("Only one permit may be armed")
    newly_approved = len(new.continuous_approvals) == len(old.continuous_approvals) + 1
    if not new.paused and (len(new.permits) == len(old.permits) and old.paused) and not newly_approved:
        raise publication_state.StateValidationError(
            "An existing permit cannot be rearmed"
        )


class FeedScheduleManager:
    def __init__(
        self,
        store: publication_state.PublicationStateStore | None = None,
        *,
        owned_media: Callable[..., set[str]] | None = None,
    ):
        self.store = store or publication_state.PublicationStateStore()
        self.owned_media = owned_media or r2_media.list_owned_publication_ids

    def inspect(self, *, now: datetime | None = None) -> dict[str, Any]:
        state, _ = self.store.load_safety()
        ledger, _ = self.store.load_receipts()
        control = state.feed_schedule_control
        effective_paused = True
        effective_reason = (
            control.pause_reason if control else "STAGE_B_SCHEDULE_NOT_INITIALIZED"
        )
        if control is not None and not control.paused and continuous_approval(control) is not None:
            effective_paused = False
            effective_reason = "CONTINUOUS_ENABLED"
        elif control is not None and not control.paused and control.permits:
            permit = control.permits[-1]
            effective_paused = not (
                parse_receipt_occurrence(permit.slot_at)
                <= utc(now or datetime.now(timezone.utc))
                < parse_receipt_occurrence(permit.expires_at)
            )
            effective_reason = (
                "SCHEDULE_WINDOW_CLOSED" if effective_paused else "ONE_SLOT_ARMED"
            )
        return {
            "safety_generation": state.generation,
            "receipt_generation": ledger.generation,
            "schedule_control": control.model_dump(mode="json") if control else None,
            "effective_paused": effective_paused,
            "effective_pause_reason": effective_reason,
        }

    def _clean(
        self, state: PublicationSafetyState, ledger: PublicationReceipts, now: datetime
    ) -> None:
        require_clear_publication_state(
            state,
            ledger,
            owned_feed_ids=self.owned_media(),
            owned_reel_ids=self.owned_media(reel=True),
            now=now,
        )

    def _save(
        self, state: PublicationSafetyState, etag: str, control: FeedScheduleControl
    ) -> None:
        value = state.model_dump(mode="json")
        value["feed_schedule_control"] = control.model_dump(mode="json")
        value["generation"] += 1
        # No retry after conflict or uncertain write; no mutation can publish.
        self.store.update_safety(publication_state.seal(value), etag)

    def _expected(
        self, generation: int
    ) -> tuple[PublicationSafetyState, str, FeedScheduleControl]:
        state, etag = self.store.load_safety()
        if type(generation) is not int or state.generation != generation:
            raise publication_state.StateConflictError(
                "Schedule expected generation changed"
            )
        return state, etag, control_for(state).model_copy(deep=True)

    def arm(
        self,
        *,
        expected_generation: int,
        approved_sha: str,
        main_sha: str,
        slot: datetime,
        expires_at: datetime,
        review_ref: str,
        now: datetime | None = None,
    ) -> str:
        now = utc(now or datetime.now(timezone.utc))
        state, etag, control = self._expected(expected_generation)
        ledger, _ = self.store.load_receipts()
        self._clean(state, ledger, now)
        require_cooldown(state, ledger, slot)
        if (
            approved_sha != main_sha
            or not control.paused
            or control.permits
            and control.permits[-1].acknowledgement is None
        ):
            raise FeedScheduleError("SCHEDULE_APPROVAL_OR_REVIEW_REQUIRED")
        permit = FeedSchedulePermit(
            permit_id=str(uuid4()),
            slot_at=stamp(slot),
            expires_at=stamp(expires_at),
            approved_sha=approved_sha,
            created_at=stamp(now),
            approval_ref=review_ref,
            status="ARMED",
        )
        control.permits.append(permit)
        control.paused = False
        control.pause_reason = "ONE_SLOT_ARMED"
        self._save(state, etag, control)
        return permit.permit_id

    def enable_continuous(
        self, *, expected_generation: int, approved_sha: str, main_sha: str,
        review_ref: str, now: datetime | None = None,
    ) -> str:
        now = utc(now or datetime.now(timezone.utc))
        state, etag, control = self._expected(expected_generation)
        ledger, _ = self.store.load_receipts()
        self._clean(state, ledger, now)
        completed, publication_id = latest_success(state, ledger)
        if (approved_sha != main_sha
                or completed != parse_receipt_occurrence(control.latest_successful_feed_at)
                or publication_id != control.latest_successful_feed_id):
            raise FeedScheduleError("CONTINUOUS_APPROVAL_OR_BASELINE_INVALID")
        active = continuous_approval(control)
        if active is not None and not control.paused and active.approved_sha == approved_sha:
            return active.approval_id
        if control.permits:
            prior = control.permits[-1]
            if prior.owner_run_id is not None and not safe_completed_attempt(prior) and prior.acknowledgement is None:
                raise FeedScheduleError("CONTINUOUS_PREVIOUS_ATTEMPT_UNRESOLVED")
            if prior.status == "ARMED":
                prior.status = "REVOKED"
                prior.revoked_at = stamp(now)
        if active is not None:
            active.revoked_at = stamp(now)
        approval = FeedContinuousApproval(
            approval_id=str(uuid4()), approved_sha=approved_sha,
            created_at=stamp(now), approval_ref=review_ref,
        )
        control.continuous_approvals.append(approval)
        control.paused = False
        control.pause_reason = "CONTINUOUS_ENABLED"
        self._save(state, etag, control)
        return approval.approval_id

    def _admit_continuous(
        self, state: PublicationSafetyState, etag: str, control: FeedScheduleControl,
        ledger: PublicationReceipts, authorization: ProductionAuthorization,
        now: datetime, approval: FeedContinuousApproval,
    ) -> str | None:
        slot = now.replace(hour=17, minute=17, second=0, microsecond=0)
        expiry = slot + timedelta(minutes=60)
        if (not authorization.is_scheduled_feed
                or authorization.repository != FEED_REPOSITORY
                or authorization.workflow_path != FEED_WORKFLOW
                or type(authorization.run_attempt) is not int or authorization.run_attempt != 1
                or authorization.head_sha != approval.approved_sha
                or authorization.key != f"schedule:{authorization.run_id}"
                or not parse_receipt_occurrence(approval.created_at) <= utc(authorization.created_at)
                or not slot <= utc(authorization.created_at) <= now < expiry
                or any(a["run_id"] == authorization.run_id or a["key"] == authorization.key
                       for a in state.active_publication_state.consumed_authorizations)):
            raise FeedScheduleError("CONTINUOUS_RUN_IDENTITY_OR_WINDOW_INVALID")
        if control.permits:
            prior = control.permits[-1]
            if prior.owner_run_id is not None and (
                slot <= parse_receipt_occurrence(prior.expires_at)
                or not safe_completed_attempt(prior) and prior.acknowledgement is None
            ):
                raise FeedScheduleError("CONTINUOUS_PREVIOUS_ATTEMPT_UNRESOLVED")
            if prior.owner_run_id is None and prior.status not in {"EXPIRED", "REVOKED"}:
                raise FeedScheduleError("CONTINUOUS_PREVIOUS_PERMIT_UNRESOLVED")
        if now < parse_receipt_occurrence(control.next_eligible_at):
            # Prove the stored baseline even on a read-only cooldown skip.
            require_cooldown(state, ledger, parse_receipt_occurrence(control.next_eligible_at))
            return None
        require_cooldown(state, ledger, now)
        permit = FeedSchedulePermit(
            permit_id=str(uuid4()), slot_at=stamp(slot), expires_at=stamp(expiry),
            approved_sha=approval.approved_sha, created_at=stamp(now),
            approval_ref=approval.approval_ref, continuous_approval_id=approval.approval_id,
            status="ADMITTED", owner_run_id=authorization.run_id, owner_run_attempt=1,
            authorization_key=authorization.key, admitted_at=stamp(now),
        )
        control.permits.append(permit)
        control.pause_reason = "CONTINUOUS_ATTEMPT_ACTIVE"
        self._save(state, etag, control)
        return permit.permit_id

    def admit(
        self, authorization: ProductionAuthorization, *, now: datetime | None = None
    ) -> str | None:
        now = utc(now or datetime.now(timezone.utc))
        require_fresh(authorization, now)
        state, etag = self.store.load_safety()
        control = control_for(state).model_copy(deep=True)
        if control.paused:
            raise FeedScheduleError("SCHEDULE_NOT_ARMED")
        ledger, _ = self.store.load_receipts()
        self._clean(state, ledger, now)
        approval = continuous_approval(control)
        if approval is not None:
            return self._admit_continuous(state, etag, control, ledger, authorization, now, approval)
        if not control.permits:
            raise FeedScheduleError("SCHEDULE_NOT_ARMED")
        require_cooldown(state, ledger, now)
        permit = control.permits[-1]
        if (
            permit.status != "ARMED"
            or permit.revoked_at is not None
            or permit.acknowledgement is not None
            or not authorization.is_scheduled_feed
            or authorization.repository != FEED_REPOSITORY
            or authorization.workflow_path != FEED_WORKFLOW
            or type(authorization.run_attempt) is not int
            or authorization.run_attempt != 1
            or authorization.head_sha != permit.approved_sha
            or authorization.key != f"schedule:{authorization.run_id}"
            or not parse_receipt_occurrence(permit.slot_at)
            <= utc(authorization.created_at)
            <= now
            < parse_receipt_occurrence(permit.expires_at)
            or any(
                a["run_id"] == authorization.run_id or a["key"] == authorization.key
                for a in state.active_publication_state.consumed_authorizations
            )
        ):
            raise FeedScheduleError("SCHEDULE_IDENTITY_OR_WINDOW_INVALID")
        permit.status = "ADMITTED"
        permit.owner_run_id = authorization.run_id
        permit.owner_run_attempt = 1
        permit.authorization_key = authorization.key
        permit.admitted_at = stamp(now)
        control.paused = True
        control.pause_reason = "ADMITTED_OPERATOR_REVIEW_REQUIRED"
        self._save(state, etag, control)
        return permit.permit_id

    def pause(
        self, *, expected_generation: int, reason: str, now: datetime | None = None
    ) -> None:
        state, etag, control = self._expected(expected_generation)
        control.paused = True
        control.pause_reason = reason
        approval = continuous_approval(control)
        if approval is not None:
            approval.revoked_at = stamp(now or datetime.now(timezone.utc))
        if control.permits:
            permit = control.permits[-1]
            if permit.acknowledgement is None and permit.revoked_at is None:
                permit.revoked_at = stamp(now or datetime.now(timezone.utc))
                if permit.status == "ARMED":
                    permit.status = "REVOKED"
        self._save(state, etag, control)

    def revoke(
        self, *, expected_generation: int, reason: str, now: datetime | None = None
    ) -> None:
        state, _, control = self._expected(expected_generation)
        if not control.permits or control.permits[-1].owner_run_id is not None:
            raise FeedScheduleError("SCHEDULE_REVOKE_REQUIRES_UNCONSUMED_PERMIT")
        self.pause(expected_generation=state.generation, reason=reason, now=now)

    def record_outcome(
        self,
        authorization: ProductionAuthorization,
        *,
        interrupted: str | None = None,
        now: datetime | None = None,
    ) -> None:
        now = utc(now or datetime.now(timezone.utc))
        state, etag = self.store.load_safety()
        control = control_for(state).model_copy(deep=True)
        if not control.permits:
            raise FeedScheduleError("SCHEDULE_OUTCOME_OWNER_INVALID")
        permit = control.permits[-1]
        if (
            permit.owner_run_id != authorization.run_id
            or permit.authorization_key != authorization.key
            or permit.owner_run_attempt != authorization.run_attempt
            or permit.approved_sha != authorization.head_sha
            or authorization.repository != FEED_REPOSITORY
            or authorization.workflow_path != FEED_WORKFLOW
        ):
            raise FeedScheduleError("SCHEDULE_OUTCOME_OWNER_INVALID")
        if permit.outcome is not None:
            raise FeedScheduleError("SCHEDULE_OUTCOME_ALREADY_RECORDED")
        ledger, _ = self.store.load_receipts()
        outcome = outcome_from_state(
            state, ledger, permit, now, interrupted=interrupted
        )
        permit.outcome = outcome
        permit.status = outcome.classification
        keep_enabled = (
            not control.paused and permit.continuous_approval_id is not None
            and continuous_approval(control) is not None and safe_completed_attempt(permit)
        )
        control.paused = not keep_enabled
        control.pause_reason = (
            "CONTINUOUS_ENABLED" if keep_enabled else f"{permit.status}_OPERATOR_REVIEW_REQUIRED"
        )
        self._save(state, etag, control)

    def acknowledge(
        self,
        *,
        expected_generation: int,
        evidence_ref: str,
        audit_runs: Callable[[FeedSchedulePermit], list[dict[str, Any]]],
        now: datetime | None = None,
    ) -> None:
        now = utc(now or datetime.now(timezone.utc))
        state, etag, control = self._expected(expected_generation)
        ledger, _ = self.store.load_receipts()
        self._clean(state, ledger, now)
        if not control.permits:
            raise FeedScheduleError("SCHEDULE_REVIEW_NO_PERMIT")
        permit = control.permits[-1]
        if permit.acknowledgement is not None or now < parse_receipt_occurrence(
            permit.expires_at
        ):
            raise FeedScheduleError("SCHEDULE_REVIEW_REQUIRES_CLOSED_WINDOW")
        runs = audit_runs(permit)
        ids, conclusions = reviewed_runs(permit, runs)
        if permit.owner_run_id is None:
            if permit.status == "ARMED":
                permit.status = "EXPIRED"
        elif permit.outcome is None:
            owner = next(r for r in runs if r["id"] == permit.owner_run_id)
            interrupted = (
                "CANCELLED" if owner["conclusion"] == "cancelled" else "INCOMPLETE"
            )
            permit.outcome = outcome_from_state(
                state, ledger, permit, now, interrupted=interrupted
            )
            permit.status = permit.outcome.classification
        permit.acknowledgement = FeedScheduleReview(
            reviewed_at=stamp(now),
            evidence_ref=evidence_ref,
            run_ids=ids,
            conclusions=conclusions,
        )
        control.paused = True
        control.pause_reason = "REVIEWED_EXPLICIT_NEW_PERMIT_REQUIRED"
        self._save(state, etag, control)


def reviewed_runs(
    permit: FeedSchedulePermit, runs: list[dict[str, Any]]
) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    conclusions: list[str] = []
    for run in runs:
        if (
            type(run.get("id")) is not int
            or run["id"] < 1
            or run["id"] in ids
            or run.get("status") != "completed"
            or not isinstance(run.get("conclusion"), str)
            or run["conclusion"] not in GITHUB_TERMINAL_CONCLUSIONS
            or run.get("event") != "schedule"
            or run.get("path") != FEED_WORKFLOW
            or run.get("head_branch") != "main"
            or run.get("repository", {}).get("full_name") != FEED_REPOSITORY
            or run.get("head_sha") != permit.approved_sha
            or type(run.get("run_attempt")) is not int
            or run.get("run_attempt") != 1
            or not parse_receipt_occurrence(permit.slot_at)
            <= parse_receipt_occurrence(run["created_at"])
            < parse_receipt_occurrence(permit.expires_at)
        ):
            raise FeedScheduleError("SCHEDULE_RUN_REVIEW_INVALID_OR_NONTERMINAL")
        ids.append(run["id"])
        conclusions.append(run["conclusion"])
    if permit.owner_run_id is not None and permit.owner_run_id not in ids:
        raise FeedScheduleError("SCHEDULE_OWNER_RUN_EVIDENCE_MISSING")
    return ids, conclusions


def outcome_from_state(
    state: PublicationSafetyState,
    ledger: PublicationReceipts,
    permit: FeedSchedulePermit,
    now: datetime,
    *,
    interrupted: str | None = None,
) -> FeedScheduleOutcome:
    rows = [
        r
        for r in state.active_publication_state.posted_artworks
        if r["publication_id"] == permit.publication_id
    ]
    statuses = {r["status"] for r in rows}
    receipt = next(
        (r for r in ledger.records if r.publication_id == permit.publication_id), None
    )
    if statuses == {"PUBLISHED"}:
        publication_state.validate_live_receipt_coverage(state, ledger)
        classification = "SUCCESS"
    elif statuses & {"PUBLISHING", "AMBIGUOUS"}:
        classification = "AMBIGUOUS"
    elif statuses == {"EXPIRED"} or permit.publication_id is None:
        classification = interrupted or "DEFINITIVE_FAILURE"
    else:
        classification = interrupted or "INCOMPLETE"
    media_ids = {r.get("publish_response_media_id") or r.get("media_id") for r in rows}
    media_ids.discard(None)
    media_id = receipt.instagram_media_id if receipt else next(iter(media_ids), None)
    return FeedScheduleOutcome(
        classification=classification,
        recorded_at=stamp(now),
        safety_generation=state.generation + 1,
        receipt_generation=ledger.generation,
        publication_id=permit.publication_id,
        instagram_media_id=media_id,
        cleanup_pending=bool(
            state.active_publication_state.staging_media_cleanup_queue
            or state.active_publication_state.reel_staging_cleanup_queue
        ),
        reconciliation_pending=bool(
            statuses & {"PENDING", "PUBLISHING", "AMBIGUOUS"}
            or state.active_publication_state.receipt_sync_pending
        ),
    )


def reserve_in_history(
    history: dict[str, Any],
    authorization: ProductionAuthorization,
    publication_id: str,
    now: datetime,
) -> None:
    state = publication_state.validate_safety_state(history["_safety_state"])
    require_owner(state, authorization, status="ADMITTED", now=now)
    control = control_for(state).model_copy(deep=True)
    current = control.permits[-1]
    current.status = "RESERVED"
    current.publication_id = publication_id
    current.reservation_generation = state.generation + 1
    history["feed_schedule_control"] = control.model_dump(mode="json")


def before_publish_in_history(
    history: dict[str, Any],
    publication_id: str,
    authorization: ProductionAuthorization | None,
    now: datetime,
) -> None:
    consumed = next(
        (
            a
            for a in history.get("consumed_authorizations", [])
            if a["publication_id"] == publication_id
            and a["key"].startswith("schedule:")
        ),
        None,
    )
    if consumed is None:
        if authorization is not None and authorization.is_scheduled_feed:
            raise FeedScheduleError("SCHEDULE_RESERVATION_MISSING")
        return
    if authorization is None:
        raise FeedScheduleError("SCHEDULE_BOUNDARY_REQUIRES_OWNER")
    state = publication_state.validate_safety_state(history["_safety_state"])
    permit = require_owner(state, authorization, status="RESERVED", now=now)
    if permit.publication_id != publication_id or consumed["key"] != authorization.key:
        raise FeedScheduleError("SCHEDULE_RESERVATION_OWNER_INVALID")
    ledger, _ = publication_state.PublicationStateStore().load_receipts()
    require_cooldown(state, ledger, now)
    # Our own PENDING unit/media are expected; every other unresolved unit still blocks.
    filtered = state.model_dump(mode="json")
    filtered["active_publication_state"]["posted_artworks"] = [
        r
        for r in filtered["active_publication_state"]["posted_artworks"]
        if r["publication_id"] != publication_id
    ]
    require_clear_publication_state(
        publication_state.validate_safety_state(publication_state.seal(filtered)),
        ledger,
        owned_feed_ids=r2_media.list_owned_publication_ids() - {publication_id},
        owned_reel_ids=r2_media.list_owned_publication_ids(reel=True),
        now=now,
    )
    control = control_for(state).model_copy(deep=True)
    control.permits[-1].status = "PUBLISHING"
    history["feed_schedule_control"] = control.model_dump(mode="json")
