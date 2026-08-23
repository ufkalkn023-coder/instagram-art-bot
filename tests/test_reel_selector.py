import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from src.models import NormalizedArtwork
from src.reel_selector import (
    DEFAULT_SHORTLIST_SIZE,
    ReelCandidate,
    resolve_shortlist_size,
    select_reel_candidates,
    write_selection_manifest,
)


def _artwork(source_id="1", **overrides):
    values = {
        "source": "met",
        "source_id": source_id,
        "title": "Reverie",
        "artist_name": "Artist",
        "creation_date": "1900",
        "medium": "Oil on canvas",
        "museum_name": "Metropolitan Museum of Art",
        "classification": "Painting",
        "artwork_url": "https://museum.example/artwork/1?token=must-not-appear",
        "is_public_domain": True,
        "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
        "image_width": 2400,
        "image_height": 1600,
    }
    values.update(overrides)
    return NormalizedArtwork(**values)


def _image(path: Path, size=(2400, 1600), color=(80, 110, 140)) -> Path:
    Image.new("RGB", size, color=color).save(path, "JPEG")
    return path


def _candidate(tmp_path, source_id="1", size=(2400, 1600), **overrides):
    image = _image(tmp_path / f"{source_id}.jpg", size=size)
    artwork = _artwork(source_id, image_width=size[0], image_height=size[1], **overrides)
    return ReelCandidate(artwork, image)


def test_confirmed_public_domain_artwork_is_eligible_and_quiet_work_is_not_rejected(tmp_path):
    result = select_reel_candidates([_candidate(tmp_path)])

    assert len(result.shortlist) == 1
    assert result.shortlist[0].eligible
    assert result.shortlist[0].score_breakdown.visual_information == 0


@pytest.mark.parametrize(
    ("candidate_factory", "reason"),
    [
        (lambda tmp_path: _candidate(tmp_path, rights_status="CONFIRMED_OPEN_ACCESS"), "RIGHTS_NOT_CONFIRMED"),
        (lambda tmp_path: ReelCandidate(_artwork(), tmp_path / "missing.jpg"), "IMAGE_MISSING"),
        (lambda tmp_path: _candidate(tmp_path, size=(600, 600)), "RESOLUTION_TOO_LOW"),
        (lambda tmp_path: _candidate(tmp_path, medium=None), "METADATA_INCOMPLETE"),
    ],
)
def test_hard_gates_reject_unsafe_or_incomplete_candidates(tmp_path, candidate_factory, reason):
    result = select_reel_candidates([candidate_factory(tmp_path)])

    assert not result.shortlist
    assert result.rejected[0].rejection_reasons == (reason,)


def test_corrupt_image_is_rejected(tmp_path):
    image = tmp_path / "corrupt.jpg"
    image.write_text("not an image", encoding="utf-8")
    result = select_reel_candidates([ReelCandidate(_artwork(), image)])

    assert result.rejected[0].rejection_reasons == ("IMAGE_INVALID",)


def test_strong_high_resolution_asset_scores_higher_than_weak_technical_asset(tmp_path):
    weak = _candidate(tmp_path, "weak", size=(1080, 720))
    strong = _candidate(tmp_path, "strong", size=(3600, 2400))
    result = select_reel_candidates([weak, strong])

    assert [item.candidate.artwork.canonical_id for item in result.shortlist] == ["met_strong", "met_weak"]


def test_landscape_artwork_is_eligible_and_scores_are_bounded_and_consistent(tmp_path):
    result = select_reel_candidates([_candidate(tmp_path, size=(3000, 1500))])
    decision = result.shortlist[0]

    assert decision.eligible
    assert 0 <= decision.reel_pre_planner_score <= 100
    assert decision.reel_pre_planner_score == pytest.approx(sum(decision.score_breakdown.as_manifest().values()))


def test_repeated_scoring_is_deterministic_and_ties_use_canonical_id(tmp_path):
    first = _candidate(tmp_path, "b")
    second = _candidate(tmp_path, "a")
    one = select_reel_candidates([first, second])
    two = select_reel_candidates([first, second])

    assert [item.reel_pre_planner_score for item in one.shortlist] == [item.reel_pre_planner_score for item in two.shortlist]
    assert [item.candidate.artwork.canonical_id for item in one.shortlist] == ["met_a", "met_b"]


def test_default_and_custom_shortlist_sizes_and_invalid_values(tmp_path):
    candidates = [_candidate(tmp_path, str(index)) for index in range(10)]

    assert DEFAULT_SHORTLIST_SIZE == 8
    assert len(select_reel_candidates(candidates).shortlist) == 8
    assert len(select_reel_candidates(candidates, shortlist_size=3).shortlist) == 3
    assert resolve_shortlist_size(environment={"REEL_SHORTLIST_SIZE": "2"}) == 2
    for value in (0, -1, "1.5", "abc", True):
        with pytest.raises(ValueError, match="integer >= 1"):
            resolve_shortlist_size(value)


def test_selector_never_calls_gemini(monkeypatch, tmp_path):
    from src import gemini_ai

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Gemini must not be called by the Reel pre-selector")

    monkeypatch.setattr(gemini_ai, "analyze_artwork", fail_if_called)

    assert select_reel_candidates([_candidate(tmp_path)]).shortlist


def test_manifest_has_required_selection_data_and_no_source_url_secret(tmp_path):
    candidate = _candidate(tmp_path)
    rejected = ReelCandidate(_artwork("bad", medium=None), tmp_path / "missing.jpg")
    result = select_reel_candidates([candidate, rejected])
    manifest_path = write_selection_manifest(
        result,
        tmp_path / "output" / "shortlist.json",
        generated_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    text = manifest_path.read_text(encoding="utf-8")

    assert manifest["selectionVersion"] == "reel-preselector-v1"
    assert manifest["candidateCount"] == 2
    assert manifest["eligibleCount"] == 1
    assert manifest["rejectedCount"] == 1
    assert manifest["shortlist"][0]["rank"] == 1
    assert manifest["shortlist"][0]["scoreBreakdown"]
    assert manifest["rejected"][0]["rejectionReasons"]
    assert "must-not-appear" not in text
