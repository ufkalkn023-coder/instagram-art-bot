from pathlib import Path

from src import art_fetcher
from src.art_fetcher import SelectionRunSeed, ThemedArtworkSelection
from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    ContrastBucket,
    DominantColorFamily,
    LuminanceBucket,
)
from src.carousel_set_optimizer import (
    FORMAT_POLICIES,
    MAX_SWAP_ITERATIONS,
    SET_BEAM_WIDTH,
    build_selection_features,
    optimize_carousel_set,
    pairwise_redundancy,
    score_carousel_set,
)
from src.carousel_themes import (
    CarouselFormat,
    CarouselThemeDefinition,
    ThemeFamily,
    get_default_theme_registry,
)
from src.engagement_learning import EngagementModel, FeatureEstimate
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.theme_acquisition import ThemeAcquisitionPolicy


def _theme(carousel_format=CarouselFormat.THEMATIC_COLLECTION):
    return CarouselThemeDefinition(
        id=f"test_{carousel_format.value.casefold()}",
        title="Test Theme",
        family=ThemeFamily.SUBJECT,
        format=carousel_format,
        description="A sufficiently detailed test carousel theme.",
        primary_queries=("reading art",),
        required_terms=("reading",),
    )


def _visual(
    orientation=ArtworkOrientation.LANDSCAPE,
    luminance=LuminanceBucket.MID,
    color=DominantColorFamily.BLUE,
):
    dimensions = {
        ArtworkOrientation.PORTRAIT: (800, 1200),
        ArtworkOrientation.SQUAREISH: (1000, 1000),
        ArtworkOrientation.LANDSCAPE: (1200, 800),
    }[orientation]
    means = {LuminanceBucket.DARK: 45.0, LuminanceBucket.MID: 125.0, LuminanceBucket.LIGHT: 210.0}
    return ArtworkVisualFeatures(
        width=dimensions[0],
        height=dimensions[1],
        aspect_ratio=dimensions[0] / dimensions[1],
        orientation=orientation,
        mean_luminance=means[luminance],
        luminance_bucket=luminance,
        mean_saturation=0.6,
        dominant_color_family=color,
        contrast_bucket=ContrastBucket.MEDIUM,
    )


def _artwork(
    index,
    *,
    artist=None,
    museum=None,
    region=None,
    date=None,
    medium="Oil on canvas",
    relevance=90.0,
    quality=85.0,
    selection=None,
    visual=None,
    identifier=None,
):
    default_regions = (
        "europe",
        "east_asia",
        "north_america",
        "latin_america_caribbean",
        "oceania",
    )
    return {
        "id": identifier or f"aic_{index}",
        "title": f"Reading Study {index}",
        "artist": artist if artist is not None else f"Artist {index}",
        "museum": museum if museum is not None else f"Museum {index % 4}",
        "region": region if region is not None else default_regions[index % len(default_regions)],
        "date": date or str(1500 + index * 60),
        "medium": medium,
        "classification": "Painting",
        "description": f"A reading scene with explicit detail {index}.",
        "theme_relevance_score": relevance,
        "quality_score": quality,
        "selection_score": selection if selection is not None else relevance * 0.68 + quality * 0.30,
        "visual_features": visual or _visual(),
    }


def test_pairwise_redundancy_is_bounded_and_unknown_artists_are_independent():
    theme = _theme()
    same = build_selection_features(_artwork(1, artist="Known Artist", museum="Museum A"), theme)
    other = build_selection_features(_artwork(2, artist="Known Artist", museum="Museum A"), theme)
    penalty = pairwise_redundancy(same, other, FORMAT_POLICIES[theme.format])

    assert penalty.same_artist == 12.0
    assert penalty.same_museum == 2.5
    assert penalty.same_orientation == 0.5
    assert penalty.similar_luminance == 0.6
    assert penalty.same_color == 0.6

    unknown_one = build_selection_features(_artwork(3, artist="Unknown Artist"), theme)
    unknown_two = build_selection_features(_artwork(4, artist="Artist unknown"), theme)
    assert pairwise_redundancy(
        unknown_one, unknown_two, FORMAT_POLICIES[theme.format]
    ).same_artist == 0.0


