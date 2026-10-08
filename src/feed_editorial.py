"""Connect adjacent prepared formats to the actual registry theme, with fallback."""

import logging
from dataclasses import replace
from uuid import uuid4

from src.carousel_themes import ThemeEvidenceMode, get_default_theme_registry
from src.feed_content import PreparedFeedContent

logger = logging.getLogger(__name__)


class ThematicSingleUnavailable(RuntimeError):
    """The bounded themed pool contains no compatible single artwork."""


class FeedPairPlanner:
    def __init__(self):
        self._anchor = None
        self._seen_themes = set()

    def prepare(self, format_name, directory, excluded, *, prepare) -> PreparedFeedContent:
        if format_name == "carousel":
            result = prepare(format_name, directory, excluded,
                             **({"excluded_theme_ids": set(self._seen_themes)} if self._seen_themes else {}))
            if result.theme_id is not None:
                self._seen_themes.add(result.theme_id)
            self._anchor = None
            themes = {theme.id: theme for theme in get_default_theme_registry().enabled_themes}
            theme = themes.get(result.theme_id)
            if theme is not None and theme.evidence_mode is ThemeEvidenceMode.METADATA:
                pair = {"pair_id": uuid4().hex, "theme_id": theme.id,
                        "role": "anchor", "status": "planned"}
                self._anchor = (theme, pair)
                result = replace(result, publication_metadata={**result.publication_metadata, "editorial_pair": pair})
            return result
        anchor, self._anchor = self._anchor, None
        if anchor is None:
            return prepare(format_name, directory, excluded)
        theme, anchor_pair = anchor
        try:
            result = prepare(format_name, directory, excluded, theme_definition=theme)
            status = "matched"
        except ThematicSingleUnavailable:
            logger.info("editorial_pair_unmatched theme=%s fallback=ordinary_single", theme.id)
            result = prepare(format_name, directory, excluded)
            status = "unmatched"
        pair = {**anchor_pair, "role": "followup", "status": status}
        return replace(result, publication_metadata={**result.publication_metadata, "editorial_pair": pair})
