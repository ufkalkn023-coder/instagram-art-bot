"""Neon collection Boards 1-9 (neon-001..neon-090): 90 distinct artwork-driven 1080x1350 layouts."""

from __future__ import annotations

from typing import Sequence

from src.neon_kit import (
    CHARCOAL, DEEP_NIGHT, GOLD, H, INK, MUTED_INK, MUTED_LIGHT, NEWSPRINT,
    OBSIDIAN, PAPER, PURE_WHITE, RULE_DARK, RULE_LIGHT, SOFT_IVORY, VELLUM,
    VERMILION, W, AstraArtworkInput, F, Image, ImageDraw, ImageEnhance,
    ImageFilter, ImageOps, add_shadow, apply_scrim, byline_for,
    compute_image_metrics, contain_fit, cover_crop, draw_brand_tag,
    draw_fit_text, duotone_image, extract_pigments, extract_vivid_accent,
    format_duet_artists_line, hexcol, ink, load_source_image, make_noise_surface, math, mix, np,
    parse_year, paste_centered, short_museum,
)


def _palette(pil: Image.Image, n: int = 5) -> list[tuple[int, int, int]]:
    return [c for c, _ in extract_pigments(pil, n)]


def _draw_footer(draw: ImageDraw.ImageDraw, art: AstraArtworkInput, y: int = 1264, fill=MUTED_LIGHT, align: str = "left") -> None:
    draw_fit_text(draw, byline_for(art), (64, y), "mono", 20, W - 128, fill, min_size=14, max_lines=1, align=align)


def _board_1(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    accent = extract_vivid_accent(pil)
    if num == 1:  # The Curatorial Aperture
        c = Image.new("RGB", (W, H), OBSIDIAN)
        paste_centered(c, pil, (96, 176, W - 96, 980), shadow=True)
        d = ImageDraw.Draw(c)
        d.rectangle((72, 152, W - 72, 1004), outline=GOLD, width=2)
        draw_brand_tag(d, 72, 64, GOLD, "ART.FOLIO  //  CURATORIAL APERTURE")
        draw_fit_text(d, title, (72, 1044), "didot", 58, W - 144, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_LIGHT)
        return c
    if num == 2:  # Macro Gaze + Smart Scrim
        c = cover_crop(pil, W, H, focus=focus, zoom=1.65)
        apply_scrim(c, "bottom", 0.46, (12, 10, 9), 235)
        apply_scrim(c, "top", 0.18, (12, 10, 9), 150)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD)
        draw_fit_text(d, title, (64, 920), "didot", 74, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1188, SOFT_IVORY)
        return c
    if num == 3:  # Swiss Museum Poster
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, W, 116), fill=INK)
        draw_brand_tag(d, 64, 44, PAPER, "ART.FOLIO  //  SWISS EXHIBITION")
        d.text((W - 220, 44), str(parse_year(art.date) or "ARCHIVE"), font=F("mono_b", 22), fill=GOLD)
        draw_fit_text(d, title.upper(), (64, 152), "din_b", 82, W - 128, INK, max_lines=2)
        d.line((64, 358, W - 64, 358), fill=VERMILION, width=4)
        c.paste(cover_crop(pil, W - 128, 790, focus=focus), (64, 390))
        _draw_footer(d, art, 1220, MUTED_INK)
        return c
    if num == 4:  # Belly-Band Triptych (3 sources)
        c = Image.new("RGB", (W, H), CHARCOAL)
        col_w = W // 3
        for idx in range(3):
            c.paste(cover_crop(pils[idx], col_w, H, focus=sources[idx].focal_point()), (idx * col_w, 0))
            if idx > 0:
                ImageDraw.Draw(c).line((idx * col_w, 0, idx * col_w, H), fill=PAPER, width=4)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 520, W, 760), fill=PAPER)
        d.line((0, 520, W, 520), fill=INK, width=3)
        d.line((0, 760, W, 760), fill=INK, width=3)
        draw_fit_text(d, title, (64, 558), "didot", 60, W - 128, INK, max_lines=2, align="center")
        names = "  ·  ".join(s.artist for s in sources[:3])
        draw_fit_text(d, names, (64, 704), "mono", 20, W - 128, MUTED_INK, max_lines=1, align="center")
        return c
    if num == 5:  # Asymmetric Pigment Split
        bg = pal[0]
        fg = PURE_WHITE if ink(bg) != "#1a1915" else INK
        c = Image.new("RGB", (W, H), bg)
        c.paste(cover_crop(pil, 660, 990, focus=focus), (420, 120))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 48, 56, GOLD)
        draw_fit_text(d, title, (48, 220), "serif", 58, 340, fg, max_lines=4)
        for j, sw in enumerate(pal):
            d.rectangle((48, 720 + j * 64, 240, 774 + j * 64), fill=sw)
            d.text((254, 734 + j * 64), hexcol(sw), font=F("mono", 18), fill=fg)
        _draw_footer(d, art, 1236, fg)
        return c
    if num == 6:  # Typographic Ghost Layer
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        ghost = mix(VELLUM, INK, 0.14)
        word = (title.split()[0] if title.split() else "ART").upper()
        for row in range(4):
            d.text((28, 80 + row * 300), f"{word}  {word}", font=F("din_c", 220), fill=ghost)
        paste_centered(c, pil, (110, 210, W - 110, 990), shadow=True)
        draw_brand_tag(d, 64, 48, INK)
        draw_fit_text(d, title, (64, 1044), "serif", 56, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_INK)
        return c
    if num == 7:  # Archival Index Card
        c = make_noise_surface(W, H, PAPER, amp=7, seed=7)
        d = ImageDraw.Draw(c)
        for y in range(210, 1180, 62):
            d.line((64, y, W - 64, y), fill=(214, 206, 192), width=1)
        d.line((64, 206, W - 64, 206), fill=VERMILION, width=3)
        draw_brand_tag(d, 64, 62, INK, "ARCHIVE CARD  //  ART.FOLIO")
        draw_fit_text(d, title, (64, 114), "mono_b", 36, W - 128, INK, max_lines=2)
        paste_centered(c, pil, (96, 246, W - 96, 940), shadow=True)
        draw_fit_text(d, f"ARTIST: {art.artist}", (64, 986), "mono_b", 22, W - 128, INK, max_lines=1)
        draw_fit_text(d, f"WORK: {art.title} ({art.date or 'n.d.'})", (64, 1046), "mono", 21, W - 128, CHARCOAL, max_lines=2)
        draw_fit_text(d, f"COLLECTION: {short_museum(art.institution)}", (64, 1138), "mono", 19, W - 128, MUTED_INK, max_lines=1)
        return c
    if num == 8:  # Chromatic Swatch Header
        c = Image.new("RGB", (W, H), PURE_WHITE)
        d = ImageDraw.Draw(c)
        sw_w = W // 5
        for j, sw in enumerate(pal):
            d.rectangle((j * sw_w, 0, (j + 1) * sw_w + 2, 168), fill=sw)
            d.text((j * sw_w + 18, 126), hexcol(sw), font=F("mono_b", 18), fill=PURE_WHITE if ink(sw) != "#1a1915" else INK)
        paste_centered(c, pil, (72, 224, W - 72, 996), shadow=True)
        draw_fit_text(d, title, (72, 1042), "serif", 54, W - 144, INK, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_INK)
        return c
    if num == 9:  # Conservator's Loupe
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        dim = ImageEnhance.Brightness(cover_crop(pil, W, H, focus=focus)).enhance(0.36)
        c.paste(dim, (0, 0))
        cx, cy, r = W // 2, 560, 290
        zoom_crop = cover_crop(pil, r * 2, r * 2, focus=focus, zoom=2.4)
        mask = Image.new("L", (r * 2, r * 2), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, r * 2, r * 2), fill=255)
        c.paste(zoom_crop, (cx - r, cy - r), mask)
        d = ImageDraw.Draw(c)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=GOLD, width=6)
        d.line((cx + 210, cy + 210, cx + 340, cy + 340), fill=GOLD, width=14)
        draw_brand_tag(d, 64, 54, GOLD, "CONSERVATOR'S LOUPE")
        draw_fit_text(d, title, (64, 960), "didot", 58, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1220, MUTED_LIGHT)
        return c
    # num == 10: Two-Beat Mystery Reveal
    c = Image.new("RGB", (W, H), OBSIDIAN)
    c.paste(cover_crop(pil, W - 128, 430, focus=focus, zoom=2.7), (64, 150))
    paste_centered(c, pil, (64, 620, W - 64, 1020))
    d = ImageDraw.Draw(c)
    d.line((64, 600, W - 64, 600), fill=GOLD, width=2)
    draw_brand_tag(d, 64, 58, GOLD, "01 DETAIL  →  02 FULL WORK")
    draw_fit_text(d, title, (64, 1060), "didot", 56, W - 128, SOFT_IVORY, max_lines=2)
    _draw_footer(d, art, 1236, MUTED_LIGHT)
    return c