def test_theme_required_similarity_is_suppressed_by_format_policy():
    cases = (
        (CarouselFormat.REGIONAL, "same_region"),
        (CarouselFormat.PERIOD_FOCUS, "same_period"),
        (CarouselFormat.MEDIUM_FOCUS, "same_medium"),
        (CarouselFormat.COLOR_STUDY, "same_color"),
    )
    for carousel_format, component in cases:
        theme = _theme(carousel_format)
        first = build_selection_features(_artwork(1), theme)
        second = build_selection_features(_artwork(9, date=_artwork(1)["date"]), theme)
        penalty = pairwise_redundancy(first, second, FORMAT_POLICIES[carousel_format])
        assert getattr(penalty, component) == 0.0


def test_optimizer_uses_marginal_value_and_can_reject_a_higher_ranked_duplicate_artist():
    theme = _theme()
    artworks = [_artwork(0, artist="Repeated", selection=99.0)]
    artworks.append(_artwork(1, artist="Repeated", selection=98.0))
    artworks.extend(_artwork(index, selection=97.0 - index) for index in range(2, 9))

    result = optimize_carousel_set(artworks, theme=theme)

    assert len(result.artworks) == 8
    assert sum(artwork["artist"] == "Repeated" for artwork in result.artworks) == 1
    assert "aic_0" not in {artwork["id"] for artwork in result.artworks}
    assert "aic_1" in {artwork["id"] for artwork in result.artworks}


def test_optimizer_preserves_floors_uniqueness_caps_and_determinism():
    theme = _theme()
    artworks = [
        _artwork(index, museum="Museum A" if index < 5 else f"Museum {index}")
        for index in range(10)
    ]
    artworks.append(_artwork(99, identifier="aic_0", selection=100.0))
    first = optimize_carousel_set(artworks, theme=theme)
    second = optimize_carousel_set(list(reversed(artworks)), theme=theme)

    assert [artwork["id"] for artwork in first.artworks] == [
        artwork["id"] for artwork in second.artworks
    ]
    assert len({artwork["id"] for artwork in first.artworks}) == 8
    assert sum(artwork["museum"] == "Museum A" for artwork in first.artworks) <= 3
    assert first.beam_width == SET_BEAM_WIDTH
    assert first.swap_iterations <= MAX_SWAP_ITERATIONS

    insufficient = [_artwork(index) for index in range(2)]
    insufficient.extend(
        [
            _artwork(2, relevance=49.9),
            _artwork(3, quality=49.9),
        ]
    )
    try:
        optimize_carousel_set(insufficient, theme=theme)
    except ValueError as error:
        assert "got 2" in str(error)
    else:
        raise AssertionError("quality and relevance floors must not be bypassed")


def test_adaptive_optimizer_accepts_five_excellent_works_with_distinct_cover():
    theme = _theme()
    artworks = [
        _artwork(
            index,
            artist="Repeated Artist",
            museum="Single Museum",
            region="europe",
            date="1880",
            medium="Watercolor on paper",
            relevance=score,
            quality=score,
            selection=score,
        )
        for index, score in enumerate((96.0, 94.0, 92.0, 90.0, 88.0))
    ]

    result = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("distinct-cover",)
    )

    assert len(result.artworks) == 5
    assert {artwork["museum"] for artwork in result.artworks} == {"Single Museum"}
    assert {artwork["artist"] for artwork in result.artworks} == {"Repeated Artist"}
    assert result.optimizer_size_decision == "no_feasible_larger_set"


def test_adaptive_optimizer_can_select_eight_excellent_nonredundant_works():
    theme = _theme()
    artworks = [
        _artwork(index, relevance=score, quality=score, selection=score)
        for index, score in enumerate((96.0, 94.0, 92.0, 90.0, 88.0, 86.0, 84.0, 82.0))
    ]

    result = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("distinct-cover",)
    )

    assert len(result.artworks) == 8
    assert result.optimizer_size_decision == "maximum_featured_reached"


