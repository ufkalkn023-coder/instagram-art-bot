from __future__ import annotations

import io
import socket
from dataclasses import replace

from PIL import Image
import pytest

from src import art_fetcher, carousel_cover, quality_filter
from src.artwork_visual_features import (
    ArtworkOrientation,
    ArtworkVisualFeatures,
    ContrastBucket,
    DominantColorFamily,
    LuminanceBucket,
)
from src.art_fetcher import SelectionRunSeed, _percentile
from src.carousel_themes import ThemeEvidenceMode, get_default_theme_registry
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.theme_acquisition import (
    DEFAULT_MIN_THEME_RELEVANCE,
    QueryType,
    ThemeQueryHit,
    ThemeAcquisitionPolicy,
    acquire_theme_candidates,
    evaluate_theme_relevance,
)


def _artwork(identifier: str, *, title: str, description: str = "") -> NormalizedArtwork:
    return NormalizedArtwork(
        source="aic",
        source_id=identifier,
        title=title,
        artist_name="Artist",
        creation_date="1880",
        medium="Oil on canvas",
        classification="Painting",
        description=description,
        museum_name="Museum",
        image_url=f"https://images.example/{identifier}.jpg",
        image_width=1600,
        image_height=1200,
        is_public_domain=True,
        rights_status="CONFIRMED_PUBLIC_DOMAIN",
    )


def _visual(
    *,
    color: DominantColorFamily = DominantColorFamily.NEUTRAL,
    luminance: LuminanceBucket = LuminanceBucket.MID,
    contrast: ContrastBucket = ContrastBucket.MEDIUM,
) -> ArtworkVisualFeatures:
    return ArtworkVisualFeatures(
        width=1600,
        height=1200,
        aspect_ratio=1.3333,
        orientation=ArtworkOrientation.LANDSCAPE,
        mean_luminance=130.0,
        luminance_bucket=luminance,
        mean_saturation=0.5,
        dominant_color_family=color,
        contrast_bucket=contrast,
        sample_width=128,
        sample_height=96,
    )


def _low_support_light() -> ArtworkVisualFeatures:
    return ArtworkVisualFeatures(
        width=1600,
        height=1200,
        aspect_ratio=1.3333,
        orientation=ArtworkOrientation.LANDSCAPE,
        mean_luminance=None,
        luminance_bucket=LuminanceBucket.LIGHT,
        mean_saturation=None,
        dominant_color_family=DominantColorFamily.UNKNOWN,
        contrast_bucket=ContrastBucket.UNKNOWN,
        sample_width=128,
        sample_height=96,
    )


def _hit(query: str, rank: int = 0) -> ThemeQueryHit:
    return ThemeQueryHit(QueryType.PRIMARY, rank, query)


def test_registry_classifies_visual_formats_without_changing_metadata_themes():
    registry = get_default_theme_registry()

    assert registry.by_id("winter_light").evidence_mode is ThemeEvidenceMode.HYBRID
    assert registry.by_id("study_in_blue").evidence_mode is ThemeEvidenceMode.HYBRID
    for theme_id in (
        "women_reading",
        "madonna_and_child",
        "storm_at_sea",
        "artists_at_work",
        "cats_in_art",
    ):
        assert registry.by_id(theme_id).evidence_mode is ThemeEvidenceMode.METADATA


def test_hybrid_prequalification_is_not_final_publication_eligibility():
    theme = get_default_theme_registry().by_id("study_in_blue")
    artwork = _artwork("provisional", title="Untitled landscape")
    provisional = evaluate_theme_relevance(
        artwork, theme, [_hit("blue painting")]
    )

    assert provisional.provisional_eligible
    assert not provisional.relevance_eligible
    assert provisional.theme_relevance_score < DEFAULT_MIN_THEME_RELEVANCE


