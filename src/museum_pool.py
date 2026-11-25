"""Museum artwork pool management: diversity quotas, century/region balance & 180-day reuse blacklist."""

from __future__ import annotations

import json
import random
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests
from PIL import Image

from src.artwork_intelligence import validate_artwork_resolution
from src.neon_kit import parse_year

REUSE_COOLDOWN_DAYS = 180
VERIFIED_RIGHTS_STATUSES = frozenset({"CONFIRMED_PUBLIC_DOMAIN", "CONFIRMED_OPEN_ACCESS"})
VALID_LEDGER_STATUSES = frozenset({"preview", "reserved", "published"})

DEFAULT_MUSEUM_QUERIES: tuple[str, ...] = (
    "landscape",
    "portrait",
    "still life",
    "sea",
    "flowers",
    "interior",
    "winter",
    "night",
    "river",
    "garden",
    "mythology",
    "storm",
    "harbor",
    "forest",
    "woman",
    "city",
    "autumn",
    "mountain",
)

AUTO_SUPPLY_MUSEUM_QUERIES: tuple[str, ...] = DEFAULT_MUSEUM_QUERIES + (
    "bridge",
    "church",
    "village",
    "boat",
    "music",
    "reading",
    "horse",
    "snow",
    "moon",
    "sunset",
    "street",
    "market",
    "coast",
    "valley",
    "ruins",
    "meadow",
    "canal",
    "farm",
)


def normalize_artist_name(name: str) -> str:
    """Accent- and case-insensitive artist normalizer (e.g. 'Paul Cézanne' == 'Paul Cezanne')."""
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", ascii_only.lower())).strip() or "unknown"


def compute_image_dhash(image_path: str | Path, hash_size: int = 8) -> str:
    """Compute a 64-bit perceptual difference hash (dHash) using Pillow only."""
    with Image.open(image_path) as im:
        gray = im.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        pixels = list(gray.getdata())
    bits = []
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            bits.append("1" if pixels[row_start + col] > pixels[row_start + col + 1] else "0")
    return f"{int(''.join(bits), 2):0{hash_size * hash_size // 4}x}"


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """Bitwise Hamming distance between two hex perceptual hashes."""
    if not hex_a or not hex_b or len(hex_a) != len(hex_b):
        return 64
    return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()


def flag_visual_near_duplicates(artworks: Sequence[dict], *, max_distance: int = 8) -> list[dict]:
    """Identify visually similar pairs across distinct records for human review (never auto-deletes)."""
    flags: list[dict] = []
    items = [a for a in artworks if a.get("dhash")]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a.get("canonical_id") == b.get("canonical_id"):
                continue
            dist = hamming_distance(a["dhash"], b["dhash"])
            if dist <= max_distance:
                flags.append(
                    {
                        "left_id": a.get("canonical_id") or a.get("source_key"),
                        "left_title": a.get("title", ""),
                        "right_id": b.get("canonical_id") or b.get("source_key"),
                        "right_title": b.get("title", ""),
                        "dhash_distance": dist,
                        "action": "flagged_for_review_kept_both",
                    }
                )
    return flags


def calculate_schedule_artwork_need(
    *,
    days: int = 30,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
    reel_revisit_ratio: float = 0.20,
) -> dict:
    """Compute exact unique artwork requirement from the actual Carousel & Reel multi-source structure."""
    total_carousels = days * carousels_per_day
    total_reels = days * reels_per_day
    # Multi-source cadence: 1 in 6 carousels is 2-work duet, 1 in 12 is 3-work gallery (so 2/3-work don't spam)
    c_two = total_carousels // 6
    c_three = total_carousels // 12
    c_one = total_carousels - c_two - c_three
    carousel_artwork_slots = c_one * 1 + c_two * 2 + c_three * 3
    reels_revisiting_earlier = int(round(total_reels * reel_revisit_ratio))
    reels_fresh = total_reels - reels_revisiting_earlier
    total_unique_needed = carousel_artwork_slots + reels_fresh
    return {
        "days": days,
        "total_posts": total_carousels + total_reels,
        "total_carousels": total_carousels,
        "carousels_1_work": c_one,
        "carousels_2_works": c_two,
        "carousels_3_works": c_three,
        "carousel_artwork_slots": carousel_artwork_slots,
        "total_reels": total_reels,
        "reels_fresh_works": reels_fresh,
        "reels_revisiting_carousel_works": reels_revisiting_earlier,
        "total_unique_artworks_needed": total_unique_needed,
    }