def _board_2(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 11:  # Spotlight Dimming Mask
        c = ImageEnhance.Brightness(cover_crop(pil, W, H, focus=focus)).enhance(0.28)
        bright = cover_crop(pil, W, H, focus=focus)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).ellipse((210, 230, W - 210, 890), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(42))
        c.paste(bright, (0, 0), mask)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD)
        draw_fit_text(d, title, (64, 980), "didot", 62, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1220, SOFT_IVORY)
        return c
    if num == 12:  # Numbered Anatomy Pins
        c = Image.new("RGB", (W, H), PAPER)
        box = paste_centered(c, pil, (64, 160, W - 64, 960), shadow=True)
        d = ImageDraw.Draw(c)
        pts = [
            (int(box[0] + (box[2] - box[0]) * 0.28), int(box[1] + (box[3] - box[1]) * 0.34)),
            (int(box[0] + (box[2] - box[0]) * 0.56), int(box[1] + (box[3] - box[1]) * 0.52)),
            (int(box[0] + (box[2] - box[0]) * 0.74), int(box[1] + (box[3] - box[1]) * 0.72)),
        ]
        for idx, (px, py) in enumerate(pts, 1):
            d.ellipse((px - 26, py - 26, px + 26, py + 26), fill=VERMILION, outline=PURE_WHITE, width=3)
            d.text((px - 8, py - 14), str(idx), font=F("mono_b", 24), fill=PURE_WHITE)
        draw_brand_tag(d, 64, 56, INK, "ANATOMY PINS  //  01 · 02 · 03")
        draw_fit_text(d, title, (64, 1020), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    if num == 13:  # Brushstroke 1:1 Strip
        c = Image.new("RGB", (W, H), CHARCOAL)
        for j, fy in enumerate((0.28, 0.52, 0.74)):
            c.paste(cover_crop(pil, W - 128, 236, focus=(focus[0], fy), zoom=2.8), (64, 150 + j * 260))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD, "1:1 BRUSHWORK SURFACE")
        draw_fit_text(d, title, (64, 966), "didot", 58, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_LIGHT)
        return c
    if num == 14:  # Compositional Sightlines
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        box = paste_centered(c, pil, (64, 140, W - 64, 950))
        d = ImageDraw.Draw(c)
        x0, y0, x1, y1 = box
        d.line((x0, y0, x1, y1), fill=GOLD, width=2)
        d.line((x1, y0, x0, y1), fill=GOLD, width=2)
        d.line((x0, (y0 + y1) // 2, x1, (y0 + y1) // 2), fill=(255, 255, 255), width=1)
        d.rectangle(box, outline=GOLD, width=2)
        draw_brand_tag(d, 64, 52, GOLD, "COMPOSITIONAL SIGHTLINES")
        draw_fit_text(d, title, (64, 1004), "serif", 56, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_LIGHT)
        return c
    if num == 15:  # Mini-Map Locator Detail
        c = cover_crop(pil, W, H, focus=focus, zoom=2.5)
        apply_scrim(c, "bottom", 0.36, (16, 14, 12), 235)
        mini = contain_fit(pil, 240, 200)
        d = ImageDraw.Draw(c)
        mx, my = W - mini.width - 64, 64
        d.rectangle((mx - 10, my - 10, mx + mini.width + 10, my + mini.height + 10), fill=OBSIDIAN, outline=GOLD, width=2)
        c.paste(mini, (mx, my))
        rx = mx + int(focus[0] * mini.width) - 24
        ry = my + int(focus[1] * mini.height) - 24
        d.rectangle((rx, ry, rx + 48, ry + 48), outline=VERMILION, width=3)
        draw_brand_tag(d, 64, 64, PURE_WHITE, "LOCATOR DETAIL")
        draw_fit_text(d, title, (64, 980), "didot", 60, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1228, SOFT_IVORY)
        return c
    if num == 16:  # Iconography Decoder Card
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        d.rectangle((48, 48, W - 48, H - 48), outline=INK, width=2)
        c.paste(cover_crop(pil, W - 160, 620, focus=focus, zoom=1.8), (80, 150))
        draw_brand_tag(d, 80, 82, VERMILION, "ICONOGRAPHY DECODER")
        draw_fit_text(d, title, (80, 816), "serif", 54, W - 160, INK, max_lines=2)
        d.line((80, 996, W - 80, 996), fill=RULE_LIGHT, width=2)
        draw_fit_text(d, f"WORK: {art.title}", (80, 1028), "mono_b", 22, W - 160, INK, max_lines=2)
        _draw_footer(d, art, 1210, MUTED_INK)
        return c
    if num == 17:  # Museum Wall Placard
        c = Image.new("RGB", (W, H), (234, 229, 218))
        paste_centered(c, pil, (96, 120, W - 96, 860), shadow=True)
        d = ImageDraw.Draw(c)
        d.rectangle((140, 926, W - 140, 1224), fill=PURE_WHITE, outline=RULE_LIGHT, width=2)
        draw_fit_text(d, art.artist, (180, 962), "sans", 24, W - 360, MUTED_INK, max_lines=1)
        draw_fit_text(d, title, (180, 1002), "serif", 42, W - 360, INK, max_lines=2)
        draw_fit_text(d, f"{art.date or 'Archive'}  ·  {short_museum(art.institution)}", (180, 1144), "mono", 20, W - 360, MUTED_INK, max_lines=1)
        return c
    if num == 18:  # Dark Vault Chiaroscuro
        c = Image.new("RGB", (W, H), (10, 9, 8))
        paste_centered(c, pil, (84, 156, W - 84, 960))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 84, 62, GOLD, "DARK VAULT")
        draw_fit_text(d, title, (84, 1018), "didot", 58, W - 168, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1226, MUTED_LIGHT)
        return c
    if num == 19:  # Phaidon Margin-Note Page
        c = Image.new("RGB", (W, H), PURE_WHITE)
        c.paste(cover_crop(pil, 700, 980, focus=focus), (316, 100))
        d = ImageDraw.Draw(c)
        d.line((284, 100, 284, 1080), fill=RULE_LIGHT, width=2)
        draw_brand_tag(d, 56, 104, VERMILION, "PLATE")
        draw_fit_text(d, title, (56, 170), "serif", 36, 206, INK, max_lines=5)
        draw_fit_text(d, byline_for(art), (56, 860), "mono", 17, 206, MUTED_INK, max_lines=6)
        _draw_footer(d, art, 1216, MUTED_INK)
        return c
    # num == 20: Safe-Zone Cinema Slide
    c = Image.new("RGB", (W, H), (16, 18, 22))
    c.paste(cover_crop(pil, W, 620, focus=focus), (0, 280))
    d = ImageDraw.Draw(c)
    d.line((64, 250, W - 64, 250), fill=RULE_DARK, width=2)
    d.line((64, 930, W - 64, 930), fill=RULE_DARK, width=2)
    draw_brand_tag(d, 64, 72, GOLD, "CINEMA SAFE-ZONE")
    draw_fit_text(d, title, (64, 978), "didot", 58, W - 128, PURE_WHITE, max_lines=2, align="center")
    _draw_footer(d, art, 1210, MUTED_LIGHT, align="center")
    return c


def _board_3(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 21:  # Adaptive Edge Mat-Board
        bg = mix(pal[2], PAPER, 0.55)
        fg = INK if ink(bg) == "#1a1915" else PURE_WHITE
        c = Image.new("RGB", (W, H), bg)
        paste_centered(c, pil, (110, 170, W - 110, 960), shadow=True)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 72, 64, fg)
        draw_fit_text(d, title, (72, 1020), "serif", 54, W - 144, fg, max_lines=2)
        _draw_footer(d, art, 1230, fg)
        return c
    if num == 22:  # Top-Band Archival Ledger
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, W, 250), fill=OBSIDIAN)
        draw_brand_tag(d, 64, 46, GOLD, "ARCHIVAL LEDGER")
        draw_fit_text(d, title, (64, 94), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        paste_centered(c, pil, (64, 290, W - 64, 1180), shadow=True)
        _draw_footer(d, art, 1244, MUTED_INK)
        return c
    if num == 23:  # Smart Quiet-Corner Tag
        c = cover_crop(pil, W, H, focus=focus)
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((54, 1040, 740, 1276), radius=14, fill=(20, 18, 16))
        draw_fit_text(d, title, (82, 1068), "serif", 42, 626, SOFT_IVORY, max_lines=2)
        draw_fit_text(d, byline_for(art), (82, 1206), "mono", 18, 626, GOLD, max_lines=1)
        return c
    if num == 24:  # Museum Excerpt Diptych
        c = Image.new("RGB", (W, H), VELLUM)
        c.paste(cover_crop(pil, W // 2 - 44, 860, focus=focus), (48, 160))
        d = ImageDraw.Draw(c)
        d.rectangle((W // 2 + 12, 160, W - 48, 1020), fill=PURE_WHITE, outline=RULE_LIGHT, width=2)
        draw_brand_tag(d, W // 2 + 44, 206, VERMILION, "CURATORIAL NOTE")
        draw_fit_text(d, title, (W // 2 + 44, 268), "serif", 44, W // 2 - 124, INK, max_lines=4)
        draw_fit_text(d, f"{art.title}\n{art.artist} ({art.date or 'Archive'})", (W // 2 + 44, 780), "mono", 20, W // 2 - 124, MUTED_INK, max_lines=5)
        _draw_footer(d, art, 1220, MUTED_INK)
        return c
    if num == 25:  # Centuries Apart Split (2 sources)
        art_b, pil_b = sources[1], pils[1]
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W // 2 - 6, 940, focus=art.focal_point()), (0, 120))
        c.paste(cover_crop(pil_b, W // 2 - 6, 940, focus=art_b.focal_point()), (W // 2 + 6, 120))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 48, 48, GOLD, "CENTURIES APART")
        draw_fit_text(d, title, (48, 1094), "didot", 54, W - 96, SOFT_IVORY, max_lines=2, align="center")
        draw_fit_text(d, format_duet_artists_line(art, art_b, sep="  vs  ", include_dates=True), (48, 1246), "mono", 19, W - 96, MUTED_LIGHT, max_lines=1, align="center")
        return c
    if num == 26:  # Cinema Before Film Echo
        c = Image.new("RGB", (W, H), (14, 16, 20))
        c.paste(cover_crop(pil, W, 460, focus=focus, zoom=1.3), (0, 170))
        c.paste(cover_crop(pil, W, 320, focus=focus, zoom=2.2), (0, 650))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 58, GOLD, "CINEMA BEFORE FILM  //  2.39:1")
        draw_fit_text(d, title, (64, 1020), "didot", 56, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_LIGHT)
        return c
    if num == 27:  # Four Centuries 2x2 Grid (4 sources)
        c = Image.new("RGB", (W, H), OBSIDIAN)
        qw, qh = (W - 144) // 2, 430
        d = ImageDraw.Draw(c)
        for idx in range(4):
            r, col = divmod(idx, 2)
            x, y = 56 + col * (qw + 32), 144 + r * (qh + 56)
            c.paste(cover_crop(pils[idx], qw, qh, focus=sources[idx].focal_point()), (x, y))
            draw_fit_text(d, f"{sources[idx].artist} · {sources[idx].date}", (x, y + qh + 10), "mono", 17, qw, MUTED_LIGHT, max_lines=1)
        draw_brand_tag(d, 56, 54, GOLD, "FOUR CENTURIES  //  2×2 STUDY")
        draw_fit_text(d, title, (56, 1146), "didot", 52, W - 112, SOFT_IVORY, max_lines=2)
        return c
    if num == 28:  # Early vs Late Masterwork (2 sources)
        art_b, pil_b = sources[1], pils[1]
        c = Image.new("RGB", (W, H), PAPER)
        c.paste(cover_crop(pil, W - 128, 430, focus=art.focal_point()), (64, 150))
        c.paste(cover_crop(pil_b, W - 128, 430, focus=art_b.focal_point()), (64, 610))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, VERMILION, "EARLY  //  LATE")
        draw_fit_text(d, title, (64, 1084), "serif", 52, W - 128, INK, max_lines=2)
        draw_fit_text(d, format_duet_artists_line(art, art_b, sep="  /  ", include_dates=True), (64, 1236), "mono", 19, W - 128, MUTED_INK, max_lines=1)
        return c
    if num == 29:  # First Glance / Closer Look
        c = Image.new("RGB", (W, H), CHARCOAL)
        c.paste(cover_crop(pil, W // 2 - 44, 860, focus=focus), (48, 160))
        c.paste(cover_crop(pil, W // 2 - 44, 860, focus=focus, zoom=2.6), (W // 2 + 12, 160))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 48, 56, GOLD, "FIRST GLANCE  |  CLOSER LOOK")
        draw_fit_text(d, title, (48, 1068), "didot", 54, W - 96, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_LIGHT)
        return c
    # num == 30: True Museum Scale Check
    c = Image.new("RGB", (W, H), (226, 221, 210))
    paste_centered(c, pil, (170, 180, W - 170, 820), shadow=True)
    d = ImageDraw.Draw(c)
    d.rectangle((0, 960, W, H), fill=(198, 188, 170))
    d.line((170, 850, W - 170, 850), fill=INK, width=2)
    draw_brand_tag(d, 64, 54, INK, "GALLERY SCALE VIEW")
    draw_fit_text(d, title, (64, 1018), "serif", 54, W - 128, INK, max_lines=2)
    _draw_footer(d, art, 1228, CHARCOAL)
    return c


def _board_4(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 31:  # East Meets West Mirror (2 sources)
        art_b, pil_b = sources[1], pils[1]
        c = Image.new("RGB", (W, H), VELLUM)
        paste_centered(c, pil, (48, 180, W // 2 - 18, 960), shadow=True)
        paste_centered(c, pil_b, (W // 2 + 18, 180, W - 48, 960), shadow=True)
        d = ImageDraw.Draw(c)
        d.line((W // 2, 160, W // 2, 980), fill=VERMILION, width=2)
        draw_brand_tag(d, 48, 56, VERMILION, "CROSS-CULTURAL DIALOGUE")
        draw_fit_text(d, title, (48, 1028), "didot", 54, W - 96, INK, max_lines=2, align="center")
        draw_fit_text(d, format_duet_artists_line(art, art_b, sep="  ↔  "), (48, 1230), "mono", 20, W - 96, MUTED_INK, max_lines=1, align="center")
        return c
    if num == 32:  # Proportional Pigment Bar
        pig = extract_pigments(pil, 5)
        total = max(1, sum(cnt for _, cnt in pig))
        c = Image.new("RGB", (W, H), OBSIDIAN)
        paste_centered(c, pil, (64, 130, W - 64, 830))
        d = ImageDraw.Draw(c)
        x = 64.0
        for col, cnt in pig:
            w = (cnt / total) * (W - 128)
            d.rectangle((x, 868, x + w, 996), fill=col)
            if w > 90:
                d.text((x + 12, 920), f"{round(cnt / total * 100)}%", font=F("mono_b", 20), fill=PURE_WHITE if ink(col) != "#1a1915" else INK)
            x += w
        draw_brand_tag(d, 64, 52, GOLD, "PROPORTIONAL PIGMENT BAR")
        draw_fit_text(d, title, (64, 1044), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_LIGHT)
        return c
    if num == 33:  # Temperature Gradient Arc
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        d = ImageDraw.Draw(c)
        for j, col in enumerate(pal):
            d.arc((120 + j * 18, 140 + j * 18, W - 120 - j * 18, 980 - j * 18), 180, 360, fill=col, width=14)
        paste_centered(c, pil, (190, 290, W - 190, 910))
        draw_brand_tag(d, 64, 54, GOLD, "COLOR TEMPERATURE ARC")
        draw_fit_text(d, title, (64, 980), "serif", 56, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1228, MUTED_LIGHT, align="center")
        return c
    if num == 34:  # 3-Tone Chiaroscuro Map
        c = Image.new("RGB", (W, H), PAPER)
        gray = pil.convert("L")
        poster = ImageOps.posterize(gray, 2).convert("RGB")
        c.paste(cover_crop(pil, W - 128, 430, focus=focus), (64, 150))
        c.paste(cover_crop(poster, W - 128, 430, focus=focus), (64, 600))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, INK, "3-TONE CHIAROSCURO VALUE MAP")
        draw_fit_text(d, title, (64, 1070), "serif", 52, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_INK)
        return c
    if num == 35:  # Single-Hue Monograph Spine
        accent = extract_vivid_accent(pil)
        fg = PURE_WHITE if ink(accent) != "#1a1915" else INK
        c = Image.new("RGB", (W, H), accent)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, 140, H), fill=mix(accent, OBSIDIAN, 0.35))
        paste_centered(c, pil, (200, 180, W - 80, 920), shadow=True)
        draw_fit_text(d, title, (200, 978), "didot", 58, W - 280, fg, max_lines=2)
        draw_fit_text(d, byline_for(art), (200, 1200), "mono", 20, W - 280, fg, max_lines=2)
        return c
    if num == 36:  # Physical Medium Specimen
        c = make_noise_surface(W, H, VELLUM, amp=9, seed=36)
        c.paste(cover_crop(pil, 520, 740, focus=focus), (64, 160))
        c.paste(cover_crop(pil, 400, 740, focus=focus, zoom=3.2), (616, 160))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 56, INK, "SURFACE & MEDIUM SPECIMEN")
        draw_fit_text(d, title, (64, 964), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1224, MUTED_INK)
        return c
    if num == 37:  # Nocturne Candlelight Mode
        c = Image.new("RGB", (W, H), (16, 12, 9))
        warm = ImageEnhance.Color(pil).enhance(1.1)
        paste_centered(c, warm, (110, 180, W - 110, 930))
        d = ImageDraw.Draw(c)
        d.rectangle((94, 164, W - 94, 946), outline=(188, 138, 72), width=2)
        draw_brand_tag(d, 94, 62, (232, 188, 118), "NOCTURNE CANDLELIGHT")
        draw_fit_text(d, title, (94, 996), "didot", 58, W - 188, (246, 222, 180), max_lines=2)
        _draw_footer(d, art, 1228, (186, 164, 136))
        return c
    if num == 38:  # Seamless Panorama Split
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W, 820, focus=(0.32, focus[1])), (0, 140))
        d = ImageDraw.Draw(c)
        d.line((W - 4, 140, W - 4, 960), fill=GOLD, width=8)
        draw_brand_tag(d, 64, 54, GOLD, "PANORAMA SLIDE 01  →")
        draw_fit_text(d, title, (64, 1008), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_LIGHT)
        return c
    if num == 39:  # Chronology Timeline Ribbon
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        yr = parse_year(art.date) or 1880
        d.line((64, 180, W - 64, 180), fill=INK, width=3)
        for j, y_mark in enumerate(range(yr - 100, yr + 101, 50)):
            x = 100 + j * 220
            d.line((x, 166, x, 194), fill=VERMILION if y_mark == yr else INK, width=4 if y_mark == yr else 2)
            d.text((x - 28, 126), str(y_mark), font=F("mono_b", 20), fill=VERMILION if y_mark == yr else MUTED_INK)
        paste_centered(c, pil, (84, 240, W - 84, 980), shadow=True)
        draw_fit_text(d, title, (84, 1028), "serif", 54, W - 168, INK, max_lines=2)
        _draw_footer(d, art, 1232, MUTED_INK)
        return c
    # num == 40: 3-Act Curatorial Wall Text
    c = Image.new("RGB", (W, H), (240, 236, 228))
    d = ImageDraw.Draw(c)
    d.rectangle((64, 80, W - 64, 126), fill=INK)
    d.text((92, 92), "ACT I: SEE   ·   ACT II: CONTEXT   ·   ACT III: RETURN", font=F("mono_b", 20), fill=PAPER)
    paste_centered(c, pil, (64, 174, W - 64, 920), shadow=True)
    draw_fit_text(d, title, (64, 968), "didot", 58, W - 128, INK, max_lines=2)
    _draw_footer(d, art, 1228, MUTED_INK)
    return c


def _board_5(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 41:  # Slow Looking Meditation
        c = Image.new("RGB", (W, H), (242, 240, 234))
        paste_centered(c, pil, (190, 260, W - 190, 880), shadow=True)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, "STAY FOR TEN SECONDS", (64, 100), "mono", 22, W - 128, MUTED_INK, max_lines=1, align="center")
        draw_fit_text(d, title, (96, 980), "georgia_i", 50, W - 192, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1228, MUTED_INK, align="center")
        return c
    if num == 42:  # World Museum Passport
        c = Image.new("RGB", (W, H), (28, 42, 56))
        d = ImageDraw.Draw(c)
        d.rectangle((48, 48, W - 48, H - 48), outline=GOLD, width=3)
        paste_centered(c, pil, (110, 210, W - 110, 900))
        d.ellipse((W - 270, 770, W - 90, 950), outline=GOLD, width=4)
        d.text((W - 235, 845), str(parse_year(art.date) or "PASS"), font=F("mono_b", 24), fill=GOLD)
        draw_brand_tag(d, 84, 88, GOLD, f"MUSEUM PASSPORT  //  {short_museum(art.institution)}")
        draw_fit_text(d, title, (84, 970), "cinzel", 48, W - 168, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1216, MUTED_LIGHT)
        return c
    if num == 43:  # Emotion Index Series
        metrics = compute_image_metrics(pil)
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W - 128, 720, focus=focus), (64, 150))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 56, GOLD, "EMOTION & ATMOSPHERE INDEX")
        draw_fit_text(d, title, (64, 912), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        d.text((64, 1104), f"WARMTH {int(metrics['warmth']*100)}%   ·   LIGHT {int(metrics['luminance']*100)}%   ·   DEPTH {int(metrics['contrast']*100)}%", font=F("mono_b", 22), fill=GOLD)
        _draw_footer(d, art, 1226, MUTED_LIGHT)
        return c
    if num == 44:  # Contact-Sheet Outro
        c = Image.new("RGB", (W, H), (18, 18, 17))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 52, GOLD, "CONTACT SHEET  //  6 FRAMES")
        focuses = [(0.2, 0.3), (0.5, 0.3), (0.8, 0.3), (0.2, 0.7), (0.5, 0.7), (0.8, 0.7)]
        cw, ch = 296, 360
        for idx, pt in enumerate(focuses):
            r, col = divmod(idx, 3)
            x, y = 64 + col * (cw + 32), 140 + r * (ch + 58)
            c.paste(cover_crop(pil, cw, ch, focus=pt, zoom=1.8), (x, y))
            d.text((x, y + ch + 8), f"0{idx + 1}A", font=F("mono", 17), fill=MUTED_LIGHT)
        draw_fit_text(d, title, (64, 1020), "serif", 52, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_LIGHT)
        return c
    if num == 45:  # 3-Column Grid Cadence
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        cw = (W - 160) // 3
        variants = [pil, ImageEnhance.Contrast(pil).enhance(1.25), ImageOps.grayscale(pil).convert("RGB")]
        for j, im_v in enumerate(variants):
            c.paste(cover_crop(im_v, cw, 760, focus=focus), (64 + j * (cw + 16), 170))
        draw_brand_tag(d, 64, 56, INK, "3-COLUMN RHYTHM CADENCE")
        draw_fit_text(d, title, (64, 984), "didot", 56, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_INK)
        return c
    if num == 46:  # Collector's Back Index
        c = Image.new("RGB", (W, H), (26, 25, 22))
        d = ImageDraw.Draw(c)
        d.rectangle((54, 54, W - 54, H - 54), outline=RULE_DARK, width=2)
        draw_brand_tag(d, 88, 92, GOLD, "COLLECTOR'S BACK INDEX")
        paste_centered(c, pil, (88, 168, W - 88, 860))
        draw_fit_text(d, title, (88, 910), "serif", 48, W - 176, SOFT_IVORY, max_lines=2)
        draw_fit_text(d, f"CATALOGUE ENTRY · {art.artist} · {art.date or 'Archive'} · {short_museum(art.institution)}", (88, 1180), "mono", 19, W - 176, GOLD, max_lines=2)
        return c
    if num == 47:  # Vertical Catalog Spine
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W - 180, H, focus=focus), (180, 0))
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, 180, H), fill=pal[0])
        lbl = Image.new("RGBA", (1100, 120), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lbl)
        ld.text((40, 26), f"ART.FOLIO  //  {title.upper()[:36]}", font=F("din_b", 44), fill=PURE_WHITE if ink(pal[0]) != "#1a1915" else INK)
        rot = lbl.rotate(90, expand=True)
        c.paste(rot, (32, 120), rot)
        return c
    if num == 48:  # Print-Shop Single Plate
        c = Image.new("RGB", (W, H), PURE_WHITE)
        paste_centered(c, pil, (124, 156, W - 124, 968), shadow=True)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title.upper(), (124, 1020), "cinzel", 44, W - 248, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1184, MUTED_INK, align="center")
        return c
    if num == 49:  # Verified Aphorism Plate
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, f"“{title}”", (96, 150), "georgia_i", 56, W - 192, INK, max_lines=3, align="center")
        d.line((380, 440, W - 380, 440), fill=VERMILION, width=3)
        paste_centered(c, pil, (140, 490, W - 140, 1160), shadow=True)
        _draw_footer(d, art, 1226, MUTED_INK, align="center")
        return c
    # num == 50: Ken Burns Detail-to-Full
    c = cover_crop(pil, W, H, focus=focus, zoom=1.2)
    apply_scrim(c, "bottom", 0.44, (12, 10, 9), 230)
    apply_scrim(c, "top", 0.22, (12, 10, 9), 155)
    d = ImageDraw.Draw(c)
    draw_brand_tag(d, 64, 56, GOLD, "REEL  ·  0:00 → 0:10 ZOOM-OUT")
    d.rectangle((220, 220, 860, 780), outline=PURE_WHITE, width=2)
    draw_fit_text(d, title, (64, 912), "didot", 68, W - 128, PURE_WHITE, max_lines=2, align="center")
    _draw_footer(d, art, 1136, SOFT_IVORY, align="center")
    d.line((180, 1210, W - 180, 1210), fill=(110, 104, 96), width=4)
    d.line((180, 1210, 400, 1210), fill=GOLD, width=4)
    return c


def _board_6(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 51:  # Museum Postage Stamp
        c = Image.new("RGB", (W, H), (34, 42, 46))
        d = ImageDraw.Draw(c)
        sx0, sy0, sx1, sy1 = 110, 130, W - 110, 1120
        d.rectangle((sx0, sy0, sx1, sy1), fill=PAPER)
        for px in range(sx0, sx1 + 1, 36):
            d.ellipse((px - 12, sy0 - 12, px + 12, sy0 + 12), fill=(34, 42, 46))
            d.ellipse((px - 12, sy1 - 12, px + 12, sy1 + 12), fill=(34, 42, 46))
        for py in range(sy0, sy1 + 1, 36):
            d.ellipse((sx0 - 12, py - 12, sx0 + 12, py + 12), fill=(34, 42, 46))
            d.ellipse((sx1 - 12, py - 12, sx1 + 12, py + 12), fill=(34, 42, 46))
        c.paste(cover_crop(pil, sx1 - sx0 - 110, 680, focus=focus), (sx0 + 55, sy0 + 60))
        draw_fit_text(d, title.upper(), (sx0 + 55, sy0 + 784), "cinzel", 44, sx1 - sx0 - 110, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (sx0 + 55, sy1 - 90), "mono", 18, sx1 - sx0 - 110, MUTED_INK, max_lines=1)
        _draw_footer(d, art, 1210, SOFT_IVORY, align="center")
        return c
    if num == 52:  # Vinyl Record Sleeve
        c = Image.new("RGB", (W, H), (26, 24, 23))
        d = ImageDraw.Draw(c)
        d.ellipse((340, 210, 1020, 890), fill=(12, 12, 12), outline=(64, 62, 58), width=3)
        for r in range(280, 140, -28):
            d.ellipse((680 - r, 550 - r, 680 + r, 550 + r), outline=(34, 33, 32), width=1)
        d.ellipse((570, 440, 790, 660), fill=VERMILION)
        d.ellipse((668, 538, 692, 562), fill=(26, 24, 23))
        add_shadow(c, (64, 170, 790, 900), blur=22, alpha=140)
        c.paste(cover_crop(pil, 726, 730, focus=focus), (64, 170))
        draw_brand_tag(d, 64, 64, GOLD, "STEREO ARCHIVE LP  //  33⅓ RPM")
        draw_fit_text(d, title, (64, 964), "didot", 58, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_LIGHT)
        return c
    if num == 53:  # Exhibition Ticket Stub
        c = Image.new("RGB", (W, H), (44, 34, 28))
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((96, 120, W - 96, 1180), radius=18, fill=VELLUM)
        d.ellipse((74, 860, 118, 904), fill=(44, 34, 28))
        d.ellipse((W - 118, 860, W - 74, 904), fill=(44, 34, 28))
        for x in range(132, W - 132, 24):
            d.line((x, 882, x + 12, 882), fill=MUTED_INK, width=2)
        c.paste(cover_crop(pil, W - 264, 660, focus=focus), (132, 166))
        draw_brand_tag(d, 132, 920, VERMILION, "ADMIT ONE  //  ART.FOLIO")
        draw_fit_text(d, title, (132, 966), "serif", 46, W - 264, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (132, 1110), "mono", 18, W - 264, MUTED_INK, max_lines=1)
        return c
    if num == 54:  # 35mm Kodachrome Slide
        c = Image.new("RGB", (W, H), (22, 22, 22))
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((84, 150, W - 84, 1130), radius=24, fill=(238, 234, 224))
        d.rectangle((146, 260, W - 146, 940), fill=OBSIDIAN)
        c.paste(cover_crop(pil, W - 308, 664, focus=focus), (154, 268))
        d.text((150, 194), "KODACHROME ARCHIVE SLIDE", font=F("mono_b", 22), fill=VERMILION)
        d.text((W - 280, 194), str(parse_year(art.date) or "35MM"), font=F("mono_b", 22), fill=INK)
        draw_fit_text(d, title, (150, 974), "mono_b", 34, W - 300, INK, max_lines=2)
        _draw_footer(d, art, 1200, SOFT_IVORY, align="center")
        return c
    if num == 55:  # Wax-Sealed Auction Lot
        c = make_noise_surface(W, H, PAPER, amp=7, seed=55)
        paste_centered(c, pil, (110, 190, W - 110, 920), shadow=True)
        d = ImageDraw.Draw(c)
        d.ellipse((W - 230, 820, W - 96, 954), fill=(154, 34, 30), outline=(112, 20, 18), width=4)
        d.text((W - 196, 872), "LOT", font=F("cinzel", 26), fill=GOLD)
        draw_brand_tag(d, 80, 66, INK, f"AUCTION LOT  //  {parse_year(art.date) or 'EDITION'}")
        draw_fit_text(d, title, (80, 984), "didot", 54, W - 160, INK, max_lines=2)
        _draw_footer(d, art, 1226, MUTED_INK)
        return c
    if num == 56:  # Vertical Tategaki Strip
        c = Image.new("RGB", (W, H), (245, 241, 232))
        c.paste(cover_crop(pil, 720, 1080, focus=focus), (72, 110))
        d = ImageDraw.Draw(c)
        d.line((838, 110, 838, 1190), fill=INK, width=2)
        chars = list(title.upper().replace(" ", "")[:14])
        for idx, ch in enumerate(chars):
            d.text((898, 130 + idx * 66), ch, font=F("cinzel", 44), fill=INK)
        d.rectangle((886, 1100, 956, 1170), outline=VERMILION, width=3)
        d.text((902, 1120), "AF", font=F("mono_b", 26), fill=VERMILION)
        _draw_footer(d, art, 1236, MUTED_INK)
        return c
    if num == 57:  # Asymmetric Ma Silence
        c = Image.new("RGB", (W, H), (246, 243, 236))
        paste_centered(c, pil, (300, 160, W - 64, 840), shadow=True)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 164, MUTED_INK, "MA  //  SILENCE")
        draw_fit_text(d, title, (64, 960), "serif", 50, 640, INK, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_INK)
        return c
    if num == 58:  # L-Shaped Obi & Hanko
        c = cover_crop(pil, W, H, focus=focus)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 980, W, H), fill=PAPER)
        d.rectangle((0, 0, 136, H), fill=PAPER)
        d.rectangle((36, 1040, 100, 1104), fill=VERMILION)
        d.text((52, 1058), "AF", font=F("mono_b", 24), fill=PURE_WHITE)
        draw_fit_text(d, title, (176, 1020), "serif", 54, W - 230, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (176, 1226), "mono", 20, W - 230, MUTED_INK, max_lines=1)
        return c
    if num == 59:  # Shoji Lattice Window
        c = Image.new("RGB", (W, H), VELLUM)
        c.paste(cover_crop(pil, W - 128, 860, focus=focus), (64, 140))
        d = ImageDraw.Draw(c)
        for col in range(1, 3):
            x = 64 + col * ((W - 128) // 3)
            d.line((x, 140, x, 1000), fill=VELLUM, width=16)
        for row in range(1, 3):
            y = 140 + row * (860 // 3)
            d.line((64, y, W - 64, y), fill=VELLUM, width=16)
        d.rectangle((64, 140, W - 64, 1000), outline=INK, width=6)
        draw_fit_text(d, title, (64, 1046), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_INK)
        return c
    # num == 60: Kintsugi Gold Seam (2 sources)
    art_b, pil_b = sources[1], pils[1]
    c = cover_crop(pil, W, H, focus=art.focal_point())
    right = cover_crop(pil_b, W, H, focus=art_b.focal_point())
    seam = [(540, 0), (500, 240), (585, 480), (490, 740), (575, 1010), (520, H)]
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(seam + [(W, H), (W, 0)], fill=255)
    c.paste(right, (0, 0), mask)
    apply_scrim(c, "bottom", 0.32, (14, 12, 10), 225)
    d = ImageDraw.Draw(c)
    d.line(seam, fill=(235, 198, 96), width=10)
    d.line(seam, fill=(255, 240, 178), width=3)
    draw_fit_text(d, title, (64, 1020), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
    draw_fit_text(d, format_duet_artists_line(art, art_b, sep="  ×  "), (64, 1220), "mono", 20, W - 128, GOLD, max_lines=1)
    return c


def _board_7(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 61:  # Bauhaus Primary Poster
        c = Image.new("RGB", (W, H), (242, 236, 220))
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, 160, H), fill=(196, 46, 36))
        d.ellipse((620, 60, 1020, 460), fill=(228, 178, 42))
        d.rectangle((160, 960, W, H), fill=(26, 58, 122))
        c.paste(cover_crop(pil, 720, 740, focus=focus), (220, 170))
        draw_fit_text(d, title.upper(), (220, 1006), "din_b", 66, 800, PURE_WHITE, max_lines=2)
        draw_fit_text(d, byline_for(art), (220, 1220), "mono", 20, 800, (236, 224, 190), max_lines=1)
        return c
    if num == 62:  # Giant Knockout Letter Mask
        c = Image.new("RGB", (W, H), OBSIDIAN)
        word = (title.split()[0] if title.split() else "ART").upper()[:5]
        art_fill = cover_crop(pil, W, 640, focus=focus)
        matte = Image.new("L", (W, 640), 0)
        md = ImageDraw.Draw(matte)
        fs = 420
        while fs > 110 and md.textlength(word, font=F("din_c", fs)) > W - 90:
            fs -= 10
        md.text((W // 2, 320), word, font=F("din_c", fs), fill=255, anchor="mm")
        c.paste(art_fill, (0, 180), matte)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 56, GOLD, "KNOCKOUT LETTER MASK")
        paste_centered(c, pil, (64, 860, 380, 1180))
        draw_fit_text(d, title, (420, 900), "didot", 52, W - 484, SOFT_IVORY, max_lines=3)
        draw_fit_text(d, byline_for(art), (420, 1136), "mono", 18, W - 484, GOLD, max_lines=2)
        return c
    if num == 63:  # Kinetic Echo Poster
        c = Image.new("RGB", (W, H), (22, 20, 18))
        d = ImageDraw.Draw(c)
        for j in range(5):
            shade = mix(GOLD, OBSIDIAN, j * 0.17)
            draw_fit_text(d, title.upper(), (64, 64 + j * 54), "din_b", 64, W - 128, shade, max_lines=1)
        c.paste(cover_crop(pil, W - 128, 760, focus=focus), (64, 390))
        _draw_footer(d, art, 1220, SOFT_IVORY)
        return c
    if num == 64:  # Neo-Brutalist Spec Card
        c = Image.new("RGB", (W, H), (235, 233, 224))
        d = ImageDraw.Draw(c)
        d.rectangle((40, 40, W - 40, H - 40), outline=INK, width=6)
        d.rectangle((40, 40, W - 40, 170), fill=(236, 212, 64), outline=INK, width=6)
        draw_fit_text(d, title.upper(), (68, 68), "din_b", 58, W - 136, INK, max_lines=1)
        c.paste(cover_crop(pil, W - 120, 780, focus=focus), (60, 194))
        d.rectangle((40, 996, W - 40, H - 40), fill=PURE_WHITE, outline=INK, width=6)
        draw_fit_text(d, f"ARTIST // {art.artist}\nWORK // {art.title}\nYEAR // {art.date or 'ARCHIVE'}", (68, 1028), "mono_b", 24, W - 136, INK, max_lines=4)
        return c
    if num == 65:  # Oversized Bleed Numeral
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        yr = str(parse_year(art.date) or "1900")
        d.text((-20, -40), yr, font=F("din_c", 470), fill=(224, 216, 200))
        paste_centered(c, pil, (100, 260, W - 100, 980), shadow=True)
        draw_fit_text(d, title, (72, 1030), "didot", 58, W - 144, INK, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_INK)
        return c
    if num == 66:  # Broadsheet Art Newspaper
        c = make_noise_surface(W, H, NEWSPRINT, amp=8, seed=66)
        d = ImageDraw.Draw(c)
        d.text((W // 2 - 260, 46), "THE ARTFOLIO GAZETTE", font=F("cinzel", 44), fill=INK)
        d.line((54, 114, W - 54, 114), fill=INK, width=4)
        d.line((54, 124, W - 54, 124), fill=INK, width=1)
        draw_fit_text(d, title, (54, 148), "georgia_b", 60, W - 108, INK, max_lines=2, align="center")
        c.paste(cover_crop(pil, W - 108, 720, focus=focus), (54, 340))
        d.line((54, 1086, W - 54, 1086), fill=INK, width=2)
        draw_fit_text(d, f"{art.artist} — {art.title} ({art.date or 'Archive'})", (54, 1110), "georgia_i", 30, W - 108, CHARCOAL, max_lines=2)
        _draw_footer(d, art, 1238, MUTED_INK)
        return c
    if num == 67:  # Criterion Collection Cover
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "bottom", 0.36, (10, 10, 12), 230)
        d = ImageDraw.Draw(c)
        d.arc((54, 54, 146, 146), 35, 325, fill=PURE_WHITE, width=7)
        d.text((84, 84), "AF", font=F("din_b", 30), fill=PURE_WHITE)
        draw_fit_text(d, title.upper(), (64, 1010), "din_b", 64, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1218, SOFT_IVORY)
        return c
    if num == 68:  # High-Fashion Masthead
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "top", 0.24, (14, 12, 12), 185)
        apply_scrim(c, "bottom", 0.30, (14, 12, 12), 215)
        d = ImageDraw.Draw(c)
        d.text((W // 2 - 310, 44), "ARTFOLIO", font=F("didot", 142), fill=PURE_WHITE)
        draw_fit_text(d, title, (64, 1044), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1236, GOLD)
        return c
    if num == 69:  # 35mm Cinema Filmstrip
        c = Image.new("RGB", (W, H), (14, 14, 14))
        d = ImageDraw.Draw(c)
        for y in range(44, H - 44, 62):
            d.rounded_rectangle((22, y, 66, y + 36), radius=6, fill=(224, 214, 192))
            d.rounded_rectangle((W - 66, y, W - 22, y + 36), radius=6, fill=(224, 214, 192))
        c.paste(cover_crop(pil, W - 196, 440, focus=focus), (98, 96))
        c.paste(cover_crop(pil, W - 196, 440, focus=focus, zoom=1.8), (98, 566))
        draw_fit_text(d, title, (98, 1056), "didot", 52, W - 196, SOFT_IVORY, max_lines=2)
        draw_fit_text(d, byline_for(art), (98, 1226), "mono", 18, W - 196, GOLD, max_lines=1)
        return c
    # num == 70: Indie Art Zine Collage
    c = make_noise_surface(W, H, (236, 224, 198), amp=12, seed=70)
    rot = cover_crop(pil, 760, 820, focus=focus).convert("RGBA").rotate(-4, expand=True)
    c.paste(rot, (130, 140), rot)
    d = ImageDraw.Draw(c)
    d.rectangle((64, 960, 820, 1070), fill=VERMILION)
    draw_fit_text(d, title.upper(), (88, 984), "din_b", 52, 708, PURE_WHITE, max_lines=1)
    _draw_footer(d, art, 1180, INK)
    return c


def _board_8(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 7)
    if num == 71:  # Architectural Cyanotype
        c = Image.new("RGB", (W, H), (19, 54, 98))
        d = ImageDraw.Draw(c)
        for x in range(60, W, 60):
            d.line((x, 0, x, H), fill=(36, 78, 128), width=1)
        for y in range(60, H, 60):
            d.line((0, y, W, y), fill=(36, 78, 128), width=1)
        cyano = duotone_image(pil, (14, 42, 82), (230, 244, 255))
        paste_centered(c, cyano, (96, 170, W - 96, 940))
        d.rectangle((80, 154, W - 80, 956), outline=(210, 232, 255), width=2)
        draw_brand_tag(d, 80, 62, (210, 232, 255), "ARCHITECTURAL CYANOTYPE")
        draw_fit_text(d, title, (80, 1008), "mono_b", 46, W - 160, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1224, (190, 218, 248))
        return c
    if num == 72:  # Natural History Herbarium
        c = make_noise_surface(W, H, (239, 232, 212), amp=8, seed=72)
        d = ImageDraw.Draw(c)
        d.rectangle((56, 56, W - 56, H - 56), outline=(128, 114, 92), width=2)
        paste_centered(c, pil, (120, 170, W - 120, 920), shadow=True)
        d.rectangle((140, 972, W - 140, 1226), fill=PAPER, outline=INK, width=2)
        draw_fit_text(d, f"SPECIMEN: {title}", (176, 1006), "georgia_i", 40, W - 352, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (176, 1146), "mono", 18, W - 352, MUTED_INK, max_lines=1)
        return c
    if num == 73:  # Optical 360 Color Wheel
        c = Image.new("RGB", (W, H), OBSIDIAN)
        d = ImageDraw.Draw(c)
        for idx, col in enumerate(pal):
            d.pieslice((150, 160, W - 150, 940), -90 + idx * 360 / 7, -90 + (idx + 1) * 360 / 7, fill=col)
        crop = cover_crop(pil, 520, 520, focus=focus)
        mask = Image.new("L", (520, 520), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 520, 520), fill=255)
        c.paste(crop, (W // 2 - 260, 550 - 260), mask)
        draw_brand_tag(d, 64, 54, GOLD, "OPTICAL 360° COLOR WHEEL")
        draw_fit_text(d, title, (64, 1000), "didot", 56, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1230, MUTED_LIGHT, align="center")
        return c
    if num == 74:  # Golden Ratio Fibonacci
        c = Image.new("RGB", (W, H), (22, 24, 28))
        box = paste_centered(c, pil, (64, 160, W - 64, 960))
        d = ImageDraw.Draw(c)
        x0, y0, x1, y1 = box
        gx = int(x0 + (x1 - x0) * 0.618)
        gy = int(y0 + (y1 - y0) * 0.618)
        d.line((gx, y0, gx, y1), fill=GOLD, width=2)
        d.line((x0, gy, x1, gy), fill=GOLD, width=2)
        d.arc((x0, y0, x1, y1), 180, 360, fill=GOLD, width=3)
        draw_brand_tag(d, 64, 56, GOLD, "φ = 1.618  //  GOLDEN RATIO")
        draw_fit_text(d, title, (64, 1018), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1232, MUTED_LIGHT)
        return c
    if num == 75:  # Coordinate & Distance Card
        c = Image.new("RGB", (W, H), PAPER)
        c.paste(cover_crop(pil, W - 140, 820, focus=focus), (70, 150))
        d = ImageDraw.Draw(c)
        for j in range(1, 5):
            d.line((70 + j * 188, 150, 70 + j * 188, 970), fill=(255, 255, 255), width=1)
            d.line((70, 150 + j * 164, W - 70, 150 + j * 164), fill=(255, 255, 255), width=1)
        draw_brand_tag(d, 70, 56, INK, f"COORDINATES  ·  {short_museum(art.institution)}")
        draw_fit_text(d, title, (70, 1020), "serif", 52, W - 140, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    if num == 76:  # Circular Medallion Monolith
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        crop = cover_crop(pil, 760, 760, focus=focus)
        mask = Image.new("L", (760, 760), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 760, 760), fill=255)
        c.paste(crop, (160, 160), mask)
        d = ImageDraw.Draw(c)
        d.ellipse((150, 150, 930, 930), outline=GOLD, width=3)
        draw_fit_text(d, title, (64, 988), "didot", 60, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1224, MUTED_LIGHT, align="center")
        return c
    if num == 77:  # Diagonal Razor Cut
        c = Image.new("RGB", (W, H), OBSIDIAN)
        crop = cover_crop(pil, W, H, focus=focus)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon([(0, 0), (W, 0), (W, 820), (0, 1040)], fill=255)
        c.paste(crop, (0, 0), mask)
        d = ImageDraw.Draw(c)
        d.line((0, 1040, W, 820), fill=GOLD, width=5)
        draw_fit_text(d, title, (64, 1040), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1238, MUTED_LIGHT)
        return c
    if num == 78:  # Frosted Glassmorphism
        c = cover_crop(pil, W, H, focus=focus)
        blur = c.filter(ImageFilter.GaussianBlur(22))
        card_box = (84, 920, W - 84, 1256)
        frosted = Image.blend(blur.crop(card_box), Image.new("RGB", (W - 168, 336), (250, 248, 244)), 0.45)
        c.paste(frosted, (84, 920))
        d = ImageDraw.Draw(c)
        d.rectangle(card_box, outline=PURE_WHITE, width=2)
        draw_fit_text(d, title, (124, 962), "serif", 52, W - 248, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (124, 1170), "mono", 20, W - 248, CHARCOAL, max_lines=1)
        return c
    if num == 79:  # Risograph Duotone Window
        accent = extract_vivid_accent(pil)
        c = duotone_image(cover_crop(pil, W, H, focus=focus), (28, 34, 72), mix(accent, PAPER, 0.5))
        win = cover_crop(pil, 680, 740, focus=focus)
        c.paste(win, (200, 190))
        d = ImageDraw.Draw(c)
        d.rectangle((200, 190, 880, 930), outline=PURE_WHITE, width=4)
        apply_scrim(c, "bottom", 0.28, (18, 20, 36), 230)
        draw_fit_text(d, title, (64, 1018), "didot", 58, W - 128, PURE_WHITE, max_lines=2, align="center")
        _draw_footer(d, art, 1230, SOFT_IVORY, align="center")
        return c
    # num == 80: Continuous Perimeter Frame
    c = Image.new("RGB", (W, H), OBSIDIAN)
    paste_centered(c, pil, (112, 112, W - 112, H - 112))
    d = ImageDraw.Draw(c)
    d.rectangle((96, 96, W - 96, H - 96), outline=GOLD, width=2)
    draw_fit_text(d, f"ART.FOLIO  ·  {title.upper()}", (96, 36), "mono_b", 22, W - 192, GOLD, max_lines=1, align="center")
    draw_fit_text(d, byline_for(art).upper(), (96, H - 68), "mono", 20, W - 192, SOFT_IVORY, max_lines=1, align="center")
    return c


def _board_9(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 81:  # Gothic Lancet Triptych
        c = Image.new("RGB", (W, H), (22, 20, 19))
        crop = cover_crop(pil, W - 120, 880, focus=focus)
        mask = Image.new("L", (W - 120, 880), 0)
        md = ImageDraw.Draw(mask)
        d = ImageDraw.Draw(c)
        cw = (W - 120 - 56) // 3
        for idx in range(3):
            x0, x1 = idx * (cw + 28), idx * (cw + 28) + cw
            mid = (x0 + x1) // 2
            md.polygon([(x0, 210), (mid, 24), (x1, 210), (x1, 880), (x0, 880)], fill=255)
            d.line((60 + x0, 340, 60 + mid, 154, 60 + x1, 340), fill=GOLD, width=3)
            d.rectangle((60 + x0, 340, 60 + x1, 1010), outline=GOLD, width=2)
        c.paste(crop, (60, 130), mask)
        draw_brand_tag(d, 64, 54, GOLD, "GOTHIC LANCET TRIPTYCH")
        draw_fit_text(d, title, (64, 1056), "cinzel", 48, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1228, MUTED_LIGHT, align="center")
        return c
    if num == 82:  # Keyhole Voyeur Portal
        c = make_noise_surface(W, H, (24, 21, 20), amp=8, seed=82)
        crop = cover_crop(pil, W, H, focus=focus)
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        cx, cy, r = W // 2, 470, 225
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        md.polygon([(cx - 102, cy + 150), (cx + 102, cy + 150), (cx + 185, 950), (cx - 185, 950)], fill=255)
        c.paste(crop, (0, 0), mask)
        d = ImageDraw.Draw(c)
        d.arc((cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6), 120, 60, fill=GOLD, width=5)
        draw_brand_tag(d, 64, 64, GOLD, "KEYHOLE PORTAL")
        draw_fit_text(d, title, (64, 1010), "didot", 62, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1226, MUTED_LIGHT, align="center")
        return c
    if num == 83:  # Venetian Blind Slats
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        crop = cover_crop(pil, W, 920, focus=focus)
        sh = 920 // 9
        for j in range(9):
            c.paste(crop.crop((0, j * sh, W, min(920, (j + 1) * sh - 16))), (0, 120 + j * sh))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 52, GOLD, "VENETIAN BLIND SLATS")
        draw_fit_text(d, title, (64, 1076), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_LIGHT)
        return c
    if num == 84:  # Brutalist Concrete Plinth
        c = make_noise_surface(W, H, (148, 146, 140), amp=14, seed=84)
        d = ImageDraw.Draw(c)
        d.line((0, 320, W, 320), fill=(118, 116, 110), width=3)
        d.line((0, 960, W, 960), fill=(118, 116, 110), width=3)
        paste_centered(c, pil, (110, 170, W - 110, 910), shadow=True)
        d.rectangle((64, 996, W - 64, 1264), fill=(36, 36, 34))
        draw_fit_text(d, title.upper(), (96, 1028), "din_b", 52, W - 192, PURE_WHITE, max_lines=2)
        draw_fit_text(d, byline_for(art), (96, 1190), "mono", 20, W - 192, GOLD, max_lines=1)
        return c
    if num == 85:  # Isometric 3D Canvas Slab
        c = Image.new("RGB", (W, H), (234, 228, 216))
        d = ImageDraw.Draw(c)
        d.polygon([(140, 210), (900, 210), (946, 256), (946, 986), (186, 986), (140, 940)], fill=(54, 46, 38))
        c.paste(cover_crop(pil, 760, 730, focus=focus), (140, 210))
        draw_brand_tag(d, 64, 62, INK, "3D GALLERY CANVAS SLAB")
        draw_fit_text(d, title, (64, 1036), "didot", 56, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_INK)
        return c
    if num == 86:  # Staircase Stepped Mask
        c = Image.new("RGB", (W, H), OBSIDIAN)
        crop = cover_crop(pil, W - 120, 860, focus=focus)
        mask = Image.new("L", (W - 120, 860), 0)
        md = ImageDraw.Draw(mask)
        for step in range(5):
            md.rectangle((step * 96, step * 80, W - 120 - (4 - step) * 40, 860 - (4 - step) * 70), fill=255)
        c.paste(crop, (60, 140), mask)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD, "STAIRCASE STEPPED MASK")
        draw_fit_text(d, title, (64, 1044), "serif", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1232, MUTED_LIGHT)
        return c
    if num == 87:  # Maritime Brass Porthole
        c = Image.new("RGB", (W, H), (18, 30, 40))
        crop = cover_crop(pil, 680, 680, focus=focus)
        mask = Image.new("L", (680, 680), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 680, 680), fill=255)
        c.paste(crop, (200, 190), mask)
        d = ImageDraw.Draw(c)
        d.ellipse((176, 166, 904, 894), outline=(196, 158, 86), width=22)
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            rx = int(540 + math.cos(rad) * 352)
            ry = int(530 + math.sin(rad) * 352)
            d.ellipse((rx - 9, ry - 9, rx + 9, ry + 9), fill=(96, 72, 34))
        draw_fit_text(d, title, (64, 964), "cinzel", 50, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1220, GOLD, align="center")
        return c
    if num == 88:  # Museum Floorplan Wall
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        d.rectangle((64, 140, W - 64, 960), outline=INK, width=4)
        d.line((64, 550, 360, 550), fill=INK, width=4)
        d.line((720, 550, W - 64, 550), fill=INK, width=4)
        paste_centered(c, pil, (150, 210, W - 150, 890), shadow=True)
        draw_brand_tag(d, 64, 58, VERMILION, "GALLERY WING FLOORPLAN")
        draw_fit_text(d, title, (64, 1014), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    if num == 89:  # Split-Door Curtain Reveal
        c = Image.new("RGB", (W, H), (36, 18, 22))
        c.paste(cover_crop(pil, 680, 880, focus=focus), (200, 130))
        d = ImageDraw.Draw(c)
        d.polygon([(0, 0), (260, 0), (180, 1010), (0, 1010)], fill=(54, 22, 28))
        d.polygon([(W, 0), (W - 260, 0), (W - 180, 1010), (W, 1010)], fill=(54, 22, 28))
        draw_fit_text(d, title, (64, 1044), "didot", 56, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1230, GOLD, align="center")
        return c
    # num == 90: Octagonal Oculus Vault
    c = Image.new("RGB", (W, H), (24, 22, 20))
    crop = cover_crop(pil, 760, 760, focus=focus)
    oct_pts = [(220, 0), (540, 0), (760, 220), (760, 540), (540, 760), (220, 760), (0, 540), (0, 220)]
    mask = Image.new("L", (760, 760), 0)
    ImageDraw.Draw(mask).polygon(oct_pts, fill=255)
    c.paste(crop, (160, 150), mask)
    d = ImageDraw.Draw(c)
    shifted = [(x + 160, y + 150) for x, y in oct_pts]
    d.polygon(shifted, outline=GOLD, width=4)
    draw_fit_text(d, title, (64, 976), "cinzel", 52, W - 128, SOFT_IVORY, max_lines=2, align="center")
    _draw_footer(d, art, 1226, MUTED_LIGHT, align="center")
    return c


_BOARDS = (_board_1, _board_2, _board_3, _board_4, _board_5, _board_6, _board_7, _board_8, _board_9)


def render_neon_boards_1_to_9(
    number: int,
    sources: Sequence[AstraArtworkInput],
    editorial_title: str,
    focus: tuple[float, float],
) -> Image.Image:
    pils = [load_source_image(item.validated_path()) for item in sources]
    board_idx = (number - 1) // 10
    title = editorial_title.strip() or sources[0].title
    return _BOARDS[board_idx](number, pils, sources, title, focus)