def test_adaptive_optimizer_rejects_a_weak_tail_instead_of_filling_to_eight():
    theme = _theme()
    strengths = (91.0, 89.0, 87.0, 85.0, 81.0, 67.0, 61.0, 60.0)
    artworks = [
        _artwork(index, relevance=score, quality=score, selection=score)
        for index, score in enumerate(strengths)
    ]

    result = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("distinct-cover",)
    )

    assert len(result.artworks) == 5
    assert result.optimizer_size_decision == "marginal_utility_threshold"
    rejected = next(
        diagnostic
        for diagnostic in result.marginal_diagnostics
        if diagnostic.featured_count == 6
    )
    assert not rejected.accepted
    assert rejected.marginal_inclusion_utility is not None


def test_soft_diversity_does_not_apply_strict_or_relaxed_profiles():
    theme = _theme()
    artworks = [
        _artwork(
            index,
            artist=f"Artist {index}",
            relevance=92.0 - index,
            quality=92.0 - index,
            selection=92.0 - index,
        )
        for index in range(6)
    ]
    artworks.extend(
        [
            _artwork(6, artist="Artist 0", relevance=78.0, quality=78.0, selection=78.0),
            _artwork(7, artist="Artist 1", relevance=77.0, quality=77.0, selection=77.0),
        ]
    )

    result = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("distinct-cover",)
    )

    assert len(result.artworks) == 6
    assert result.hard_constraint_profile == "intrinsic_only"
    eight = next(item for item in result.marginal_diagnostics if item.featured_count == 8)
    assert eight.hard_constraint_profile == "intrinsic_only"


def test_cover_aware_optimizer_uses_seven_when_eight_consumes_only_cover_option():
    theme = _theme()
    artworks = [
        _artwork(index, relevance=94.0, quality=94.0, selection=94.0)
        for index in range(8)
    ]

    result = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("aic_7",)
    )

    assert len(result.artworks) == 7
    assert "aic_7" not in {artwork["id"] for artwork in result.artworks}
    assert result.optimizer_size_decision == "no_feasible_larger_set"


def test_adaptive_cardinality_and_membership_are_deterministic():
    theme = _theme()
    strengths = (93.0, 91.0, 88.0, 84.0, 80.0, 66.0, 62.0, 60.0)
    artworks = [
        _artwork(index, relevance=score, quality=score, selection=score)
        for index, score in enumerate(strengths)
    ]

    first = optimize_carousel_set(
        artworks, theme=theme, cover_candidate_ids=("distinct-cover",)
    )
    second = optimize_carousel_set(
        list(reversed(artworks)),
        theme=theme,
        cover_candidate_ids=("distinct-cover",),
    )

    assert [artwork["id"] for artwork in first.artworks] == [
        artwork["id"] for artwork in second.artworks
    ]
    assert first.marginal_diagnostics == second.marginal_diagnostics


def test_general_theme_improves_region_diversity_when_equivalent_candidates_exist():
    theme = _theme()
    artworks = [_artwork(index, region="europe") for index in range(5)]
    alternatives = (
        "east_asia",
        "north_america",
        "latin_america_caribbean",
        "oceania",
        "middle_east_north_africa",
        "sub_saharan_africa",
    )
    artworks.extend(
        _artwork(index, region=region) for index, region in enumerate(alternatives, start=5)
    )

    result = optimize_carousel_set(artworks, theme=theme)
    regions = [artwork["region"] for artwork in result.artworks]

    assert regions.count("europe") <= 2
    assert len(set(regions)) >= 5


def test_set_score_applies_bounded_learned_engagement_prediction():
    theme = _theme()
    features = tuple(build_selection_features(_artwork(index), theme) for index in range(5))
    baseline = score_carousel_set(features, theme)
    model = EngagementModel(
        global_score=50.0,
        confidence=1.0,
        useful_publications=10,
        effective_observations=10.0,
        feature_estimates={
            f"theme:{theme.id}": FeatureEstimate(
                score=90.0,
                observed_score=90.0,
                confidence=1.0,
                observations=10,
                effective_observations=10.0,
            )
        },
    )

    learned = score_carousel_set(
        features,
        theme,
        engagement_model=model,
        engagement_context={"carousel_theme": theme.id},
    )

    assert learned.engagement_prediction_adjustment == 4.0
    assert learned.total == baseline.total + 4.0