def calculate_carousel_only_artwork_need(
    *,
    days: int = 30,
    carousels_per_day: int = 4,
    reels_per_day: int = 2,
    fallback_single_count: int = 0,
    reel_revisit_ratio: float = 0.20,
) -> dict:
    """Compute unique artwork need strictly for Carousels, keeping Reel preview consumption separate.

    Never derives Carousel shortfall from the mixed 208-artwork (Carousel + Reel) total.
    """
    total_carousels = days * carousels_per_day
    c_two = total_carousels // 6
    c_three = total_carousels // 12
    c_one = total_carousels - c_two - c_three
    carousel_gross_slots = c_one * 1 + c_two * 2 + c_three * 3
    saved_by_fallback = max(0, int(fallback_single_count))
    carousel_net_unique = max(total_carousels, carousel_gross_slots - saved_by_fallback)

    total_reels = days * reels_per_day
    reels_revisiting = int(round(total_reels * reel_revisit_ratio))
    reels_fresh_if_available = max(0, total_reels - reels_revisiting)
    return {
        "days": days,
        "total_carousels": total_carousels,
        "carousels_1_work": c_one,
        "carousels_2_works": c_two,
        "carousels_3_works": c_three,
        "carousel_gross_artwork_slots": carousel_gross_slots,
        "fallback_single_saved_slots": saved_by_fallback,
        "carousel_net_unique_needed": carousel_net_unique,
        "reel_preview_consumption": {
            "total_reels": total_reels,
            "reels_fresh_if_available": reels_fresh_if_available,
            "reels_revisiting_carousels": reels_revisiting,
            "excluded_from_carousel_shortfall": True,
        },
    }


def _extract_companion_queries(artworks: Sequence[dict], existing_ids: set[str]) -> list[tuple[str, str]]:
    """Find museum queries for artworks whose documented companion may be missing from the pool."""
    queries: list[tuple[str, str]] = []
    seen_q: set[tuple[str, str]] = set()
    for aw in artworks:
        src = (aw.get("source") or "").strip().lower()
        if not src:
            continue
        title = (aw.get("title") or "").strip()
        artist = (aw.get("artist") or "").split("(")[0].strip()
        # Check explicit companion_catalog_ids missing from pool
        comp_ids = aw.get("companion_catalog_ids") or []
        has_missing_comp_id = any(str(cid) not in existing_ids for cid in comp_ids if cid)
        has_pair_title = bool(
            re.search(r"\(\s*one of a pair\s*\)", title, re.IGNORECASE)
            or re.search(r"(?:sketch|study)\s+for\s+", title, re.IGNORECASE)
            or re.search(r"from (?:an?|the)\s+", title, re.IGNORECASE)
        )
        if not (has_missing_comp_id or has_pair_title or aw.get("pair_group") or aw.get("series_id")):
            continue
        base_title = re.sub(r"\([^)]*\)", "", title).strip()
        m_study = re.search(r"(?:sketch|study)\s+for\s+[\"'“]?([^\"'”.,;]{4,40})", base_title, re.IGNORECASE)
        q_text = m_study.group(1).strip() if m_study else (f"{artist} {base_title[:28]}".strip() if artist and artist.lower() != "unknown artist" else base_title[:36])
        if len(q_text) >= 4 and (src, q_text) not in seen_q:
            seen_q.add((src, q_text))
            queries.append((src, q_text))
    return queries[:8]


