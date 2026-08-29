import pytest

from src.artwork_visual_features import ArtworkOrientation, features_from_dimensions
from src.carousel_caption import format_carousel_caption
from src.carousel_editorial import (
    derive_carousel_editorial_facts,
    derive_cover_micro_facts,
    display_theme_title,
    fallback_carousel_intro,
    fallback_editorial_subtitle,
    format_count,
    grounded_gemini_intro,
)
from src.carousel_themes import CarouselFormat


def _facts(artworks, *, theme_id="impressionist_light", title="Impressionist Light"):
    return derive_carousel_editorial_facts(
        artworks,
        theme_id=theme_id,
        theme_title=title,
        carousel_format=CarouselFormat.LIGHT_STUDY,
    )


def _work(index, *, artist=None, date=None, museum=None, orientation=None):
    work = {
        "id": f"work_{index}",
        "title": f"Title {index}",
        "artist": artist if artist is not None else f"Artist {index}",
        "date": date if date is not None else str(1880 + index),
        "museum": museum if museum is not None else f"Museum {index}",
        "region": "europe",
        "medium": "Oil on canvas",
        "visual_features": {"orientation": "portrait"},
    }
    if orientation is not None:
        work["visual_features"] = orientation
    return work


@pytest.mark.parametrize(
    ("count", "singular", "plural", "expected"),
    [
        (1, "work", None, "1 work"),
        (2, "work", None, "2 works"),
        (1, "collection", None, "1 collection"),
        (2, "collection", None, "2 collections"),
        (1, "artist", None, "1 artist"),
        (4, "artist", None, "4 artists"),
        (1, "museum collection", None, "1 museum collection"),
        (3, "museum collection", None, "3 museum collections"),
    ],
)
def test_format_count_handles_singular_and_plural(count, singular, plural, expected):
    assert format_count(count, singular, plural) == expected


def test_exact_live_regression_is_grounded_and_excludes_cover():
    museum = "Art Institute of Chicago"
    artworks = [
        _work(1, artist="Mary Cassatt", date="1893", museum=museum,
              orientation=features_from_dimensions(800, 1200)),
        _work(2, artist="Edward Henry Potthast", date="c. 1915", museum=museum,
              orientation=features_from_dimensions(1200, 800)),
        _work(3, artist="Claude Monet", date="1901", museum=museum,
              orientation=features_from_dimensions(1000, 1000)),
        _work(4, artist="Cecilia Beaux", date="1898", museum=museum,
              orientation=features_from_dimensions(800, 1200)),
    ]
    cover = _work(99, artist="Cover Artist", date="1900", museum=museum)
    facts = _facts(artworks)
    subtitle = fallback_editorial_subtitle(facts)
    microfacts = derive_cover_micro_facts(facts)
    intro = fallback_carousel_intro(facts)
    caption = format_carousel_caption(
        theme_title=facts.theme_title,
        editorial_intro=intro,
        hashtags="#Art",
        featured_artworks=artworks,
    )
    visible_copy = " ".join((subtitle, *microfacts, intro, caption))

    assert facts.featured_count == 4
    assert facts.total_slide_count == 5
    assert facts.distinct_artist_count == 4
    assert facts.distinct_museum_count == 1
    assert facts.date_span_label == "1893–c. 1915"
    assert facts.earliest_approximate is False
    assert facts.latest_approximate is True
    assert facts.distinct_orientation_count == 3
    assert subtitle == (
        "4 works from the Art Institute of Chicago, selected around Impressionist Light."
    )
    assert microfacts == ("4 works", "4 artists", "Works from 1893–c. 1915")
    assert "across museum collections" not in visible_copy.casefold()
    assert "1 collections" not in visible_copy
    assert "from the Art Institute of Chicago" in intro
    assert "c. 1893–1915" not in visible_copy
    assert not any(
        phrase in intro.casefold()
        for phrase in (
            "source metadata",
            "source records",
            "selected order",
            "grounded comparison",
            "preserves each",
        )
    )
    assert "5 works" not in visible_copy
    assert cover["title"] not in caption
    assert cover["artist"] not in caption


def test_multi_museum_set_allows_grounded_plural_language():
    museums = ["Museum A", "Museum B", "Museum C", "Museum A", "Museum B"]
    artworks = [
        _work(index, museum=museums[index - 1], date=str(1800 + index * 20))
        for index in range(1, 6)
    ]
    facts = _facts(artworks, theme_id="shared_light", title="Shared Light")

    assert facts.featured_count == 5
    assert facts.distinct_artist_count == 5
    assert facts.distinct_museum_count == 3
    assert fallback_editorial_subtitle(facts) == (
        "5 works across 3 museum collections, selected around Shared Light."
    )
    assert derive_cover_micro_facts(facts) == (
        "5 works",
        "5 artists",
        "3 collections",
    )
    assert "Museum A" not in fallback_editorial_subtitle(facts)