def test_structured_fetch_validates_a_finalist_pool_then_returns_typed_set_result(
    monkeypatch, tmp_path
):
    theme = _theme()
    theme = theme.model_copy(update={"minimum_candidate_target": 9})
    regions = (
        "europe",
        "east_asia",
        "north_america",
        "latin_america_caribbean",
        "oceania",
        "middle_east_north_africa",
    )
    candidates = [
        NormalizedArtwork(
            source="aic",
            source_id=str(index),
            title=f"Reading Artwork {index}",
            artist_name=f"Artist {index}",
            creation_date=str(1600 + index * 25),
            medium="Oil on canvas" if index % 2 else "Watercolor",
            classification="Painting",
            museum_name=f"Museum {index % 4}",
            image_url=f"https://images.example/{index}.jpg",
            region=regions[index % len(regions)],
            is_public_domain=True,
            rights_status="CONFIRMED_PUBLIC_DOMAIN",
        )
        for index in range(12)
    ]

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(art_fetcher, "get_museum_adapters", lambda: [Adapter()])
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr("src.theme_acquisition.calculate_quality_score", lambda *args: 90.0)

    def download(url, output_path, **kwargs):
        Path(output_path).write_bytes(b"validated test double")
        return ImageValidationResult(True, width=1600, height=1200, image_format="JPEG", reason="ok")

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)
    result = art_fetcher.fetch_themed_artworks(
        set(),
        "reading art",
        count=8,
        color_tone="warm",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
        theme_definition=theme,
        return_acquisition=True,
        acquisition_policy=ThemeAcquisitionPolicy(
            max_primary_queries=1,
            max_secondary_queries=0,
            minimum_safe_pool=9,
        ),
    )

    assert isinstance(result, ThemedArtworkSelection)
    assert 5 <= len(result.artworks) <= 8
    assert result.set_optimization.optimizer_size_decision == "marginal_utility_threshold"
    assert result.set_optimization is not None
    assert result.set_optimization.finalist_count == 12
    assert all(Path(artwork["local_image_path"]).exists() for artwork in result.artworks)


def test_twelve_valid_watercolors_cannot_fail_on_general_diversity(monkeypatch, tmp_path):
    theme = get_default_theme_registry().by_id("what_watercolor_can_do")
    candidates = [
        NormalizedArtwork(
            source="aic",
            source_id=str(index),
            title=f"Watercolor Study {index}",
            artist_name="Repeated Artist",
            creation_date="1880",
            medium="Watercolor on paper",
            classification="Drawing",
            museum_name="Single Museum",
            image_url=f"https://images.example/watercolor-{index}.jpg",
            image_width=1600,
            image_height=1200,
            region="europe",
            is_public_domain=True,
            rights_status="CONFIRMED_PUBLIC_DOMAIN",
        )
        for index in range(12)
    ]

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(art_fetcher, "get_museum_adapters", lambda: [Adapter()])
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr("src.theme_acquisition.calculate_quality_score", lambda *args: 90.0)

    def download(url, output_path, **kwargs):
        Path(output_path).write_bytes(b"validated test double")
        return ImageValidationResult(
            True,
            width=1600,
            height=1200,
            image_format="JPEG",
            reason="ok",
        )

    monkeypatch.setattr(art_fetcher, "validate_and_download_image_with_metadata", download)

    result = art_fetcher.fetch_themed_artworks(
        set(),
        "watercolor painting",
        count=8,
        color_tone="warm",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
        theme_definition=theme,
        return_acquisition=True,
    )

    assert isinstance(result, ThemedArtworkSelection)
    assert 5 <= len(result.artworks) <= 8
    assert len({artwork["id"] for artwork in result.artworks}) == len(result.artworks)
    assert {artwork["museum"] for artwork in result.artworks} == {"Single Museum"}
    assert {artwork["artist"] for artwork in result.artworks} == {"Repeated Artist"}
