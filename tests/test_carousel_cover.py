from pathlib import Path

import pytest
from PIL import Image, ImageOps

from src import carousel_cover
from src.art_fetcher import SelectionRunSeed
from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
from src.models import NormalizedArtwork
from src.quality_filter import ImageValidationResult
from src.carousel_themes import CarouselFormat, CarouselThemeDefinition, ThemeFamily
from src.theme_acquisition import ThemeAcquisitionPolicy, acquire_theme_candidates


def _candidate(identifier, *, rights=True, width=2000, height=2500):
    return NormalizedArtwork(
        source="aic",
        source_id=str(identifier),
        title=f"Winter landscape {identifier}",
        artist_name=f"Artist {identifier}",
        creation_date="1880",
        medium="Oil on canvas",
        classification="Painting",
        museum_name="Museum",
        image_url=f"https://images.example/{identifier}.jpg",
        image_width=width,
        image_height=height,
        is_public_domain=rights,
        rights_status="CONFIRMED_PUBLIC_DOMAIN" if rights else None,
    )


def _featured():
    return [
        {
            "id": f"aic_featured_{index}",
            "title": f"Featured {index}",
            "artist": f"Artist {index}",
            "date": str(1700 + index * 20),
            "museum": f"Museum {index % 3}",
        }
        for index in range(8)
    ]


def _install_adapter(monkeypatch, candidates, *, error=False, queries=None):
    class Adapter:
        source_id = "test"

        def fetch_candidates(self, **kwargs):
            if queries is not None:
                queries.append(kwargs["query"])
            if error:
                raise RuntimeError("source unavailable")
            return candidates

    monkeypatch.setattr(carousel_cover, "get_museum_adapters", lambda: [Adapter()])


def _install_downloads(monkeypatch, tmp_path, invalid_ids=()):
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))
    invalid_ids = set(invalid_ids)
    attempted = []

    def download(url, output_path):
        identifier = Path(url).stem
        attempted.append(identifier)
        if identifier in invalid_ids:
            return ImageValidationResult(False, reason="invalid_image")
        Image.new("RGB", (1600, 2000), (80, 110, 140)).save(output_path, "JPEG")
        return ImageValidationResult(True, 1600, 2000, "JPEG", "ok")

    monkeypatch.setattr(carousel_cover, "validate_and_download_image_with_metadata", download)
    return attempted


def test_cover_selection_is_distinct_permissive_and_theme_queried(monkeypatch, tmp_path):
    excluded = _candidate("featured_0")
    unconfirmed = _candidate("unconfirmed", rights=False)
    safe = _candidate("cover")
    queries = []
    _install_adapter(monkeypatch, [excluded, unconfirmed, safe], queries=queries)
    _install_downloads(monkeypatch, tmp_path)
    monkeypatch.setattr(carousel_cover, "calculate_quality_score", lambda candidate, weights: 90.0)
    featured = _featured()
    featured[0]["id"] = excluded.canonical_id

    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=featured,
        theme="winter",
        color_tone="cool",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
    )

    assert cover.canonical_id == unconfirmed.canonical_id
    assert cover.canonical_id not in {artwork["id"] for artwork in featured}
    assert cover.artwork["is_public_domain"] is False
    assert cover.artwork["rights_status"] is None
    assert queries == ["cool winter"]


def test_invalid_cover_image_is_rejected_and_next_ranked_candidate_is_used(monkeypatch, tmp_path):
    first = _candidate("first")
    second = _candidate("second")
    _install_adapter(monkeypatch, [first, second])
    attempted = _install_downloads(monkeypatch, tmp_path, invalid_ids={"first"})
    scores = {first.canonical_id: 100.0, second.canonical_id: 80.0}
    monkeypatch.setattr(
        carousel_cover,
        "calculate_quality_score",
        lambda candidate, weights: scores[candidate.canonical_id],
    )

    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=_featured(),
        theme="winter",
        color_tone="cool",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
    )

    assert attempted[:2] == ["first", "second"]
    assert cover.canonical_id == second.canonical_id


