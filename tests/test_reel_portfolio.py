import json
from datetime import UTC, datetime

import pytest

from src.models import NormalizedArtwork
from src.reel_portfolio import (
    ADJUSTMENTS,
    DEFAULT_SELECTION_TARGET,
    RECENT_REEL_HISTORY_LOOKBACK,
    PORTFOLIO_SELECTION_VERSION,
    resolve_selection_target,
    select_portfolio_candidates,
    write_portfolio_selection_manifest,
)
from src.reel_selector import ReelCandidate, ReelCandidateDecision, ReelScoreBreakdown


def _decision(
    source_id: str,
    base: float,
    *,
    artist: str = "Artist",
    museum: str = "Museum",
    classification: str | None = "Painting",
    source: str = "met",
    dimensions: tuple[int, int] = (2400, 1600),
    technical: float | None = None,
    detail: float | None = None,
) -> ReelCandidateDecision:
    artwork = NormalizedArtwork(
        source=source,
        source_id=source_id,
        title=f"Artwork {source_id}",
        artist_name=artist,
        creation_date="1900",
        medium="Oil on canvas",
        museum_name=museum,
        classification=classification,
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
        image_width=dimensions[0],
        image_height=dimensions[1],
    )
    technical = base * 0.35 if technical is None else technical
    detail = base * 0.25 if detail is None else detail
    breakdown = ReelScoreBreakdown(
        technical_quality=technical,
        detail_headroom=detail,
        metadata_completeness=base - technical - detail,
        composition_flexibility=0.0,
        visual_information=0.0,
    )
    return ReelCandidateDecision(
        candidate=ReelCandidate(artwork, f"/safe/{source_id}.jpg"),
        eligible=True,
        image_width=dimensions[0],
        image_height=dimensions[1],
        score_breakdown=breakdown,
    )


def _ids(result):
    return [item.candidate_decision.candidate.artwork.canonical_id for item in result.selected]


def test_highest_quality_candidate_wins_first_selection():
    result = select_portfolio_candidates([_decision("a", 91), _decision("b", 96, artist="Other")])

    assert _ids(result)[0] == "met_b"
    assert result.selection_target == DEFAULT_SELECTION_TARGET == 4
    assert result.selected[0].portfolio_adjustment == 0


def test_batch_diversity_adjustments_are_soft_and_centralized():
    first = _decision("first", 100, artist="Artist", museum="Museum", classification="Painting", dimensions=(2400, 1600))
    repeated = _decision("repeat", 99, artist=" artist ", museum=" museum ", classification=" painting ", dimensions=(2400, 1600))
    result = select_portfolio_candidates([first, repeated], target=2)
    breakdown = result.selected[1].adjustment_breakdown

    assert breakdown.same_batch_artist == ADJUSTMENTS.same_batch_artist
    assert breakdown.same_batch_museum == ADJUSTMENTS.same_batch_museum
    assert breakdown.same_batch_classification == ADJUSTMENTS.same_batch_classification
    assert breakdown.same_batch_orientation == ADJUSTMENTS.same_batch_orientation
    assert breakdown.same_batch_source == ADJUSTMENTS.same_batch_source
    assert result.selected[1].portfolio_adjustment == ADJUSTMENTS.minimum_total


def test_weak_candidate_cannot_beat_much_stronger_candidate_only_for_diversity():
    first = _decision("first", 100, artist="A")
    stronger_repeat = _decision("stronger-repeat", 95, artist="A")
    weak_different = _decision("weak-different", 80, artist="B", museum="Other", classification="Sculpture", source="aic", dimensions=(1600, 2400))

    result = select_portfolio_candidates([first, stronger_repeat, weak_different], target=2)

    assert _ids(result) == ["met_first", "met_stronger-repeat"]


@pytest.mark.parametrize("field,values", [("museum", ["Met", "Met", "Met"]), ("classification", ["Painting", "Painting", "Painting"])])
def test_repetition_never_prevents_target_from_being_filled(field, values):
    kwargs = [{field: value} for value in values]
    candidates = [_decision(str(index), 100 - index, **value) for index, value in enumerate(kwargs)]

    result = select_portfolio_candidates(candidates, target=3)

    assert len(result.selected) == 3


