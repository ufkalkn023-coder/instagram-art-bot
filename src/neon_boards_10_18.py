"""Neon collection Boards 10-18 (neon-091..neon-180): 90 distinct artwork-driven 1080x1350 layouts."""

from __future__ import annotations

from typing import Sequence

from src.neon_kit import (
    CHARCOAL, DEEP_NIGHT, GOLD, H, INK, MUTED_INK, MUTED_LIGHT, NEWSPRINT,
    OBSIDIAN, PAPER, PURE_WHITE, RULE_DARK, RULE_LIGHT, SOFT_IVORY, VELLUM,
    VERMILION, W, AstraArtworkInput, F, Image, ImageChops, ImageDraw,
    ImageEnhance, ImageFilter, ImageOps, add_shadow, apply_scrim, byline_for,
    compute_image_metrics, contain_fit, cover_crop, draw_brand_tag,
    draw_fit_text, duotone_image, extract_pigments, extract_vivid_accent,
    halftone_dots, hexcol, ink, load_source_image, make_noise_surface, math,
    mix, np, parse_year, paste_centered, short_museum, thermal_false_color,
)


def _palette(pil: Image.Image, n: int = 5) -> list[tuple[int, int, int]]:
    return [c for c, _ in extract_pigments(pil, n)]


def _draw_footer(draw: ImageDraw.ImageDraw, art: AstraArtworkInput, y: int = 1264, fill=MUTED_LIGHT, align: str = "left") -> None:
    draw_fit_text(draw, byline_for(art), (64, y), "mono", 20, W - 128, fill, min_size=14, max_lines=1, align=align)