def test_winter_semantics_and_measurable_light_both_contribute():
    theme = get_default_theme_registry().by_id("winter_light")
    winter = _artwork("winter", title="Winter landscape with snow")
    matching = evaluate_theme_relevance(
        winter,
        theme,
        [_hit("winter light painting")],
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.MEDIUM),
    )
    semantic_only = evaluate_theme_relevance(
        winter, theme, [_hit("winter light painting")]
    )
    unrelated = evaluate_theme_relevance(
        _artwork("bright", title="Portrait of a gentleman"),
        theme,
        [_hit("museum painting")],
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.MEDIUM),
    )

    assert matching.relevance_eligible
    assert matching.theme_relevance_score >= DEFAULT_MIN_THEME_RELEVANCE
    assert semantic_only.theme_relevance_score < matching.theme_relevance_score
    assert not unrelated.relevance_eligible


def test_winter_light_reachability_and_component_boundaries():
    theme = get_default_theme_registry().by_id("winter_light")
    strong_metadata_without_literal_light = _artwork(
        "strong",
        title="Winter snow landscape with long shadows",
        description="A pale seasonal scene.",
    )
    hits = [
        _hit("winter light painting", 0),
        _hit("snow light landscape", 1),
        _hit("sunlight on snow", 2),
    ]
    valid = evaluate_theme_relevance(
        strong_metadata_without_literal_light,
        theme,
        hits,
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH),
    )
    poor_light = evaluate_theme_relevance(
        strong_metadata_without_literal_light,
        theme,
        hits,
        _visual(luminance=LuminanceBucket.DARK, contrast=ContrastBucket.LOW),
    )
    unrelated_bright = evaluate_theme_relevance(
        _artwork("bright", title="Portrait of a gentleman"),
        theme,
        [_hit("museum painting")],
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH),
    )
    secondary_bright = evaluate_theme_relevance(
        _artwork("secondary", title="Untitled bright composition"),
        theme,
        [ThemeQueryHit(QueryType.SECONDARY, 0, "winter sun art")],
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH),
    )

    assert valid.relevance_eligible
    assert valid.theme_relevance_score == 100.0
    assert "light" not in strong_metadata_without_literal_light.title.casefold()
    assert poor_light.theme_relevance_score < valid.theme_relevance_score
    assert not poor_light.visual_grounded
    assert not unrelated_bright.semantic_grounded
    assert not unrelated_bright.relevance_eligible
    assert not secondary_bright.semantic_grounded
    assert not secondary_bright.relevance_eligible


@pytest.mark.parametrize(
    ("theme_id", "visual", "primary_indexes"),
    [
        (
            "winter_light",
            _visual(
                luminance=LuminanceBucket.LIGHT,
                contrast=ContrastBucket.HIGH,
            ),
            (0, 1),
        ),
        (
            "impressionist_light",
            _visual(luminance=LuminanceBucket.LIGHT),
            (0, 1),
        ),
        (
            "autumn_light",
            _visual(
                color=DominantColorFamily.ORANGE,
                luminance=LuminanceBucket.LIGHT,
            ),
            (0, 2),
        ),
        (
            "candlelight",
            _visual(
                luminance=LuminanceBucket.DARK,
                contrast=ContrastBucket.HIGH,
            ),
            (0, 2),
        ),
    ],
)
def test_light_study_primary_provenance_is_bounded_semantic_support(
    theme_id, visual, primary_indexes
):
    theme = get_default_theme_registry().by_id(theme_id)
    unrelated_metadata = _artwork("provenance", title="Untitled composition")
    first_primary = evaluate_theme_relevance(
        unrelated_metadata,
        theme,
        [_hit(theme.primary_queries[primary_indexes[0]])],
        visual,
    )
    repeated_primary = evaluate_theme_relevance(
        unrelated_metadata,
        theme,
        [
            _hit(theme.primary_queries[index], rank)
            for rank, index in enumerate(primary_indexes)
        ],
        visual,
    )
    arbitrary_primary = evaluate_theme_relevance(
        unrelated_metadata,
        theme,
        [_hit("museum painting")],
        visual,
    )
    missing_visual = evaluate_theme_relevance(
        unrelated_metadata,
        theme,
        [_hit(theme.primary_queries[primary_indexes[0]])],
    )

    assert first_primary.semantic_grounded
    assert first_primary.visual_grounded
    assert first_primary.relevance_eligible
    assert first_primary.theme_relevance_score >= DEFAULT_MIN_THEME_RELEVANCE
    assert repeated_primary.relevance_eligible
    assert repeated_primary.theme_relevance_score >= first_primary.theme_relevance_score
    assert not arbitrary_primary.semantic_grounded
    assert not arbitrary_primary.relevance_eligible
    assert not missing_visual.visual_grounded
    assert not missing_visual.semantic_grounded
    assert not missing_visual.relevance_eligible