def test_no_safe_cover_raises_explicit_error_and_adapter_failures_are_isolated(monkeypatch, tmp_path):
    unsafe = _candidate("unsafe", rights=False)

    class FailingAdapter:
        source_id = "failing"

        def fetch_candidates(self, **kwargs):
            raise RuntimeError("source unavailable")

    class UnsafeAdapter:
        source_id = "unsafe"

        def fetch_candidates(self, **kwargs):
            return [unsafe]

    monkeypatch.setattr(carousel_cover, "get_museum_adapters", lambda: [FailingAdapter(), UnsafeAdapter()])
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))

    with pytest.raises(carousel_cover.EditorialCoverSelectionError):
        carousel_cover.select_editorial_cover(
            posted_ids=set(),
            featured_artworks=_featured(),
            theme="winter",
            color_tone="cool",
            selection_run_seed=SelectionRunSeed("fixed", "test"),
        )


def _cover_asset(path, mode):
    breakdown = CoverScoreBreakdown(20, 30, 12, 9, 9, 4, 4)
    artwork = {
        "id": "aic_cover",
        "title": "SECRET COVER TITLE",
        "artist": "SECRET COVER ARTIST",
        "date": "1499",
        "museum": "SECRET COVER MUSEUM",
    }
    return CoverAsset(artwork, str(path), mode, breakdown.total, breakdown)


@pytest.mark.parametrize("mode", [CoverMode.FULL_ARTWORK, CoverMode.DETAIL_CROP])
def test_cover_renderer_supports_both_modes_at_exact_instagram_size(tmp_path, mode):
    source_path = tmp_path / "source.jpg"
    Image.new("RGB", (800, 500), (180, 80, 40)).save(source_path, "JPEG")
    output_path = tmp_path / f"cover-{mode.value}.jpg"

    result = carousel_cover.create_carousel_editorial_cover(
        cover=_cover_asset(source_path, mode),
        editorial_title="WINTER LIGHT",
        editorial_subtitle="Snow, silence and changing light across centuries.",
        micro_facts=("8 works · 5 collections", "Works from 1750–1912"),
        output_path=str(output_path),
    )

    assert result == str(output_path)
    with Image.open(output_path) as rendered:
        assert rendered.size == (1080, 1350)


def test_full_artwork_background_preserves_source_aspect_ratio():
    source = Image.new("RGB", (400, 200), "red")
    background = carousel_cover._cover_background(source, CoverMode.FULL_ARTWORK)

    # A 2:1 source fitted to 1080px is 1080x540, centered without distortion.
    expected = source.resize((1080, 540), Image.Resampling.LANCZOS)
    assert background.crop((0, 405, 1080, 945)).tobytes() == expected.tobytes()