def _board_10(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 91:  # Concentric Zoom Ripples
        c = cover_crop(pil, W, H, focus=focus)
        cx, cy = W // 2, 560
        for r, z in ((360, 1.35), (270, 1.75), (185, 2.25), (105, 2.9)):
            crop = cover_crop(pil, r * 2, r * 2, focus=focus, zoom=z)
            mask = Image.new("L", (r * 2, r * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, r * 2, r * 2), fill=255)
            c.paste(crop, (cx - r, cy - r), mask)
            ImageDraw.Draw(c).ellipse((cx - r, cy - r, cx + r, cy + r), outline=GOLD, width=2)
        apply_scrim(c, "bottom", 0.34, (14, 12, 10), 230)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title, (64, 1020), "didot", 58, W - 128, PURE_WHITE, max_lines=2, align="center")
        _draw_footer(d, art, 1230, GOLD, align="center")
        return c
    if num == 92:  # Fluted Glass Prism Shift
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        crop = cover_crop(pil, W - 120, 880, focus=focus)
        w_tot = W - 120
        sw = 32
        for x in range(0, w_tot, sw):
            strip = crop.crop((x, 0, min(w_tot, x + sw), 880))
            if (x // sw) % 2 == 1:
                strip = strip.filter(ImageFilter.GaussianBlur(3))
            c.paste(strip, (60 + x, 130))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 60, 54, GOLD, "FLUTED GLASS PRISM")
        draw_fit_text(d, title, (60, 1054), "didot", 56, W - 120, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_LIGHT)
        return c
    if num == 93:  # Negative / Positive Split
        c = cover_crop(pil, W, H, focus=focus)
        inv = ImageOps.invert(c)
        c.paste(inv.crop((W // 2, 0, W, H)), (W // 2, 0))
        apply_scrim(c, "bottom", 0.32, (12, 12, 12), 235)
        d = ImageDraw.Draw(c)
        d.line((W // 2, 0, W // 2, H), fill=PURE_WHITE, width=3)
        draw_brand_tag(d, 64, 54, PURE_WHITE, "POSITIVE  |  NEGATIVE")
        draw_fit_text(d, title, (64, 1030), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, SOFT_IVORY)
        return c
    if num == 94:  # Pixel-to-HD Ladder
        c = Image.new("RGB", (W, H), OBSIDIAN)
        base = cover_crop(pil, W - 128, 880, focus=focus)
        bands = (14, 34, 80, 220, W - 128)
        bh = 880 // 5
        for j, res in enumerate(bands):
            strip = base.crop((0, j * bh, W - 128, (j + 1) * bh))
            if res < W - 128:
                strip = strip.resize((res, max(4, int(bh * res / (W - 128)))), Image.Resampling.BILINEAR).resize(strip.size, Image.Resampling.NEAREST)
            c.paste(strip, (64, 130 + j * bh))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD, "PIXEL-TO-HD LADDER")
        draw_fit_text(d, title, (64, 1054), "serif", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_LIGHT)
        return c
    if num == 95:  # Dual-Exposure Reflection
        top = cover_crop(pil, W, 680, focus=focus)
        bot = ImageOps.flip(top).filter(ImageFilter.GaussianBlur(2))
        bot = Image.blend(bot, Image.new("RGB", bot.size, (18, 22, 28)), 0.35)
        c = Image.new("RGB", (W, H), (18, 22, 28))
        c.paste(top, (0, 0))
        c.paste(bot, (0, 680))
        d = ImageDraw.Draw(c)
        d.line((0, 680, W, 680), fill=GOLD, width=3)
        draw_fit_text(d, title, (64, 1030), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, SOFT_IVORY)
        return c
    if num == 96:  # Halftone + True-Color
        base = cover_crop(pil, W, H, focus=focus)
        c = halftone_dots(base, cell=11, bg=PAPER, dot=INK)
        inset = base.crop((180, 220, W - 180, 960))
        c.paste(inset, (180, 220))
        d = ImageDraw.Draw(c)
        d.rectangle((180, 220, W - 180, 960), outline=VERMILION, width=4)
        d.rectangle((64, 1020, W - 64, 1270), fill=PAPER, outline=INK, width=2)
        draw_fit_text(d, title, (96, 1050), "didot", 50, W - 192, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (96, 1200), "mono", 19, W - 192, MUTED_INK, max_lines=1)
        return c
    if num == 97:  # Spectral RGB Offset Frame
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        crop = cover_crop(pil, W - 160, 860, focus=focus)
        r_ch, g_ch, b_ch = crop.split()
        r_ch = ImageChops.offset(r_ch, -10, 0)
        b_ch = ImageChops.offset(b_ch, 10, 0)
        shifted = Image.merge("RGB", (r_ch, g_ch, b_ch))
        c.paste(shifted, (80, 140))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 80, 56, GOLD, "SPECTRAL RGB OFFSET")
        draw_fit_text(d, title, (80, 1044), "din_b", 56, W - 160, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_LIGHT)
        return c
    if num == 98:  # Kaleidoscope Quad Border
        c = Image.new("RGB", (W, H), OBSIDIAN)
        q = cover_crop(pil, W // 2, 480, focus=focus)
        c.paste(q, (0, 60))
        c.paste(ImageOps.mirror(q), (W // 2, 60))
        c.paste(ImageOps.flip(q), (0, 540))
        c.paste(ImageOps.flip(ImageOps.mirror(q)), (W // 2, 540))
        paste_centered(c, pil, (220, 220, W - 220, 860), shadow=True)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title, (64, 1054), "didot", 56, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1230, GOLD, align="center")
        return c
    if num == 99:  # Louvered Perspective
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        for j in range(6):
            x = 64 + j * 162
            h = 840 - abs(j - 2.5) * 70
            y = int(140 + (840 - h) / 2)
            c.paste(cover_crop(pil, 142, int(h), focus=(0.12 + j * 0.15, focus[1])), (x, y))
        draw_brand_tag(d, 64, 54, INK, "LOUVERED PERSPECTIVE")
        draw_fit_text(d, title, (64, 1036), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    # num == 100: Infrared Thermal Scan
    c = cover_crop(pil, W, H, focus=focus)
    therm = thermal_false_color(c)
    c.paste(therm.crop((W // 2, 0, W, H)), (W // 2, 0))
    apply_scrim(c, "bottom", 0.34, (12, 12, 18), 235)
    d = ImageDraw.Draw(c)
    d.line((W // 2, 0, W // 2, H), fill=PURE_WHITE, width=4)
    draw_brand_tag(d, 56, 54, PURE_WHITE, "VISIBLE LIGHT")
    draw_brand_tag(d, W // 2 + 32, 54, GOLD, "INFRARED THERMAL MAP")
    draw_fit_text(d, title, (64, 1020), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
    _draw_footer(d, art, 1230, SOFT_IVORY)
    return c


def _board_11(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    accent = extract_vivid_accent(pil)
    if num == 101:  # Penguin Tri-Band Classic
        band = (224, 96, 36)
        c = Image.new("RGB", (W, H), PURE_WHITE)
        d = ImageDraw.Draw(c)
        d.rectangle((0, 0, W, 240), fill=band)
        d.rectangle((0, 1080, W, H), fill=band)
        draw_brand_tag(d, 64, 42, PURE_WHITE, "ARTFOLIO CLASSICS")
        draw_fit_text(d, title, (64, 86), "georgia_b", 54, W - 128, PURE_WHITE, max_lines=2)
        paste_centered(c, pil, (96, 276, W - 96, 1044), shadow=True)
        draw_fit_text(d, byline_for(art), (64, 1148), "mono_b", 22, W - 128, PURE_WHITE, max_lines=2, align="center")
        return c
    if num == 102:  # Linen Hardcover Jacket
        c = make_noise_surface(W, H, mix(accent, (44, 52, 48), 0.55), amp=10, seed=102)
        d = ImageDraw.Draw(c)
        d.line((96, 0, 96, H), fill=(20, 18, 16), width=4)
        d.rectangle((164, 130, W - 96, 940), fill=PAPER)
        paste_centered(c, pil, (196, 162, W - 128, 908))
        draw_fit_text(d, title, (164, 996), "cinzel", 50, W - 260, GOLD, max_lines=2)
        draw_fit_text(d, byline_for(art), (164, 1200), "mono", 20, W - 260, SOFT_IVORY, max_lines=1)
        return c
    if num == 103:  # Open Book Spread
        c = Image.new("RGB", (W, H), (44, 38, 34))
        d = ImageDraw.Draw(c)
        d.rectangle((54, 160, W - 54, 1040), fill=PAPER)
        d.line((W // 2, 160, W // 2, 1040), fill=(182, 174, 158), width=6)
        paste_centered(c, pil, (96, 220, W // 2 - 36, 960))
        draw_brand_tag(d, W // 2 + 44, 240, VERMILION, f"PLATE  ·  {parse_year(art.date) or 'FOLIO'}")
        draw_fit_text(d, title, (W // 2 + 44, 310), "serif", 46, W // 2 - 140, INK, max_lines=4)
        draw_fit_text(d, f"{art.title}\n{art.artist}\n{short_museum(art.institution)}", (W // 2 + 44, 720), "mono", 20, W // 2 - 140, MUTED_INK, max_lines=6)
        _draw_footer(d, art, 1140, SOFT_IVORY, align="center")
        return c
    if num == 104:  # Illuminated Manuscript
        c = make_noise_surface(W, H, (238, 226, 196), amp=9, seed=104)
        d = ImageDraw.Draw(c)
        d.rectangle((56, 56, W - 56, H - 56), outline=GOLD, width=6)
        d.rectangle((76, 76, W - 76, H - 76), outline=(144, 34, 30), width=2)
        paste_centered(c, pil, (124, 156, W - 124, 880))
        d.rectangle((124, 932, 236, 1044), fill=(144, 34, 30), outline=GOLD, width=3)
        first_ch = next((ch.upper() for ch in title if ch.isalpha()), "A")
        d.text((156, 948), first_ch, font=F("cinzel", 76), fill=GOLD)
        draw_fit_text(d, title, (260, 944), "serif", 46, W - 384, INK, max_lines=2)
        _draw_footer(d, art, 1180, MUTED_INK)
        return c
    if num == 105:  # Ex-Libris Bookplate
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        d.rectangle((110, 110, W - 110, H - 110), outline=INK, width=5)
        d.rectangle((126, 126, W - 126, H - 126), outline=INK, width=1)
        draw_fit_text(d, "EX LIBRIS  ·  ART.FOLIO", (150, 160), "cinzel", 34, W - 300, INK, max_lines=1, align="center")
        paste_centered(c, pil, (176, 244, W - 176, 920))
        draw_fit_text(d, title, (160, 964), "georgia_i", 44, W - 320, INK, max_lines=2, align="center")
        draw_fit_text(d, byline_for(art), (160, 1136), "mono", 18, W - 320, MUTED_INK, max_lines=1, align="center")
        return c
    if num == 106:  # Encyclopedia Lexicon
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        d.line((64, 130, W - 64, 130), fill=INK, width=3)
        draw_brand_tag(d, 64, 68, INK, f"ENCYCLOPEDIA OF ART  ·  VOL. {str(parse_year(art.date) or 'XX')[:2]}")
        c.paste(cover_crop(pil, 540, 820, focus=focus), (64, 180))
        draw_fit_text(d, title, (636, 180), "georgia_b", 40, 380, INK, max_lines=4)
        draw_fit_text(d, f"n. {art.title}\n\nArtist: {art.artist}\nDate: {art.date or 'Archive'}\nHolding: {short_museum(art.institution)}", (636, 460), "serif", 26, 380, CHARCOAL, max_lines=10)
        _draw_footer(d, art, 1226, MUTED_INK)
        return c
    if num == 107:  # Library Due-Date Slip
        c = make_noise_surface(W, H, (242, 232, 206), amp=8, seed=107)
        d = ImageDraw.Draw(c)
        d.rectangle((72, 72, W - 72, H - 72), outline=INK, width=2)
        d.text((W // 2 - 190, 96), "DATE DUE  //  ART.FOLIO", font=F("mono_b", 26), fill=INK)
        d.line((72, 146, W - 72, 146), fill=INK, width=2)
        paste_centered(c, pil, (112, 182, W - 112, 860), shadow=True)
        for row in range(3):
            y = 910 + row * 72
            d.line((100, y, W - 100, y), fill=RULE_LIGHT, width=2)
        draw_fit_text(d, title, (100, 926), "mono_b", 32, W - 200, VERMILION, max_lines=2)
        draw_fit_text(d, byline_for(art), (100, 1140), "mono", 19, W - 200, INK, max_lines=1)
        return c
    if num == 108:  # Deckle-Edge Lithograph
        c = Image.new("RGB", (W, H), (36, 34, 30))
        d = ImageDraw.Draw(c)
        d.rectangle((84, 96, W - 84, H - 96), fill=PAPER)
        paste_centered(c, pil, (148, 170, W - 148, 920), shadow=True)
        draw_fit_text(d, title, (148, 980), "georgia_i", 48, W - 296, INK, max_lines=2, align="center")
        draw_fit_text(d, byline_for(art), (148, 1152), "mono", 18, W - 296, MUTED_INK, max_lines=1, align="center")
        return c
    if num == 109:  # Silk Bookmark Ribbon
        c = Image.new("RGB", (W, H), PAPER)
        paste_centered(c, pil, (84, 180, W - 84, 960), shadow=True)
        d = ImageDraw.Draw(c)
        d.polygon([(830, 0), (924, 0), (924, 540), (877, 480), (830, 540)], fill=(156, 38, 36))
        draw_brand_tag(d, 84, 66, INK)
        draw_fit_text(d, title, (84, 1020), "didot", 56, W - 168, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    # num == 110: Press Proof & CMYK Marks
    c = Image.new("RGB", (W, H), PURE_WHITE)
    paste_centered(c, pil, (110, 150, W - 110, 940))
    d = ImageDraw.Draw(c)
    for cx, cy in ((56, 56), (W - 56, 56), (56, H - 56), (W - 56, H - 56)):
        d.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline=INK, width=2)
        d.line((cx - 26, cy, cx + 26, cy), fill=INK, width=1)
        d.line((cx, cy - 26, cx, cy + 26), fill=INK, width=1)
    for j, col in enumerate(((0, 174, 239), (236, 0, 140), (255, 242, 0), (28, 28, 28))):
        d.rectangle((110 + j * 54, 964, 154 + j * 54, 996), fill=col)
    draw_fit_text(d, title, (110, 1028), "serif", 52, W - 220, INK, max_lines=2)
    _draw_footer(d, art, 1218, MUTED_INK)
    return c


def _board_12(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 111:  # SLR Camera Viewfinder
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "bottom", 0.30, (8, 8, 8), 220)
        d = ImageDraw.Draw(c)
        d.ellipse((W // 2 - 140, 460, W // 2 + 140, 740), outline=(255, 255, 255), width=2)
        d.line((W // 2 - 36, 600, W // 2 + 36, 600), fill=VERMILION, width=2)
        d.line((W // 2, 564, W // 2, 636), fill=VERMILION, width=2)
        d.text((64, 54), "ISO 100   f/2.8   1/125   RAW", font=F("mono_b", 22), fill=(116, 236, 134))
        draw_fit_text(d, title, (64, 1044), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, SOFT_IVORY)
        return c
    if num == 112:  # Cassette Tape J-Card
        c = Image.new("RGB", (W, H), (28, 28, 30))
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((110, 100, W - 110, H - 100), radius=18, fill=PAPER)
        d.rectangle((110, 100, W - 110, 260), fill=(214, 72, 42))
        draw_fit_text(d, title.upper(), (146, 146), "din_b", 52, W - 292, PURE_WHITE, max_lines=2)
        c.paste(cover_crop(pil, W - 280, 680, focus=focus), (140, 290))
        draw_fit_text(d, f"SIDE A  ·  {byline_for(art)}", (146, 1020), "mono_b", 20, W - 292, INK, max_lines=2)
        return c
    if num == 113:  # Museum Audio Player
        c = Image.new("RGB", (W, H), (18, 20, 24))
        paste_centered(c, pil, (110, 120, W - 110, 820), shadow=True)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title, (110, 868), "serif", 48, W - 220, PURE_WHITE, max_lines=2)
        draw_fit_text(d, byline_for(art), (110, 996), "sans", 22, W - 220, MUTED_LIGHT, max_lines=1)
        d.line((110, 1084, W - 110, 1084), fill=(76, 80, 86), width=6)
        d.line((110, 1084, 520, 1084), fill=GOLD, width=6)
        d.ellipse((508, 1072, 532, 1096), fill=PURE_WHITE)
        d.ellipse((W // 2 - 42, 1134, W // 2 + 42, 1218), fill=PURE_WHITE)
        d.polygon([(W // 2 - 12, 1156), (W // 2 - 12, 1196), (W // 2 + 22, 1176)], fill=OBSIDIAN)
        return c
    if num == 114:  # Polaroid SX-70 Print
        c = Image.new("RGB", (W, H), (34, 31, 29))
        d = ImageDraw.Draw(c)
        d.rectangle((110, 110, W - 110, 1220), fill=(248, 246, 240))
        c.paste(cover_crop(pil, W - 296, 820, focus=focus), (148, 156))
        draw_fit_text(d, title, (148, 1020), "georgia_i", 46, W - 296, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (148, 1146), "mono", 18, W - 296, MUTED_INK, max_lines=1)
        return c
    if num == 115:  # CRT Phosphor Scanlines
        c = cover_crop(pil, W, H, focus=focus)
        arr = np.asarray(c).copy()
        arr[::4, :, :] = (arr[::4, :, :] * 0.45).astype(np.uint8)
        c = Image.fromarray(arr)
        apply_scrim(c, "bottom", 0.34, (8, 16, 12), 235)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, (132, 245, 162), "CRT ARCHIVE MONITOR")
        draw_fit_text(d, title, (64, 1020), "mono_b", 48, W - 128, (186, 255, 204), max_lines=2)
        _draw_footer(d, art, 1226, (132, 245, 162))
        return c
    if num == 116:  # Dieter Rams Braun Panel
        c = Image.new("RGB", (W, H), (232, 231, 226))
        d = ImageDraw.Draw(c)
        c.paste(cover_crop(pil, W - 140, 740, focus=focus), (70, 120))
        d.line((70, 900, W - 70, 900), fill=INK, width=2)
        for j in range(5):
            d.ellipse((70 + j * 86, 932, 124 + j * 86, 986), fill=VERMILION if j == 0 else CHARCOAL)
        draw_fit_text(d, title, (70, 1028), "futura", 48, W - 140, INK, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_INK)
        return c
    if num == 117:  # Oscilloscope Luma Wave (real luma waveform!)
        c = Image.new("RGB", (W, H), (10, 18, 16))
        c.paste(cover_crop(pil, W - 128, 660, focus=focus), (64, 140))
        d = ImageDraw.Draw(c)
        cols = np.asarray(pil.convert("L").resize((240, 80), Image.Resampling.BILINEAR)).mean(axis=0)
        pts = [(64 + int(j * (W - 128) / 239), 1020 - int((float(val) / 255.0) * 180)) for j, val in enumerate(cols)]
        d.line(pts, fill=(96, 245, 166), width=3)
        draw_brand_tag(d, 64, 54, (96, 245, 166), "OSCILLOSCOPE LUMA WAVE")
        draw_fit_text(d, title, (64, 1068), "mono_b", 42, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, (96, 245, 166))
        return c
    if num == 118:  # Microfiche Archive Card
        c = Image.new("RGB", (W, H), (22, 34, 42))
        d = ImageDraw.Draw(c)
        d.rectangle((48, 84, W - 48, 196), fill=(214, 196, 134))
        draw_fit_text(d, f"MICROFICHE  //  {title.upper()}", (72, 118), "mono_b", 30, W - 144, INK, max_lines=1)
        pts = [(0.25, 0.35), (0.5, 0.35), (0.75, 0.35), (0.25, 0.7), (0.5, 0.7), (0.75, 0.7)]
        cw, ch = 300, 390
        for idx, pt in enumerate(pts):
            r, col = divmod(idx, 3)
            c.paste(cover_crop(pil, cw, ch, focus=pt, zoom=1.6), (64 + col * (cw + 26), 240 + r * (ch + 34)))
        _draw_footer(d, art, 1200, (214, 196, 134))
        return c
    if num == 119:  # Typewriter Field Dispatch
        c = make_noise_surface(W, H, (240, 234, 218), amp=8, seed=119)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 72, 58, INK, "FIELD DISPATCH  //  ART.FOLIO")
        paste_centered(c, pil, (72, 130, W - 72, 860), shadow=True)
        draw_fit_text(d, f"SUBJECT: {title}", (72, 916), "mono_b", 34, W - 144, INK, max_lines=2)
        draw_fit_text(d, f"FILED BY: {art.artist}  //  {art.date or 'ARCHIVE'}  //  {short_museum(art.institution)}", (72, 1126), "mono", 20, W - 144, CHARCOAL, max_lines=2)
        return c
    # num == 120: Museum Boarding Pass
    c = Image.new("RGB", (W, H), (22, 30, 44))
    d = ImageDraw.Draw(c)
    d.rounded_rectangle((72, 110, W - 72, H - 110), radius=24, fill=PURE_WHITE)
    d.rectangle((72, 110, W - 72, 230), fill=VERMILION)
    draw_brand_tag(d, 108, 154, PURE_WHITE, f"BOARDING PASS  //  {short_museum(art.institution)}")
    c.paste(cover_crop(pil, W - 216, 620, focus=focus), (108, 268))
    draw_fit_text(d, title, (108, 932), "din_b", 48, W - 216, INK, max_lines=2)
    draw_fit_text(d, byline_for(art), (108, 1140), "mono", 19, W - 216, MUTED_INK, max_lines=1)
    return c


def _board_13(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 121:  # Celestial Star Astrolabe
        c = Image.new("RGB", (W, H), (14, 20, 34))
        d = ImageDraw.Draw(c)
        for r in (410, 350, 290):
            d.ellipse((W // 2 - r, 540 - r, W // 2 + r, 540 + r), outline=GOLD, width=2)
        crop = cover_crop(pil, 540, 540, focus=focus)
        mask = Image.new("L", (540, 540), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 540, 540), fill=255)
        c.paste(crop, (W // 2 - 270, 540 - 270), mask)
        draw_fit_text(d, title, (64, 1008), "cinzel", 50, W - 128, GOLD, max_lines=2, align="center")
        _draw_footer(d, art, 1228, SOFT_IVORY, align="center")
        return c
    if num == 122:  # Periodic Pigment Table
        accent = extract_vivid_accent(pil)
        sym = (art.artist.split()[-1][:2] if art.artist.split() else "AF").title()
        c = Image.new("RGB", (W, H), OBSIDIAN)
        d = ImageDraw.Draw(c)
        d.rectangle((64, 120, 420, 496), fill=accent, outline=PURE_WHITE, width=4)
        fg = PURE_WHITE if ink(accent) != "#1a1915" else INK
        d.text((92, 146), hexcol(accent), font=F("mono_b", 24), fill=fg)
        d.text((92, 224), sym, font=F("din_b", 148), fill=fg)
        d.text((92, 424), str(parse_year(art.date) or "PIGMENT"), font=F("mono", 24), fill=fg)
        c.paste(cover_crop(pil, 560, 820, focus=focus), (456, 120))
        draw_fit_text(d, title, (64, 984), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1230, GOLD)
        return c
    if num == 123:  # Topographic Contours
        c = Image.new("RGB", (W, H), (236, 232, 220))
        d = ImageDraw.Draw(c)
        for r in range(80, 620, 48):
            d.ellipse((W // 2 - r, 520 - int(r * 0.78), W // 2 + r, 520 + int(r * 0.78)), outline=(192, 184, 166), width=2)
        paste_centered(c, pil, (130, 180, W - 130, 920), shadow=True)
        draw_fit_text(d, title, (64, 996), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    if num == 124:  # Real RGB Histogram
        c = Image.new("RGB", (W, H), (16, 17, 20))
        paste_centered(c, pil, (64, 120, W - 64, 720))
        d = ImageDraw.Draw(c)
        arr = np.asarray(pil.resize((180, 180)))
        for ch_idx, col in enumerate(((230, 74, 64), (78, 204, 118), (74, 152, 240))):
            hist, _ = np.histogram(arr[:, :, ch_idx], bins=64, range=(0, 256))
            mx = max(1, int(hist.max()))
            pts = [(64 + int(b * (W - 128) / 63), 980 - int((hist[b] / mx) * 200)) for b in range(64)]
            d.line(pts, fill=col, width=3)
        draw_brand_tag(d, 64, 48, GOLD, "REAL RGB PIXEL HISTOGRAM")
        draw_fit_text(d, title, (64, 1020), "serif", 52, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_LIGHT)
        return c
    if num == 125:  # Sundial Hour-of-Light
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        for deg in range(180, 361, 15):
            rad = math.radians(deg)
            d.line((W // 2, 560, int(W // 2 + math.cos(rad) * 440), int(560 + math.sin(rad) * 440)), fill=RULE_LIGHT, width=2)
        paste_centered(c, pil, (130, 190, W - 130, 920), shadow=True)
        draw_fit_text(d, title, (64, 988), "cinzel", 50, W - 128, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1230, MUTED_INK, align="center")
        return c
    if num == 126:  # Vernier Caliper Scale
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        for x in range(64, W - 64, 20):
            tick = 26 if (x - 64) % 100 == 0 else 14
            d.line((x, 120, x, 120 + tick), fill=INK, width=2)
        paste_centered(c, pil, (96, 190, W - 96, 940), shadow=True)
        draw_fit_text(d, title, (64, 1004), "mono_b", 44, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    if num == 127:  # Geological Pigment Strata
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W, 620, focus=focus), (0, 0))
        d = ImageDraw.Draw(c)
        for j, col in enumerate(pal):
            y0 = 620 + j * 86
            d.polygon([(0, y0), (W, y0 + (j % 2) * 18 - 9), (W, y0 + 96), (0, y0 + 96)], fill=col)
            d.text((64, y0 + 30), hexcol(col), font=F("mono_b", 22), fill=PURE_WHITE if ink(col) != "#1a1915" else INK)
        d.rectangle((0, 1050, W, H), fill=OBSIDIAN)
        draw_fit_text(d, title, (64, 1086), "didot", 52, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1252, MUTED_LIGHT)
        return c
    if num == 128:  # Microscope Glass Slide
        c = Image.new("RGB", (W, H), (222, 228, 232))
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((72, 250, W - 72, 870), radius=14, fill=(242, 246, 248), outline=(140, 152, 162), width=3)
        c.paste(cover_crop(pil, 520, 520, focus=focus, zoom=2.3), (280, 300))
        d.rectangle((96, 300, 256, 820), fill=PAPER, outline=INK, width=2)
        draw_fit_text(d, title, (64, 944), "mono_b", 44, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1218, CHARCOAL)
        return c
    if num == 129:  # Maritime Compass Chart
        c = Image.new("RGB", (W, H), (240, 233, 214))
        d = ImageDraw.Draw(c)
        d.ellipse((140, 140, W - 140, 940), outline=(156, 138, 108), width=2)
        for lbl, pos in (("N", (W // 2 - 12, 92)), ("S", (W // 2 - 12, 954)), ("W", (92, 526)), ("E", (W - 114, 526))):
            d.text(pos, lbl, font=F("cinzel", 32), fill=INK)
        paste_centered(c, pil, (200, 220, W - 200, 860), shadow=True)
        draw_fit_text(d, title, (64, 1018), "serif", 52, W - 128, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1236, MUTED_INK, align="center")
        return c
    # num == 130: Emotion Scatter Plot
    metrics = compute_image_metrics(pil)
    c = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(c)
    paste_centered(c, pil, (110, 140, W - 110, 680), shadow=True)
    ax0, ay0, ax1, ay1 = 140, 740, W - 140, 1080
    d.line(((ax0 + ax1) // 2, ay0, (ax0 + ax1) // 2, ay1), fill=INK, width=2)
    d.line((ax0, (ay0 + ay1) // 2, ax1, (ay0 + ay1) // 2), fill=INK, width=2)
    px = int(ax0 + metrics["warmth"] * (ax1 - ax0))
    py = int(ay1 - metrics["luminance"] * (ay1 - ay0))
    d.ellipse((px - 18, py - 18, px + 18, py + 18), fill=VERMILION)
    draw_fit_text(d, title, (64, 1114), "serif", 46, W - 128, INK, max_lines=2, align="center")
    _draw_footer(d, art, 1252, MUTED_INK, align="center")
    return c


def _board_14(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 131:  # Art Deco Gatsby Frame
        c = Image.new("RGB", (W, H), (14, 24, 22))
        d = ImageDraw.Draw(c)
        for inset, lw in ((44, 3), (64, 1), (84, 2)):
            d.rectangle((inset, inset, W - inset, H - inset), outline=GOLD, width=lw)
        paste_centered(c, pil, (124, 166, W - 124, 960))
        draw_fit_text(d, title.upper(), (96, 1018), "cinzel", 50, W - 192, GOLD, max_lines=2, align="center")
        _draw_footer(d, art, 1200, SOFT_IVORY, align="center")
        return c
    if num == 132:  # Apothecary Perfume Label (dynamic notes!)
        c = cover_crop(pil, W, H, focus=focus)
        lx0, ly0, lx1, ly1 = 180, 290, W - 180, 1060
        add_shadow(c, (lx0, ly0, lx1, ly1), blur=26, alpha=110)
        d = ImageDraw.Draw(c)
        d.rectangle((lx0, ly0, lx1, ly1), fill=(248, 245, 238), outline=INK, width=2)
        d.rectangle((lx0 + 16, ly0 + 16, lx1 - 16, ly1 - 16), outline=INK, width=1)
        draw_fit_text(d, "ARTFOLIO APOTHECARY", (lx0 + 36, ly0 + 48), "mono_b", 22, lx1 - lx0 - 72, INK, max_lines=1, align="center")
        draw_fit_text(d, title.upper(), (lx0 + 40, ly0 + 120), "cinzel", 52, lx1 - lx0 - 80, INK, max_lines=2, align="center")
        rows = [
            ("PIGMENT:", f"{hexcol(pal[0])} · {hexcol(pal[2])} · {hexcol(pal[4])}"),
            ("AUTHOR:", art.artist),
            ("EDITION:", art.date or "Archive"),
            ("HOUSE:", short_museum(art.institution)),
        ]
        ny = ly0 + 360
        for k_lbl, v_lbl in rows:
            d.text((lx0 + 48, ny), k_lbl, font=F("mono_b", 20), fill=VERMILION)
            draw_fit_text(d, v_lbl, (lx0 + 188, ny), "mono", 20, lx1 - lx0 - 230, CHARCOAL, max_lines=1)
            ny += 64
        return c
    if num == 133:  # Velvet Jewelry Box
        c = Image.new("RGB", (W, H), (54, 16, 24))
        d = ImageDraw.Draw(c)
        d.rectangle((146, 156, W - 146, 984), fill=(26, 10, 14), outline=GOLD, width=4)
        paste_centered(c, pil, (166, 176, W - 166, 964))
        draw_fit_text(d, title, (64, 1028), "didot", 58, W - 128, GOLD, max_lines=2, align="center")
        _draw_footer(d, art, 1226, SOFT_IVORY, align="center")
        return c
    if num == 134:  # Heraldic Crest Shield
        c = Image.new("RGB", (W, H), (24, 24, 28))
        shield = [(200, 160), (880, 160), (880, 540), (820, 750), (700, 900), (W // 2, 1010), (380, 900), (260, 750), (200, 540)]
        crop = cover_crop(pil, W, H, focus=focus)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon(shield, fill=255)
        c.paste(crop, (0, 0), mask)
        d = ImageDraw.Draw(c)
        d.polygon(shield, outline=GOLD, width=5)
        draw_fit_text(d, title.upper(), (64, 1054), "cinzel", 48, W - 128, GOLD, max_lines=2, align="center")
        _draw_footer(d, art, 1230, SOFT_IVORY, align="center")
        return c
    if num == 135:  # Silk Scarf Border
        accent = extract_vivid_accent(pil)
        c = Image.new("RGB", (W, H), accent)
        d = ImageDraw.Draw(c)
        d.rectangle((54, 54, W - 54, H - 54), fill=PAPER, outline=GOLD, width=6)
        d.rectangle((82, 82, W - 82, H - 82), outline=accent, width=3)
        paste_centered(c, pil, (124, 150, W - 124, 950))
        draw_fit_text(d, title, (124, 1004), "didot", 52, W - 248, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1184, MUTED_INK, align="center")
        return c
    if num == 136:  # Marble Pedestal Card
        c = make_noise_surface(W, H, (236, 234, 230), amp=12, seed=136)
        d = ImageDraw.Draw(c)
        d.polygon([(220, 920), (860, 920), (920, 1280), (160, 1280)], fill=(214, 210, 202), outline=(170, 166, 158))
        paste_centered(c, pil, (140, 150, W - 140, 900), shadow=True)
        draw_fit_text(d, title, (220, 976), "cinzel", 46, 640, INK, max_lines=2, align="center")
        draw_fit_text(d, byline_for(art), (220, 1170), "mono", 18, 640, CHARCOAL, max_lines=1, align="center")
        return c
    if num == 137:  # Oval Cameo Brooch
        c = Image.new("RGB", (W, H), (28, 34, 42))
        crop = cover_crop(pil, 620, 780, focus=focus)
        mask = Image.new("L", (620, 780), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 620, 780), fill=255)
        c.paste(crop, (230, 140), mask)
        d = ImageDraw.Draw(c)
        d.ellipse((218, 128, 862, 932), outline=GOLD, width=8)
        draw_fit_text(d, title, (64, 984), "didot", 58, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1226, GOLD, align="center")
        return c
    if num == 138:  # Fashion Lookbook Sheet
        c = Image.new("RGB", (W, H), PURE_WHITE)
        c.paste(cover_crop(pil, 660, 1020, focus=focus), (64, 120))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 756, 128, INK, "LOOKBOOK")
        draw_fit_text(d, title, (756, 200), "didot", 44, 260, INK, max_lines=5)
        draw_fit_text(d, f"{art.artist}\n{art.date or 'Archive'}\n{short_museum(art.institution)}", (756, 860), "mono", 19, 260, MUTED_INK, max_lines=6)
        return c
    if num == 139:  # Vintage Grand Reserve
        c = Image.new("RGB", (W, H), (24, 18, 16))
        d = ImageDraw.Draw(c)
        d.rectangle((140, 140, W - 140, H - 140), fill=(244, 236, 218), outline=GOLD, width=3)
        draw_fit_text(d, f"GRAND RESERVE  ·  {parse_year(art.date) or 'VINTAGE'}", (172, 182), "cinzel", 26, W - 344, VERMILION, max_lines=1, align="center")
        paste_centered(c, pil, (190, 244, W - 190, 860))
        draw_fit_text(d, title, (172, 900), "didot", 48, W - 344, INK, max_lines=2, align="center")
        draw_fit_text(d, byline_for(art), (172, 1110), "mono", 18, W - 344, MUTED_INK, max_lines=1, align="center")
        return c
    # num == 140: Monogrammed Stationery
    c = Image.new("RGB", (W, H), (248, 245, 238))
    d = ImageDraw.Draw(c)
    d.ellipse((W // 2 - 44, 60, W // 2 + 44, 148), outline=INK, width=2)
    d.text((W // 2 - 22, 86), "AF", font=F("cinzel", 34), fill=INK)
    paste_centered(c, pil, (120, 200, W - 120, 960), shadow=True)
    draw_fit_text(d, title, (120, 1020), "georgia_i", 50, W - 240, INK, max_lines=2, align="center")
    _draw_footer(d, art, 1228, MUTED_INK, align="center")
    return c


def _board_15(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 141:  # Warhol 2x2 Pop-Art
        c = Image.new("RGB", (W, H), OBSIDIAN)
        q = cover_crop(pil, 480, 480, focus=focus)
        tints = [((20, 18, 64), (246, 208, 52)), ((128, 20, 56), (112, 232, 212)), ((24, 96, 48), (248, 120, 74)), ((72, 20, 104), (248, 234, 160))]
        for idx, (dk, lt) in enumerate(tints):
            r, col = divmod(idx, 2)
            c.paste(duotone_image(q, dk, lt), (52 + col * 496, 80 + r * 496))
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title.upper(), (52, 1106), "din_b", 58, W - 104, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1256, GOLD)
        return c
    if num == 142:  # Wheatpaste Street Poster
        c = make_noise_surface(W, H, (86, 84, 78), amp=18, seed=142)
        c.paste(cover_crop(pil, W - 120, 880, focus=focus), (60, 120))
        d = ImageDraw.Draw(c)
        d.rectangle((48, 960, W - 48, 1250), fill=(242, 224, 58))
        draw_fit_text(d, title.upper(), (80, 996), "din_b", 62, W - 160, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (80, 1188), "mono_b", 20, W - 160, INK, max_lines=1)
        return c
    if num == 143:  # Curator Sticker Notebook
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        for x in range(48, W, 36):
            for y in range(48, H, 36):
                d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(196, 190, 178))
        paste_centered(c, pil, (96, 170, W - 96, 940), shadow=True)
        d.rounded_rectangle((700, 110, 1000, 196), radius=24, fill=VERMILION)
        d.text((738, 136), "CURATOR PICK", font=F("mono_b", 24), fill=PURE_WHITE)
        draw_fit_text(d, title, (96, 1004), "serif", 54, W - 192, INK, max_lines=2)
        _draw_footer(d, art, 1228, MUTED_INK)
        return c
    if num == 144:  # Swiss Punk Diagonal Band
        c = cover_crop(pil, W, H, focus=focus)
        d = ImageDraw.Draw(c)
        d.polygon([(0, 820), (W, 640), (W, 960), (0, 1140)], fill=VERMILION)
        draw_fit_text(d, title.upper(), (64, 836), "din_b", 64, W - 128, PURE_WHITE, max_lines=2)
        apply_scrim(c, "bottom", 0.16, (14, 14, 14), 220)
        _draw_footer(d, art, 1244, PURE_WHITE)
        return c
    if num == 145:  # Cutout Block Headline
        c = Image.new("RGB", (W, H), CHARCOAL)
        c.paste(cover_crop(pil, W - 128, 760, focus=focus), (64, 130))
        d = ImageDraw.Draw(c)
        words = title.upper().split()[:4] or ["ART"]
        wx, wy = 64, 940
        for idx, word in enumerate(words):
            fw = int(d.textlength(word, font=F("din_b", 58))) + 34
            if wx + fw > W - 64:
                wx, wy = 64, wy + 92
            bg_b = PAPER if idx % 2 == 0 else VERMILION
            fg_b = INK if idx % 2 == 0 else PURE_WHITE
            d.rectangle((wx, wy, wx + fw, wy + 78), fill=bg_b)
            d.text((wx + 17, wy + 12), word, font=F("din_b", 54), fill=fg_b)
            wx += fw + 14
        _draw_footer(d, art, 1236, SOFT_IVORY)
        return c
    if num == 146:  # Op-Art Checkerboard
        c = Image.new("RGB", (W, H), PURE_WHITE)
        d = ImageDraw.Draw(c)
        sz = 90
        for r in range(H // sz + 1):
            for col in range(W // sz + 1):
                if (r + col) % 2 == 0:
                    d.rectangle((col * sz, r * sz, (col + 1) * sz, (r + 1) * sz), fill=OBSIDIAN)
        d.rectangle((96, 120, W - 96, 1210), fill=PAPER, outline=INK, width=6)
        paste_centered(c, pil, (132, 166, W - 132, 920))
        draw_fit_text(d, title, (132, 964), "din_b", 52, W - 264, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (132, 1136), "mono", 19, W - 264, MUTED_INK, max_lines=1)
        return c
    if num == 147:  # De Stijl Mondrian Grid
        c = Image.new("RGB", (W, H), PURE_WHITE)
        d = ImageDraw.Draw(c)
        c.paste(cover_crop(pil, 760, 820, focus=focus), (280, 40))
        d.rectangle((40, 40, 260, 420), fill=(208, 42, 34))
        d.rectangle((40, 440, 260, 860), fill=(238, 198, 42))
        d.rectangle((40, 880, 260, H - 40), fill=(28, 66, 148))
        for x in (40, 260, 280, W - 40):
            d.line((x, 40, x, H - 40), fill=INK, width=10)
        for y in (40, 420, 860, H - 40):
            d.line((40, y, W - 40, y), fill=INK, width=10)
        draw_fit_text(d, title.upper(), (308, 920), "din_b", 54, W - 368, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (308, 1180), "mono", 19, W - 368, MUTED_INK, max_lines=2)
        return c
    if num == 148:  # Stencil Exhibition Banner
        c = Image.new("RGB", (W, H), (28, 36, 30))
        c.paste(cover_crop(pil, W - 128, 760, focus=focus), (64, 140))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, (238, 224, 180), "EXHIBITION BANNER")
        draw_fit_text(d, title.upper(), (64, 956), "din_c", 78, W - 128, (242, 232, 198), max_lines=2)
        _draw_footer(d, art, 1230, GOLD)
        return c
    if num == 149:  # Tri-Fold Gallery Brochure
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        c.paste(cover_crop(pil, W - 128, 860, focus=focus), (64, 140))
        d.line((W // 3, 0, W // 3, H), fill=(180, 174, 162), width=3)
        d.line((2 * W // 3, 0, 2 * W // 3, H), fill=(180, 174, 162), width=3)
        draw_fit_text(d, title, (64, 1044), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_INK)
        return c
    # num == 150: Diagonal B&W / Color
    color_im = cover_crop(pil, W, H, focus=focus)
    bw_im = ImageOps.grayscale(color_im).convert("RGB")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon([(0, 0), (W, 0), (0, H)], fill=255)
    c = color_im.copy()
    c.paste(bw_im, (0, 0), mask)
    apply_scrim(c, "bottom", 0.32, (12, 12, 12), 230)
    d = ImageDraw.Draw(c)
    d.line((0, H, W, 0), fill=GOLD, width=4)
    draw_fit_text(d, title, (64, 1030), "didot", 58, W - 128, PURE_WHITE, max_lines=2)
    _draw_footer(d, art, 1230, SOFT_IVORY)
    return c


def _board_16(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    pal = _palette(pil, 5)
    if num == 151:  # Guess the Year Quiz (dynamic years!)
        yr = parse_year(art.date) or 1885
        options = sorted({yr - 40, yr, yr + 25, yr + 65})
        c = Image.new("RGB", (W, H), OBSIDIAN)
        paste_centered(c, pil, (84, 130, W - 84, 780))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 84, 52, GOLD, "QUIZ  //  GUESS THE YEAR")
        draw_fit_text(d, title, (84, 822), "didot", 50, W - 168, PURE_WHITE, max_lines=2)
        for idx, val in enumerate(options[:4]):
            r, col = divmod(idx, 2)
            bx, by = 84 + col * 468, 986 + r * 96
            d.rounded_rectangle((bx, by, bx + 444, by + 76), radius=12, outline=GOLD, width=2)
            d.text((bx + 28, by + 22), f"{'ABCD'[idx]}.  {val}", font=F("mono_b", 28), fill=SOFT_IVORY)
        return c
    if num == 152:  # 3-Step Zoom Mystery
        c = Image.new("RGB", (W, H), PAPER)
        for j, z in enumerate((3.2, 1.8, 1.0)):
            c.paste(cover_crop(pil, 296, 680, focus=focus, zoom=z), (64 + j * 328, 170))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 56, VERMILION, "STEP 01  →  STEP 02  →  STEP 03")
        draw_fit_text(d, title, (64, 920), "didot", 56, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1226, MUTED_INK)
        return c
    if num == 153:  # Art DNA Recipe Bars (real metrics!)
        metrics = compute_image_metrics(pil)
        c = Image.new("RGB", (W, H), (22, 24, 22))
        paste_centered(c, pil, (84, 120, W - 84, 680))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 84, 50, GOLD, "ARTWORK VISUAL DNA")
        draw_fit_text(d, title, (84, 720), "serif", 48, W - 168, SOFT_IVORY, max_lines=2)
        rows = [("LIGHT", metrics["luminance"]), ("WARMTH", metrics["warmth"]), ("CHROMA", metrics["saturation"]), ("CONTRAST", metrics["contrast"])]
        for idx, (lbl, val) in enumerate(rows):
            y = 876 + idx * 78
            d.text((84, y), lbl, font=F("mono_b", 22), fill=SOFT_IVORY)
            d.rectangle((260, y + 4, W - 160, y + 32), fill=(52, 54, 50))
            d.rectangle((260, y + 4, 260 + int(val * (W - 420)), y + 32), fill=GOLD)
            d.text((W - 140, y), f"{int(val * 100)}%", font=F("mono_b", 22), fill=GOLD)
        return c
    if num == 154:  # Spot 3 Details Checklist
        c = Image.new("RGB", (W, H), VELLUM)
        paste_centered(c, pil, (72, 140, W - 72, 820), shadow=True)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 72, 54, VERMILION, "LOOKING CHECKLIST  //  3 DETAILS")
        draw_fit_text(d, title, (72, 876), "serif", 50, W - 144, INK, max_lines=2)
        for j, prompt in enumerate(("01  Primary light source", "02  Foreground brushwork", "03  Background atmosphere")):
            d.text((72, 1020 + j * 56), f"[  ]  {prompt}", font=F("mono", 24), fill=CHARCOAL)
        _draw_footer(d, art, 1232, MUTED_INK)
        return c
    if num == 155:  # First Glance vs Remains
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(cover_crop(pil, W - 128, 410, focus=focus), (64, 140))
        c.paste(cover_crop(pil, W - 128, 410, focus=focus, zoom=2.4), (64, 590))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD, "FIRST GLANCE  /  WHAT REMAINS")
        draw_fit_text(d, title, (64, 1044), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_LIGHT)
        return c
    if num == 156:  # Sensory Moodboard Card
        c = Image.new("RGB", (W, H), PAPER)
        c.paste(cover_crop(pil, 610, 820, focus=focus), (64, 150))
        d = ImageDraw.Draw(c)
        for j, col in enumerate(pal):
            d.rectangle((704, 150 + j * 168, W - 64, 294 + j * 168), fill=col)
            d.text((724, 206 + j * 168), hexcol(col), font=F("mono_b", 20), fill=PURE_WHITE if ink(col) != "#1a1915" else INK)
        draw_brand_tag(d, 64, 56, INK, "SENSORY MOODBOARD")
        draw_fit_text(d, title, (64, 1020), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1234, MUTED_INK)
        return c
    if num == 157:  # Palette-First Mystery
        c = Image.new("RGB", (W, H), OBSIDIAN)
        d = ImageDraw.Draw(c)
        for j, col in enumerate(pal):
            d.rectangle((64 + j * 190, 160, 238 + j * 190, 680), fill=col)
        c.paste(cover_crop(pil, W - 128, 280, focus=focus, zoom=2.1), (64, 710))
        draw_brand_tag(d, 64, 56, GOLD, "PALETTE-FIRST TEASER")
        draw_fit_text(d, title, (64, 1030), "didot", 54, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_LIGHT)
        return c
    if num == 158:  # Art History Lineage Map
        c = Image.new("RGB", (W, H), VELLUM)
        d = ImageDraw.Draw(c)
        d.line((W // 2, 160, W // 2, 940), fill=VERMILION, width=3)
        paste_centered(c, pil, (140, 210, W - 140, 890), shadow=True)
        draw_brand_tag(d, 64, 56, VERMILION, f"LINEAGE  ·  {parse_year(art.date) or 'ERA'}")
        draw_fit_text(d, title, (64, 980), "serif", 54, W - 128, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1228, MUTED_INK, align="center")
        return c
    if num == 159:  # Frame vs Pure Canvas
        c = Image.new("RGB", (W, H), (228, 224, 214))
        d = ImageDraw.Draw(c)
        d.rectangle((64, 170, W // 2, 950), fill=(96, 70, 42))
        d.rectangle((96, 202, W // 2, 918), fill=PAPER)
        crop = cover_crop(pil, W - 128, 720, focus=focus)
        c.paste(crop.crop((0, 0, W // 2 - 124, 660)), (124, 230))
        c.paste(crop.crop((W // 2 - 64, 0, W - 128, 720)), (W // 2, 200))
        draw_brand_tag(d, 64, 56, INK, "FRAMED  |  PURE CANVAS")
        draw_fit_text(d, title, (64, 1018), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1230, MUTED_INK)
        return c
    # num == 160: Vote Your Room 4-Tile (4 sources)
    c = Image.new("RGB", (W, H), OBSIDIAN)
    d = ImageDraw.Draw(c)
    qw, qh = 460, 440
    for idx in range(4):
        r, col = divmod(idx, 2)
        x, y = 64 + col * (qw + 32), 140 + r * (qh + 54)
        c.paste(cover_crop(pils[idx], qw, qh, focus=sources[idx].focal_point()), (x, y))
        d.rectangle((x + 14, y + 14, x + 70, y + 70), fill=VERMILION)
        d.text((x + 30, y + 24), str(idx + 1), font=F("din_b", 34), fill=PURE_WHITE)
    draw_brand_tag(d, 64, 52, GOLD, "VOTE YOUR ROOM  //  1 · 2 · 3 · 4")
    draw_fit_text(d, title, (64, 1140), "didot", 52, W - 128, PURE_WHITE, max_lines=2)
    return c


def _board_17(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 161:  # Vintage Postcard Split
        c = make_noise_surface(W, H, (242, 232, 208), amp=7, seed=161)
        d = ImageDraw.Draw(c)
        c.paste(cover_crop(pil, W - 128, 660, focus=focus), (64, 110))
        d.line((W // 2, 810, W // 2, 1240), fill=MUTED_INK, width=2)
        draw_fit_text(d, title, (64, 836), "georgia_i", 44, W // 2 - 96, INK, max_lines=3)
        draw_fit_text(d, byline_for(art), (W // 2 + 32, 860), "mono", 20, W // 2 - 96, CHARCOAL, max_lines=5)
        return c
    if num == 162:  # Letterpress Poem Plate
        c = Image.new("RGB", (W, H), (246, 242, 234))
        paste_centered(c, pil, (160, 140, W - 160, 820), shadow=True)
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title, (120, 896), "georgia_i", 54, W - 240, INK, max_lines=3, align="center")
        _draw_footer(d, art, 1210, MUTED_INK, align="center")
        return c
    if num == 163:  # Midnight Moon Phases
        c = Image.new("RGB", (W, H), (14, 16, 26))
        d = ImageDraw.Draw(c)
        for j in range(5):
            cx = 220 + j * 160
            d.ellipse((cx - 24, 72, cx + 24, 120), fill=GOLD if j == 2 else (216, 204, 168))
            if j != 2:
                d.ellipse((cx - 24 + (14 if j < 2 else -14), 72, cx + 24 + (14 if j < 2 else -14), 120), fill=(14, 16, 26))
        paste_centered(c, pil, (100, 180, W - 100, 940))
        draw_fit_text(d, title, (64, 1000), "didot", 56, W - 128, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1228, GOLD, align="center")
        return c
    if num == 164:  # Archival Envelope Peek
        c = Image.new("RGB", (W, H), (40, 34, 28))
        paste_centered(c, pil, (120, 120, W - 120, 900), shadow=True)
        d = ImageDraw.Draw(c)
        d.polygon([(0, 740), (W // 2, 920), (W, 740), (W, H), (0, H)], fill=VELLUM)
        d.line((0, 740, W // 2, 920, W, 740), fill=(186, 174, 150), width=3)
        draw_fit_text(d, title, (84, 988), "didot", 54, W - 168, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1226, MUTED_INK, align="center")
        return c
    if num == 165:  # Horizon-Resting Serif
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "bottom", 0.36, (16, 16, 18), 220)
        d = ImageDraw.Draw(c)
        d.line((64, 990, W - 64, 990), fill=PURE_WHITE, width=2)
        draw_fit_text(d, title, (64, 868), "didot", 64, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1020, SOFT_IVORY)
        return c
    if num == 166:  # Triple Shadowbox Frame
        c = Image.new("RGB", (W, H), (224, 218, 206))
        d = ImageDraw.Draw(c)
        for inset, col in ((64, (58, 50, 42)), (96, (238, 232, 220)), (128, (248, 245, 238))):
            d.rectangle((inset, inset + 40, W - inset, 980 - inset + 64), fill=col)
        paste_centered(c, pil, (156, 200, W - 156, 888), shadow=True)
        draw_fit_text(d, title, (64, 1044), "serif", 54, W - 128, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1236, MUTED_INK, align="center")
        return c
    if num == 167:  # Stained Glass Border
        pal = _palette(pil, 5)
        c = Image.new("RGB", (W, H), OBSIDIAN)
        d = ImageDraw.Draw(c)
        for j in range(12):
            d.rectangle((j * 90, 0, (j + 1) * 90, 56), fill=pal[j % 5], outline=OBSIDIAN, width=3)
            d.rectangle((j * 90, H - 56, (j + 1) * 90, H), fill=pal[(j + 2) % 5], outline=OBSIDIAN, width=3)
        paste_centered(c, pil, (84, 100, W - 84, 950))
        draw_fit_text(d, title, (84, 1004), "cinzel", 50, W - 168, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1200, GOLD, align="center")
        return c
    if num == 168:  # Candlelight Vignette
        c = cover_crop(pil, W, H, focus=focus)
        vig = Image.new("L", (W, H), 0)
        ImageDraw.Draw(vig).ellipse((90, 90, W - 90, 1040), fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(110))
        dark = Image.new("RGB", (W, H), (14, 10, 8))
        dark.paste(c, (0, 0), vig)
        d = ImageDraw.Draw(dark)
        draw_fit_text(d, title, (64, 1020), "didot", 58, W - 128, (248, 224, 180), max_lines=2, align="center")
        _draw_footer(d, art, 1230, (190, 168, 136), align="center")
        return dark
    if num == 169:  # Swiss Hairline 3x4 Grid
        c = Image.new("RGB", (W, H), PAPER)
        paste_centered(c, pil, (110, 160, W - 110, 940))
        d = ImageDraw.Draw(c)
        for col in range(1, 3):
            d.line((col * W // 3, 0, col * W // 3, H), fill=RULE_LIGHT, width=1)
        for row in range(1, 4):
            d.line((0, row * H // 4, W, row * H // 4), fill=RULE_LIGHT, width=1)
        draw_fit_text(d, title, (64, 1020), "serif", 54, W - 128, INK, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_INK)
        return c
    # num == 170: Translucent Glassine Veil
    base = cover_crop(pil, W, H, focus=focus)
    veil = Image.blend(base, Image.new("RGB", (W, H), (246, 244, 238)), 0.62)
    c = veil.copy()
    c.paste(base.crop((96, 170, W - 96, 940)), (96, 170))
    d = ImageDraw.Draw(c)
    d.rectangle((96, 170, W - 96, 940), outline=PURE_WHITE, width=3)
    draw_fit_text(d, title, (96, 1008), "didot", 56, W - 192, INK, max_lines=2)
    _draw_footer(d, art, 1228, CHARCOAL)
    return c


def _board_18(num: int, pils: list[Image.Image], sources: Sequence[AstraArtworkInput], title: str, focus: tuple[float, float]) -> Image.Image:
    art, pil = sources[0], pils[0]
    if num == 171:  # 4-Corner Crop Brackets
        c = Image.new("RGB", (W, H), OBSIDIAN)
        box = paste_centered(c, pil, (110, 170, W - 110, 960))
        d = ImageDraw.Draw(c)
        x0, y0, x1, y1 = box
        arm = 44
        for px, py, dx, dy in ((x0 - 18, y0 - 18, 1, 1), (x1 + 18, y0 - 18, -1, 1), (x0 - 18, y1 + 18, 1, -1), (x1 + 18, y1 + 18, -1, -1)):
            d.line((px, py, px + dx * arm, py), fill=GOLD, width=4)
            d.line((px, py, px, py + dy * arm), fill=GOLD, width=4)
        draw_fit_text(d, title, (84, 1030), "didot", 58, W - 168, SOFT_IVORY, max_lines=2, align="center")
        _draw_footer(d, art, 1230, MUTED_LIGHT, align="center")
        return c
    if num == 172:  # Giant Split Monogram AF
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "bottom", 0.34, (14, 12, 10), 230)
        d = ImageDraw.Draw(c)
        d.text((48, 40), "A", font=F("didot", 240), fill=(255, 255, 255))
        d.text((W - 220, 40), "F", font=F("didot", 240), fill=(255, 255, 255))
        draw_fit_text(d, title, (64, 1020), "didot", 60, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, GOLD)
        return c
    if num == 173:  # Century Ruler & Needle (real year!)
        yr = parse_year(art.date) or 1880
        frac = max(0.0, min(1.0, (yr - 1400) / 560.0))
        c = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(c)
        d.line((116, 120, 116, 1180), fill=INK, width=2)
        for mark in range(1400, 1961, 100):
            my = int(120 + ((mark - 1400) / 560.0) * 1060)
            d.line((96, my, 126, my), fill=INK, width=2)
            d.text((24, my - 12), str(mark), font=F("mono", 18), fill=MUTED_INK)
        ny = int(120 + frac * 1060)
        d.polygon([(126, ny), (154, ny - 12), (154, ny + 12)], fill=VERMILION)
        d.line((154, ny, 200, ny), fill=VERMILION, width=3)
        paste_centered(c, pil, (210, 130, W - 64, 940), shadow=True)
        draw_fit_text(d, title, (210, 996), "didot", 54, W - 274, INK, max_lines=2)
        draw_fit_text(d, byline_for(art), (210, 1216), "mono", 20, W - 274, MUTED_INK, max_lines=1)
        return c
    if num == 174:  # Pixel-Stretch Waterfall (real stretched pixels!)
        top_art = cover_crop(pil, W, 760, focus=focus)
        last_row = top_art.crop((0, 756, W, 758)).resize((W, H - 760), Image.Resampling.BILINEAR)
        c = Image.new("RGB", (W, H), OBSIDIAN)
        c.paste(top_art, (0, 0))
        c.paste(last_row, (0, 760))
        apply_scrim(c, "bottom", 0.36, (14, 12, 10), 215)
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, PURE_WHITE)
        draw_fit_text(d, title, (64, 1020), "didot", 60, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, SOFT_IVORY)
        return c
    if num == 175:  # Modern Pill-Tag Cover
        c = cover_crop(pil, W, H, focus=focus)
        apply_scrim(c, "bottom", 0.36, (16, 16, 16), 235)
        d = ImageDraw.Draw(c)
        d.rounded_rectangle((64, 64, 340, 120), radius=28, fill=PURE_WHITE)
        d.text((92, 80), "ART.FOLIO", font=F("din_b", 26), fill=INK)
        draw_fit_text(d, title, (64, 1018), "serif", 58, W - 128, PURE_WHITE, max_lines=2)
        _draw_footer(d, art, 1230, GOLD)
        return c
    if num == 176:  # Double-Framed Mirror
        c = Image.new("RGB", (W, H), (242, 238, 228))
        d = ImageDraw.Draw(c)
        d.rectangle((72, 110, W - 72, 970), outline=INK, width=2)
        d.rectangle((100, 138, W - 100, 942), outline=GOLD, width=2)
        paste_centered(c, pil, (132, 170, W - 132, 910))
        draw_fit_text(d, title, (72, 1026), "didot", 56, W - 144, INK, max_lines=2, align="center")
        _draw_footer(d, art, 1232, MUTED_INK, align="center")
        return c
    if num == 177:  # Horizontal Ribbon Cut
        c = Image.new("RGB", (W, H), OBSIDIAN)
        crop = cover_crop(pil, W - 120, 920, focus=focus)
        c.paste(crop.crop((0, 0, W - 120, 290)), (60, 110))
        c.paste(crop.crop((0, 290, W - 120, 580)), (60, 520))
        c.paste(crop.crop((0, 580, W - 120, 870)), (60, 840))
        d = ImageDraw.Draw(c)
        draw_fit_text(d, title.upper(), (60, 418), "din_b", 64, W - 120, GOLD, max_lines=1, align="center")
        _draw_footer(d, art, 1210, SOFT_IVORY, align="center")
        return c
    if num == 178:  # Asymmetric Right-Bleed
        c = Image.new("RGB", (W, H), PAPER)
        c.paste(cover_crop(pil, 840, 960, focus=focus), (240, 120))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 48, 120, VERMILION)
        draw_fit_text(d, title, (48, 1118), "didot", 54, W - 96, INK, max_lines=2)
        _draw_footer(d, art, 1268, MUTED_INK)
        return c
    if num == 179:  # Crosshair Quartet Split
        c = Image.new("RGB", (W, H), DEEP_NIGHT)
        crop = cover_crop(pil, W - 128, 860, focus=focus)
        hw, hh = (W - 128) // 2, 430
        c.paste(crop.crop((0, 0, hw - 8, hh - 8)), (64, 130))
        c.paste(crop.crop((hw + 8, 0, W - 128, hh - 8)), (64 + hw + 8, 130))
        c.paste(crop.crop((0, hh + 8, hw - 8, 860)), (64, 130 + hh + 8))
        c.paste(crop.crop((hw + 8, hh + 8, W - 128, 860)), (64 + hw + 8, 130 + hh + 8))
        d = ImageDraw.Draw(c)
        draw_brand_tag(d, 64, 54, GOLD, "CROSSHAIR QUARTET")
        draw_fit_text(d, title, (64, 1044), "didot", 56, W - 128, SOFT_IVORY, max_lines=2)
        _draw_footer(d, art, 1236, MUTED_LIGHT)
        return c
    # num == 180: Museum Room Number (dynamic room derived from year!)
    yr = parse_year(art.date) or 1880
    room_num = max(1, (yr // 100) % 25)
    c = cover_crop(pil, W, H, focus=focus)
    apply_scrim(c, "bottom", 0.34, (14, 12, 10), 235)
    d = ImageDraw.Draw(c)
    d.rectangle((64, 1040, 260, 1250), fill=VERMILION)
    d.text((88, 1062), "ROOM", font=F("mono_b", 24), fill=PURE_WHITE)
    d.text((88, 1104), f"{room_num:02d}", font=F("din_b", 104), fill=PURE_WHITE)
    draw_fit_text(d, title, (296, 1044), "didot", 54, W - 360, PURE_WHITE, max_lines=2)
    draw_fit_text(d, byline_for(art), (296, 1210), "mono", 19, W - 360, SOFT_IVORY, max_lines=1)
    return c


_BOARDS = (_board_10, _board_11, _board_12, _board_13, _board_14, _board_15, _board_16, _board_17, _board_18)


def render_neon_boards_10_to_18(
    number: int,
    sources: Sequence[AstraArtworkInput],
    editorial_title: str,
    focus: tuple[float, float],
) -> Image.Image:
    pils = [load_source_image(item.validated_path()) for item in sources]
    board_idx = (number - 91) // 10
    title = editorial_title.strip() or sources[0].title
    return _BOARDS[board_idx](number, pils, sources, title, focus)