@pytest.mark.parametrize(
    ("theme_id", "required_term", "visual"),
    [
        (
            "winter_light",
            "winter",
            _visual(
                luminance=LuminanceBucket.LIGHT,
                contrast=ContrastBucket.HIGH,
            ),
        ),
        (
            "impressionist_light",
            "Impressionist",
            _visual(luminance=LuminanceBucket.LIGHT),
        ),
        (
            "autumn_light",
            "autumn",
            _visual(
                color=DominantColorFamily.ORANGE,
                luminance=LuminanceBucket.LIGHT,
            ),
        ),
        (
            "candlelight",
            "candlelight",
            _visual(
                luminance=LuminanceBucket.DARK,
                contrast=ContrastBucket.HIGH,
            ),
        ),
    ],
)
def test_light_study_explicit_metadata_remains_independent_semantic_evidence(
    theme_id, required_term, visual
):
    theme = get_default_theme_registry().by_id(theme_id)
    evidence = evaluate_theme_relevance(
        _artwork("metadata", title=f"Study of {required_term}"),
        theme,
        [],
        visual,
    )

    assert evidence.required_matches
    assert evidence.semantic_grounded
    assert evidence.relevance_eligible
    assert evidence.theme_relevance_score >= DEFAULT_MIN_THEME_RELEVANCE


def test_light_study_provenance_never_overrides_visual_or_exclusion_failures():
    theme = get_default_theme_registry().by_id("winter_light")
    primary = [_hit(theme.primary_queries[0])]
    non_matching_visual = evaluate_theme_relevance(
        _artwork("dark", title="Untitled composition"),
        theme,
        primary,
        _visual(luminance=LuminanceBucket.DARK, contrast=ContrastBucket.LOW),
    )
    excluded_theme = theme.model_copy(update={"excluded_terms": ("forbidden",)})
    excluded = evaluate_theme_relevance(
        _artwork("excluded", title="Forbidden winter scene"),
        excluded_theme,
        primary,
        _visual(
            luminance=LuminanceBucket.LIGHT,
            contrast=ContrastBucket.HIGH,
        ),
    )

    assert not non_matching_visual.visual_grounded
    assert not non_matching_visual.semantic_grounded
    assert not non_matching_visual.relevance_eligible
    assert excluded.excluded_matches == ("forbidden",)
    assert not excluded.relevance_eligible


def test_candlelight_single_visual_dimension_explains_score_fifty_cluster():
    theme = get_default_theme_registry().by_id("candlelight")
    evidence = evaluate_theme_relevance(
        _artwork("candle", title="Untitled composition"),
        theme,
        [_hit(theme.primary_queries[0])],
        _visual(luminance=LuminanceBucket.DARK, contrast=ContrastBucket.LOW),
    )

    assert evidence.semantic_grounded
    assert evidence.visual_grounded
    assert evidence.relevance_breakdown.primary_query == 24.0
    assert evidence.relevance_breakdown.visual_target == 14.0
    assert evidence.relevance_breakdown.visual_support == 12.0
    assert evidence.theme_relevance_score == 50.0
    assert evidence.theme_relevance_score < DEFAULT_MIN_THEME_RELEVANCE


def test_color_study_requires_actual_target_color_evidence():
    theme = get_default_theme_registry().by_id("study_in_blue")
    artwork = _artwork("color", title="Untitled composition")
    blue = evaluate_theme_relevance(
        artwork,
        theme,
        [_hit("blue painting")],
        _visual(color=DominantColorFamily.BLUE),
    )
    red = evaluate_theme_relevance(
        artwork,
        theme,
        [_hit("blue painting")],
        _visual(color=DominantColorFamily.RED),
    )

    assert blue.relevance_eligible
    assert blue.theme_relevance_score >= DEFAULT_MIN_THEME_RELEVANCE
    assert blue.theme_relevance_score > red.theme_relevance_score
    assert not red.relevance_eligible


