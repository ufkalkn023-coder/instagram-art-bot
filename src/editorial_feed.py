"""Read-only selection of individually reviewed projects for alternating Feed slots."""
from pathlib import Path

from src.feed_content_policy import is_editorial_content
from src.story_feed import prepare_story_content


class EditorialStockUnavailable(RuntimeError):
    pass


def editorial_readiness(entries, *, target, first_format, excluded_ids=()):
    if (type(target) is not int or target not in {3, 4, 5} or first_format not in {'single', 'carousel'}
            or not 1 <= len(entries) <= 12):
        raise ValueError('Select one to twelve projects for three to five alternating slots')
    formats = [first_format if n % 2 == 0 else ('single' if first_format == 'carousel' else 'carousel')
               for n in range(target)]
    candidates, held = [], []
    directories = set()
    for entry in entries:
        directory = Path(entry['directory']).resolve()
        if directory in directories:
            raise ValueError('Do not repeat a project in editorial stock')
        directories.add(directory)
        try:
            content = prepare_story_content(directory)
            if not is_editorial_content(content):
                raise ValueError('Editorial v2 delivery required')
        except (ValueError, OSError) as error:
            held.append(dict(id=entry['id'], reason='CURRENT_REVIEW_REQUIRED', error_type=type(error).__name__))
            continue
        candidates.append((entry, directory, content))
    selected, used = [], set(excluded_ids)
    for format_name in formats:
        match = next(((entry, directory, content) for entry, directory, content in candidates
                      if content.publication_format == format_name and not used.intersection(content.publication_ids)), None)
        if match is None:
            continue
        entry, directory, content = match
        used.update(content.publication_ids)
        selected.append(dict(id=entry['id'], directory=str(directory), publication_format=format_name,
                             title=content.story_delivery.public_title,
                             revision=content.story_delivery.reviewed_revision,
                             digest=content.story_delivery.reviewed_digest))
    missing = {name: formats.count(name) - sum(row['publication_format'] == name for row in selected)
               for name in ('single', 'carousel')}
    return dict(ready=not any(missing.values()), content_mode='editorial', target=target,
                first_format=first_format, selected=selected, held=held, missing=missing,
                reason='READY' if not any(missing.values()) else 'EDITORIAL_REVIEW_REQUIRED')
