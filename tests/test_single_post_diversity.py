from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import main
from src import art_fetcher, content_diversity
from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    ContrastBucket,
    DominantColorFamily,
    LuminanceBucket,
    extract_visual_features,
)
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.single_post_diversity import (
    SINGLE_DIVERSITY_HISTORY_WINDOW,
    SemanticFamily,
    SinglePostDiversityFeatures,
    SinglePostOrientation,
    VisualCategoryFingerprint,
    classify_orientation,
    infer_semantic_family,
    normalized_artist_key,
    recent_single_publications,
    score_single_post_diversity,
)


def _candidate(
    identifier: str = "candidate",
    *,
    artist: str = "Claude Monet",
    classification: str | None = "Landscape",
    width: int | None = 1600,
    height: int | None = 1000,
) -> NormalizedArtwork:
    return NormalizedArtwork(
        source="aic",
        source_id=identifier,
        title="A title that must not be semantic evidence",
        artist_name=artist,
        creation_date="1900",
        medium="Oil on canvas",
        classification=classification,
        museum_name="Museum",
        image_url=f"https://images.example/{identifier}.jpg",
        image_width=width,
        image_height=height,
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )


def _visual(
    *,
    width: int = 1600,
    height: int = 1000,
    tone: LuminanceBucket = LuminanceBucket.DARK,
    color: DominantColorFamily = DominantColorFamily.BLUE,
) -> ArtworkVisualFeatures:
    return ArtworkVisualFeatures(
        width=width,
        height=height,
        aspect_ratio=width / height,
        orientation=ArtworkOrientation.LANDSCAPE,
        mean_luminance=50.0,
        luminance_bucket=tone,
        mean_saturation=0.5,
        dominant_color_family=color,
        contrast_bucket=ContrastBucket.MEDIUM,
    )


def _record(
    *,
    orientation: SinglePostOrientation = SinglePostOrientation.PORTRAIT,
    artist: str = "Claude Monet",
    semantic: SemanticFamily = SemanticFamily.PORTRAITURE,
    tone: LuminanceBucket = LuminanceBucket.DARK,
    color: DominantColorFamily = DominantColorFamily.BLUE,
    publication_type: str = "SINGLE",
    publication_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "publication_type": publication_type,
        "published_orientation": orientation.value,
        "artist_name": artist,
        "normalized_artist_key": normalized_artist_key(artist),
        "semantic_family": semantic.value,
        "visual_tone": tone.value,
        "visual_color_family": color.value,
    }
    if publication_id is not None:
        record["publication_id"] = publication_id
    return record


def _features(
    *,
    orientation: SinglePostOrientation = SinglePostOrientation.PORTRAIT,
    artist: str | None = "Claude Monet",
    semantic: SemanticFamily = SemanticFamily.PORTRAITURE,
    tone: LuminanceBucket = LuminanceBucket.DARK,
    color: DominantColorFamily = DominantColorFamily.BLUE,
) -> SinglePostDiversityFeatures:
    return SinglePostDiversityFeatures(
        orientation=orientation,
        artist_key=normalized_artist_key(artist),
        visual_category=VisualCategoryFingerprint(semantic, tone, color),
    )


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [
        ((1000, 1200), SinglePostOrientation.PORTRAIT),
        ((1000, 1000), SinglePostOrientation.SQUARE),
        ((1000, 980), SinglePostOrientation.SQUARE),
        ((1000, 979), SinglePostOrientation.LANDSCAPE),
        ((1200, 1000), SinglePostOrientation.LANDSCAPE),
    ],
)
def test_orientation_uses_small_deterministic_square_tolerance(dimensions, expected):
    assert classify_orientation(*dimensions) is expected


def test_exif_correct_display_dimensions_determine_orientation(tmp_path):
    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (120, 200), "navy")
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)

    features = extract_visual_features(path)

    assert (features.width, features.height) == (200, 120)
    assert classify_orientation(features.width, features.height) is SinglePostOrientation.LANDSCAPE


