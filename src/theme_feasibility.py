"""Bounded, fail-soft theme acquisition evidence and attempt scoring."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import config
from src.carousel_themes import CarouselThemeDefinition
from src.format_contracts import constrained_adapter_source_ids
from src.theme_acquisition import AcquisitionRunState, ThemeAvailabilityResult

logger = logging.getLogger(__name__)

FEASIBILITY_OBJECT_KEY = "theme_feasibility.json"
SCHEMA_VERSION = 1
MAX_OBSERVATIONS_PER_THEME = 12
MAX_TRACKED_THEMES = 256
CONDITIONAL_WRITE_ATTEMPTS = 3

# Editorial totals normally span roughly 30-62. Historical availability may
# break close editorial ties, but cannot overwhelm meaningful fatigue/priority.
MAX_HISTORICAL_PENALTY = 6.0
MAX_HISTORICAL_BOOST = 3.0
IMPOSSIBLE_SOURCE_PENALTY = 8.0
MAX_TOTAL_FEASIBILITY_PENALTY = 8.0
MAX_TOTAL_FEASIBILITY_BOOST = 3.0
LOW_SOURCE_CAPACITY_PENALTY = 3.0
REDUCED_SOURCE_CAPACITY_PENALTY = 1.0
HISTORICAL_CONFIDENCE_SAMPLES = 3.0
RECENCY_HALF_LIFE_DAYS = 30.0

_R2_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class ThemeFeasibilityStorageError(RuntimeError):
    """Feasibility telemetry could not be safely read or written."""


class ThemeFeasibilityConcurrencyError(ThemeFeasibilityStorageError):
    """A conditional R2 update lost a concurrent write."""


@dataclass(frozen=True)
class ThemeFeasibilityObservation:
    theme_id: str
    attempted_at: str
    success: bool
    pool_status: str
    qualified_count: int
    absolute_minimum: int
    preferred_target: int
    raw_count: int
    unique_count: int
    rights_qualified_count: int
    quality_qualified_count: int
    relevance_qualified_count: int
    queries_attempted: int
    active_sources: tuple[str, ...]
    unavailable_sources: tuple[str, ...]
    disabled_sources: tuple[str, ...]
    failure_reason: str | None
    outcome_category: str
    evidence_mode: str
    adapter_failures: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class SourceCapacity:
    compatible_sources: tuple[str, ...]
    active_sources: tuple[str, ...]
    unavailable_sources: tuple[str, ...]
    disabled_sources: tuple[str, ...]
    adjustment: float
    reason: str


@dataclass(frozen=True)
class ThemeAttemptAssessment:
    theme: CarouselThemeDefinition
    editorial_score: float
    feasibility_adjustment: float
    final_attempt_score: float
    historical_sample_count: int
    historical_success_signal: float
    evidence_age_days: float | None
    qualified_headroom_signal: float
    feasibility_confidence: float
    capacity: SourceCapacity
    reason_summary: str


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "themes": {}}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _non_negative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_string_list(value: object) -> bool:
    return isinstance(value, list) and len(value) <= 64 and all(
        isinstance(item, str) and 0 < len(item) <= 80 for item in value
    )


def _validate_observation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ThemeFeasibilityStorageError("Malformed feasibility observation")
    required_strings = ("theme_id", "pool_status", "outcome_category", "evidence_mode")
    if any(
        not isinstance(value.get(field), str) or not value[field] or len(value[field]) > 120
        for field in required_strings
    ):
        raise ThemeFeasibilityStorageError("Malformed feasibility observation identity")
    if _parse_timestamp(value.get("attempted_at")) is None:
        raise ThemeFeasibilityStorageError("Malformed feasibility observation timestamp")
    if not isinstance(value.get("success"), bool):
        raise ThemeFeasibilityStorageError("Malformed feasibility observation outcome")
    numeric_fields = (
        "qualified_count",
        "absolute_minimum",
        "preferred_target",
        "raw_count",
        "unique_count",
        "rights_qualified_count",
        "quality_qualified_count",
        "relevance_qualified_count",
        "queries_attempted",
    )
    if any(not _non_negative_int(value.get(field)) for field in numeric_fields):
        raise ThemeFeasibilityStorageError("Malformed feasibility observation counts")
    if value["absolute_minimum"] < 1 or value["preferred_target"] < 1:
        raise ThemeFeasibilityStorageError("Malformed feasibility observation thresholds")
    for field in ("active_sources", "unavailable_sources", "disabled_sources"):
        if not _valid_string_list(value.get(field)):
            raise ThemeFeasibilityStorageError("Malformed feasibility source context")
    failure_reason = value.get("failure_reason")
    if failure_reason is not None and (
        not isinstance(failure_reason, str) or len(failure_reason) > 120
    ):
        raise ThemeFeasibilityStorageError("Malformed feasibility failure reason")
    failures = value.get("adapter_failures")
    if not isinstance(failures, list) or len(failures) > 100:
        raise ThemeFeasibilityStorageError("Malformed adapter failure evidence")
    safe_failure_fields = {
        "source", "http_status", "operation", "category", "retryable", "disabled_for_run"
    }
    for failure in failures:
        if not isinstance(failure, dict) or not set(failure) <= safe_failure_fields:
            raise ThemeFeasibilityStorageError("Malformed adapter failure evidence")
    return value


def validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ThemeFeasibilityStorageError("Malformed theme feasibility object")
    themes = value.get("themes")
    if not isinstance(themes, dict) or len(themes) > MAX_TRACKED_THEMES:
        raise ThemeFeasibilityStorageError("Malformed theme feasibility themes")
    for theme_id, observations in themes.items():
        if not isinstance(theme_id, str) or not theme_id or len(theme_id) > 120:
            raise ThemeFeasibilityStorageError("Malformed theme feasibility theme id")
        if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS_PER_THEME:
            raise ThemeFeasibilityStorageError("Unbounded theme feasibility observations")
        for observation in observations:
            validated = _validate_observation(observation)
            if validated["theme_id"] != theme_id:
                raise ThemeFeasibilityStorageError("Mismatched feasibility theme id")
    return value


def _is_missing(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


def _is_conflict(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"PreconditionFailed", "412"} or status == 412


class ThemeFeasibilityStorage:
    """Small standalone state object; production uses R2, local runs use a file."""

    def __init__(
        self,
        *,
        s3_client: object | None = None,
        bucket_name: str | None = None,
        local_path: str | Path | None = None,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket_name
        self._local_path = Path(local_path) if local_path is not None else None
        if self._s3 is None and self._local_path is None:
            account_id = os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", "").strip()
            access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
            secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()
            bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "").strip()
            if all((account_id, access_key, secret_key, bucket)):
                self._s3 = boto3.client(
                    "s3",
                    endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name="auto",
                    config=_R2_CONFIG,
                )
                self._bucket = bucket
            else:
                self._local_path = Path(config.DATA_DIR) / FEASIBILITY_OBJECT_KEY

    def load(self) -> tuple[dict[str, Any], str | None]:
        if self._local_path is not None:
            if not self._local_path.exists():
                return _empty_state(), None
            try:
                payload = json.loads(self._local_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ThemeFeasibilityStorageError(
                    "Local theme feasibility state is unreadable"
                ) from error
            return validate_state(payload), None
        try:
            response = self._s3.get_object(
                Bucket=self._bucket, Key=FEASIBILITY_OBJECT_KEY
            )
        except ClientError as error:
            if _is_missing(error):
                return _empty_state(), None
            raise ThemeFeasibilityStorageError(
                "Unable to read theme feasibility state from R2"
            ) from error
        try:
            payload = json.loads(response["Body"].read().decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ThemeFeasibilityStorageError(
                "R2 theme feasibility state is unreadable"
            ) from error
        etag = response.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise ThemeFeasibilityStorageError("R2 theme feasibility ETag is missing")
        return validate_state(payload), etag

    def _write(self, state: dict[str, Any], etag: str | None) -> None:
        payload = (
            json.dumps(validate_state(state), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if self._local_path is not None:
            self._local_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._local_path.with_name(f".{self._local_path.name}.tmp")
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, self._local_path)
            except OSError as error:
                raise ThemeFeasibilityStorageError(
                    "Unable to write local theme feasibility state"
                ) from error
            return
        request: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": FEASIBILITY_OBJECT_KEY,
            "Body": payload,
            "ContentType": "application/json",
        }
        request["IfMatch" if etag else "IfNoneMatch"] = etag or "*"
        try:
            self._s3.put_object(**request)
        except ClientError as error:
            if _is_conflict(error):
                raise ThemeFeasibilityConcurrencyError(
                    "Theme feasibility conditional write conflict"
                ) from error
            raise ThemeFeasibilityStorageError(
                "Unable to write theme feasibility state to R2"
            ) from error

    def append(self, observation: ThemeFeasibilityObservation) -> None:
        serialized = asdict(observation)
        serialized["active_sources"] = list(observation.active_sources)
        serialized["unavailable_sources"] = list(observation.unavailable_sources)
        serialized["disabled_sources"] = list(observation.disabled_sources)
        serialized["adapter_failures"] = [dict(item) for item in observation.adapter_failures]
        _validate_observation(serialized)
        for attempt in range(CONDITIONAL_WRITE_ATTEMPTS):
            state, etag = self.load()
            themes = dict(state["themes"])
            observations = [*themes.get(observation.theme_id, ()), serialized]
            observations.sort(key=lambda item: (item["attempted_at"], item["theme_id"]))
            themes[observation.theme_id] = observations[-MAX_OBSERVATIONS_PER_THEME:]
            if len(themes) > MAX_TRACKED_THEMES:
                oldest_first = sorted(
                    themes,
                    key=lambda theme_id: (
                        themes[theme_id][-1]["attempted_at"] if themes[theme_id] else "",
                        theme_id,
                    ),
                )
                for theme_id in oldest_first[: len(themes) - MAX_TRACKED_THEMES]:
                    del themes[theme_id]
            try:
                self._write({"schema_version": SCHEMA_VERSION, "themes": themes}, etag)
                return
            except ThemeFeasibilityConcurrencyError:
                if attempt + 1 == CONDITIONAL_WRITE_ATTEMPTS:
                    raise


def load_feasibility_state(
    storage: ThemeFeasibilityStorage | None = None,
) -> dict[str, Any]:
    """Cold-start on missing/corrupt/unavailable telemetry; publishing must continue."""
    try:
        state, _ = (storage or ThemeFeasibilityStorage()).load()
        return state
    except Exception as error:
        logger.warning(
            "theme_feasibility_read_failed error=%s result=cold_start",
            type(error).__name__,
        )
        return _empty_state()


def source_capacity(
    theme: CarouselThemeDefinition,
    adapters: Sequence[object],
    run_state: AcquisitionRunState,
) -> SourceCapacity:
    available_by_config: set[str] = set()
    unavailable: set[str] = set()
    all_sources: set[str] = set()
    for adapter in adapters:
        source = str(getattr(adapter, "source_id", type(adapter).__name__)).casefold()
        all_sources.add(source)
        try:
            reason = getattr(adapter, "unavailable_reason", lambda: None)()
        except Exception:
            reason = "UNKNOWN"
        if reason:
            unavailable.add(source)
        else:
            available_by_config.add(source)
    constrained = constrained_adapter_source_ids(theme)
    compatible = set(constrained) if constrained else all_sources
    unavailable &= compatible
    disabled = set(run_state.disabled_adapters) & compatible
    active = (available_by_config & compatible) - disabled
    if not active:
        adjustment = -IMPOSSIBLE_SOURCE_PENALTY
        reason = "no_active_compatible_sources"
    elif constrained:
        adjustment = 0.0
        reason = "constrained_source_available"
    elif len(active) == 1:
        adjustment = -LOW_SOURCE_CAPACITY_PENALTY
        reason = "single_active_source"
    elif len(active) == 2:
        adjustment = -REDUCED_SOURCE_CAPACITY_PENALTY
        reason = "two_active_sources"
    else:
        adjustment = 0.0
        reason = "source_capacity_healthy"
    return SourceCapacity(
        compatible_sources=tuple(sorted(compatible)),
        active_sources=tuple(sorted(active)),
        unavailable_sources=tuple(sorted(unavailable)),
        disabled_sources=tuple(sorted(disabled)),
        adjustment=adjustment,
        reason=reason,
    )


def _observation_signal(observation: Mapping[str, object]) -> tuple[float, float]:
    minimum = max(1, int(observation["absolute_minimum"]))
    target = max(minimum, int(observation["preferred_target"]))
    qualified = max(0, int(observation["qualified_count"]))
    if bool(observation["success"]):
        headroom = min(1.0, max(0.0, (qualified - minimum) / max(1, target - minimum)))
        return 0.2 + 0.8 * headroom, headroom
    deficit = min(1.0, max(0.0, (minimum - qualified) / minimum))
    return -max(0.25, deficit), -deficit


def _source_similarity(observation: Mapping[str, object], active_sources: set[str]) -> float:
    previous = set(str(item) for item in observation.get("active_sources", ()))
    if not previous or not active_sources:
        return 0.5
    return 0.5 + 0.5 * (len(previous & active_sources) / len(previous | active_sources))


def assess_theme(
    theme: CarouselThemeDefinition,
    *,
    editorial_score: float,
    state: Mapping[str, object],
    adapters: Sequence[object],
    run_state: AcquisitionRunState,
    now: datetime,
) -> ThemeAttemptAssessment:
    capacity = source_capacity(theme, adapters, run_state)
    raw_themes = state.get("themes", {}) if isinstance(state, Mapping) else {}
    observations = (
        raw_themes.get(theme.id, ()) if isinstance(raw_themes, Mapping) else ()
    )
    weighted_signal = 0.0
    weighted_headroom = 0.0
    total_weight = 0.0
    usable_samples = 0
    newest_age: float | None = None
    success_weight = 0.0
    for observation in observations if isinstance(observations, list) else ():
        if not isinstance(observation, Mapping):
            continue
        attempted_at = _parse_timestamp(observation.get("attempted_at"))
        if attempted_at is None:
            continue
        age_days = max(0.0, (now.astimezone(timezone.utc) - attempted_at).total_seconds() / 86400)
        newest_age = age_days if newest_age is None else min(newest_age, age_days)
        category = observation.get("outcome_category")
        if category not in {"success", "theme_unavailable", "source_degraded"}:
            continue
        category_weight = 0.35 if category == "source_degraded" else 1.0
        weight = (
            0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            * _source_similarity(observation, set(capacity.active_sources))
            * category_weight
        )
        if weight <= 0:
            continue
        signal, headroom = _observation_signal(observation)
        weighted_signal += signal * weight
        weighted_headroom += headroom * weight
        success_weight += (1.0 if bool(observation.get("success")) else 0.0) * weight
        total_weight += weight
        usable_samples += 1
    if total_weight:
        signal = weighted_signal / total_weight
        headroom_signal = weighted_headroom / total_weight
        success_signal = success_weight / total_weight
        confidence = min(1.0, total_weight / HISTORICAL_CONFIDENCE_SAMPLES)
        bound = MAX_HISTORICAL_BOOST if signal >= 0 else MAX_HISTORICAL_PENALTY
        historical_adjustment = signal * confidence * bound
    else:
        signal = headroom_signal = success_signal = confidence = historical_adjustment = 0.0
    feasibility_adjustment = max(
        -MAX_TOTAL_FEASIBILITY_PENALTY,
        min(
            MAX_TOTAL_FEASIBILITY_BOOST,
            capacity.adjustment + historical_adjustment,
        ),
    )
    reason = (
        f"capacity={capacity.reason};history=neutral"
        if not usable_samples
        else f"capacity={capacity.reason};history_signal={signal:+.2f}"
    )
    return ThemeAttemptAssessment(
        theme=theme,
        editorial_score=round(editorial_score, 4),
        feasibility_adjustment=round(feasibility_adjustment, 4),
        final_attempt_score=round(editorial_score + feasibility_adjustment, 4),
        historical_sample_count=usable_samples,
        historical_success_signal=round(success_signal, 4),
        evidence_age_days=round(newest_age, 2) if newest_age is not None else None,
        qualified_headroom_signal=round(headroom_signal, 4),
        feasibility_confidence=round(confidence, 4),
        capacity=capacity,
        reason_summary=reason,
    )


class FeasibilityAttemptRanker:
    """Order editorially eligible themes that current source capacity can attempt."""

    def __init__(
        self,
        *,
        editorial_scores: Mapping[str, float],
        state: Mapping[str, object],
        adapters: Sequence[object],
        run_state: AcquisitionRunState,
        now: datetime,
        log_limit: int = 5,
    ) -> None:
        self._editorial_scores = editorial_scores
        self._state = state
        self._adapters = adapters
        self._run_state = run_state
        self._now = now
        self._log_limit = log_limit
        self._last_logged_signature: tuple[object, ...] | None = None

    def assess(self, themes: Sequence[CarouselThemeDefinition]) -> tuple[ThemeAttemptAssessment, ...]:
        assessments = tuple(
            assess_theme(
                theme,
                editorial_score=self._editorial_scores.get(theme.id, 0.0),
                state=self._state,
                adapters=self._adapters,
                run_state=self._run_state,
                now=self._now,
            )
            for theme in themes
        )
        original_index = {theme.id: index for index, theme in enumerate(themes)}
        return tuple(
            sorted(
                assessments,
                key=lambda item: (
                    -item.final_attempt_score,
                    original_index[item.theme.id],
                    item.theme.id,
                ),
            )
        )

    def rank(self, themes: Sequence[CarouselThemeDefinition]) -> tuple[CarouselThemeDefinition, ...]:
        return tuple(
            item.theme
            for item in self.assess(themes)
            if item.capacity.active_sources
        )

    def log_shortlist(self, themes: Sequence[CarouselThemeDefinition]) -> None:
        """Log a bounded shortlist only when its explainable state changes."""
        assessments = self.assess(themes)[: self._log_limit]
        signature = tuple(
            (
                item.theme.id,
                item.final_attempt_score,
                item.capacity.disabled_sources,
                item.capacity.unavailable_sources,
            )
            for item in assessments
        )
        if signature == self._last_logged_signature:
            return
        self._last_logged_signature = signature
        for item in assessments:
            logger.info(
                "theme_feasibility theme=%s editorial_score=%.2f feasibility_adjustment=%+.2f "
                "final_attempt_score=%.2f historical_sample_count=%s historical_success_signal=%.2f "
                "evidence_age_days=%s qualified_headroom_signal=%+.2f compatible_sources=%s "
                "unavailable_sources=%s disabled_sources=%s feasibility_confidence=%.2f reasons=%s",
                item.theme.id,
                item.editorial_score,
                item.feasibility_adjustment,
                item.final_attempt_score,
                item.historical_sample_count,
                item.historical_success_signal,
                item.evidence_age_days if item.evidence_age_days is not None else "none",
                item.qualified_headroom_signal,
                ",".join(item.capacity.compatible_sources) or "none",
                ",".join(item.capacity.unavailable_sources) or "none",
                ",".join(item.capacity.disabled_sources) or "none",
                item.feasibility_confidence,
                item.reason_summary,
            )


def _outcome_category(
    availability: ThemeAvailabilityResult,
    capacity: SourceCapacity,
) -> str:
    if availability.sufficient:
        return "success"
    if availability.adapter_failures or capacity.disabled_sources or not capacity.active_sources:
        return "source_degraded"
    if availability.failure_reason in {
        "no_adapter_results",
        "all_candidates_already_posted",
        "insufficient_format_target_pool",
        "insufficient_unique_pool",
        "insufficient_rights_policy_pool",
        "insufficient_quality_pool",
        "insufficient_relevance_pool",
        "image_validation_exhausted",
        "insufficient_final_relevance_pool",
        "insufficient_eligible_featured_pool",
    }:
        return "theme_unavailable"
    return "other_availability"


def observation_from_result(
    availability: ThemeAvailabilityResult,
    *,
    theme: CarouselThemeDefinition,
    adapters: Sequence[object],
    run_state: AcquisitionRunState,
    attempted_at: datetime,
) -> ThemeFeasibilityObservation:
    capacity = source_capacity(theme, adapters, run_state)
    failures = tuple(
        {
            "source": failure.source_id,
            "http_status": failure.http_status,
            "operation": failure.operation,
            "category": failure.category,
            "retryable": failure.retryable,
            "disabled_for_run": failure.disabled_for_run,
        }
        for failure in availability.adapter_failures
    )
    return ThemeFeasibilityObservation(
        theme_id=theme.id,
        attempted_at=attempted_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        success=availability.sufficient,
        pool_status=availability.pool_status,
        qualified_count=availability.estimated_safe_pool,
        absolute_minimum=availability.absolute_minimum,
        preferred_target=availability.target,
        raw_count=availability.raw_candidates,
        unique_count=availability.unique_candidates,
        rights_qualified_count=availability.rights_eligible,
        quality_qualified_count=availability.quality_eligible,
        relevance_qualified_count=availability.relevance_eligible,
        queries_attempted=availability.query_count,
        active_sources=capacity.active_sources,
        unavailable_sources=capacity.unavailable_sources,
        disabled_sources=capacity.disabled_sources,
        failure_reason=availability.failure_reason,
        outcome_category=_outcome_category(availability, capacity),
        evidence_mode=theme.evidence_mode.value,
        adapter_failures=failures,
    )


def record_theme_availability(
    availability: ThemeAvailabilityResult,
    *,
    theme: CarouselThemeDefinition,
    adapters: Sequence[object],
    run_state: AcquisitionRunState,
    attempted_at: datetime,
    storage: ThemeFeasibilityStorage | None = None,
) -> bool:
    """Best-effort telemetry: every failure is isolated from publication."""
    try:
        observation = observation_from_result(
            availability,
            theme=theme,
            adapters=adapters,
            run_state=run_state,
            attempted_at=attempted_at,
        )
        (storage or ThemeFeasibilityStorage()).append(observation)
    except Exception as error:
        logger.warning(
            "theme_feasibility_write_failed theme=%s error=%s result=ignored",
            theme.id,
            type(error).__name__,
        )
        return False
    logger.info(
        "theme_feasibility_recorded theme=%s result=%s qualified=%s category=%s",
        theme.id,
        "success" if observation.success else "failure",
        observation.qualified_count,
        observation.outcome_category,
    )
    return True