def ensure_carousel_pool_supply(
    pool_index_path: str | Path,
    *,
    needed_fresh_artworks: int,
    reuse_ledger: "ArtworkReuseLedger | None" = None,
    current_plan_id: str | None = None,
    cooldown_days: int = REUSE_COOLDOWN_DAYS,
    max_per_artist: int = 3,
    download_workers: int = 10,
    adapters: Sequence[tuple] | None = None,
    seed: int | None = None,
) -> dict:
    """Automatically top up pool_index.json from working museum adapters when eligible works < needed_fresh_artworks.

    Phase A prioritizes missing documented companions for artworks already in the pool.
    Phase B fetches diversified fresh artworks across working museum adapters.
    """
    index_path = Path(pool_index_path).resolve()
    root = index_path.parent
    img_dir = root / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    raw_payload = {}
    cached_by_id: dict[str, dict] = {}
    if index_path.is_file():
        try:
            raw_payload = json.loads(index_path.read_text(encoding="utf-8"))
            for item in raw_payload.get("artworks", []):
                p = Path(item.get("image_path", ""))
                if p.is_file() and item.get("rights_status") in VERIFIED_RIGHTS_STATUSES:
                    cached_by_id[item["canonical_id"]] = item
        except Exception:
            cached_by_id = {}

    def _count_eligible(items_by_id: dict[str, dict]) -> int:
        if reuse_ledger is None:
            return len(items_by_id)
        return sum(
            1
            for cid in items_by_id
            if not reuse_ledger.is_blocked(cid, cooldown_days=cooldown_days, current_plan_id=current_plan_id)
        )

    eligible_before = _count_eligible(cached_by_id)
    if eligible_before >= needed_fresh_artworks:
        return {
            "pool_index_path": str(index_path),
            "eligible_before": eligible_before,
            "needed_fresh_artworks": needed_fresh_artworks,
            "auto_fetch_triggered": False,
            "added_total": 0,
            "added_by_museum": {},
            "companion_candidates_added": 0,
            "rejected_total": 0,
            "rejected_counts": {},
            "failed_adapters": [],
            "eligible_after": eligible_before,
            "remaining_shortfall": 0,
            "total_pool_size": len(cached_by_id),
        }

    if adapters is None:
        from src.museums.cleveland import ClevelandAdapter
        from src.museums.met import MetAdapter
        from src.museums.rijksmuseum import RijksmuseumAdapter

        adapter_specs = [
            ("cleveland", ClevelandAdapter(), 35, len(AUTO_SUPPLY_MUSEUM_QUERIES)),
            ("rijksmuseum", RijksmuseumAdapter(), 15, len(AUTO_SUPPLY_MUSEUM_QUERIES)),
            ("met", MetAdapter(), 6, 4),
        ]
    else:
        adapter_specs = [
            (spec[0], spec[1], spec[2], spec[3] if len(spec) > 3 else len(AUTO_SUPPLY_MUSEUM_QUERIES))
            for spec in adapters
        ]

    if seed is None:
        plan_tag = current_plan_id or "default"
        seed = 20261124 + sum(ord(ch) * (i + 1) for i, ch in enumerate(plan_tag))
    rng = random.Random(seed)

    artist_counts = Counter(normalize_artist_name(a.get("artist", "")) for a in cached_by_id.values())
    existing_dhashes = {a.get("dhash") for a in cached_by_id.values() if a.get("dhash")}
    seen_ids = set(cached_by_id.keys())
    rejected_counts: Counter[str] = Counter()
    failed_adapters: list[dict] = []
    consecutive_failures: Counter[str] = Counter()
    added_by_museum: Counter[str] = Counter()
    companion_added = 0

    def _collect_candidate(aw, is_companion: bool = False) -> dict | None:
        if aw.canonical_id in seen_ids:
            rejected_counts["duplicate_id"] += 1
            return None
        if aw.rights_status not in VERIFIED_RIGHTS_STATUSES:
            rejected_counts["rights_unverified"] += 1
            return None
        if not aw.image_url:
            rejected_counts["missing_image_url"] += 1
            return None
        artist_name = (aw.artist_display_name or aw.artist_name or "Unknown Artist").strip()
        norm_art = normalize_artist_name(artist_name)
        if norm_art != "unknown" and artist_counts[norm_art] >= max_per_artist:
            rejected_counts["artist_cap_reached"] += 1
            return None
        seen_ids.add(aw.canonical_id)
        artist_counts[norm_art] += 1
        safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", aw.canonical_id)
        rec = {
            "canonical_id": aw.canonical_id,
            "source_key": safe_stem,
            "source": aw.source,
            "artist": artist_name,
            "artist_normalized": norm_art,
            "title": " ".join((aw.title or "Untitled").split()).strip(),
            "date": " ".join((aw.creation_date_display or aw.creation_date or "").split()).strip(),
            "institution": " ".join((aw.museum_name or aw.source).split()).strip(),
            "museum_url": (aw.artwork_url or aw.museum_url or "").strip(),
            "rights_status": aw.rights_status,
            "rights_basis": getattr(aw, "rights_statement", None) or getattr(aw, "license_name", None) or aw.rights_status,
            "image_url": aw.image_url,
            "image_path": str(img_dir / f"{safe_stem}.jpg"),
            "medium": (getattr(aw, "medium", None) or "").strip(),
            "description": (getattr(aw, "description", None) or "").strip(),
            "dimensions_cm": (getattr(aw, "dimensions", None) or "").strip(),
            "_is_companion": is_companion,
        }
        desc = rec["description"]
        if desc and any(
            tok in desc.lower()
            for tok in ("pair", "pendant", "companion", "series", "sketch for", "study for", "outer panels", "altarpiece", "wings", "half-brother", "half-sister")
        ):
            rec["museum_relation_note"] = desc[:320]
        return rec

    pending_candidates: list[dict] = []
    shortfall = needed_fresh_artworks - eligible_before
    candidate_target = max(shortfall + 12, int(shortfall * 1.25))

    # Phase A: Prioritized search for missing documented companions
    adapter_by_name = {name: (adapter, lim) for name, adapter, lim, _ in adapter_specs}
    for src_name, q_text in _extract_companion_queries(list(cached_by_id.values()), seen_ids):
        if len(pending_candidates) >= candidate_target:
            break
        if src_name not in adapter_by_name or consecutive_failures[src_name] >= 2:
            continue
        adapter, lim = adapter_by_name[src_name]
        try:
            batch = adapter.fetch_candidates(limit=min(12, lim), query=q_text, rng=rng, verification_id="companion")
            consecutive_failures[src_name] = 0
        except Exception as exc:
            consecutive_failures[src_name] += 1
            failed_adapters.append({"adapter": src_name, "phase": "companion", "query": q_text, "error": str(exc)[:140]})
            continue
        for aw in batch:
            cand = _collect_candidate(aw, is_companion=True)
            if cand is not None:
                pending_candidates.append(cand)

    # Phase B: Diversified round-robin across working adapters
    # Offset query order by seed so consecutive months explore fresh topics first
    q_list = list(AUTO_SUPPLY_MUSEUM_QUERIES)
    offset = seed % len(q_list)
    ordered_queries = q_list[offset:] + q_list[:offset]
    for q_idx, q in enumerate(ordered_queries):
        if len(pending_candidates) >= candidate_target:
            break
        for name, adapter, per_query_limit, max_q in adapter_specs:
            if q_idx >= max_q or consecutive_failures[name] >= 2:
                continue
            if len(pending_candidates) >= candidate_target:
                break
            try:
                batch = adapter.fetch_candidates(limit=per_query_limit, query=q, rng=rng)
                consecutive_failures[name] = 0
            except Exception as exc:
                consecutive_failures[name] += 1
                failed_adapters.append({"adapter": name, "phase": "diversify", "query": q, "error": str(exc)[:140]})
                continue
            for aw in batch:
                cand = _collect_candidate(aw, is_companion=False)
                if cand is not None:
                    pending_candidates.append(cand)

    def _download_job(cand: dict) -> tuple[bool, dict]:
        ok, w, h = _download_verified_image(cand["image_url"], Path(cand["image_path"]))
        if not ok:
            return False, cand
        cand["width"] = w
        cand["height"] = h
        cand["aspect_ratio"] = round(w / max(1, h), 3)
        cand["dhash"] = compute_image_dhash(cand["image_path"])
        return True, cand

    added_total = 0
    with ThreadPoolExecutor(max_workers=max(1, download_workers)) as pool:
        futures = [pool.submit(_download_job, c) for c in pending_candidates]
        for fut in as_completed(futures):
            ok, res = fut.result()
            is_comp = bool(res.pop("_is_companion", False))
            if not ok:
                rejected_counts["download_or_quality_failed"] += 1
                continue
            if res.get("dhash") and res["dhash"] in existing_dhashes:
                rejected_counts["duplicate_dhash"] += 1
                Path(res["image_path"]).unlink(missing_ok=True)
                continue
            if res.get("dhash"):
                existing_dhashes.add(res["dhash"])
            cached_by_id[res["canonical_id"]] = res
            added_total += 1
            added_by_museum[res.get("institution") or res.get("source") or "Unknown"] += 1
            if is_comp:
                companion_added += 1

    artworks = sorted(cached_by_id.values(), key=lambda x: x["canonical_id"])
    vis_flags = flag_visual_near_duplicates(artworks)
    payload = {
        "schema_version": raw_payload.get("schema_version") or "artfolio-verified-museum-cache-v1",
        "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_artworks": len(artworks),
        "visual_similarity_flags": vis_flags,
        "artworks": artworks,
    }
    tmp_idx = index_path.with_suffix(".tmp.json")
    tmp_idx.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_idx.replace(index_path)

    eligible_after = _count_eligible(cached_by_id)
    return {
        "pool_index_path": str(index_path),
        "eligible_before": eligible_before,
        "needed_fresh_artworks": needed_fresh_artworks,
        "auto_fetch_triggered": True,
        "added_total": added_total,
        "added_by_museum": dict(added_by_museum.most_common()),
        "companion_candidates_added": companion_added,
        "rejected_total": sum(rejected_counts.values()),
        "rejected_counts": dict(rejected_counts),
        "failed_adapters": failed_adapters,
        "eligible_after": eligible_after,
        "remaining_shortfall": max(0, needed_fresh_artworks - eligible_after),
        "total_pool_size": len(artworks),
    }