def test_orientation_repetition_is_progressive_bounded_and_never_rejects():
    candidate = _features()
    adjustments = [
        score_single_post_diversity(candidate, [_record()] * count).orientation
        for count in (1, 2, 3, 4, 8, 12)
    ]

    assert adjustments[:4] == [0.0, -1.75, -3.5, -5.5]
    assert adjustments[-2:] == [-7.5, -7.5]
    assert all(isinstance(value, float) for value in adjustments)


def test_rare_orientation_has_advantage_only_through_lower_repetition_pressure():
    history = [_record()] * 4
    repeated = score_single_post_diversity(_features(), history)
    fresh = score_single_post_diversity(
        _features(orientation=SinglePostOrientation.LANDSCAPE),
        history,
    )

    assert repeated.orientation == -5.5
    assert fresh.orientation == 0.0


def test_large_quality_gap_is_not_overpowered_by_single_diversity_alone():
    history = [_record()] * 12
    repetitive_total = 95 + score_single_post_diversity(_features(), history).total
    fresh_total = 70 + score_single_post_diversity(
        _features(
            orientation=SinglePostOrientation.LANDSCAPE,
            artist="Berthe Morisot",
            semantic=SemanticFamily.LANDSCAPE,
            tone=LuminanceBucket.LIGHT,
            color=DominantColorFamily.GREEN,
        ),
        history,
    ).total

    assert repetitive_total > fresh_total


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Vincent van Gogh", "  vincent   van-gogh "),
        ("Van Gogh, Vincent", "Vincent van Gogh"),
    ],
)
def test_artist_identity_normalizes_case_spacing_punctuation_and_surname_first(left, right):
    assert normalized_artist_key(left) == normalized_artist_key(right)


@pytest.mark.parametrize(
    "value",
    [None, "", "Unknown Artist", "Artist unknown", "Anonymous", "Anonymous Artist", "Unidentified maker"],
)
def test_unknown_and_anonymous_artists_have_no_shared_identity(value):
    assert normalized_artist_key(value) is None


def test_known_artist_penalty_grows_and_immediate_repeat_is_additional():
    candidate = _features()
    one_non_immediate = [_record(artist="Claude Monet"), _record(artist="Other Artist")]
    one_immediate = list(reversed(one_non_immediate))

    assert score_single_post_diversity(candidate, one_non_immediate).artist == -1.0
    assert score_single_post_diversity(candidate, one_immediate).artist == -3.0
    assert score_single_post_diversity(candidate, [_record()] * 2).artist == -4.5
    assert score_single_post_diversity(candidate, [_record()] * 6).artist == -6.0


def test_unknown_artist_candidates_do_not_accumulate_fake_repetition():
    history = [_record(artist="Unknown Artist")] * 12

    score = score_single_post_diversity(_features(artist=None), history)

    assert score.artist == 0.0
    assert score.artist_count == 0
    assert score.immediate_artist_repeat is False


def test_artist_falls_out_of_the_twelve_publication_window():
    history = [_record(artist="Claude Monet")] + [
        _record(artist=f"Artist {index}")
        for index in range(SINGLE_DIVERSITY_HISTORY_WINDOW)
    ]

    assert score_single_post_diversity(_features(), history).artist == 0.0


def test_discovery_bonus_and_repeat_penalty_are_coherent_not_duplicate_rewards():
    history = [_record(artist="Claude Monet")]
    known_features = {"artist_name": "Claude Monet"}
    fresh_features = {"artist_name": "Hilma af Klint"}

    repeat = score_single_post_diversity(_features(), history)

    assert repeat.artist == -3.0
    assert content_diversity.analyze_discovery_score(known_features, 90, history) == 0.0
    assert content_diversity.analyze_discovery_score(fresh_features, 90, history) == 2.0


def test_semantic_inference_uses_metadata_precedence_not_title_or_artist():
    explicit = _candidate(classification="Still Life")
    title_only = _candidate(classification="Painting", artist="Portrait Master")
    title_only.title = "Portrait of a woman in a landscape"

    assert infer_semantic_family(explicit) is SemanticFamily.STILL_LIFE
    assert infer_semantic_family(title_only) is SemanticFamily.UNKNOWN