def test_hybrid_image_inspection_is_secure_and_bounded(monkeypatch, tmp_path):
    theme = get_default_theme_registry().by_id("winter_light")
    candidates = [
        _artwork(str(index), title=f"Unclassified landscape {index}")
        for index in range(82)
    ]

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score",
        lambda artwork, weights: 90.0,
    )
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage",
        lambda artwork: 1.0,
    )
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[Adapter()],
        run_seed="bounded",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(candidates_per_adapter_query=100),
    )
    attempts = []

    def reject_securely(url, output_path, **kwargs):
        attempts.append(url)
        return ImageValidationResult(False, reason="invalid_image")

    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        art_fetcher, "validate_and_download_image_with_metadata", reject_securely
    )

    with pytest.raises(
        art_fetcher.CarouselSelectionError,
        match="insufficient_final_relevance_pool",
    ):
        art_fetcher._select_acquired_theme_artworks(acquisition, count=8)

    assert len(acquisition.candidates) == 82
    assert len(attempts) == art_fetcher.MAX_FINALIST_VALIDATION_ATTEMPTS == 40


def test_hybrid_aic_analysis_requests_843_directly(monkeypatch, tmp_path):
    theme = get_default_theme_registry().by_id("winter_light")
    candidates = [
        _artwork(str(index), title=f"Winter snow landscape {index}")
        for index in range(6)
    ]
    for candidate in candidates:
        candidate.artist_name = f"Artist {candidate.source_id}"
        candidate.museum_name = f"Museum {candidate.source_id}"
        candidate.image_url = (
            f"https://www.artic.edu/iiif/2/image-{candidate.source_id}"
            "/full/1686,/0/default.jpg"
        )

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    class Response:
        def __init__(self, status_code, payload=b""):
            self.status_code = status_code
            self.headers = {"Content-Type": "image/jpeg"} if payload else {}
            self.payload = payload

        def iter_content(self, chunk_size):
            yield self.payload

        def close(self):
            pass

    image_stream = io.BytesIO()
    Image.new("RGB", (843, 600), "lightblue").save(image_stream, "JPEG")
    image_bytes = image_stream.getvalue()
    requested_urls = []

    def get(url, **kwargs):
        requested_urls.append(url)
        return Response(404) if "/1686,/" in url else Response(200, image_bytes)

    monkeypatch.setattr(
        quality_filter.socket,
        "getaddrinfo",
        lambda hostname, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    monkeypatch.setattr(quality_filter.requests, "get", get)
    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score", lambda artwork, weights: 90.0
    )
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage", lambda artwork: 1.0
    )
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[Adapter()],
        run_seed="aic-fallback",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(candidates_per_adapter_query=10),
    )

    selected, _, acquisition = art_fetcher._select_acquired_theme_artworks(
        acquisition, count=5
    )

    assert len(selected) == 5
    assert acquisition.availability.images_attempted == 6
    assert acquisition.availability.aic_fallback_attempted == 0
    assert acquisition.availability.aic_fallback_recovered == 0
    assert acquisition.availability.aic_fallback_failed == 0
    assert set(requested_urls[:6]) == {
        f"https://www.artic.edu/iiif/2/image-{index}/full/843,/0/default.jpg"
        for index in range(6)
    }


