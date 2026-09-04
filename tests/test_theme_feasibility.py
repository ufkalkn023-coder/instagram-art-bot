from __future__ import annotations

import json
from io import BytesIO
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from src.carousel_themes import get_default_theme_registry
from src.museums.base import AdapterHTTPError
from src.theme_acquisition import (
    AcquisitionRunState,
    AdapterFailure,
    ThemeAvailabilityResult,
    acquire_theme_candidates,
)
from src.theme_fallback import ThemeAttemptPlanner
from src.theme_feasibility import (
    MAX_HISTORICAL_BOOST,
    MAX_HISTORICAL_PENALTY,
    MAX_OBSERVATIONS_PER_THEME,
    MAX_TOTAL_FEASIBILITY_PENALTY,
    FeasibilityAttemptRanker,
    ThemeFeasibilityStorage,
    assess_theme,
    load_feasibility_state,
    observation_from_result,
    record_theme_availability,
    source_capacity,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


class Adapter:
    def __init__(self, source_id: str, unavailable_reason: str | None = None):
        self.source_id = source_id
        self._unavailable_reason = unavailable_reason

    def unavailable_reason(self):
        return self._unavailable_reason


def _adapters(*, rijksmuseum_available=True):
    return tuple(
        Adapter(source, "missing_api_key" if source == "rijksmuseum" and not rijksmuseum_available else None)
        for source in ("aic", "cleveland", "met", "rijksmuseum", "smithsonian", "europeana")
    )


def _observation(
    theme_id: str,
    *,
    age_days: float = 0,
    success: bool = False,
    qualified: int = 0,
    category: str | None = None,
):
    return {
        "theme_id": theme_id,
        "attempted_at": (NOW - timedelta(days=age_days)).isoformat(),
        "success": success,
        "pool_status": "preferred" if success and qualified >= 12 else "narrow" if success else "unavailable",
        "qualified_count": qualified,
        "absolute_minimum": 5,
        "preferred_target": 12,
        "raw_count": qualified,
        "unique_count": qualified,
        "rights_qualified_count": qualified,
        "quality_qualified_count": qualified,
        "relevance_qualified_count": qualified,
        "queries_attempted": 3,
        "active_sources": ["aic", "cleveland", "met"],
        "unavailable_sources": [],
        "disabled_sources": [],
        "failure_reason": None if success else "insufficient_relevance_pool",
        "outcome_category": category or ("success" if success else "theme_unavailable"),
        "evidence_mode": "METADATA",
        "adapter_failures": [],
    }


def _state(theme_id: str, observations):
    return {"schema_version": 1, "themes": {theme_id: list(observations)}}


def _assessment(theme_id: str, observations, *, editorial_score=50.0):
    theme = get_default_theme_registry().by_id(theme_id)
    return assess_theme(
        theme,
        editorial_score=editorial_score,
        state=_state(theme_id, observations),
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )


def _availability(theme_id: str, *, success: bool, qualified: int, failures=()):
    return ThemeAvailabilityResult(
        theme_id=theme_id,
        raw_candidates=qualified + 2,
        unique_candidates=qualified + 1,
        history_eligible=qualified,
        rights_eligible=qualified,
        relevance_eligible=qualified,
        quality_eligible=qualified,
        estimated_safe_pool=qualified,
        target=12,
        sufficient=success,
        failure_reason=None if success else "insufficient_relevance_pool",
        query_count=3,
        network_call_count=9,
        adapter_failures=tuple(failures),
        absolute_minimum=5,
        pool_status="preferred" if success and qualified >= 12 else "narrow" if success else "unavailable",
    )


def test_cold_start_preserves_existing_editorial_order_and_unknown_theme_is_explorable():
    registry = get_default_theme_registry()
    themes = (
        registry.by_id("windows_within_paintings"),
        registry.by_id("baroque_drama"),
        registry.by_id("approaches_to_portraiture"),
    )
    scores = {theme.id: 60.0 - index for index, theme in enumerate(themes)}
    ranker = FeasibilityAttemptRanker(
        editorial_scores=scores,
        state={"schema_version": 1, "themes": {}},
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    assert ranker.rank(themes) == themes
    assert set(ranker.rank(themes)) == set(themes)


def test_current_source_impossibility_does_not_consume_attempt_before_viable_theme():
    registry = get_default_theme_registry()
    impossible = registry.by_id("rijksmuseum_spotlight")
    viable = registry.by_id("baroque_drama")
    ranker = FeasibilityAttemptRanker(
        editorial_scores={impossible.id: 62, viable.id: 50},
        state={"schema_version": 1, "themes": {}},
        adapters=_adapters(rijksmuseum_available=False),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    impossible_assessment = next(
        item for item in ranker.assess((impossible, viable)) if item.theme == impossible
    )
    planner = ThemeAttemptPlanner(
        (impossible, viable), attempt_limit=1, ranker=ranker.rank
    )

    assert impossible_assessment.feasibility_adjustment == -8
    assert impossible_assessment.final_attempt_score == 54
    assert planner.preview() == (viable,)


def test_viable_themes_retain_final_attempt_score_ordering():
    registry = get_default_theme_registry()
    themes = tuple(
        registry.by_id(theme_id)
        for theme_id in (
            "windows_within_paintings",
            "baroque_drama",
            "approaches_to_portraiture",
        )
    )
    ranker = FeasibilityAttemptRanker(
        editorial_scores={themes[0].id: 50, themes[1].id: 57, themes[2].id: 53},
        state={"schema_version": 1, "themes": {}},
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    assert ranker.rank(themes) == (themes[1], themes[2], themes[0])


def test_maximum_historical_penalty_remains_soft_for_currently_viable_theme():
    registry = get_default_theme_registry()
    penalized = registry.by_id("windows_within_paintings")
    preferred = registry.by_id("baroque_drama")
    failures = []
    for _ in range(6):
        observation = _observation(penalized.id)
        observation["active_sources"] = [
            "aic",
            "cleveland",
            "europeana",
            "met",
            "rijksmuseum",
            "smithsonian",
        ]
        failures.append(observation)
    ranker = FeasibilityAttemptRanker(
        editorial_scores={penalized.id: 50, preferred.id: 51},
        state=_state(penalized.id, failures),
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    assessment = next(
        item for item in ranker.assess((penalized, preferred)) if item.theme == penalized
    )

    assert assessment.feasibility_adjustment == -MAX_HISTORICAL_PENALTY
    assert ranker.rank((penalized, preferred)) == (preferred, penalized)


def test_new_theme_with_active_compatible_sources_remains_explorable():
    registry = get_default_theme_registry()
    new_theme = registry.by_id("baroque_drama").model_copy(
        update={"id": "newly_added_theme"}
    )
    ranker = FeasibilityAttemptRanker(
        editorial_scores={new_theme.id: 50},
        state={"schema_version": 1, "themes": {}},
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    assert ranker.rank((new_theme,)) == (new_theme,)


def test_recent_failures_scale_but_remain_bounded_and_never_blacklist():
    one = _assessment("windows_within_paintings", [_observation("windows_within_paintings")])
    repeated = _assessment(
        "windows_within_paintings",
        [_observation("windows_within_paintings") for _ in range(6)],
    )

    assert -MAX_HISTORICAL_PENALTY <= repeated.feasibility_adjustment < one.feasibility_adjustment < 0
    assert repeated.feasibility_adjustment >= -MAX_TOTAL_FEASIBILITY_PENALTY

    theme = get_default_theme_registry().by_id("windows_within_paintings")
    planner = ThemeAttemptPlanner(
        [theme],
        attempt_limit=1,
        ranker=lambda themes: (theme,),
    )
    assert planner.next_theme() == theme


def test_stale_failure_decays_toward_neutral():
    recent = _assessment("baroque_drama", [_observation("baroque_drama", age_days=1)])
    stale = _assessment("baroque_drama", [_observation("baroque_drama", age_days=365)])

    assert recent.feasibility_adjustment < stale.feasibility_adjustment < 0
    assert abs(stale.feasibility_adjustment) < 0.01


def test_recent_success_and_headroom_produce_bounded_positive_evidence():
    barely = _assessment(
        "approaches_to_portraiture",
        [_observation("approaches_to_portraiture", success=True, qualified=5)],
    )
    strong = _assessment(
        "approaches_to_portraiture",
        [_observation("approaches_to_portraiture", success=True, qualified=13)],
    )

    assert 0 < barely.feasibility_adjustment < strong.feasibility_adjustment
    assert strong.feasibility_adjustment <= MAX_HISTORICAL_BOOST
    assert strong.qualified_headroom_signal > barely.qualified_headroom_signal


def test_source_capacity_is_theme_specific_and_generic_fleet_stays_viable():
    registry = get_default_theme_registry()
    adapters = _adapters(rijksmuseum_available=False)
    state = AcquisitionRunState()

    constrained = source_capacity(registry.by_id("rijksmuseum_spotlight"), adapters, state)
    generic = source_capacity(registry.by_id("baroque_drama"), adapters, state)

    assert constrained.active_sources == ()
    assert constrained.unavailable_sources == ("rijksmuseum",)
    assert constrained.adjustment == -MAX_TOTAL_FEASIBILITY_PENALTY
    assert generic.adjustment == 0
    assert len(generic.active_sources) == 5


def test_run_local_met_disable_affects_met_theme_but_not_healthy_generic_theme():
    registry = get_default_theme_registry()
    state = AcquisitionRunState()
    state.disable("met", "HTTP403")

    met = source_capacity(registry.by_id("metropolitan_museum_spotlight"), _adapters(), state)
    generic = source_capacity(registry.by_id("approaches_to_portraiture"), _adapters(), state)

    assert met.active_sources == ()
    assert met.disabled_sources == ("met",)
    assert met.adjustment == -MAX_TOTAL_FEASIBILITY_PENALTY
    assert generic.adjustment == 0


def test_met_circuit_breaker_makes_theme_current_run_impossible_for_later_attempts():
    registry = get_default_theme_registry()
    generic = registry.by_id("baroque_drama")
    met = registry.by_id("metropolitan_museum_spotlight")
    cleveland = registry.by_id("cleveland_museum_spotlight")
    run_state = AcquisitionRunState()
    ranker = FeasibilityAttemptRanker(
        editorial_scores={generic.id: 61, met.id: 60, cleveland.id: 59},
        state={"schema_version": 1, "themes": {}},
        adapters=_adapters(),
        run_state=run_state,
        now=NOW,
    )
    planner = ThemeAttemptPlanner(
        (generic, met, cleveland), attempt_limit=3, ranker=ranker.rank
    )

    assert planner.next_theme() == generic
    planner.record_failure(generic, "insufficient_relevance_pool")

    class FailingMet:
        source_id = "met"

        def fetch_candidates(self, **kwargs):
            raise AdapterHTTPError(self.source_id, 403)

    acquire_theme_candidates(
        met,
        posted_ids=set(),
        adapters=[FailingMet()],
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        run_state=run_state,
    )

    assert run_state.disabled_adapters == {"met": "HTTP403"}
    assert planner.next_theme() == cleveland
    assert met not in planner.remaining_preview()


def test_availability_evidence_moves_viable_portrait_theme_earlier():
    registry = get_default_theme_registry()
    windows = registry.by_id("windows_within_paintings")
    baroque = registry.by_id("baroque_drama")
    portrait = registry.by_id("approaches_to_portraiture")
    state = {
        "schema_version": 1,
        "themes": {
            windows.id: [_observation(windows.id, qualified=1) for _ in range(4)],
            baroque.id: [_observation(baroque.id, qualified=0) for _ in range(4)],
            portrait.id: [
                _observation(portrait.id, success=True, qualified=13)
                for _ in range(3)
            ],
        },
    }
    ranker = FeasibilityAttemptRanker(
        editorial_scores={windows.id: 54, baroque.id: 53, portrait.id: 51},
        state=state,
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        now=NOW,
    )

    ordered = ranker.rank((windows, baroque, portrait))

    assert ordered[0] == portrait
    assert set(ordered) == {windows, baroque, portrait}


def test_local_storage_missing_and_corrupt_state_fail_soft(tmp_path):
    path = tmp_path / "theme_feasibility.json"
    storage = ThemeFeasibilityStorage(local_path=path)
    assert load_feasibility_state(storage) == {"schema_version": 1, "themes": {}}

    path.write_text("{bad", encoding="utf-8")
    assert load_feasibility_state(storage) == {"schema_version": 1, "themes": {}}


def test_r2_storage_uses_conditional_create_and_retries_conflict():
    class FakeR2:
        def __init__(self):
            self.payload = None
            self.etag = None
            self.puts = []
            self.conflict_once = True

        def get_object(self, **kwargs):
            if self.payload is None:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchKey"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "GetObject",
                )
            return {"Body": BytesIO(self.payload), "ETag": self.etag}

        def put_object(self, **kwargs):
            self.puts.append(kwargs)
            if self.payload is not None and self.conflict_once:
                self.conflict_once = False
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.payload = kwargs["Body"]
            self.etag = '"etag-2"'

    theme = get_default_theme_registry().by_id("baroque_drama")
    client = FakeR2()
    storage = ThemeFeasibilityStorage(s3_client=client, bucket_name="bucket")
    first = observation_from_result(
        _availability(theme.id, success=False, qualified=0),
        theme=theme,
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        attempted_at=NOW,
    )
    storage.append(first)
    assert client.puts[0]["IfNoneMatch"] == "*"

    second = observation_from_result(
        _availability(theme.id, success=True, qualified=12),
        theme=theme,
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        attempted_at=NOW + timedelta(minutes=1),
    )
    storage.append(second)

    assert len(client.puts) == 3
    assert client.puts[1]["IfMatch"] == '"etag-2"'
    assert client.puts[2]["IfMatch"] == '"etag-2"'
    state, _ = storage.load()
    assert len(state["themes"][theme.id]) == 2


def test_success_and_failure_results_are_persisted_and_storage_is_bounded(tmp_path):
    theme = get_default_theme_registry().by_id("approaches_to_portraiture")
    storage = ThemeFeasibilityStorage(local_path=tmp_path / "theme_feasibility.json")
    adapters = _adapters()
    run_state = AcquisitionRunState()

    assert record_theme_availability(
        _availability(theme.id, success=True, qualified=13),
        theme=theme,
        adapters=adapters,
        run_state=run_state,
        attempted_at=NOW,
        storage=storage,
    )
    assert record_theme_availability(
        _availability(theme.id, success=False, qualified=1),
        theme=theme,
        adapters=adapters,
        run_state=run_state,
        attempted_at=NOW + timedelta(seconds=1),
        storage=storage,
    )
    for index in range(MAX_OBSERVATIONS_PER_THEME + 3):
        storage.append(
            observation_from_result(
                _availability(theme.id, success=False, qualified=index % 5),
                theme=theme,
                adapters=adapters,
                run_state=run_state,
                attempted_at=NOW + timedelta(minutes=index + 1),
            )
        )

    state, _ = storage.load()
    observations = state["themes"][theme.id]
    assert len(observations) == MAX_OBSERVATIONS_PER_THEME
    assert {item["success"] for item in observations} == {False}


def test_telemetry_write_failure_is_non_blocking():
    class BrokenStorage:
        def append(self, observation):
            raise OSError("storage unavailable")

    theme = get_default_theme_registry().by_id("baroque_drama")

    assert not record_theme_availability(
        _availability(theme.id, success=False, qualified=0),
        theme=theme,
        adapters=_adapters(),
        run_state=AcquisitionRunState(),
        attempted_at=NOW,
        storage=BrokenStorage(),
    )


def test_persisted_adapter_diagnostics_are_normalized_and_secret_free():
    theme = get_default_theme_registry().by_id("metropolitan_museum_spotlight")
    failure = AdapterFailure(
        "met",
        "query must not be persisted",
        "HTTP403",
        http_status=403,
        operation="object",
        category="HTTP_BLOCKED",
        retryable=False,
        disabled_for_run=True,
    )
    state = AcquisitionRunState()
    state.disable("met", "HTTP403")
    observation = observation_from_result(
        _availability(theme.id, success=False, qualified=0, failures=(failure,)),
        theme=theme,
        adapters=_adapters(),
        run_state=state,
        attempted_at=NOW,
    )

    serialized = json.dumps(asdict(observation))
    assert "query must not be persisted" not in serialized
    assert "HTTP_BLOCKED" in serialized
    assert observation.adapter_failures == (
        {
            "source": "met",
            "http_status": 403,
            "operation": "object",
            "category": "HTTP_BLOCKED",
            "retryable": False,
            "disabled_for_run": True,
        },
    )