def test_missing_museum_metadata_stays_unknown_without_fabrication():
    candidate = _candidate(classification=None)
    candidate.department = None
    candidate.style_or_period = None
    candidate.medium = None

    assert infer_semantic_family(candidate) is SemanticFamily.UNKNOWN


def test_repeated_semantic_family_and_visual_fingerprint_are_moderate_and_bounded():
    candidate = _features()
    first = score_single_post_diversity(candidate, [_record()])
    repeated = score_single_post_diversity(candidate, [_record()] * 5)
    saturated = score_single_post_diversity(candidate, [_record()] * 12)

    assert first.visual_category == -2.0
    assert repeated.visual_category == -5.0
    assert saturated.visual_category == -5.0
    assert saturated.visual_category >= -5.0
    assert saturated.tone_count == 12
    assert saturated.color_count == 12


def test_mixed_feed_has_lower_visual_repetition_pressure():
    repeated = [_record()] * 3
    mixed = [
        _record(),
        _record(
            semantic=SemanticFamily.LANDSCAPE,
            tone=LuminanceBucket.LIGHT,
            color=DominantColorFamily.GREEN,
        ),
        _record(),
    ]

    repeated_score = score_single_post_diversity(_features(), repeated)
    mixed_score = score_single_post_diversity(_features(), mixed)

    assert mixed_score.visual_category > repeated_score.visual_category


def test_unknown_visual_category_is_safe_and_neutral():
    unknown = _features(
        semantic=SemanticFamily.UNKNOWN,
        tone=LuminanceBucket.UNKNOWN,
        color=DominantColorFamily.UNKNOWN,
    )

    score = score_single_post_diversity(unknown, [_record()] * 12)

    assert score.visual_category == 0.0


def test_carousel_rows_are_not_counted_as_nine_single_publications():
    carousel = [
        {
            **_record(publication_type="CAROUSEL", publication_id="carousel-1"),
            "publication_role": "FEATURED",
        }
        for _ in range(9)
    ]
    singles = [_record(publication_id=f"single-{index}") for index in range(3)]

    events = recent_single_publications([*singles, *carousel])

    assert events == singles
    assert score_single_post_diversity(_features(), [*singles, *carousel]).orientation_count == 3


def test_legacy_single_records_load_safely_and_missing_fields_are_unknown():
    legacy = {"artist_name": "Claude Monet", "image_width": 1000, "image_height": 1200}

    score = score_single_post_diversity(_features(), [legacy])

    assert score.orientation_count == 1
    assert score.artist_count == 1
    assert score.semantic_count == 0


def test_nearly_equal_fresh_candidate_beats_repetitive_candidate():
    history = [_record()] * 4
    repetitive = 94 + score_single_post_diversity(_features(), history).total
    fresh = 93 + score_single_post_diversity(
        _features(
            orientation=SinglePostOrientation.LANDSCAPE,
            artist="Hilma af Klint",
            semantic=SemanticFamily.ABSTRACT,
            tone=LuminanceBucket.LIGHT,
            color=DominantColorFamily.RED,
        ),
        history,
    ).total

    assert fresh > repetitive


def test_heavy_portrait_history_and_artist_dominance_help_similarly_strong_fresh_work():
    history = [_record()] * 4
    repeated = score_single_post_diversity(_features(), history)
    fresh = score_single_post_diversity(
        _features(
            orientation=SinglePostOrientation.LANDSCAPE,
            artist="Berthe Morisot",
            semantic=SemanticFamily.LANDSCAPE,
        ),
        history,
    )

    assert fresh.orientation > repeated.orientation
    assert fresh.artist > repeated.artist
    assert 93 + fresh.total > 94 + repeated.total


def test_scoring_is_pure_deterministic_and_does_not_add_rejected_candidate_to_history():
    history = [_record()] * 3
    before = list(history)
    candidate = _features()

    first = score_single_post_diversity(candidate, history)
    second = score_single_post_diversity(candidate, history)

    assert first == second
    assert history == before


