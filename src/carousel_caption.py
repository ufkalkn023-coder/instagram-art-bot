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
) -> str:
    """Combine Gemini editorial copy with the application-owned featured works list.

    ``featured_artworks`` is deliberately the only input accepted for the list. This
    keeps a future editorial-cover artwork outside the Featured Works contract.
    """
    featured_works = format_featured_works(featured_artworks)
    return (
        f"{theme_title}\n\n"
        f"{editorial_intro}\n\n"
        f"Featured Works\n\n"
        f"{featured_works}\n\n"
        f"{hashtags}"
    )