def _download_verified_image(url: str, dest: Path, timeout: int = 20) -> tuple[bool, int, int]:
    """Download a museum image to cache and verify Pillow readability + minimum short edge."""
    if dest.is_file() and dest.stat().st_size > 12_000:
        try:
            with Image.open(dest) as im:
                w, h = im.size
                if min(w, h) >= 600:
                    return True, w, h
        except Exception:
            dest.unlink(missing_ok=True)

    headers = {"User-Agent": "ArtfolioLocalRehearsal/1.0 (+https://github.com/ufkalkn023-coder/instagram-art-bot)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200 or len(resp.content) < 12_000:
            return False, 0, 0
        tmp = dest.with_suffix(".tmp.jpg")
        tmp.write_bytes(resp.content)
        with Image.open(tmp) as im:
            im.load()
            w, h = im.size
            if min(w, h) < 600 or (w / max(1, h)) < 0.35 or (w / max(1, h)) > 2.85:
                tmp.unlink(missing_ok=True)
                return False, 0, 0
            if im.mode != "RGB":
                im.convert("RGB").save(dest, format="JPEG", quality=92)
                tmp.unlink(missing_ok=True)
            else:
                tmp.replace(dest)
        return True, w, h
    except Exception:
        return False, 0, 0


def fetch_verified_museum_pool(
    cache_dir: str | Path,
    *,
    target_count: int = 220,
    queries: Sequence[str] = DEFAULT_MUSEUM_QUERIES,
    max_per_artist: int = 3,
    download_workers: int = 8,
) -> dict:
    """Fetch and locally cache real public-domain artworks using existing MuseumAdapter classes.

    Reuses already-cached verified records/images first so re-runs never re-download unnecessarily.
    Never pads with synthetic or demo images.
    """
    from src.museums.cleveland import ClevelandAdapter
    from src.museums.met import MetAdapter
    from src.museums.rijksmuseum import RijksmuseumAdapter

    root = Path(cache_dir).resolve()
    img_dir = root / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    index_path = root / "pool_index.json"

    cached_by_id: dict[str, dict] = {}
    if index_path.is_file():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            for item in raw.get("artworks", []):
                p = Path(item.get("image_path", ""))
                if p.is_file() and item.get("rights_status") in VERIFIED_RIGHTS_STATUSES:
                    cached_by_id[item["canonical_id"]] = item
        except Exception:
            cached_by_id = {}

    if len(cached_by_id) >= target_count:
        artworks = list(cached_by_id.values())
        return {
            "cache_dir": str(root),
            "total_cached": len(artworks),
            "newly_fetched": 0,
            "artworks": artworks,
            "visual_similarity_flags": flag_visual_near_duplicates(artworks),
        }

    adapters = [
        (ClevelandAdapter(), 28, len(queries)),
        (RijksmuseumAdapter(), 14, min(10, len(queries))),
        (MetAdapter(), 6, min(4, len(queries))),
    ]
    rng = random.Random(20261124)
    artist_counts = Counter(normalize_artist_name(a.get("artist", "")) for a in cached_by_id.values())
    pending_candidates: list[dict] = []
    seen_ids = set(cached_by_id.keys())

    # Fast round-robin across museums and queries with bounded per-adapter limits
    for q_idx, q in enumerate(queries):
        if len(cached_by_id) + len(pending_candidates) >= int(target_count * 1.40):
            break
        for adapter, per_query_limit, max_q in adapters:
            if q_idx >= max_q:
                continue
            try:
                batch = adapter.fetch_candidates(limit=per_query_limit, query=q, rng=rng)
            except Exception:
                continue
            for aw in batch:
                if aw.canonical_id in seen_ids:
                    continue
                if aw.rights_status not in VERIFIED_RIGHTS_STATUSES or not aw.image_url:
                    continue
                artist_name = (aw.artist_display_name or aw.artist_name or "Unknown Artist").strip()
                norm_art = normalize_artist_name(artist_name)
                if norm_art != "unknown" and artist_counts[norm_art] >= max_per_artist:
                    continue
                seen_ids.add(aw.canonical_id)
                artist_counts[norm_art] += 1
                safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", aw.canonical_id)
                pending_candidates.append(
                    {
                        "canonical_id": aw.canonical_id,
                        "source_key": safe_stem,
                        "source": aw.source,
                        "artist": artist_name,
                        "artist_normalized": norm_art,
                        "title": (aw.title or "Untitled").strip(),
                        "date": (aw.creation_date_display or aw.creation_date or "").strip(),
                        "institution": (aw.museum_name or aw.source).strip(),
                        "museum_url": (aw.artwork_url or aw.museum_url or "").strip(),
                        "rights_status": aw.rights_status,
                        "rights_basis": getattr(aw, "rights_statement", None) or getattr(aw, "license_name", None) or aw.rights_status,
                        "image_url": aw.image_url,
                        "image_path": str(img_dir / f"{safe_stem}.jpg"),
                        "medium": (getattr(aw, "medium", None) or "").strip(),
                        "description": (getattr(aw, "description", None) or "").strip(),
                        "dimensions_cm": (getattr(aw, "dimensions", None) or "").strip(),
                    }
                )
                if pending_candidates[-1]["description"] and any(
                    tok in pending_candidates[-1]["description"].lower()
                    for tok in ("pair", "pendant", "companion", "series", "sketch for", "study for", "outer panels", "altarpiece", "wings", "half-brother", "half-sister")
                ):
                    pending_candidates[-1]["museum_relation_note"] = pending_candidates[-1]["description"][:320]

    newly_fetched = 0

    def _job(cand: dict) -> dict | None:
        ok, w, h = _download_verified_image(cand["image_url"], Path(cand["image_path"]))
        if not ok:
            return None
        cand["width"] = w
        cand["height"] = h
        cand["aspect_ratio"] = round(w / max(1, h), 3)
        cand["dhash"] = compute_image_dhash(cand["image_path"])
        return cand

    with ThreadPoolExecutor(max_workers=max(1, download_workers)) as pool:
        futures = [pool.submit(_job, c) for c in pending_candidates]
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                cached_by_id[res["canonical_id"]] = res
                newly_fetched += 1

    artworks = sorted(cached_by_id.values(), key=lambda x: x["canonical_id"])
    vis_flags = flag_visual_near_duplicates(artworks)
    payload = {
        "schema_version": "artfolio-verified-museum-cache-v1",
        "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_artworks": len(artworks),
        "visual_similarity_flags": vis_flags,
        "artworks": artworks,
    }
    tmp_idx = index_path.with_suffix(".tmp.json")
    tmp_idx.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_idx.replace(index_path)

    return {
        "cache_dir": str(root),
        "total_cached": len(artworks),
        "newly_fetched": newly_fetched,
        "artworks": artworks,
        "visual_similarity_flags": vis_flags,
    }


class ArtworkReuseLedger:
    """Local JSON ledger preventing finalized/published artworks from repeating within 180 days.

    Supports lifecycle states:
    - 'preview': exploratory draft, NEVER blocks other months
    - 'reserved': planned/exported month, blocks OTHER plan_ids within cooldown_days
    - 'published': live/confirmed post, blocks OTHER plan_ids within cooldown_days
    Re-opening or fixing the SAME plan_id never self-blocks its own artworks.
    Also tracks active plan calendar windows so overlapping schedule slots can be reported.
    """

    def __init__(self, ledger_path: str | Path):
        self.path = Path(ledger_path).resolve()
        self._records: dict[str, dict] = {}
        self._plans: dict[str, dict] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("used_artworks"), dict):
                for k, v in raw["used_artworks"].items():
                    if isinstance(v, dict):
                        status = str(v.get("status") or "reserved").lower()
                        if status not in VALID_LEDGER_STATUSES:
                            status = "reserved"
                        self._records[str(k)] = {
                            "used_at": str(v.get("used_at") or ""),
                            "plan_id": str(v.get("plan_id") or "legacy"),
                            "status": status,
                        }
                    else:
                        self._records[str(k)] = {
                            "used_at": str(v),
                            "plan_id": "legacy",
                            "status": "published",
                        }
            if isinstance(raw, dict) and isinstance(raw.get("plans"), dict):
                self._plans = {str(k): dict(v) for k, v in raw["plans"].items() if isinstance(v, dict)}

    def is_blocked(
        self,
        artwork_key: str,
        *,
        now: datetime | None = None,
        cooldown_days: int = REUSE_COOLDOWN_DAYS,
        current_plan_id: str | None = None,
    ) -> bool:
        rec = self._records.get(artwork_key)
        if not rec or not rec.get("used_at"):
            return False
        if rec.get("status") == "preview":
            return False
        if current_plan_id and rec.get("plan_id") == current_plan_id:
            return False
        ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        used_at = datetime.fromisoformat(rec["used_at"])
        if used_at.tzinfo is None:
            used_at = used_at.replace(tzinfo=timezone.utc)
        return (ref_now - used_at) < timedelta(days=cooldown_days)

    def mark_used(
        self,
        artwork_keys: Sequence[str],
        *,
        used_at: datetime | None = None,
        plan_id: str = "default",
        status: str = "reserved",
        replace_plan: bool = False,
        schedule_start: str | None = None,
        schedule_end: str | None = None,
    ) -> None:
        norm_status = str(status or "reserved").lower()
        if norm_status not in VALID_LEDGER_STATUSES:
            raise ValueError(f"Unsupported ledger status '{status}'; expected one of {sorted(VALID_LEDGER_STATUSES)}")
        if replace_plan and plan_id:
            self._records = {
                k: v
                for k, v in self._records.items()
                if not (v.get("plan_id") == plan_id and not (v.get("status") == "published" and norm_status == "preview"))
            }
        stamp = (used_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        for key in artwork_keys:
            if key:
                self._records[str(key)] = {
                    "used_at": stamp,
                    "plan_id": str(plan_id or "default"),
                    "status": norm_status,
                }
        if plan_id:
            prev_plan = self._plans.get(str(plan_id), {})
            self._plans[str(plan_id)] = {
                "status": norm_status,
                "updated_at": stamp,
                "schedule_start": schedule_start or prev_plan.get("schedule_start", ""),
                "schedule_end": schedule_end or prev_plan.get("schedule_end", ""),
            }
        self.save()

    def find_schedule_collisions(
        self,
        start_date: str,
        end_date: str,
        *,
        exclude_plan_id: str | None = None,
    ) -> list[dict]:
        """Return active reservations/published plans whose calendar window overlaps [start_date, end_date]."""
        if not start_date or not end_date:
            return []
        s_new = datetime.fromisoformat(start_date[:10]).date()
        e_new = datetime.fromisoformat(end_date[:10]).date()
        collisions: list[dict] = []
        for pid, meta in sorted(self._plans.items()):
            if exclude_plan_id and pid == exclude_plan_id:
                continue
            if meta.get("status") not in {"reserved", "published"}:
                continue
            ps = (meta.get("schedule_start") or "")[:10]
            pe = (meta.get("schedule_end") or "")[:10]
            if not ps or not pe:
                continue
            s_old = datetime.fromisoformat(ps).date()
            e_old = datetime.fromisoformat(pe).date()
            overlap_start = max(s_new, s_old)
            overlap_end = min(e_new, e_old)
            if overlap_start <= overlap_end:
                collisions.append(
                    {
                        "existing_plan_id": pid,
                        "existing_status": meta.get("status"),
                        "existing_window": f"{s_old.isoformat()}..{e_old.isoformat()}",
                        "overlap_start": overlap_start.isoformat(),
                        "overlap_end": overlap_end.isoformat(),
                        "overlap_days": (overlap_end - overlap_start).days + 1,
                    }
                )
        return collisions

    def status_summary(self) -> dict:
        by_status = Counter(v.get("status", "reserved") for v in self._records.values())
        by_plan = Counter(v.get("plan_id", "default") for v in self._records.values())
        return {
            "total_entries": len(self._records),
            "by_status": dict(by_status),
            "by_plan": dict(by_plan),
            "plans": self._plans,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "artfolio-reuse-ledger-v2",
            "cooldown_days": REUSE_COOLDOWN_DAYS,
            "plans": self._plans,
            "used_artworks": self._records,
        }
        tmp = self.path.with_suffix(".tmp.json")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)