def test_sparse_metadata_omits_unsupported_cover_facts_and_variation_claims():
    artworks = [
        _work(1, artist="Unknown Artist", date="Date unknown", museum="Unknown Museum"),
        _work(2, artist="Artist unknown", date="", museum=""),
        _work(3, artist="Anonymous", date="19th century", museum="Museum C"),
    ]
    facts = _facts(artworks, theme_id="strong_theme", title="Strong Theme")
    subtitle = fallback_editorial_subtitle(facts)
    intro = fallback_carousel_intro(facts)

    assert facts.distinct_artist_count == 0
    assert facts.known_date_count == 1
    assert facts.date_span_label is None
    assert not facts.museum_metadata_complete
    assert subtitle == "3 works selected around Strong Theme."
    assert derive_cover_micro_facts(facts) == ("3 works",)
    assert "unknown" not in subtitle.casefold()
    assert "dates" not in intro
    assert "museum collections" not in intro


def test_strong_theme_remains_title_without_becoming_formal_classification():
    artworks = [_work(index, museum="One Museum") for index in range(1, 5)]
    facts = _facts(artworks)
    fallback = fallback_carousel_intro(facts)

    assert facts.theme_title == "Impressionist Light"
    assert "around Impressionist Light" in fallback
    assert "Impressionist works" not in fallback
    assert grounded_gemini_intro(
        "These Impressionist works span a focused selection.", facts, fallback
    ) == fallback
    assert grounded_gemini_intro(
        "The sequence is connected by the theme Impressionist Light.", facts, fallback
    ) != fallback
    assert grounded_gemini_intro(
        "Four works from several collections share this theme.", facts, fallback
    ) == fallback


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (("1893", "1915"), "1893–1915"),
        (("c. 1893", "1915"), "c. 1893–1915"),
        (("1893", "c. 1915"), "1893–c. 1915"),
        (("c. 1893", "c. 1915"), "c. 1893–c. 1915"),
        (("1877–79", "1901"), "1877–1901"),
        (("c. 1877", "1901"), "c. 1877–1901"),
        (("19th century", "1901"), None),
        (("1877", "Date unknown"), None),
    ],
)
def test_date_span_requires_complete_non_century_evidence(dates, expected):
    artworks = [
        _work(index, date=date, museum="One Museum")
        for index, date in enumerate(dates, 1)
    ]
    assert _facts(artworks).date_span_label == expected


@pytest.mark.parametrize(
    ("orientations", "expected"),
    [
        ((ArtworkOrientation.PORTRAIT,), 1),
        ((ArtworkOrientation.PORTRAIT, ArtworkOrientation.LANDSCAPE), 2),
        ((ArtworkOrientation.PORTRAIT, ArtworkOrientation.LANDSCAPE,
          ArtworkOrientation.SQUAREISH), 3),
        ((ArtworkOrientation.PORTRAIT, ArtworkOrientation.UNKNOWN), 1),
    ],
)
def test_orientation_facts_use_canonical_final_visual_features(orientations, expected):
    dimensions = {
        ArtworkOrientation.PORTRAIT: (800, 1200),
        ArtworkOrientation.LANDSCAPE: (1200, 800),
        ArtworkOrientation.SQUAREISH: (1000, 1000),
        ArtworkOrientation.UNKNOWN: (None, None),
    }
    artworks = [
        _work(index, orientation=features_from_dimensions(*dimensions[orientation]))
        for index, orientation in enumerate(reversed(orientations), 1)
    ]
    cover = _work(99, orientation=features_from_dimensions(1000, 1000))

    assert _facts(artworks).distinct_orientation_count == expected
    assert cover not in artworks


@pytest.mark.parametrize(
    "artworks",
    [
        [_work(1, museum="One Museum"), _work(2, museum="One Museum")],
        [_work(1, museum="Museum A"), _work(2, museum="Museum B")],
        [_work(1, artist="One Artist", museum="One Museum"),
         _work(2, artist="One Artist", museum="One Museum")],
        [_work(1, artist="Unknown Artist", date="Date unknown", museum="Unknown Museum")],
    ],
)
def test_fallback_intro_is_publishable_across_fact_availability(artworks):
    intro = fallback_carousel_intro(_facts(artworks))

    assert intro.endswith(".")
    assert len(intro.split(". ")) <= 2
    assert not any(
        phrase in intro.casefold()
        for phrase in (
            "source metadata",
            "source records",
            "selected order",
            "grounded comparison",
            "preserves each",
        )
    )


def test_theme_display_title_never_leaks_internal_id():
    assert display_theme_title("impressionist_light", " impressionist_light ") == (
        "Impressionist Light"
    )


@pytest.mark.parametrize("featured_count", range(5, 9))
def test_grounded_copy_supports_every_adaptive_featured_count(featured_count):
    artworks = [
        _work(index, museum="One Museum")
        for index in range(1, featured_count + 1)
    ]
    facts = _facts(artworks)
    copy = " ".join(
        (
            fallback_editorial_subtitle(facts),
            *derive_cover_micro_facts(facts),
            fallback_carousel_intro(facts),
        )
    )

    assert facts.total_slide_count == featured_count + 1
    assert f"{featured_count} works" in copy
    assert f"{featured_count + 1} works" not in copy
