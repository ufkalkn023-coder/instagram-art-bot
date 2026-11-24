from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.carousel_delivery import (
    build_per_slide_alt_texts,
    evaluate_multi_work_connection,
    plan_final_carousel_slides,
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

    # 1C) Documented seasonal pendant (Jan van Goyen Summer/Winter 1625 Rijksmuseum)
    doc_goyen = evaluate_multi_work_connection(
        [
            {"title": "Summer", "artist": "Jan van Goyen (signed by artist)", "date": "1625", "institution": "Rijksmuseum, Amsterdam", "image_path": str(p_light)},
            {"title": "Winter", "artist": "Jan van Goyen (signed by artist)", "date": "1625", "institution": "Rijksmuseum, Amsterdam", "image_path": str(p_dark)},
        ]
    )
    assert doc_goyen["strong"] is True
    assert doc_goyen["tier"] == "documented_pair_or_series"
    assert doc_goyen["relation_source_field"] == "title+artist+date+museum:companion_pendant"

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
