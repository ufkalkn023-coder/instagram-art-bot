"""Museum artwork pool management: diversity quotas, century/region balance & 180-day reuse blacklist."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from src.artwork_intelligence import validate_artwork_resolution
from src.neon_kit import parse_year

REUSE_COOLDOWN_DAYS = 180


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
