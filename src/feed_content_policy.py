"""Automatic editorial is the regular Feed; reviewed-only and legacy are explicit."""
import os

from src.feed_content import PreparedStoryContent


def resolve_content_mode(value=None):
    mode = value if value is not None else os.environ.get('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    if mode not in {'automatic', 'editorial', 'legacy'}:
        raise ValueError('Feed content mode must be automatic, editorial or legacy')
    return mode


def is_editorial_content(content):
    return (isinstance(content, PreparedStoryContent)
            and content.story_delivery.schema_version == 'artfolio-story-delivery-v2')


def is_automatic_editorial_content(content):
    return (isinstance(content, PreparedStoryContent)
            and content.story_delivery.schema_version in {'artfolio-story-delivery-v2', 'artfolio-story-delivery-v3'})
