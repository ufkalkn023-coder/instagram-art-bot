"""Pre-publication quality gate, operator feedback memory & future Insights media-ID registry."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

FeedbackTag = Literal[
    "approved",
    "text_too_generic",
    "crop_bad",
    "pair_works_well",
    "too_dark",
    "needs_revision",
]

_BROKEN_ARTIST_RE = re.compile(r"(?:\bobject\)|\bartist\)|\s\d+$|^\s*$)", re.IGNORECASE)
_DANGLING_TITLE_RE = re.compile(r"\b(?:in|the|of|at|with|for|and|a|an|prickly)\s*$", re.IGNORECASE)


def run_preflight_checks(posts: Sequence[dict], workspace_dir: str | Path) -> dict:
    """Detect concrete, actionable pre-publication defects across an entire rehearsal/plan."""
    root = Path(workspace_dir).resolve()
    defects: list[dict] = []
    openings_by_fmt: dict[str, Counter[str]] = {"carousel": Counter(), "reel": Counter()}
    questions_by_fmt: dict[str, Counter[str]] = {"carousel": Counter(), "reel": Counter()}

    for post in posts:
        pid = post.get("id", "unknown")
        fmt = post.get("post_format", "carousel")
        caption = (post.get("caption_en") or "").strip()
        alt_text = (post.get("alt_text_en") or "").strip()
        title = (post.get("editorial_title") or "").strip()

        # 1. Missing rendered media
        slides = post.get("slides") or ([post["image_path"]] if post.get("image_path") else [])
        if not slides:
            defects.append({"post_id": pid, "severity": "error", "code": "missing_media_file", "fix_hint": "No rendered slide path recorded."})
        else:
            for rel in slides:
                if not (root / rel).is_file():
                    defects.append({"post_id": pid, "severity": "error", "code": "missing_media_file", "fix_hint": f"Missing slide file: {rel}"})

        # 2. Broken artist names & missing museum source URL
        for a in post.get("artworks", []):
            art_name = (a.get("artist") or "").strip()
            if _BROKEN_ARTIST_RE.search(art_name) or "object)" in caption.lower() or "by artist)" in caption.lower():
                defects.append(
                    {
                        "post_id": pid,
                        "severity": "error",
                        "code": "broken_artist_name",
                        "fix_hint": f"Clean broken artist string: '{art_name}'",
                    }
                )
            if not (a.get("museum_url") or "").startswith(("http://", "https://")):
                defects.append(
                    {
                        "post_id": pid,
                        "severity": "warning",
                        "code": "missing_museum_url",
                        "fix_hint": f"Add verifiable museum URL for {a.get('canonical_id', 'artwork')}.",
                    }
                )

        # 3. Truncated / dangling title or unbalanced quotes
        if _DANGLING_TITLE_RE.search(title) or (title.count('"') % 2 == 1):
            defects.append(
                {
                    "post_id": pid,
                    "severity": "warning",
                    "code": "truncated_or_broken_title",
                    "fix_hint": f"Tighten title cleanly without dangling word/quote: '{title}'",
                }
            )

        # 4. Incomplete Alt-Text
        if len(alt_text) < 35 or not alt_text.endswith("."):
            defects.append(
                {
                    "post_id": pid,
                    "severity": "warning",
                    "code": "incomplete_alt_text",
                    "fix_hint": "Ensure Alt-Text is a complete descriptive English sentence (>=35 chars).",
                }
            )

        # 5. Ungrounded 'pigment' claim for digital swatches
        if re.search(r"\bpigments?\b", caption, re.IGNORECASE):
            defects.append(
                {
                    "post_id": pid,
                    "severity": "warning",
                    "code": "ungrounded_pigment_claim",
                    "fix_hint": "Refer to sampled digital colors/tones rather than chemically documented pigments.",
                }
            )

        # 6. Exact-year quiz on uncertain date
        if post.get("design_id") in {"neon-151", "neon-157"}:
            lead_date = (post.get("artworks", [{}])[0].get("date") or "").strip()
            if not re.fullmatch(r"\d{4}", lead_date):
                defects.append(
                    {
                        "post_id": pid,
                        "severity": "error",
                        "code": "uncertain_date_quiz",
                        "fix_hint": f"Avoid exact-year quiz for non-exact museum date '{lead_date}'.",
                    }
                )

        # Track repetition across the month
        lines = [ln.strip() for ln in caption.splitlines() if ln.strip()]
        if lines:
            openings_by_fmt.setdefault(fmt, Counter())[lines[0]] += 1
        for ln in lines:
            if "?" in ln:
                questions_by_fmt.setdefault(fmt, Counter())[ln] += 1

    # 7. Flag excessive opening or closing question repetition
    for fmt, counter in openings_by_fmt.items():
        for text, cnt in counter.items():
            if cnt > 2:
                defects.append(
                    {
                        "post_id": f"ALL_{fmt.upper()}",
                        "severity": "warning",
                        "code": "excessive_opening_repeat",
                        "fix_hint": f"Opening repeated {cnt}x in {fmt}s: '{text[:70]}'",
                    }
                )
    for fmt, counter in questions_by_fmt.items():
        for text, cnt in counter.items():
            if cnt > 4:
                defects.append(
                    {
                        "post_id": f"ALL_{fmt.upper()}",
                        "severity": "warning",
                        "code": "excessive_question_repeat",
                        "fix_hint": f"Question repeated {cnt}x in {fmt}s: '{text[:70]}'",
                    }
                )

    err_count = sum(1 for d in defects if d["severity"] == "error")
    warn_count = sum(1 for d in defects if d["severity"] == "warning")
    return {
        "passed": err_count == 0,
        "error_count": err_count,
        "warning_count": warn_count,
        "total_defects": len(defects),
        "defects": defects,
    }


class EditorialFeedbackStore:
    """Persist operator approvals & nuanced feedback linked to (artwork, design, copy_version).

    Provides gentle preference nudges for future runs without turning one comment into a rigid month-wide ban.
    """

    def __init__(self, store_path: str | Path):
        self.path = Path(store_path).resolve()
        self.records: list[dict] = []
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = list(raw.get("feedback", []))

    def record(
        self,
        *,
        post_id: str,
        tag: FeedbackTag,
        artwork_ids: Sequence[str],
        design_id: str,
        design_family: str,
        copy_version: str = "artfolio-voice-v2",
        note: str = "",
    ) -> dict:
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "post_id": post_id,
            "tag": tag,
            "artwork_ids": list(artwork_ids),
            "design_id": design_id,
            "design_family": design_family,
            "copy_version": copy_version,
            "note": note.strip(),
        }
        self.records.append(entry)
        self.save()
        return entry

    def soft_design_nudges(self) -> dict[str, float]:
        """Bounded [-1.5, +1.5] preference nudges so one single feedback never overrides artwork fit."""
        raw: Counter[str] = Counter()
        for r in self.records:
            did = r.get("design_id", "")
            if not did:
                continue
            if r["tag"] in {"approved", "pair_works_well"}:
                raw[did] += 1
            elif r["tag"] in {"crop_bad", "too_dark", "needs_revision"}:
                raw[did] -= 1
        return {did: max(-1.5, min(1.5, val * 0.4)) for did, val in raw.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": "artfolio-editorial-feedback-v1", "feedback": self.records}
        tmp = self.path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class InsightsMediaLinkRegistry:
    """Link planned/exported posts (design_id, family, narrative angle, artworks) to future Instagram media_ids."""

    def __init__(self, registry_path: str | Path):
        self.path = Path(registry_path).resolve()
        self.links: dict[str, dict] = {}
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.links = dict(raw.get("posts", {}))

    def register_post(self, post: dict) -> None:
        self.links[post["id"]] = {
            "post_id": post["id"],
            "post_format": post["post_format"],
            "design_id": post["design_id"],
            "design_family": post.get("family", ""),
            "curatorial_angle": post.get("curatorial_angle", ""),
            "copy_version": post.get("copy_version", ""),
            "artwork_ids": [a.get("canonical_id", "") for a in post.get("artworks", [])],
            "instagram_media_id": self.links.get(post["id"], {}).get("instagram_media_id", ""),
        }

    def attach_instagram_media_id(self, post_id: str, instagram_media_id: str) -> None:
        if post_id not in self.links:
            raise KeyError(f"Unknown post_id in Insights registry: {post_id}")
        self.links[post_id]["instagram_media_id"] = str(instagram_media_id).strip()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": "artfolio-insights-link-v1", "posts": self.links}
        tmp = self.path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