def test_missing_classification_does_not_crash_or_penalize():
    result = select_portfolio_candidates([_decision("a", 100, classification=None), _decision("b", 99, classification=None)], target=2)

    assert result.selected[1].adjustment_breakdown.same_batch_classification == 0


def test_exact_recent_reel_duplicate_is_excluded_without_consulting_feed_history():
    duplicate = _decision("duplicate", 100)
    fresh = _decision("fresh", 90, artist="Fresh")
    result = select_portfolio_candidates(
        [duplicate, fresh],
        recent_reel_history=[{"canonicalId": "MET_DUPLICATE", "artist": "Old"}],
    )

    assert _ids(result) == ["met_fresh"]
    assert result.skipped_recent_reel_duplicates[0].reason == "RECENT_REEL_DUPLICATE"


def test_recent_artist_and_museum_penalties_use_only_reel_history_window():
    candidate = _decision("candidate", 100, artist="Artist", museum="Museum")
    old = {"canonicalId": "old", "artist": "Artist", "museum": "Museum"}
    result = select_portfolio_candidates([candidate], recent_reel_history=[old] * (RECENT_REEL_HISTORY_LOOKBACK + 1))
    assert result.selected[0].adjustment_breakdown.recent_artist == ADJUSTMENTS.recent_artist
    assert result.selected[0].adjustment_breakdown.recent_museum == ADJUSTMENTS.recent_museum


def test_exact_produced_duplicate_is_excluded_beyond_the_soft_history_lookback():
    candidate = _decision("duplicate", 100)
    old_duplicate = {"canonicalId": "MET_DUPLICATE", "artist": "Old", "museum": "Old Museum"}
    newer = {"canonicalId": "new", "artist": "New", "museum": "New Museum"}

    result = select_portfolio_candidates(
        [candidate], recent_reel_history=[old_duplicate] + [newer] * RECENT_REEL_HISTORY_LOOKBACK
    )

    assert not result.selected
    assert result.skipped_recent_reel_duplicates[0].reason == "RECENT_REEL_DUPLICATE"


def test_empty_history_target_resolution_shortfall_determinism_and_stable_ties():
    candidates = [_decision("b", 90), _decision("a", 90), _decision("c", 88)]
    first = select_portfolio_candidates(candidates, target=5, recent_reel_history=[])
    second = select_portfolio_candidates(candidates, target=5, recent_reel_history=[])

    assert DEFAULT_SELECTION_TARGET == 4
    assert resolve_selection_target(environment={"REEL_SELECTION_TARGET": "2"}) == 2
    assert first.selection_target == 5
    assert _ids(first) == _ids(second) == ["met_a", "met_b", "met_c"]
    assert first.shortfall == 2
    for value in (0, -1, "1.5", "bad", True):
        with pytest.raises(ValueError, match="integer >= 1"):
            resolve_selection_target(value)


def test_adjustment_never_exceeds_configured_bounds():
    repeated = [_decision(str(index), 100 - index) for index in range(4)]
    result = select_portfolio_candidates(repeated, target=4, recent_reel_history=[{"artist": "Artist", "museum": "Museum"}])

    assert all(ADJUSTMENTS.minimum_total <= item.portfolio_adjustment <= ADJUSTMENTS.maximum_total for item in result.selected)


def test_manifest_is_secret_free_and_has_portfolio_metadata(tmp_path):
    candidate = _decision("safe", 91)
    candidate.candidate.artwork.artwork_url = "https://museum.example/?token=must-not-appear"
    selection = select_portfolio_candidates([candidate])
    manifest_path = write_portfolio_selection_manifest(
        selection,
        tmp_path / "portfolio-selection.json",
        generated_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["portfolioSelectionVersion"] == PORTFOLIO_SELECTION_VERSION
    assert manifest["selectionTarget"] == 4
    assert manifest["selected"][0]["adjustmentBreakdown"]
    assert "must-not-appear" not in manifest_path.read_text(encoding="utf-8")


def test_portfolio_layer_never_calls_gemini(monkeypatch):
    from src import gemini_ai

    monkeypatch.setattr(gemini_ai, "analyze_artwork", lambda *args, **kwargs: pytest.fail("Gemini must not be called"))

    assert select_portfolio_candidates([_decision("safe", 91)]).selected
