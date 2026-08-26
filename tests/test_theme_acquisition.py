from __future__ import annotations

from src.carousel_themes import CarouselFormat, CarouselThemeDefinition, ThemeFamily
from src.models import NormalizedArtwork
from src.museums.base import AdapterHTTPError
from src.theme_acquisition import (
    ABSOLUTE_MINIMUM,
    PREFERRED_PREFLIGHT_TARGET,
    AcquisitionRunState,
    QueryType,
    ThemeAcquisitionPolicy,
    ThemeQueryHit,
    acquire_theme_candidates,
    evaluate_theme_relevance,
    normalize_theme_text,
    phrase_matches,
)


def _theme(**overrides) -> CarouselThemeDefinition:
    data = {
        "id": "women_reading_test",
        "title": "Women Reading Test",
        "family": ThemeFamily.HUMAN_ACTIVITY,
        "format": CarouselFormat.THEMATIC_COLLECTION,
        "description": "Women reading books in clearly documented settings.",
        "primary_queries": ["woman reading", "female reader", "woman with book"],
        "secondary_queries": ["reading", "book interior"],
        "required_terms": ["reading", "reader", "book"],
        "required_term_groups": [
            ["reading", "reader", "book"],
            ["woman", "women", "female", "girl"],
        ],
        "preferred_terms": ["woman", "interior", "seated", "window"],
        "excluded_terms": ["bookplate"],
        "minimum_candidate_target": 9,
    }
    data.update(overrides)
    return CarouselThemeDefinition(**data)


def _candidate(identifier: str, *, title=None, description=None, rights=True, quality_marker="good"):
    return NormalizedArtwork(
        source="aic",
        source_id=identifier,
        title=title or f"Woman Reading {identifier}",
        artist_name="Artist",
        creation_date="1880",
        medium="Oil on canvas",
        classification="Painting",
        description=description or "A seated female reader holds a book in an interior.",
        museum_name="Museum",
        image_url=f"https://images.example/{identifier}-{quality_marker}.jpg",
        image_width=2000,
        image_height=1600,
        is_public_domain=rights,
        rights_status="CONFIRMED_PUBLIC_DOMAIN" if rights else None,
    )


class QueryAdapter:
    source_id = "test"

    def __init__(self, results, *, fail_queries=()):
        self.results = results
        self.fail_queries = set(fail_queries)
        self.calls = []

    def fetch_candidates(self, *, query, limit, rng):
        self.calls.append((query, limit, rng.random()))
        if query in self.fail_queries:
            raise RuntimeError("adapter unavailable")
        return list(self.results.get(query, ()))[:limit]


