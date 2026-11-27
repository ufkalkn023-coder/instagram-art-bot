"""Instagram Insights CSV importer, performance scorecard & self-optimizing design weights."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _to_int(val: str | None) -> int:
    clean = re.sub(r"[^0-9]", "", str(val or ""))
    return int(clean) if clean else 0


def _pick(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key])
    return ""


def analyze_instagram_insights_csv(csv_path: str | Path) -> dict:
    """Parse an exported Instagram Insights CSV (English or Turkish Meta headers) into a scorecard."""
    path = Path(csv_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Insights CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    by_format: dict[str, list[dict]] = defaultdict(list)
    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_design_tag: dict[str, list[dict]] = defaultdict(list)
    with_hashtags: list[int] = []
    without_hashtags: list[int] = []
    with_question: list[int] = []
    without_question: list[int] = []

    parsed_rows = []
    for row in rows:
        views = _to_int(_pick(row, "Views", "Görüntülemeler"))
        reach = _to_int(_pick(row, "Reach", "Erişim"))
        likes = _to_int(_pick(row, "Likes", "Beğenmeler"))
        saves = _to_int(_pick(row, "Saves", "Kaydedilenler"))
        comments = _to_int(_pick(row, "Comments", "Yorumlar"))
        shares = _to_int(_pick(row, "Shares", "Paylaşımlar"))
        post_type = (_pick(row, "Post type", "Gönderi türü") or "unknown").strip()
        desc = _pick(row, "Description", "Açıklama").strip()
        pub_time = _pick(row, "Publish time", "Yayınlanma zamanı").strip()

        save_rate = saves / max(1, reach)
        eng_rate = (likes + saves + comments + shares) / max(1, reach)
        item = {
            "post_id": _pick(row, "Post ID", "Gönderi Kodu"),
            "post_type": post_type,
            "views": views,
            "reach": reach,
            "likes": likes,
            "saves": saves,
            "comments": comments,
            "shares": shares,
            "save_rate": round(save_rate, 4),
            "engagement_rate": round(eng_rate, 4),
            "has_hashtags": "#" in desc,
            "has_question": "?" in desc,
            "publish_time": pub_time,
        }
        parsed_rows.append(item)
        by_format[post_type].append(item)

        if item["has_hashtags"]:
            with_hashtags.append(reach)
        else:
            without_hashtags.append(reach)
        if item["has_question"]:
            with_question.append(reach)
        else:
            without_question.append(reach)

        # Extract publish hour if formatted like MM/DD/YYYY HH:MM
        m_hr = re.search(r"(\d{2}):(\d{2})", pub_time)
        if m_hr:
            by_hour[int(m_hr.group(1))].append(item)

        # Optional design ID tag inside caption/notes (e.g. astra-001 or neon-180)
        for did in re.findall(r"\b(?:astra|neon)-\d{3}\b", desc.lower()):
            by_design_tag[did].append(item)

    def _avg(values: list[int | float]) -> float:
        return round(sum(values) / max(1, len(values)), 2)

    format_summary = {
        fmt: {
            "count": len(items),
            "avg_views": _avg([x["views"] for x in items]),
            "avg_reach": _avg([x["reach"] for x in items]),
            "avg_likes": _avg([x["likes"] for x in items]),
            "total_saves": sum(x["saves"] for x in items),
            "avg_save_rate": _avg([x["save_rate"] for x in items]),
        }
        for fmt, items in by_format.items()
    }

    best_hours = sorted(
        (
            {
                "hour": hr,
                "count": len(items),
                "avg_reach": _avg([x["reach"] for x in items]),
                "avg_views": _avg([x["views"] for x in items]),
            }
            for hr, items in by_hour.items()
        ),
        key=lambda row: row["avg_reach"],
        reverse=True,
    )

    design_scores = {
        did: round(
            _avg([x["reach"] for x in items]) * (1.0 + 3.0 * _avg([x["save_rate"] for x in items])),
            2,
        )
        for did, items in by_design_tag.items()
    }

    return {
        "schema_version": "artfolio-insights-scorecard-v1",
        "analyzed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_posts": len(parsed_rows),
        "by_format": format_summary,
        "best_hours": best_hours[:6],
        "hashtag_impact": {
            "with_hashtags_avg_reach": _avg(with_hashtags),
            "without_hashtags_avg_reach": _avg(without_hashtags),
        },
        "question_cta_impact": {
            "with_question_avg_reach": _avg(with_question),
            "without_question_avg_reach": _avg(without_question),
        },
        "design_scores": design_scores,
    }


def compute_design_weights(
    scorecard: dict,
    candidate_ids: list[str],
    *,
    exploration_quota: float = 0.25,
) -> dict[str, float]:
    """Boost top-performing designs by Reach + Save Rate while preserving >=25% exploration quota."""
    if not candidate_ids:
        return {}
    scores = scorecard.get("design_scores") or {}
    known = [did for did in candidate_ids if did in scores]
    fresh = [did for did in candidate_ids if did not in scores]

    if not known:
        uniform = round(1.0 / len(candidate_ids), 6)
        return {did: uniform for did in candidate_ids}

    exploit_share = 1.0 - (exploration_quota if fresh else 0.0)
    explore_share = 1.0 - exploit_share
    total_score = sum(max(0.1, float(scores[did])) for did in known)

    weights: dict[str, float] = {}
    for did in known:
        weights[did] = round(exploit_share * (max(0.1, float(scores[did])) / total_score), 6)
    for did in fresh:
        weights[did] = round(explore_share / len(fresh), 6)
    return weights
