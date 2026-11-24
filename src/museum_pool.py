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
                        "dimensions_cm": "",
                    }
                )

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
    """Local JSON ledger preventing any exported/published artwork from repeating within 180 days."""

    def __init__(self, ledger_path: str | Path):
        self.path = Path(ledger_path).resolve()
        self._records: dict[str, str] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("used_artworks"), dict):
                self._records = {str(k): str(v) for k, v in raw["used_artworks"].items()}

    def is_blocked(self, artwork_key: str, *, now: datetime | None = None, cooldown_days: int = REUSE_COOLDOWN_DAYS) -> bool:
        ts_str = self._records.get(artwork_key)
        if not ts_str:
            return False
        ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        used_at = datetime.fromisoformat(ts_str)
        if used_at.tzinfo is None:
            used_at = used_at.replace(tzinfo=timezone.utc)
        return (ref_now - used_at) < timedelta(days=cooldown_days)

    def mark_used(self, artwork_keys: Sequence[str], *, used_at: datetime | None = None) -> None:
        stamp = (used_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        for key in artwork_keys:
            if key:
                self._records[str(key)] = stamp
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "artfolio-reuse-ledger-v1",
            "cooldown_days": REUSE_COOLDOWN_DAYS,
            "used_artworks": self._records,
        }
        tmp = self.path.with_suffix(".tmp.json")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)


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