def _acquire(theme, adapters, *, posted_ids=None, policy=None, quality=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(
            "src.theme_acquisition.calculate_quality_score",
            quality or (lambda artwork, weights: 90.0),
        )
        monkeypatch.setattr(
            "src.theme_acquisition.calculate_measurement_coverage",
            lambda artwork: 1.0,
        )
    return acquire_theme_candidates(
        theme,
        posted_ids=posted_ids or set(),
        adapters=adapters,
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        policy=policy or ThemeAcquisitionPolicy(minimum_safe_pool=9),
    )


def test_normalization_is_unicode_aware_and_matching_uses_phrase_boundaries():
    tokens = normalize_theme_text("  L’ART—bleu; CARTOGRAPHY  ")

    assert phrase_matches(tokens, "l art bleu")
    assert phrase_matches(tokens, "cartography")
    assert not phrase_matches(tokens, "artography")
    assert not phrase_matches(normalize_theme_text("cartography"), "art")


def test_strong_primary_required_metadata_scores_high_and_secondary_is_lower():
    artwork = _candidate("strong")
    theme = _theme()
    primary = evaluate_theme_relevance(
        artwork,
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )
    secondary = evaluate_theme_relevance(
        artwork,
        theme,
        [ThemeQueryHit(QueryType.SECONDARY, 0, "reading")],
    )

    assert primary.relevance_eligible
    assert primary.theme_relevance_score >= 75
    assert secondary.theme_relevance_score < primary.theme_relevance_score
    assert primary.relevance_breakdown.primary_query > secondary.relevance_breakdown.secondary_query


def test_missing_required_group_and_excluded_signal_are_hard_gates():
    theme = _theme()
    missing = evaluate_theme_relevance(
        _candidate("generic", title="Portrait of a Woman", description="A seated woman indoors."),
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )
    excluded = evaluate_theme_relevance(
        _candidate("excluded", title="Bookplate with a Woman Reading"),
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )

    assert not missing.relevance_eligible
    assert missing.missing_required_groups == (("reading", "reader", "book"),)
    assert not excluded.relevance_eligible
    assert excluded.excluded_matches == ("bookplate",)
    assert excluded.theme_relevance_score == 0


def test_preferred_bonus_is_bounded_and_title_outweighs_description():
    theme = _theme()
    many_preferred = evaluate_theme_relevance(
        _candidate("many", title="Woman Reading at a Window"),
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )
    title_match = evaluate_theme_relevance(
        _candidate("title", title="Woman Reading", description=""),
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )
    description_match = evaluate_theme_relevance(
        _candidate("description", title="Study", description="A woman reading a book."),
        theme,
        [ThemeQueryHit(QueryType.PRIMARY, 0, "woman reading")],
    )

    assert many_preferred.relevance_breakdown.preferred == 12
    assert title_match.relevance_breakdown.title_evidence > description_match.relevance_breakdown.description_evidence
    assert title_match.theme_relevance_score > description_match.theme_relevance_score


def test_all_allowed_primary_queries_participate_and_secondary_is_not_needed(monkeypatch):
    theme = _theme(primary_queries=["q1", "q2", "q3", "q4"], secondary_queries=["secondary"])
    results = {
        query: [_candidate(f"{query}-{index}") for index in range(3)]
        for query in theme.primary_queries
    }
    adapter = QueryAdapter(results)
    result = _acquire(
        theme,
        [adapter],
        monkeypatch=monkeypatch,
        policy=ThemeAcquisitionPolicy(
            max_primary_queries=4,
            max_secondary_queries=1,
            minimum_safe_pool=12,
        ),
    )

    assert [call[0] for call in adapter.calls] == ["q1", "q2", "q3", "q4"]
    assert result.availability.sufficient
    assert result.availability.query_count == 4


def test_secondary_queries_are_staged_and_early_stop_avoids_extra_calls(monkeypatch):
    theme = _theme(primary_queries=["p1"], secondary_queries=["s1", "s2"])
    adapter = QueryAdapter(
        {
            "p1": [_candidate(f"p-{index}") for index in range(5)],
            "s1": [_candidate(f"s-{index}") for index in range(4)],
            "s2": [_candidate(f"unused-{index}") for index in range(9)],
        }
    )
    result = _acquire(theme, [adapter], monkeypatch=monkeypatch)

    assert [call[0] for call in adapter.calls] == ["p1", "s1", "s2"]
    assert result.availability.sufficient


def test_publication_minimum_is_separate_from_preferred_headroom(monkeypatch):
    def acquire_count(count: int, *, preferred: int = 24):
        theme = _theme(
            primary_queries=["woman reading"],
            secondary_queries=[],
            minimum_candidate_target=preferred,
        )
        return _acquire(
            theme,
            [QueryAdapter({"woman reading": [_candidate(str(index)) for index in range(count)]})],
            monkeypatch=monkeypatch,
        )

    three = acquire_count(3)
    assert not three.availability.sufficient
    assert three.availability.failure_reason == "insufficient_unique_pool"

    for count in range(4, 12):
        narrow = acquire_count(count)
        assert narrow.availability.sufficient
        assert narrow.availability.narrow_pool
        assert narrow.availability.absolute_minimum == ABSOLUTE_MINIMUM
        assert narrow.availability.target == 24

    comfortable = acquire_count(12)
    assert comfortable.availability.sufficient
    assert not comfortable.availability.narrow_pool
    assert PREFERRED_PREFLIGHT_TARGET == 12


def test_preferred_target_early_stop_avoids_unused_queries(monkeypatch):
    theme = _theme(
        primary_queries=["p1", "p2"],
        secondary_queries=["s1"],
        minimum_candidate_target=12,
    )
    adapter = QueryAdapter(
        {
            "p1": [_candidate(str(index)) for index in range(12)],
            "p2": [_candidate(f"unused-p-{index}") for index in range(12)],
            "s1": [_candidate(f"unused-s-{index}") for index in range(12)],
        }
    )

    result = _acquire(theme, [adapter], monkeypatch=monkeypatch)

    assert result.availability.sufficient
    assert [call[0] for call in adapter.calls] == ["p1"]


class BackoffAdapter:
    source_id = "met"

    def __init__(self):
        self.calls = 0

    def fetch_candidates(self, **kwargs):
        self.calls += 1
        raise AdapterHTTPError(self.source_id, 403)


def test_http_403_circuit_breaker_is_run_local(monkeypatch):
    theme = _theme(primary_queries=["p1", "p2", "p3"], secondary_queries=[])
    state = AcquisitionRunState()
    shared_first = BackoffAdapter()
    first = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[shared_first],
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        run_state=state,
    )
    shared_second = BackoffAdapter()
    second = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[shared_second],
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        run_state=state,
    )

    assert not first.availability.sufficient
    assert not second.availability.sufficient
    assert shared_first.calls == 2
    assert shared_second.calls == 0
    assert state.http_403_failures == 2
    assert state.disabled_adapters == {"met": "HTTP403"}

    fresh_adapter = BackoffAdapter()
    fresh_state = AcquisitionRunState()
    acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[fresh_adapter],
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        run_state=fresh_state,
    )
    assert fresh_adapter.calls == 2


