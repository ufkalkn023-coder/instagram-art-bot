from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.carousel_delivery import (
    _compose_strong_multi_work_caption,
    _find_strong_partner_group,
    build_per_slide_alt_texts,
    evaluate_multi_work_connection,
    plan_final_carousel_slides,
)
from src.curatorial_brief import (
    _extract_event_date_from_text,
    compose_multi_work_headline,
    decide_slide_two_intent,
)
from src.models import NormalizedArtwork
from src.monthly_planner import resolve_month_schedule_window
from src.museum_pool import (
    ArtworkReuseLedger,
    calculate_carousel_only_artwork_need,
    ensure_carousel_pool_supply,
)
from src.profile_rehearsal import build_real_museum_rehearsal


def _img(path: Path, size: tuple[int, int], bg: tuple[int, int, int]) -> Path:
    im = Image.new("RGB", size, bg)
    ImageDraw.Draw(im).rectangle((size[0] // 5, size[1] // 5, size[0] * 4 // 5, size[1] * 4 // 5), fill=(180, 150, 110))
    im.save(path, format="JPEG", quality=90)
    return path


def test_documented_pair_strictly_separated_from_editorial_same_artist_and_contrast(tmp_path: Path) -> None:
    p_dark = _img(tmp_path / "dark.jpg", (1200, 900), (18, 22, 28))
    p_light = _img(tmp_path / "light.jpg", (1200, 900), (232, 226, 214))

    # 1A) Documented pair via metadata.pair_group
    doc_meta = evaluate_multi_work_connection(
        [
            {"title": "Sketch for The Storm", "artist": "Master A", "date": "1820", "pair_group": "storm_pair", "image_path": str(p_dark)},
            {"title": "The Storm", "artist": "Master A", "date": "1822", "pair_group": "storm_pair", "image_path": str(p_light)},
        ]
    )
    assert doc_meta["strong"] is True
    assert doc_meta["tier"] == "documented_pair_or_series"
    assert doc_meta["relation_source_field"] == "metadata.pair_group"
    assert "storm_pair" in doc_meta["relation_evidence"]

    # 1A-bis) Documented pair via metadata.companion_catalog_ids
    doc_comp = evaluate_multi_work_connection(
        [
            {"canonical_id": "cma_10", "title": "River Morning", "artist": "Master B", "date": "1830", "companion_catalog_ids": ["cma_11"], "image_path": str(p_light)},
            {"canonical_id": "cma_11", "title": "River Dusk", "artist": "Master B", "date": "1830", "image_path": str(p_dark)},
        ]
    )
    assert doc_comp["strong"] is True
    assert doc_comp["tier"] == "documented_pair_or_series"
    assert doc_comp["relation_source_field"] == "metadata.companion_catalog_ids"
    assert "cma_11" in doc_comp["relation_evidence"]

    # 1B) Documented pair via '(one of a pair)' museum title (like Unkoku Toyo cleveland_86112/86113)
    doc_title = evaluate_multi_work_connection(
        [
            {"title": "Landscapes (one of a pair)", "artist": "Unkoku Tōyo", "institution": "Cleveland Museum of Art", "date": "1650", "image_path": str(p_dark)},
            {"title": "Landscapes (one of a pair)", "artist": "Unkoku Tōyo", "institution": "Cleveland Museum of Art", "date": "1650", "image_path": str(p_light)},
        ]
    )
    assert doc_title["strong"] is True
    assert doc_title["tier"] == "documented_pair_or_series"
    assert doc_title["relation_source_field"] == "title:one_of_a_pair"
    assert doc_title["relation_evidence"] != ""

    # 1C) Year+artist+title pendant WITHOUT explicit catalog link -> MUST be editorial_same_artist (not documented_pair_or_series)
    ed_goyen = evaluate_multi_work_connection(
        [
            {"title": "Summer", "artist": "Jan van Goyen (signed by artist)", "date": "1625", "institution": "Rijksmuseum, Amsterdam", "image_path": str(p_light)},
            {"title": "Winter", "artist": "Jan van Goyen (signed by artist)", "date": "1625", "institution": "Rijksmuseum, Amsterdam", "image_path": str(p_dark)},
        ]
    )
    assert ed_goyen["strong"] is True
    assert ed_goyen["tier"] == "editorial_same_artist"
    assert ed_goyen["relation_source_field"] == ""
    assert ed_goyen["relation_evidence"] == ""
    assert ed_goyen["candidate_discovery_hint"] == "year_artist_title_pendant_candidate"
    assert ed_goyen["editorial_bond_en"] != ""

    # 2) Editorial same-artist pairing (NOT a documented pair -> records editorial_bond_en, empty relation_source_field!)
    ed_same = evaluate_multi_work_connection(
        [
            {"title": "Ruins in Rome", "artist": "Hubert Robert", "date": "1765", "institution": "Cleveland Museum of Art", "image_path": str(p_dark)},
            {"title": "The Old Bridge", "artist": "Hubert Robert", "date": "1782", "institution": "Rijksmuseum, Amsterdam", "image_path": str(p_light)},
        ]
    )
    assert ed_same["strong"] is True
    assert ed_same["tier"] == "editorial_same_artist"
    assert ed_same["relation_source_field"] == ""
    assert ed_same["editorial_bond_en"] != ""

    # 3) Editorial cross-artist visual contrast
    ed_contrast = evaluate_multi_work_connection(
        [
            {"title": "Winter in the Woods", "artist": "Hendrick Avercamp", "date": "1650", "image_path": str(p_dark)},
            {"title": "Frozen River in Winter", "artist": "Claude Monet", "date": "1875", "image_path": str(p_dark)},
        ]
    )
    assert ed_contrast["strong"] is True
    assert ed_contrast["tier"] == "editorial_visual_contrast"
    assert ed_contrast["relation_source_field"] == ""
    assert ed_contrast["editorial_bond_en"] != ""

    # 4) Reject plain similar landscapes without concrete contrast
    plain = evaluate_multi_work_connection(
        [
            {"title": "Landscape with Road", "artist": "Jean-Baptiste Corot", "date": "1860", "image_path": str(p_dark)},
            {"title": "Landscape with Trees", "artist": "Théodore Rousseau", "date": "1865", "image_path": str(p_dark)},
        ]
    )
    assert plain["strong"] is False


def test_finalized_slide_plan_and_per_slide_alt_texts() -> None:
    art = {
        "artist_short": "Claude Monet",
        "artist_surname": "Monet",
        "title_clean": "Waterloo Bridge, Gray Weather",
        "date": "1900",
        "institution": "Art Institute of Chicago",
    }
    plan_3 = plan_final_carousel_slides(post_id="post-001", design_id="astra-001", design_family="gallery", arts=[art])
    assert len(plan_3) == 3
    assert [x["page_sig"] for x in plan_3] == ["01 / 03", "02 / 03", "03 / 03"]

    alts = build_per_slide_alt_texts(
        {"design_id": "astra-001", "tone_band": "mid", "focus_region": "the center", "artworks": [art]},
        plan_3,
    )
    assert len(alts) == 3
    assert alts[2]["page_sig"] == "03 / 03"


def test_consecutive_two_months_from_same_pool_have_zero_unintended_repeats(tmp_path: Path) -> None:
    pool = []
    for i in range(1, 25):
        p = _img(tmp_path / f"w{i:02d}.jpg", (1200, 1000), (20 + i * 8, 50, 90))
        pool.append(
            {
                "canonical_id": f"cleveland_{i:04d}",
                "source_key": f"cleveland_{i:04d}",
                "artist": f"Painter {chr(65 + (i % 20))}",
                "artist_normalized": f"painter {chr(97 + (i % 20))}",
                "title": f"View of Harbor {i}",
                "date": str(1650 + i * 8),
                "institution": "Cleveland Museum of Art",
                "museum_url": f"https://www.clevelandart.org/art/{i:04d}",
                "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
                "rights_basis": "CC0",
                "image_path": str(p),
            }
        )

    ledger = tmp_path / "reuse_ledger.json"
    m1 = build_real_museum_rehearsal(pool, tmp_path / "month1", days=1, carousels_per_day=4, reels_per_day=2, render_sample_mp4_count=0, reuse_ledger_path=ledger)
    m2 = build_real_museum_rehearsal(pool, tmp_path / "month2", days=1, carousels_per_day=4, reels_per_day=2, render_sample_mp4_count=0, reuse_ledger_path=ledger)

    import json
    m1_ids = {a["canonical_id"] for p in json.loads(Path(m1["manifest_path"]).read_text())["posts"] for a in p["artworks"]}
    m2_ids = {a["canonical_id"] for p in json.loads(Path(m2["manifest_path"]).read_text())["posts"] for a in p["artworks"]}
    assert len(m1_ids) >= 6
    assert len(m2_ids) >= 6
    assert m1_ids.intersection(m2_ids) == set()
    assert m2["summary"]["blocked_by_reuse_ledger"] == len(m1_ids)


def test_carousel_only_artwork_need_separated_from_reels() -> None:
    need = calculate_carousel_only_artwork_need(days=30, carousels_per_day=4, reels_per_day=2, fallback_single_count=7)
    assert need["total_carousels"] == 120
    assert need["carousel_gross_artwork_slots"] == 160
    assert need["carousel_net_unique_needed"] == 153
    assert need["reel_preview_consumption"]["total_reels"] == 60
    assert need["reel_preview_consumption"]["excluded_from_carousel_shortfall"] is True


def test_reuse_ledger_lifecycle_preview_reopen_and_replace(tmp_path: Path) -> None:
    import json

    ledger_path = tmp_path / "reuse_ledger.json"
    # Legacy v1 string timestamp compatibility
    ledger_path.write_text(
        json.dumps({"schema_version": "artfolio-reuse-ledger-v1", "used_artworks": {"legacy_1": "2026-11-20T10:00:00+00:00"}}),
        encoding="utf-8",
    )
    ledger = ArtworkReuseLedger(ledger_path)
    assert ledger.is_blocked("legacy_1", current_plan_id="2026-12") is True

    # Preview never blocks other months
    ledger.mark_used(["prev_1"], plan_id="2026-11", status="preview")
    assert ledger.is_blocked("prev_1", current_plan_id="2026-12") is False

    # Reserved blocks OTHER plan_ids, NEVER self-blocks the SAME plan_id
    ledger.mark_used(["cma_1", "cma_2"], plan_id="2026-11", status="reserved", replace_plan=True)
    assert ledger.is_blocked("cma_1", current_plan_id="2026-11") is False
    assert ledger.is_blocked("cma_1", current_plan_id="2026-12") is True

    # replace_plan=True releases dropped works when fixing the same month
    ledger.mark_used(["cma_1", "cma_3"], plan_id="2026-11", status="reserved", replace_plan=True)
    assert ledger.is_blocked("cma_2", current_plan_id="2026-12") is False
    assert ledger.is_blocked("cma_3", current_plan_id="2026-12") is True


def test_ensure_carousel_pool_supply_fault_tolerant_and_persists(tmp_path: Path, monkeypatch) -> None:
    import json

    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    existing_img = _img(tmp_path / "images" / "cma_1.jpg", (900, 900), (60, 70, 80))
    new_img = _img(tmp_path / "src_new.jpg", (1000, 800), (140, 120, 95))
    pool_path = tmp_path / "pool_index.json"
    pool_path.write_text(
        json.dumps(
            {
                "artworks": [
                    {
                        "canonical_id": "cleveland_1",
                        "source_key": "cleveland_1",
                        "source": "cleveland",
                        "artist": "Unkoku Toyo",
                        "artist_normalized": "unkoku toyo",
                        "title": "Landscapes (one of a pair)",
                        "date": "1650",
                        "institution": "Cleveland Museum of Art",
                        "museum_url": "https://www.clevelandart.org/art/1",
                        "rights_status": "CONFIRMED_PUBLIC_DOMAIN",
                        "image_path": str(existing_img),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class _FailingAdapter:
        def fetch_candidates(self, **kwargs):
            raise RuntimeError("503 upstream down")

    class _WorkingAdapter:
        def fetch_candidates(self, *, limit=10, query=None, rng=None, verification_id=None):
            return [
                NormalizedArtwork(
                    source="cleveland",
                    source_id="2",
                    title="Landscapes (one of a pair)",
                    artist_name="Unkoku Toyo",
                    creation_date="1650",
                    medium="Ink on paper",
                    museum_name="Cleveland Museum of Art",
                    artwork_url="https://www.clevelandart.org/art/2",
                    image_url="https://example.org/2.jpg",
                    rights_status="CONFIRMED_PUBLIC_DOMAIN",
                ),
                NormalizedArtwork(
                    source="cleveland",
                    source_id="3",
                    title="Restricted Work",
                    artist_name="Modern Painter",
                    creation_date="1950",
                    medium="Oil",
                    museum_name="Cleveland Museum of Art",
                    artwork_url="https://www.clevelandart.org/art/3",
                    image_url="https://example.org/3.jpg",
                    rights_status="COPYRIGHT_RESTRICTED",
                ),
            ]

    def _fake_download(url: str, dest: Path, timeout: int = 20):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(new_img.read_bytes())
        return True, 1000, 800

    monkeypatch.setattr("src.museum_pool._download_verified_image", _fake_download)
    report = ensure_carousel_pool_supply(
        pool_path,
        needed_fresh_artworks=2,
        adapters=[("rijks_down", _FailingAdapter(), 10, 2), ("cleveland", _WorkingAdapter(), 10, 2)],
    )
    assert report["auto_fetch_triggered"] is True
    assert report["added_total"] == 1
    assert report["companion_candidates_added"] == 1
    assert report["rejected_counts"]["rights_unverified"] >= 1
    assert len(report["failed_adapters"]) >= 1
    assert report["remaining_shortfall"] == 0
    saved = json.loads(pool_path.read_text(encoding="utf-8"))
    assert len(saved["artworks"]) == 2


def test_month_calendar_resolution_february_leap_and_collisions(tmp_path: Path) -> None:
    dec_start, dec_days = resolve_month_schedule_window("2026-12")
    assert dec_start.strftime("%Y-%m-%d %H:%M") == "2026-12-01 09:30"
    assert dec_days == 31

    feb_norm_start, feb_norm_days = resolve_month_schedule_window("2027-02")
    assert feb_norm_start.strftime("%Y-%m-%d") == "2027-02-01"
    assert feb_norm_days == 28

    feb_leap_start, feb_leap_days = resolve_month_schedule_window("2028-02")
    assert feb_leap_start.strftime("%Y-%m-%d") == "2028-02-01"
    assert feb_leap_days == 29

    short_start, short_days = resolve_month_schedule_window("2026-12", explicit_days=5)
    assert short_start.strftime("%Y-%m-%d") == "2026-12-01"
    assert short_days == 5

    ledger = ArtworkReuseLedger(tmp_path / "ledger.json")
    ledger.mark_used(
        ["a1"],
        plan_id="2026-11",
        status="reserved",
        schedule_start="2026-11-24",
        schedule_end="2026-12-23",
    )
    cols = ledger.find_schedule_collisions("2026-12-01", "2026-12-31", exclude_plan_id="2026-12")
    assert len(cols) == 1
    assert cols[0]["existing_plan_id"] == "2026-11"
    assert cols[0]["overlap_start"] == "2026-12-01"
    assert cols[0]["overlap_end"] == "2026-12-23"


def test_general_outer_wings_and_half_siblings_relations_and_headlines(tmp_path: Path) -> None:
    p1 = _img(tmp_path / "p1.jpg", (1200, 1400), (60, 50, 40))
    p2 = _img(tmp_path / "p2.jpg", (1200, 1400), (70, 55, 45))

    wings = [
        {
            "canonical_id": "rijks_a",
            "artist": "Master of the Saint Elizabeth Panels",
            "artist_short": "Master of the Saint Elizabeth Panels",
            "artist_surname": "Panels",
            "title": "The Saint Elizabeth’s Day Flood",
            "title_clean": "The Saint Elizabeth’s Day Flood",
            "date": "c. 1490 - c. 1495",
            "medium": "oil on panel",
            "institution": "Rijksmuseum, Amsterdam",
            "description": "On the night of 19 November 1421 much of Holland was flooded. The outer panels depict the disaster.",
            "image_path": str(p1),
        },
        {
            "canonical_id": "rijks_b",
            "artist": "Master of the Saint Elizabeth Panels",
            "artist_short": "Master of the Saint Elizabeth Panels",
            "artist_surname": "Panels",
            "title": "The Saint Elizabeth’s Day Flood",
            "title_clean": "The Saint Elizabeth’s Day Flood",
            "date": "c. 1490 - c. 1495",
            "medium": "oil on panel",
            "institution": "Rijksmuseum, Amsterdam",
            "description": "On the night of 19 November 1421 much of Holland was flooded. The outer panels depict the disaster.",
            "image_path": str(p2),
        },
    ]
    conn_w = evaluate_multi_work_connection(wings)
    assert conn_w["tier"] == "documented_pair_or_series"
    assert conn_w["relation_source_field"] == "metadata.museum_description"
    assert "outer panels" in conn_w["relation_evidence"].lower()
    headline_w = compose_multi_work_headline(wings, conn_w)
    assert "Panels / Panels" not in headline_w
    assert "Outer Wings" in headline_w
    cap_w = _compose_strong_multi_work_caption(wings, conn_w, 1, 4)
    assert "panel" in cap_w.lower()
    assert "canvases" not in cap_w.lower()
    assert _extract_event_date_from_text("", wings[0]["description"], wings[0]["date"]) == "19 November 1421"

    siblings = [
        {
            "canonical_id": "rijks_s1",
            "artist": "Willem Jansz. Ploy, attributed",
            "artist_short": "Willem Jansz. Ploy",
            "artist_surname": "Ploy",
            "title": "Portrait of Johannes van Rees (1662-1690/91), Half Brother of Elisabeth van Oosten, as a Child",
            "title_clean": "Portrait of Johannes van Rees",
            "date": "1663",
            "medium": "oil on canvas",
            "institution": "Rijksmuseum, Amsterdam",
            "image_path": str(p1),
        },
        {
            "canonical_id": "rijks_s2",
            "artist": "Willem Jansz. Ploy, attributed",
            "artist_short": "Willem Jansz. Ploy",
            "artist_surname": "Ploy",
            "title": "Portrait of Elisabeth van Oosten (1660-1714), as a Child",
            "title_clean": "Portrait of Elisabeth van Oosten",
            "date": "1663",
            "medium": "oil on canvas",
            "institution": "Rijksmuseum, Amsterdam",
            "image_path": str(p2),
        },
    ]
    conn_s = evaluate_multi_work_connection(siblings)
    assert conn_s["tier"] == "documented_pair_or_series"
    assert conn_s["relation_source_field"] == "title_or_description:sitter_relation"
    assert compose_multi_work_headline(siblings, conn_s) == "Half-Siblings, Painted in 1663"


def test_zero_within_month_partner_borrowing_and_slide2_truth(tmp_path: Path) -> None:
    from collections import Counter

    p1 = _img(tmp_path / "a1.jpg", (1800, 1400), (30, 40, 50))
    p2 = _img(tmp_path / "a2.jpg", (1800, 1400), (210, 200, 185))
    lead = {
        "canonical_id": "c_lead",
        "artist": "Master A",
        "artist_short": "Master A",
        "artist_surname": "Master A",
        "is_anonymous": False,
        "title": "Winter Forest",
        "title_clean": "Winter Forest",
        "date": "1650",
        "institution": "Museum X",
        "image_path": str(p1),
    }
    reserved_partner = {
        "canonical_id": "c_reserved",
        "artist": "Master B",
        "artist_short": "Master B",
        "artist_surname": "Master B",
        "is_anonymous": False,
        "title": "Winter River",
        "title_clean": "Winter River",
        "date": "1880",
        "institution": "Museum Y",
        "image_path": str(p2),
    }
    # Excluded IDs prevent borrowing an artwork already used in another Carousel this month
    assert _find_strong_partner_group(lead, 2, [lead, reserved_partner], Counter(), excluded_ids={"c_reserved"}) is None

    # Close-up cover (experimental/detail) -> Slide 2 opens full_plate; color card omitted when use_color_card=False
    s2_closeup = decide_slide_two_intent([lead], focus=(0.45, 0.52), width=1800, height=1400, cover_shows_full=False, use_color_card=False)
    assert s2_closeup["spec"] == "full_plate"
    plan = plan_final_carousel_slides(
        post_id="post-x",
        design_id="astra-092",
        design_family="experimental",
        arts=[lead],
        slide_two_intent=s2_closeup,
        use_color_card=False,
    )
    assert [x["spec"] for x in plan] == ["cover", "full_plate", "credit_plate"]
    alts = build_per_slide_alt_texts({"design_id": "astra-092", "tone_band": "dark", "artworks": [lead]}, plan)
    assert "Uncropped" in alts[1]["alt_text_en"]
    assert "Museum Record" in alts[2]["alt_text_en"]