def test_single_pipeline_reuses_validated_file_for_features_without_extra_download(
    monkeypatch,
    tmp_path,
):
    candidate = _candidate()

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return [candidate]

    raw_path = tmp_path / "raw.jpg"
    calls = {"download": 0, "features": 0}
    downloaded_paths = []
    actual_extract = extract_visual_features

    def download(url, output_path):
        calls["download"] += 1
        downloaded_paths.append(Path(output_path))
        Image.new("RGB", (1600, 1000), "navy").save(output_path)
        return ImageValidationResult(True, 1600, 1000, "JPEG", "ok")

    def features(path):
        calls["features"] += 1
        assert Path(path) == downloaded_paths[-1]
        return actual_extract(path)

    monkeypatch.setattr(art_fetcher, "get_museum_adapters", lambda: [Adapter()])
    monkeypatch.setattr(art_fetcher.history_tracker, "get_recent_history", lambda: [])
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(raw_path))
    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    monkeypatch.setattr(art_fetcher, "extract_visual_features", features)
    monkeypatch.setattr(
        main.gemini_ai,
        "analyze_artwork",
        lambda *args, **kwargs: pytest.fail(
            "single diversity selection must not call Gemini"
        ),
    )

    result = art_fetcher.fetch_random_artwork(set())

    assert calls == {"download": 1, "features": 1}
    assert result["visual_tone"] is not None
    assert result["semantic_family"] == SemanticFamily.LANDSCAPE.value


@pytest.mark.parametrize(
    ("repetitive_quality", "fresh_quality", "expected_first"),
    [
        (94.0, 93.0, "aic_fresh"),
        (95.0, 70.0, "aic_repetitive"),
    ],
)
def test_final_single_ranking_balances_measured_diversity_against_quality(
    monkeypatch,
    tmp_path,
    repetitive_quality,
    fresh_quality,
    expected_first,
):
    repetitive = _candidate(
        "repetitive",
        artist="Claude Monet",
        classification="Portrait",
        width=None,
        height=None,
    )
    fresh = _candidate(
        "fresh",
        artist="Berthe Morisot",
        classification="Landscape",
        width=None,
        height=None,
    )

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return [repetitive, fresh]

    scores = {
        repetitive.canonical_id: repetitive_quality,
        fresh.canonical_id: fresh_quality,
    }

    def download(url, output_path):
        if "repetitive" in url:
            Image.new("RGB", (1000, 1400), (10, 20, 80)).save(output_path)
            return ImageValidationResult(True, 1000, 1400, "JPEG", "ok")
        Image.new("RGB", (1400, 1000), (230, 240, 210)).save(output_path)
        return ImageValidationResult(True, 1400, 1000, "JPEG", "ok")

    monkeypatch.setattr(art_fetcher, "get_museum_adapters", lambda: [Adapter()])
    monkeypatch.setattr(
        art_fetcher.history_tracker,
        "get_recent_history",
        lambda: [_record()] * 4,
    )
    monkeypatch.setattr(art_fetcher.config, "OUTPUT_RAW_IMAGE_PATH", str(tmp_path / "raw.jpg"))
    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    monkeypatch.setattr(
        art_fetcher,
        "calculate_quality_score",
        lambda candidate, weights: scores[candidate.canonical_id],
    )
    monkeypatch.setattr(art_fetcher, "calculate_serendipity_bonus", lambda *args: 0.0)
    monkeypatch.setattr(
        art_fetcher.content_diversity,
        "analyze_museum_diversity",
        lambda *args: 0.0,
    )
    monkeypatch.setattr(
        art_fetcher.content_diversity,
        "analyze_regional_diversity",
        lambda *args: 0.0,
    )

    ranked = list(
        art_fetcher.iter_single_post_candidates(
            set(),
            max_candidates=2,
            selection_run_seed=art_fetcher.SelectionRunSeed("fixed", "test"),
        )
    )

    assert ranked[0]["id"] == expected_first
    assert {item["published_orientation"] for item in ranked} == {
        "PORTRAIT",
        "LANDSCAPE",
    }