def test_missing_optional_adapter_is_logged_and_skipped_once(monkeypatch, caplog):
    class MissingCredentialAdapter:
        source_id = "rijksmuseum"

        def __init__(self):
            self.calls = 0

        def unavailable_reason(self):
            return "missing_api_key"

        def fetch_candidates(self, **kwargs):
            self.calls += 1
            return []

    caplog.set_level("WARNING", logger="src.theme_acquisition")
    state = AcquisitionRunState()
    theme = _theme(primary_queries=["p1"], secondary_queries=[])
    adapters = [MissingCredentialAdapter(), MissingCredentialAdapter()]
    for adapter in adapters:
        acquire_theme_candidates(
            theme,
            posted_ids=set(),
            adapters=[adapter],
            run_seed="fixed",
            museum_weights={},
            min_quality=50,
            run_state=state,
        )

    assert [adapter.calls for adapter in adapters] == [0, 0]
    assert caplog.text.count("reason=missing_api_key") == 1


def test_query_budget_and_adapter_failure_isolation(monkeypatch):
    theme = _theme(primary_queries=["p1", "p2"], secondary_queries=["s1"])
    failing = QueryAdapter({}, fail_queries={"p1", "p2", "s1"})
    safe = QueryAdapter({"p1": [_candidate(str(index)) for index in range(9)]})
    result = _acquire(
        theme,
        [failing, safe],
        monkeypatch=monkeypatch,
        policy=ThemeAcquisitionPolicy(max_network_calls=2, minimum_safe_pool=9),
    )

    assert result.availability.sufficient
    assert result.availability.network_call_count == 2
    assert len(result.availability.adapter_failures) == 1


def test_canonical_dedup_merges_provenance_idempotently(monkeypatch):
    theme = _theme(minimum_candidate_target=20)
    duplicate = _candidate("same")
    adapter = QueryAdapter(
        {
            "woman reading": [duplicate, duplicate],
            "female reader": [duplicate],
            "woman with book": [duplicate],
            "reading": [duplicate],
            "book interior": [duplicate],
        }
    )
    result = _acquire(theme, [adapter], monkeypatch=monkeypatch)
    candidate = result.all_candidates[0]

    assert result.availability.raw_candidates == 6
    assert result.availability.unique_candidates == 1
    assert len(candidate.evidence.matched_queries) == 5
    assert candidate.evidence.strongest_query.query == "woman reading"
    assert candidate.evidence.relevance_breakdown.repeated_queries == 10


def test_raw_abundance_does_not_make_rights_or_relevance_pool_viable(monkeypatch):
    theme = _theme(primary_queries=["woman reading"], secondary_queries=[])
    unsafe = [_candidate(f"unsafe-{index}", rights=False) for index in range(20)]
    rights_result = _acquire(theme, [QueryAdapter({"woman reading": unsafe})], monkeypatch=monkeypatch)

    irrelevant = [
        _candidate(f"irrelevant-{index}", title="Portrait", description="A painted landscape.")
        for index in range(20)
    ]
    relevance_result = _acquire(
        theme,
        [QueryAdapter({"woman reading": irrelevant})],
        monkeypatch=monkeypatch,
    )

    assert not rights_result.availability.sufficient
    assert rights_result.availability.failure_reason == "insufficient_confirmed_rights"
    assert not relevance_result.availability.sufficient
    assert relevance_result.availability.failure_reason == "insufficient_relevance_pool"