def test_detail_crop_uses_aspect_preserving_center_crop():
    source = Image.new("RGB", (1600, 1000), "blue")
    background = carousel_cover._cover_background(source, CoverMode.DETAIL_CROP)
    expected = ImageOps.fit(
        source,
        (1080, 1350),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    assert background.tobytes() == expected.tobytes()


def test_renderer_draws_editorial_copy_but_never_cover_identity(monkeypatch, tmp_path):
    source_path = tmp_path / "source.jpg"
    Image.new("RGB", (800, 1000), "navy").save(source_path, "JPEG")
    drawn_text = []
    original_text = carousel_cover.ImageDraw.ImageDraw.text

    def record_text(draw, xy, text, *args, **kwargs):
        drawn_text.append(str(text))
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(carousel_cover.ImageDraw.ImageDraw, "text", record_text)
    carousel_cover.create_carousel_editorial_cover(
        cover=_cover_asset(source_path, CoverMode.FULL_ARTWORK),
        editorial_title="A STUDY IN BLUE",
        editorial_subtitle="How painters used blue across different periods.",
        micro_facts=("8 works",),
        output_path=str(tmp_path / "cover.jpg"),
    )

    visible_copy = " ".join(drawn_text)
    assert "A STUDY IN BLUE" in visible_copy
    assert "ARTFOLIO" in visible_copy
    assert "SECRET COVER" not in visible_copy
    assert "1499" not in visible_copy


def test_grounded_micro_facts_use_only_featured_metadata():
    facts = carousel_cover.derive_cover_micro_facts(_featured())

    assert facts == ("8 works · 3 collections", "Works from 1700–1840")


def test_structured_cover_reuses_multi_query_pool_and_replaces_invalid_candidate(monkeypatch, tmp_path):
    theme = CarouselThemeDefinition(
        id="winter_pool_test",
        title="Winter Pool Test",
        family=ThemeFamily.SEASON,
        format=CarouselFormat.THEMATIC_COLLECTION,
        description="Snowy winter landscapes selected from documented museum metadata.",
        primary_queries=["winter snow", "snowy landscape"],
        secondary_queries=["winter scene"],
        required_terms=["winter", "snow"],
        preferred_terms=["landscape"],
        minimum_candidate_target=9,
    )
    candidates = [
        NormalizedArtwork(
            source="aic",
            source_id=str(index),
            title=f"Winter snow landscape {index}",
            artist_name=f"Artist {index}",
            creation_date="1880",
            medium="Oil on canvas",
            classification="Painting",
            museum_name=f"Museum {index % 4}",
            image_url=f"https://images.example/{index}.jpg",
            image_width=2000,
            image_height=1600,
            is_public_domain=True,
            rights_status="CONFIRMED_PUBLIC_DOMAIN",
        )
        for index in range(10)
    ]

    class Adapter:
        source_id = "test"

        def __init__(self):
            self.calls = []

        def fetch_candidates(self, **kwargs):
            self.calls.append(kwargs["query"])
            return candidates

    adapter = Adapter()
    scores = {candidate.canonical_id: 100.0 - index for index, candidate in enumerate(candidates)}
    monkeypatch.setattr(
        "src.theme_acquisition.calculate_quality_score",
        lambda candidate, weights: scores[candidate.canonical_id],
    )
    monkeypatch.setattr("src.theme_acquisition.calculate_measurement_coverage", lambda candidate: 1.0)
    acquisition = acquire_theme_candidates(
        theme,
        posted_ids=set(),
        adapters=[adapter],
        run_seed="fixed",
        museum_weights={},
        min_quality=50,
        policy=ThemeAcquisitionPolicy(minimum_safe_pool=9),
    )
    featured = [
        {"id": candidate.artwork.canonical_id}
        for candidate in acquisition.candidates[:8]
    ]
    remaining = [candidate.artwork.canonical_id for candidate in acquisition.candidates[8:]]
    monkeypatch.setattr(carousel_cover.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        carousel_cover,
        "calculate_quality_score",
        lambda candidate, weights: scores[candidate.canonical_id],
    )
    attempted = []

    def download(url, output_path):
        identifier = Path(url).stem
        attempted.append(f"aic_{identifier}")
        if f"aic_{identifier}" == remaining[0]:
            return ImageValidationResult(False, reason="invalid_image")
        Image.new("RGB", (1600, 2000), (80, 110, 140)).save(output_path, "JPEG")
        return ImageValidationResult(True, 1600, 2000, "JPEG", "ok")

    monkeypatch.setattr(carousel_cover, "validate_and_download_image_with_metadata", download)
    calls_before_cover = list(adapter.calls)
    cover = carousel_cover.select_editorial_cover(
        posted_ids=set(),
        featured_artworks=featured,
        theme="winter snow",
        color_tone="cool",
        selection_run_seed=SelectionRunSeed("fixed", "test"),
        theme_definition=theme,
        acquisition=acquisition,
    )

    assert adapter.calls == calls_before_cover
    assert attempted[:2] == remaining
    assert cover.canonical_id == remaining[1]
    assert cover.canonical_id not in {artwork["id"] for artwork in featured}
    assert cover.artwork["theme_relevance_score"] >= acquisition.policy.minimum_relevance
    assert all("title" not in artwork for artwork in featured)
