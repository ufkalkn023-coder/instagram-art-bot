"""Art-history On-This-Day anniversary radar & Split-Screen Before/After comparison renderer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFilter

from src.astra_renderer import AstraArtworkInput
from src.neon_kit import (
    F,
    GOLD,
    INK,
    MUTED_LIGHT,
    OBSIDIAN,
    PURE_WHITE,
    SOFT_IVORY,
    cover_crop,
    draw_brand_tag,
    draw_fit_text,
    load_source_image,
    short_museum,
)


@dataclass(frozen=True)
class ArtAnniversary:
    month: int
    day: int
    artist: str
    year: int
    event_type: Literal["birth", "death", "exhibition"]
    headline: str

    def format_badge(self) -> str:
        kind_label = {"birth": "Birthday", "death": "Anniversary", "exhibition": "Milestone"}[self.event_type]
        return f"On This Day ({self.day:02d}/{self.month:02d}): {self.artist} {kind_label} ({self.year}) — {self.headline}"


# Curated museum-verified calendar of iconic art-history dates across all 12 months
ART_HISTORY_CALENDAR: tuple[ArtAnniversary, ...] = (
    ArtAnniversary(1, 19, "Paul Cézanne", 1839, "birth", "Father of modern geometric painting"),
    ArtAnniversary(1, 23, "Édouard Manet", 1832, "birth", "Bridge from Realism to Impressionism"),
    ArtAnniversary(2, 6, "Gustav Klimt", 1918, "death", "Master of Vienna Secession gold leaf"),
    ArtAnniversary(2, 25, "Pierre-Auguste Renoir", 1841, "birth", "Luminous Impressionist warmth"),
    ArtAnniversary(3, 1, "Sandro Botticelli", 1445, "birth", "Florentine Renaissance grace"),
    ArtAnniversary(3, 6, "Michelangelo", 1475, "birth", "High Renaissance sculpture & fresco"),
    ArtAnniversary(3, 30, "Vincent van Gogh", 1853, "birth", "Expressive impasto & night skies"),
    ArtAnniversary(3, 30, "Francisco Goya", 1746, "birth", "Dramatic chiaroscuro & court portraiture"),
    ArtAnniversary(4, 6, "Raphael", 1483, "birth", "Harmony of High Renaissance composition"),
    ArtAnniversary(4, 15, "Leonardo da Vinci", 1452, "birth", "Sfumato & Renaissance polymath"),
    ArtAnniversary(4, 15, "First Impressionist Exhibition", 1874, "exhibition", "Paris salon rebels redefine modern light"),
    ArtAnniversary(4, 23, "J. M. W. Turner", 1775, "birth", "Painter of atmospheric storms & golden mist"),
    ArtAnniversary(5, 10, "Katsushika Hokusai", 1849, "death", "Master of ukiyo-e woodblock rhythm"),
    ArtAnniversary(5, 22, "Mary Cassatt", 1844, "birth", "Intimate Impressionist domestic scenes"),
    ArtAnniversary(6, 6, "Diego Velázquez", 1599, "birth", "Spanish Golden Age illusion & depth"),
    ArtAnniversary(6, 11, "John Constable", 1776, "birth", "English landscape clouds & pastoral light"),
    ArtAnniversary(6, 28, "Peter Paul Rubens", 1577, "birth", "Baroque dynamism & rich vermilion"),
    ArtAnniversary(7, 14, "Gustav Klimt", 1862, "birth", "Gilded ornamentation & Viennese modernism"),
    ArtAnniversary(7, 15, "Rembrandt van Rijn", 1606, "birth", "Dutch Golden Age chiaroscuro & humanity"),
    ArtAnniversary(7, 19, "Edgar Degas", 1834, "birth", "Ballet movement & pastel framing"),
    ArtAnniversary(7, 22, "Edward Hopper", 1882, "birth", "Quiet American architectural solitude"),
    ArtAnniversary(7, 29, "Vincent van Gogh", 1890, "death", "Auvers-sur-Oise final canvases"),
    ArtAnniversary(8, 5, "Ilya Repin", 1844, "birth", "Russian Realist psychological depth"),
    ArtAnniversary(8, 9, "Hieronymus Bosch", 1516, "death", "Visionary Northern Renaissance panels"),
    ArtAnniversary(9, 5, "Caspar David Friedrich", 1774, "birth", "German Romantic sublime landscapes"),
    ArtAnniversary(9, 29, "Caravaggio", 1571, "birth", "Tenebrism & theatrical spotlight"),
    ArtAnniversary(10, 4, "Rembrandt van Rijn", 1669, "death", "Master of shadow & introspection"),
    ArtAnniversary(10, 12, "Utagawa Hiroshige", 1858, "death", "Poetic rain & snow woodblocks"),
    ArtAnniversary(10, 31, "Johannes Vermeer", 1632, "birth", "Quiet Delft interiors & pearl light"),
    ArtAnniversary(11, 14, "Claude Monet", 1840, "birth", "Water lilies, fog & shifting daylight"),
    ArtAnniversary(11, 24, "Henri de Toulouse-Lautrec", 1864, "birth", "Montmartre lithographs & graphic silhouettes"),
    ArtAnniversary(12, 5, "Claude Monet", 1926, "death", "Giverny's enduring color legacy"),
    ArtAnniversary(12, 15, "Johannes Vermeer", 1675, "death", "Legacy of stillness in Dutch painting"),
)


def find_anniversaries_for_date(target_date: date) -> list[ArtAnniversary]:
    """Return all curated art-history anniversaries falling on the given month/day."""
    return [item for item in ART_HISTORY_CALENDAR if item.month == target_date.month and item.day == target_date.day]


def match_anniversary_artist(anniversary: ArtAnniversary, artist_name: str) -> bool:
    """Fuzzy surname match to prioritize an anniversary artist when available in the local pool."""
    surname = anniversary.artist.split()[-1].lower()
    return len(surname) >= 4 and surname in (artist_name or "").lower()


def render_split_screen_comparison(
    left_artwork: AstraArtworkInput,
    right_artwork: AstraArtworkInput,
    output_path: str | Path,
    *,
    mode: Literal["study_vs_final", "before_after_restoration"] = "study_vs_final",
    editorial_title: str = "",
) -> Path:
    """Render a 1080x1350 museum-grade vertical split-screen comparison (Study vs Final / Before vs After)."""
    W, H = 1080, 1350
    left_label = "PRELIMINARY STUDY" if mode == "study_vs_final" else "BEFORE RESTORATION"
    right_label = "FINAL CANVAS" if mode == "study_vs_final" else "AFTER RESTORATION"
    kicker = "STUDY VS. FINAL" if mode == "study_vs_final" else "CONSERVATION COMPARE"

    canvas = Image.new("RGB", (W, H), OBSIDIAN)
    left_im = cover_crop(load_source_image(left_artwork.image_path), W, H)
    right_im = cover_crop(load_source_image(right_artwork.image_path), W, H)

    # Slight warm archival desaturation on left side for visual distinction in restoration/study view
    if mode == "study_vs_final":
        left_gray = left_im.convert("L").convert("RGB")
        left_im = Image.blend(left_im, left_gray, 0.35)

    mid_x = W // 2
    canvas.paste(left_im.crop((0, 0, mid_x, H)), (0, 0))
    canvas.paste(right_im.crop((mid_x, 0, W, H)), (mid_x, 0))

    # Top and bottom subtle dark gradients for crisp readability
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for y in range(200):
        alpha = int(175 * (1.0 - y / 200))
        sd.line((0, y, W, y), fill=(12, 12, 11, alpha))
    for y in range(H - 290, H):
        ratio = (y - (H - 290)) / 290
        alpha = int(215 * ratio)
        sd.line((0, y, W, y), fill=(12, 12, 11, alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    # Crisp vertical split divider + gallery slider handle
    draw.line((mid_x, 0, mid_x, H), fill=SOFT_IVORY, width=4)
    handle_y = H // 2
    draw.ellipse((mid_x - 30, handle_y - 30, mid_x + 30, handle_y + 30), fill=OBSIDIAN, outline=SOFT_IVORY, width=3)
    draw.polygon([(mid_x - 16, handle_y), (mid_x - 6, handle_y - 9), (mid_x - 6, handle_y + 9)], fill=SOFT_IVORY)
    draw.polygon([(mid_x + 16, handle_y), (mid_x + 6, handle_y - 9), (mid_x + 6, handle_y + 9)], fill=SOFT_IVORY)

    # Top kicker & brand tag
    draw.text((48, 44), kicker, font=F("sans_b", 18), fill=GOLD)
    draw_brand_tag(draw, W - 260, 44, MUTED_LIGHT, "ART.FOLIO")

    # Left / Right comparison pills
    pill_f = F("sans_b", 17)
    for px, lbl, bg in [(48, left_label, (28, 28, 26)), (mid_x + 28, right_label, (169, 108, 34))]:
        tw = int(draw.textlength(lbl, font=pill_f))
        draw.rounded_rectangle((px, 104, px + tw + 28, 142), radius=19, fill=bg, outline=SOFT_IVORY, width=1)
        draw.text((px + 14, 113), lbl, font=pill_f, fill=PURE_WHITE)

    # Bottom title & dual credit line
    title_text = editorial_title or f"{left_artwork.title} — Split Study"
    draw_fit_text(draw, title_text, (48, H - 205), "didot", 50, W - 96, PURE_WHITE, max_lines=2)
    credit = f"{left_artwork.artist} · {short_museum(left_artwork.institution or right_artwork.institution)} · Swipe for full panels"
    draw.text((48, H - 86), credit[:88], font=F("sans", 20), fill=MUTED_LIGHT)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="JPEG", quality=93, subsampling=0)
    return out