def test_hybrid_diagnostics_separate_image_and_relevance_failures(
    monkeypatch, tmp_path, caplog
):
    theme = get_default_theme_registry().by_id("winter_light")
    source_artworks = [
        _artwork("invalid", title="Winter snow scene"),
        _artwork("poor-light", title="Winter snow scene"),
        _artwork("unrelated", title="Portrait of a gentleman"),
        _artwork("below-threshold", title="Untitled", description="Winter scene"),
        _artwork("qualified", title="Winter snow with shadows"),
    ]
    source_artworks[0].source = "smithsonian"

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return source_artworks

    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score", lambda artwork, weights: 90.0
    )
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage", lambda artwork: 1.0
    )
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[Adapter()],
        run_seed="diagnostics",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(candidates_per_adapter_query=10),
    )
    one_hit = (_hit("winter light painting"),)
    secondary_hit = (ThemeQueryHit(QueryType.SECONDARY, 0, "winter sun art"),)
    hits_by_id = {
        "below-threshold": secondary_hit,
        "unrelated": (_hit("museum painting"),),
    }
    acquisition = replace(
        acquisition,
        candidates=tuple(
            replace(
                candidate,
                evidence=replace(
                    candidate.evidence,
                    matched_queries=hits_by_id.get(candidate.artwork.source_id, one_hit),
                ),
            )
            for candidate in acquisition.candidates
        ),
    )
    path_to_id = {}

    def download(url, output_path, **kwargs):
        identifier = url.rsplit("/", 1)[-1].removesuffix(".jpg")
        path_to_id[output_path] = identifier
        if identifier == "invalid":
            return ImageValidationResult(False, reason="http_status")
        return ImageValidationResult(
            True, width=1600, height=1200, image_format="JPEG", reason="ok"
        )

    def visual_features(path):
        identifier = path_to_id[path]
        if identifier == "poor-light":
            return _visual(
                luminance=LuminanceBucket.DARK, contrast=ContrastBucket.LOW
            )
        if identifier == "below-threshold":
            return _low_support_light()
        return _visual(
            luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH
        )

    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        art_fetcher, "validate_and_download_image_with_metadata", download
    )
    monkeypatch.setattr(art_fetcher, "extract_visual_features", visual_features)
    caplog.set_level("INFO", logger="src.art_fetcher")

    with pytest.raises(art_fetcher.CarouselSelectionError) as error:
        art_fetcher._select_acquired_theme_artworks(acquisition, count=8)

    diagnostics = error.value.availability
    assert diagnostics.images_attempted == 5
    assert diagnostics.images_validated == 4
    assert diagnostics.image_validation_failed == 1
    assert diagnostics.visually_scored == 4
    assert diagnostics.final_relevance_qualified == 1
    assert diagnostics.qualified_at_60 == 1
    assert dict(diagnostics.final_relevance_failures) == {
        "final_score_below_threshold": 1,
        "image_validation_failure": 1,
        "insufficient_semantic_evidence": 1,
        "insufficient_visual_target_evidence": 1,
    }
    scores = [
        evaluate_theme_relevance(
            candidate.artwork,
            theme,
            hits_by_id.get(candidate.artwork.source_id, one_hit),
            visual_features_for_candidate,
        ).theme_relevance_score
        for candidate, visual_features_for_candidate in (
            (
                next(c for c in acquisition.candidates if c.artwork.source_id == "poor-light"),
                _visual(luminance=LuminanceBucket.DARK, contrast=ContrastBucket.LOW),
            ),
            (
                next(c for c in acquisition.candidates if c.artwork.source_id == "unrelated"),
                _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH),
            ),
            (
                next(
                    c
                    for c in acquisition.candidates
                    if c.artwork.source_id == "below-threshold"
                ),
                _low_support_light(),
            ),
            (
                next(c for c in acquisition.candidates if c.artwork.source_id == "qualified"),
                _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.HIGH),
            ),
        )
    ]
    assert diagnostics.final_score_min == min(scores)
    assert diagnostics.final_score_p25 == _percentile(scores, 0.25)
    assert diagnostics.final_score_median == _percentile(scores, 0.5)
    assert diagnostics.final_score_p75 == _percentile(scores, 0.75)
    assert diagnostics.final_score_max == max(scores)
    assert (
        "image_validation_failures theme=winter_light total=1 "
        "reasons=http_status:1 sources=smithsonian:1 "
        "source_reasons=smithsonian/http_status:1"
    ) in caplog.text


