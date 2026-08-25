"""Pure/local helpers for Artfolio Reel discovery, matching, and rates."""

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.insights_storage import InsightsStorageError, parse_aware_timestamp, utc_timestamp
from src.instagram_insights import InstagramMedia

SECONDARY_MATCH_EARLY_TOLERANCE_HOURS = 6
SECONDARY_MATCH_MAX_DELAY_DAYS = 30
MATCH_METHODS = {"caption_exact", "caption_normalized", "title_artist_timestamp", "manual", "bot_publication"}
SAFE_REEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class LocalReel:
    reel_id: str
    canonical_artwork_id: str
    title: str
    artist: str
    produced_at: datetime
    caption: str | None
    render_path: str | None


@dataclass(frozen=True)
class MatchResult:
    associations: tuple[dict[str, Any], ...]
    ambiguous_media_ids: tuple[str, ...]
    unmatched_media_ids: tuple[str, ...]


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InsightsStorageError(f"{label} is unreadable or malformed at {path}") from exc


def _social_caption(root: Path, reel_id: str) -> str | None:
    social_root = root / "output" / "social"
    matches = sorted(
        path for path in social_root.glob(f"{reel_id}*.txt")
        if path.is_file() and (path.name == f"{reel_id}.txt" or path.name.startswith(f"{reel_id}-"))
    )
    if len(matches) > 1:
        raise InsightsStorageError(f"Multiple social-copy files found for local Reel {reel_id}")
    if not matches:
        return None
    try:
        return matches[0].read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InsightsStorageError(f"Social copy is unreadable for local Reel {reel_id}") from exc


def load_local_reels(reels_root: str | Path) -> tuple[LocalReel, ...]:
    """Read the ignored Remotion ledger and immutable ReelData artifacts."""
    root = Path(reels_root).expanduser().resolve()
    history = _read_json(root / "data" / "reel-production-history.json", "Reel production history")
    entries = history.get("entries") if isinstance(history, dict) else None
    if not isinstance(history, dict) or history.get("version") != "reel-production-history-v1" or not isinstance(entries, list):
        raise InsightsStorageError("Reel production history has an unsupported schema")

    reels: list[LocalReel] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "RENDERED":
            continue
        reel_id = entry.get("canonicalId")
        rendered_at = parse_aware_timestamp(entry.get("renderedAt"))
        if not isinstance(reel_id, str) or not SAFE_REEL_ID.fullmatch(reel_id) or rendered_at is None or reel_id in seen:
            raise InsightsStorageError("Reel production history contains an invalid or duplicate rendered entry")
        plan = _read_json(root / "data" / "reels" / f"{reel_id}.json", f"ReelData for {reel_id}")
        artworks = plan.get("artworks") if isinstance(plan, dict) else None
        artwork = artworks[0] if isinstance(artworks, list) and len(artworks) == 1 else None
        if not isinstance(artwork, dict) or plan.get("id") != reel_id or artwork.get("id") != reel_id:
            raise InsightsStorageError(f"ReelData identity does not match local Reel {reel_id}")
        title = artwork.get("title")
        artist = artwork.get("artist")
        if not isinstance(title, str) or not title.strip() or not isinstance(artist, str) or not artist.strip():
            raise InsightsStorageError(f"ReelData metadata is incomplete for local Reel {reel_id}")
        reels.append(LocalReel(
            reel_id=reel_id,
            canonical_artwork_id=reel_id,
            title=title.strip(),
            artist=artist.strip(),
            produced_at=rendered_at,
            caption=_social_caption(root, reel_id),
            render_path=entry.get("renderPath") if isinstance(entry.get("renderPath"), str) else None,
        ))
        seen.add(reel_id)
    return tuple(reels)