def test_low_quality_pool_and_fully_posted_pool_have_distinct_reasons(monkeypatch):
    theme = _theme(primary_queries=["woman reading"], secondary_queries=[])
    candidates = [_candidate(str(index)) for index in range(10)]
    low_quality = _acquire(
        theme,
        [QueryAdapter({"woman reading": candidates})],
        monkeypatch=monkeypatch,
        quality=lambda artwork, weights: 40.0,
    )
    fully_posted = _acquire(
        theme,
        [QueryAdapter({"woman reading": candidates})],
        posted_ids={candidate.canonical_id for candidate in candidates},
        monkeypatch=monkeypatch,
    )

    assert low_quality.availability.failure_reason == "insufficient_quality_pool"
    assert fully_posted.availability.failure_reason == "all_candidates_already_posted"


def test_posted_exclusion_affects_only_matching_artwork_ids(monkeypatch):
    theme = _theme(primary_queries=["woman reading"], secondary_queries=[])
    candidates = [_candidate(str(index)) for index in range(10)]
    result = _acquire(
        theme,
        [QueryAdapter({"woman reading": candidates})],
        posted_ids={candidates[0].canonical_id, "unrelated_single_publication"},
        monkeypatch=monkeypatch,
    )

    assert result.availability.sufficient
    assert result.availability.history_eligible == 9
    assert candidates[0].canonical_id not in {
        candidate.artwork.canonical_id for candidate in result.candidates
    }


def test_quality_is_independent_and_cannot_rescue_low_relevance(monkeypatch):
    theme = _theme(primary_queries=["woman reading"], secondary_queries=[])
    relevant = _candidate("relevant")
    irrelevant = _candidate("irrelevant", title="Portrait", description="A landscape study.")
    quality = {relevant.canonical_id: 70.0, irrelevant.canonical_id: 100.0}
    result = _acquire(
        theme,
        [QueryAdapter({"woman reading": [relevant, irrelevant]})],
        monkeypatch=monkeypatch,
        quality=lambda artwork, weights: quality[artwork.canonical_id],
    )

    assert [candidate.artwork.canonical_id for candidate in result.candidates] == [relevant.canonical_id]
    irrelevant_result = next(
        candidate for candidate in result.all_candidates if candidate.artwork.canonical_id == irrelevant.canonical_id
    )
    assert irrelevant_result.artwork.quality_score == 100
    assert irrelevant_result.evidence.theme_relevance_score < 60


def test_relevance_dominates_quality_between_eligible_carousel_candidates(monkeypatch):
    theme = _theme(primary_queries=["woman reading"], secondary_queries=[])
    high_relevance = _candidate("high-relevance")
    high_quality = _candidate(
        "high-quality",
        title="Study",
        description="A woman reading a book.",
    )
    quality = {
        high_relevance.canonical_id: 87.0,
        high_quality.canonical_id: 98.0,
    }
    result = _acquire(
        theme,
        [QueryAdapter({"woman reading": [high_quality, high_relevance]})],
        monkeypatch=monkeypatch,
        quality=lambda artwork, weights: quality[artwork.canonical_id],
    )

    assert [candidate.artwork.canonical_id for candidate in result.candidates[:2]] == [
        high_relevance.canonical_id,
        high_quality.canonical_id,
    ]
    assert result.candidates[0].artwork.quality_score < result.candidates[1].artwork.quality_score
    assert (
        result.candidates[0].evidence.theme_relevance_score
        > result.candidates[1].evidence.theme_relevance_score
    )


def test_same_seed_and_results_produce_identical_calls_provenance_and_ranking(monkeypatch):
    theme = _theme(minimum_candidate_target=20)
    results = {
        query: [_candidate(f"{query}-{index}") for index in range(3)]
        for query in (*theme.primary_queries, *theme.secondary_queries)
    }
    first_adapter = QueryAdapter(results)
    second_adapter = QueryAdapter(results)

    first = _acquire(theme, [first_adapter], monkeypatch=monkeypatch)
    second = _acquire(theme, [second_adapter], monkeypatch=monkeypatch)

    assert first_adapter.calls == second_adapter.calls
    assert [candidate.artwork.canonical_id for candidate in first.candidates] == [
        candidate.artwork.canonical_id for candidate in second.candidates
    ]
    assert [candidate.evidence for candidate in first.all_candidates] == [
        candidate.evidence for candidate in second.all_candidates
    ]