def enrich_pool_metadata(
    pool_index_path: str | Path,
    *,
    canonical_ids: Sequence[str] | None = None,
    max_workers: int = 4,
) -> dict:
    """Refresh missing medium/description/relation notes and English Rijksmuseum titles in pool_index.json."""
    from src.museums.rijksmuseum import RijksmuseumAdapter

    index_path = Path(pool_index_path).resolve()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    artworks = payload.get("artworks", [])
    target_set = set(canonical_ids) if canonical_ids else None
    to_refresh = [
        item
        for item in artworks
        if (target_set is None or item.get("canonical_id") in target_set)
        and item.get("source") == "rijksmuseum"
        and (not item.get("medium") or not item.get("description"))
    ]
    if not to_refresh:
        return {"enriched": 0, "checked": 0}

    adapter = RijksmuseumAdapter()

    def _fetch_one(item: dict) -> tuple[str, object | None]:
        cid = item["canonical_id"]
        obj_num = cid[len("rijksmuseum_"):] if cid.startswith("rijksmuseum_") else cid
        try:
            res = adapter.fetch_candidates(limit=2, verification_id=obj_num)
            for aw in res:
                if aw.canonical_id == cid or aw.source_id == obj_num:
                    return cid, aw
        except Exception:
            pass
        return cid, None

    updates: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for cid, aw in pool.map(_fetch_one, to_refresh):
            if aw is not None:
                updates[cid] = aw

    enriched_count = 0
    for item in artworks:
        aw = updates.get(item.get("canonical_id"))
        if aw is None:
            continue
        if aw.title and aw.title != item.get("title"):
            item.setdefault("title_original", item.get("title", ""))
            item["title"] = aw.title.strip()
        if aw.medium and not item.get("medium"):
            item["medium"] = aw.medium.strip()
        if aw.description:
            item["description"] = aw.description.strip()
            if any(
                tok in item["description"].lower()
                for tok in ("pair", "pendant", "companion", "series", "sketch for", "study for", "outer panels", "altarpiece", "wings", "half-brother", "half-sister")
            ):
                item["museum_relation_note"] = item["description"][:320]
        if getattr(aw, "dimensions", None) and not item.get("dimensions_cm"):
            item["dimensions_cm"] = str(aw.dimensions).strip()
        enriched_count += 1

    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = index_path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(index_path)
    return {"checked": len(to_refresh), "enriched": enriched_count}


