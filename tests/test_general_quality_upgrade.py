from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from src.curatorial_brief import (
    build_curatorial_brief,
    clean_artist_record,
    clean_editorial_title,
    derive_museum_url,
    is_exact_year_date,
)
from src.preflight_and_feedback import (
    EditorialFeedbackStore,
    InsightsMediaLinkRegistry,
    run_preflight_checks,
)


def _img(path: Path, size: tuple[int, int] = (1200, 1500)) -> Path:
    im = Image.new("RGB", size, (36, 48, 66))
    d = ImageDraw.Draw(im)
    d.ellipse((240, 260, 960, 1080), fill=(212, 168, 96))
    im.save(path, format="JPEG", quality=92)
    return path


def test_clean_artist_urls_titles_and_exact_year_guard() -> None:
    a1 = clean_artist_record("Abraham Mignon (mentioned on object)")
    assert a1["surname"] == "Mignon"
    assert "object)" not in a1["surname"]
    assert a1["attribution_note"] == "mentioned on object"

    a2 = clean_artist_record("Banavari 1")
    assert a2["short_name"] == "Banavari"
    assert a2["surname"] == "Banavari"

    a3 = clean_artist_record("anonymous")
    assert a3["is_anonymous"] is True
    assert a3["display"] == "Unknown artist"

    assert derive_museum_url("met_438001", "") == "https://www.metmuseum.org/art/collection/search/438001"
    assert derive_museum_url("aic_81557", "") == "https://www.artic.edu/artworks/81557"
    assert derive_museum_url("rijksmuseum_SK-A-2727", "") == "https://www.rijksmuseum.nl/en/collection/SK-A-2727"

    assert is_exact_year_date("1873") is True
    assert is_exact_year_date("c. 1635 - c. 1665") is False
    assert is_exact_year_date("") is False

    cleaned = clean_editorial_title('Still Life with Flowers and Prickly Pears', max_len=50)
    assert cleaned == "Still Life with Flowers and Prickly Pears"


def test_curatorial_brief_distinct_roles_and_zero_pigment_claim(tmp_path: Path) -> None:
    p = _img(tmp_path / "work.jpg", (1500, 1000))
    aw = {
        "canonical_id": "met_438001",
        "source_key": "met_438001",
        "artist": "Paul Gauguin",
        "title": "Two Women",
        "date": "1901 or 1902",
        "institution": "The Metropolitan Museum of Art",
        "museum_url": "",
        "rights_basis": "CONFIRMED_PUBLIC_DOMAIN",
        "image_path": str(p),
    }
    brief = build_curatorial_brief([aw], post_format="reel", slot_index=5, revisits_post_id="post-001")
    assert brief["curatorial_angle"] == "revisit_motion_discovery"
    assert "pigment" not in brief["caption_en"].lower()
    assert brief["caption_en"].splitlines()[0] != brief["reel_subtitle_en"]
    assert brief["alt_text_en"] != brief["caption_en"]
    assert "https://www.metmuseum.org/art/collection/search/438001" in brief["first_comment_en"]
    assert len(brief["reel_subtitle_en"]) <= 68


def test_preflight_checks_feedback_store_and_insights_registry(tmp_path: Path) -> None:
    _img(tmp_path / "slide1.jpg")
    bad_post = {
        "id": "post-001",
        "post_format": "carousel",
        "design_id": "neon-151",
        "editorial_title": "Inside The Blauwpoort in the",
        "caption_en": "Watch how object) builds pigments from shadow.",
        "alt_text_en": "Short",
        "slides": ["slide1.jpg"],
        "artworks": [{"canonical_id": "x1", "artist": "Mignon (mentioned on object)", "date": "c. 1650", "museum_url": ""}],
    }
    report = run_preflight_checks([bad_post], tmp_path)
    codes = {d["code"] for d in report["defects"]}
    assert report["passed"] is False
    assert {"broken_artist_name", "missing_museum_url", "truncated_or_broken_title", "incomplete_alt_text", "ungrounded_pigment_claim", "uncertain_date_quiz"} <= codes

    # Feedback store bounded nudges
    fb = EditorialFeedbackStore(tmp_path / "fb.json")
    fb.record(post_id="post-001", tag="crop_bad", artwork_ids=["x1"], design_id="neon-060", design_family="gallery")
    fb.record(post_id="post-002", tag="approved", artwork_ids=["x2"], design_id="astra-041", design_family="archive")
    nudges = fb.soft_design_nudges()
    assert -1.5 <= nudges["neon-060"] < 0
    assert 0 < nudges["astra-041"] <= 1.5

    # Insights link registry
    reg = InsightsMediaLinkRegistry(tmp_path / "links.json")
    reg.register_post({"id": "post-001", "post_format": "carousel", "design_id": "astra-041", "family": "archive", "curatorial_angle": "composition_whole", "copy_version": "artfolio-voice-v2", "artworks": [{"canonical_id": "x1"}]})
    reg.attach_instagram_media_id("post-001", "18128542507862787")
    reloaded = InsightsMediaLinkRegistry(tmp_path / "links.json")
    assert reloaded.links["post-001"]["instagram_media_id"] == "18128542507862787"
