"""Deterministic formatting for carousel captions."""

from collections.abc import Mapping, Sequence


def format_featured_works(artworks: Sequence[Mapping[str, object]]) -> str:
    """Format only the selected carousel artworks, preserving source metadata."""
    entries = []
    for index, artwork in enumerate(artworks, start=1):
        entries.append(
            f"{index}. {artwork['title']} — {artwork['artist']}, {artwork['date']}\n"
            f"   {artwork['museum']}"
        )
    return "\n".join(entries)


def format_carousel_caption(
    *,
    theme_title: str,
    editorial_intro: str,
    hashtags: str,
    featured_artworks: Sequence[Mapping[str, object]],
    cover_artwork: Mapping[str, object] | None = None,
) -> str:
    """Combine editorial copy with deterministic cover and featured credits."""
    featured_works = format_featured_works(featured_artworks)
    cover_credit = ""
    if cover_artwork is not None:
        cover_id = cover_artwork.get("id")
        if cover_id is None or not any(item.get("id") == cover_id for item in featured_artworks):
            def metadata(name: str, fallback: str) -> str:
                value = cover_artwork.get(name)
                return str(value).strip() if value is not None and str(value).strip() else fallback

            title = metadata("title", "Untitled work")
            artist = metadata("artist", "Artist unknown")
            date = metadata("date", "")
            museum = metadata("museum", metadata("source", "Source unknown"))
            date_credit = f", {date}" if date else ""
            cover_credit = f"Cover Artwork\n\n{title} — {artist}{date_credit}\n{museum}\n\n"
    return (
        f"{theme_title}\n\n"
        f"{editorial_intro}\n\n"
        f"{cover_credit}"
        f"Featured Works\n\n"
        f"{featured_works}\n\n"
        f"{hashtags}"
    )