def test_hybrid_cover_reuses_final_relevant_validated_image(monkeypatch, tmp_path):
    theme = get_default_theme_registry().by_id("winter_light")
    candidates = [
        _artwork(str(index), title=f"Winter snow landscape {index}")
        for index in range(9)
    ]

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score",
        lambda artwork, weights: 90.0,
    )
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage",
        lambda artwork: 1.0,
    )
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[Adapter()],
        run_seed="cover",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(candidates_per_adapter_query=20),
    )
    cover_candidate = acquisition.candidates[-1]
    final_evidence = evaluate_theme_relevance(
        cover_candidate.artwork,
        theme,
        cover_candidate.evidence.matched_queries,
        _visual(luminance=LuminanceBucket.LIGHT, contrast=ContrastBucket.MEDIUM),
    )
    source_path = tmp_path / "validated_cover.jpg"
    Image.new("RGB", (1600, 1200), "lightblue").save(source_path, "JPEG")
    validated = {
        "id": cover_candidate.artwork.canonical_id,
        "local_image_path": str(source_path),
        "theme_relevance_score": final_evidence.theme_relevance_score,
        "matched_queries": tuple(hit.query for hit in final_evidence.matched_queries),
        "visual_features": _visual(
            luminance=LuminanceBucket.LIGHT,
            contrast=ContrastBucket.MEDIUM,
        ),
    }
    acquisition = replace(acquisition, validated_artworks=(validated,))
    featured = [
        {"id": candidate.artwork.canonical_id}
        for candidate in acquisition.candidates[:-1]
    ]
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        carousel_cover,
        "validate_and_download_image_with_metadata",
        lambda *args, **kwargs: pytest.fail("hybrid cover must not download twice"),
    )

    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=featured,
        theme=theme.title,
        color_tone="cool",
        selection_run_seed=SelectionRunSeed("cover", "test"),
        theme_definition=theme,
        acquisition=acquisition,
    )

    assert cover.canonical_id == cover_candidate.artwork.canonical_id
    assert cover.canonical_id not in {str(artwork["id"]) for artwork in featured}
    assert cover.artwork["theme_relevance_score"] >= DEFAULT_MIN_THEME_RELEVANCE
    assert not source_path.exists()


def test_metadata_finalist_gate_counts_five_featured_separately_from_cover(
    monkeypatch, tmp_path
):
    theme = get_default_theme_registry().by_id("landscape_across_centuries")
    candidates = [
        _artwork(str(index), title=f"Landscape study {index}")
        for index in range(8)
    ]
    for index, candidate in enumerate(candidates):
        candidate.artist_name = f"Artist {index}"
        candidate.museum_name = f"Museum {index}"
        candidate.creation_date = str(1500 + index * 60)

    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            return candidates

    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score",
        lambda artwork, weights: 90.0,
    )
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_measurement_coverage",
        lambda artwork: 1.0,
    )
    monkeypatch.setattr(art_fetcher, "calculate_quality_score", lambda *args: 90.0)
    monkeypatch.setattr(
        art_fetcher, "calculate_measurement_coverage", lambda artwork: 1.0
    )
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[Adapter()],
        run_seed="metadata-five",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(candidates_per_adapter_query=20),
    )
    attempts = 0

    def download(url, output_path, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts > 5:
            return ImageValidationResult(False, reason="http_status")
        Image.new("RGB", (1600, 1200), "navy").save(output_path, "JPEG")
        return ImageValidationResult(
            True,
            width=1600,
            height=1200,
            image_format="JPEG",
            reason="ok",
        )

    monkeypatch.setattr(art_fetcher.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        art_fetcher, "validate_and_download_image_with_metadata", download
    )
    monkeypatch.setattr(art_fetcher, "extract_visual_features", lambda path: _visual())

    selected, optimization, final_acquisition = (
        art_fetcher._select_acquired_theme_artworks(acquisition, count=8)
    )

    assert acquisition.availability.absolute_minimum == 5
    assert len(selected) == 5
    assert len(optimization.artworks) == 5
    assert final_acquisition.validated_artworks == ()
    assert attempts == 8