def infer_century(date_str: str) -> int:
    yr = parse_year(date_str)
    if yr is None:
        return 19
    return max(14, min(20, (yr // 100) + 1))


def select_diverse_pool(
    candidates: Sequence[dict],
    target_count: int,
    *,
    max_per_artist: int = 2,
    max_museum_share: float = 0.30,
    reuse_ledger: ArtworkReuseLedger | None = None,
    min_short_edge: int = 300,
) -> tuple[list[dict], dict]:
    """Select artworks respecting 180-day blacklist, artist cap, museum share, century balance & resolution."""
    eligible = []
    blocked_reuse = 0
    blocked_res = 0
    for item in candidates:
        key = item.get("source_key") or item.get("canonical_id") or ""
        if reuse_ledger is not None and reuse_ledger.is_blocked(key):
            blocked_reuse += 1
            continue
        res = validate_artwork_resolution(item["image_path"], min_short_edge=min_short_edge)
        if not res["valid"]:
            blocked_res += 1
            continue
        enriched = {
            **item,
            "century": infer_century(item.get("date", "")),
            "hd_ready": res["hd_ready"],
        }
        eligible.append(enriched)

    unique_museums = {item.get("institution", "Unknown") for item in eligible}
    max_per_museum = (
        max(1, int(target_count * max_museum_share))
        if len(unique_museums) >= 3
        else target_count
    )

    selected: list[dict] = []
    artist_counts: Counter[str] = Counter()
    museum_counts: Counter[str] = Counter()
    century_counts: Counter[int] = Counter()

    # Pass 1: strict diversity + century balance
    remaining = list(eligible)
    while len(selected) < target_count and remaining:
        best_idx = None
        best_score = -1e9
        for idx, item in enumerate(remaining):
            art_name = item.get("artist", "Unknown")
            mus_name = item.get("institution", "Unknown")
            if artist_counts[art_name] >= max_per_artist or museum_counts[mus_name] >= max_per_museum:
                continue
            # Prefer underrepresented centuries + HD images
            score = -century_counts[item["century"]] * 2.0 + (1.0 if item["hd_ready"] else 0.0)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is None:
            break
        chosen = remaining.pop(best_idx)
        selected.append(chosen)
        artist_counts[chosen.get("artist", "Unknown")] += 1
        museum_counts[chosen.get("institution", "Unknown")] += 1
        century_counts[chosen["century"]] += 1

    # Pass 2: graceful fill if local demo pool is smaller than target_count
    if len(selected) < target_count and eligible:
        idx = 0
        while len(selected) < target_count:
            selected.append(eligible[idx % len(eligible)])
            idx += 1

    return selected, {
        "requested": target_count,
        "unique_eligible": len(eligible),
        "blocked_by_180d_reuse": blocked_reuse,
        "blocked_by_resolution": blocked_res,
        "unique_artists_selected": len(artist_counts),
        "unique_museums_selected": len(museum_counts),
        "centuries_covered": sorted(century_counts.keys()),
    }
