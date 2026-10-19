"""The regular Feed consumes reviewed editorial pages; legacy is explicit."""
import os

from src.feed_content import PreparedStoryContent


def resolve_content_mode(value=None):
    mode = value if value is not None else os.environ.get('ARTFOLIO_FEED_CONTENT_MODE', 'editorial')
    if mode not in {'editorial', 'legacy'}:
        raise ValueError('Feed content mode must be editorial or legacy')
    return mode


def is_editorial_content(content):
    return (isinstance(content, PreparedStoryContent)
            and content.story_delivery.schema_version == 'artfolio-story-delivery-v2')
