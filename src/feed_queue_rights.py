"""Conservative fresh-source confirmation using existing bounded museum adapters."""

import logging
import random
from collections.abc import Sequence

from src.feed_content import PreparedFeedContent
from src.models import CONFIRMED_RIGHTS_STATUSES
from src.museums.base import MuseumAdapter

logger = logging.getLogger(__name__)


def revalidate_source_rights(content: PreparedFeedContent, *,
                             adapters: Sequence[MuseumAdapter] | None = None) -> bool:
    if adapters is None:
        from src.art_fetcher import get_museum_adapters
        adapters = tuple(get_museum_adapters())
    sources = {adapter.source_id: adapter for adapter in adapters}
    for artwork in content.artworks:
        identifier = artwork["id"]
        source = identifier.split("_", 1)[0]
        adapter = sources.get(source)
        title = artwork.get("title")
        if adapter is None or not isinstance(title, str) or not title.strip():
            return False
        try:
            candidates = adapter.fetch_candidates(limit=20, query=title[:200], rng=random.Random(identifier))
        except Exception as error:
            logger.warning("prepared_rights_unconfirmed source=%s error=%s", source, type(error).__name__)
            return False
        matches = [candidate for candidate in candidates if candidate.canonical_id == identifier]
        if len(matches) != 1 or not matches[0].is_public_domain or matches[0].rights_status not in CONFIRMED_RIGHTS_STATUSES:
            logger.warning("prepared_rights_unconfirmed source=%s reason=exact_identity_or_rights", source)
            return False
    return True