def exact_caption(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def normalized_caption(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _search_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").casefold()
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _secondary_candidate(media: InstagramMedia, reel: LocalReel) -> bool:
    caption = _search_text(media.caption)
    title = _search_text(reel.title)
    artist = _search_text(reel.artist)
    if min(len(title), len(artist)) < 4 or title in {"untitled", "unknown"} or artist in {"anonymous", "unknown artist"}:
        return False
    published_at = parse_aware_timestamp(media.timestamp)
    if published_at is None:
        return False
    earliest = reel.produced_at - timedelta(hours=SECONDARY_MATCH_EARLY_TOLERANCE_HOURS)
    latest = reel.produced_at + timedelta(days=SECONDARY_MATCH_MAX_DELAY_DAYS)
    padded_caption = f" {caption} "
    return f" {title} " in padded_caption and f" {artist} " in padded_caption and earliest <= published_at <= latest


def _inside_candidate_window(media: InstagramMedia, reel: LocalReel) -> bool:
    published_at = parse_aware_timestamp(media.timestamp)
    if published_at is None:
        return False
    return (
        reel.produced_at - timedelta(hours=SECONDARY_MATCH_EARLY_TOLERANCE_HOURS)
        <= published_at
        <= reel.produced_at + timedelta(days=SECONDARY_MATCH_MAX_DELAY_DAYS)
    )


def _match_candidates(media: InstagramMedia, reels: tuple[LocalReel, ...]) -> tuple[str | None, tuple[LocalReel, ...]]:
    reels = tuple(reel for reel in reels if _inside_candidate_window(media, reel))
    if media.caption:
        exact = tuple(reel for reel in reels if reel.caption and exact_caption(reel.caption) == exact_caption(media.caption))
        if exact:
            return "caption_exact", exact
        normalized = tuple(
            reel for reel in reels
            if reel.caption and normalized_caption(reel.caption) == normalized_caption(media.caption)
        )
        if normalized:
            return "caption_normalized", normalized
    secondary = tuple(reel for reel in reels if _secondary_candidate(media, reel))
    return ("title_artist_timestamp", secondary) if secondary else (None, ())


def match_recent_media(
    local_reels: tuple[LocalReel, ...],
    media: tuple[InstagramMedia, ...],
    existing_associations: list[dict[str, Any]],
    matched_at: datetime,
) -> MatchResult:
    """Return only globally one-to-one high-confidence automatic matches."""
    linked_reels = {item.get("reel_id") for item in existing_associations}
    linked_media = {item.get("instagram_media_id") for item in existing_associations}
    available_reels = tuple(reel for reel in local_reels if reel.reel_id not in linked_reels)
    available_media = tuple(item for item in media if item.id not in linked_media)
    candidates: dict[str, tuple[str | None, tuple[LocalReel, ...]]] = {
        item.id: _match_candidates(item, available_reels) for item in available_media
    }
    associations: list[dict[str, Any]] = []
    media_by_id = {item.id: item for item in available_media}
    linked_this_run: set[str] = set()
    claimed_reels: set[str] = set()
    for method in ("caption_exact", "caption_normalized", "title_artist_timestamp"):
        round_candidates = {
            media_id: tuple(reel for reel in reels if reel.reel_id not in claimed_reels)
            for media_id, (candidate_method, reels) in candidates.items()
            if candidate_method == method and media_id not in linked_this_run
        }
        reel_frequency: dict[str, int] = {}
        for reels in round_candidates.values():
            for reel in reels:
                reel_frequency[reel.reel_id] = reel_frequency.get(reel.reel_id, 0) + 1
        for media_id, reels in round_candidates.items():
            if len(reels) != 1 or reel_frequency.get(reels[0].reel_id) != 1:
                continue
            reel = reels[0]
            item = media_by_id[media_id]
            associations.append({
                "canonical_artwork_id": reel.canonical_artwork_id,
                "reel_id": reel.reel_id,
                "instagram_media_id": item.id,
                **({"permalink": item.permalink} if item.permalink else {}),
                "published_at": item.timestamp,
                "matched_at": utc_timestamp(matched_at),
                "match_method": method,
            })
            linked_this_run.add(media_id)
            claimed_reels.add(reel.reel_id)

    ambiguous = [media_id for media_id, (_, reels) in candidates.items() if media_id not in linked_this_run and reels]
    unmatched = [media_id for media_id, (_, reels) in candidates.items() if media_id not in linked_this_run and not reels]
    return MatchResult(tuple(associations), tuple(sorted(ambiguous)), tuple(sorted(unmatched)))


def safe_rate(numerator: Any, denominator: Any) -> float | None:
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        return None
    if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or numerator < 0 or denominator <= 0:
        return None
    return numerator / denominator


def derive_engagement_rates(metrics: dict[str, int | float]) -> dict[str, float]:
    """Calculate numerator/reach rates only when both raw values are usable."""
    output: dict[str, float] = {}
    for metric, derived_name in (
        ("saved", "save_rate"),
        ("shares", "share_rate"),
        ("likes", "like_rate"),
        ("comments", "comment_rate"),
    ):
        value = safe_rate(metrics.get(metric), metrics.get("reach"))
        if value is not None:
            output[derived_name] = value
    return output
