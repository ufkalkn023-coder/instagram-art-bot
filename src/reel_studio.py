"""Local Reels production studio: 9:16 safe-zone covers & real 1080x1920 H.264 MP4 motion rendering."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from src.artwork_intelligence import detect_smart_focus, trim_museum_scan_borders
from src.astra_renderer import (
    AstraArtworkInput,
    extract_pigments,
    hexcol,
    ink,
    load_source_image,
)
from src.neon_kit import (
    F, GOLD, INK, MUTED_LIGHT, OBSIDIAN, PURE_WHITE, SOFT_IVORY,
    apply_scrim, byline_for, cover_crop, draw_brand_tag, draw_fit_text,
    paste_centered, short_museum,
)

REEL_W = 1080
REEL_H = 1920
GRID_W = 1080
GRID_H = 1350
GRID_TOP_OFFSET = (REEL_H - GRID_H) // 2  # 285px -> exact center 4:5 crop on Instagram profile grid

ReelMotionPreset = Literal[
    "detail_to_full",
    "slow_pan_horizontal",
    "palette_reveal",
    "chiaroscuro_zoom",
]

REEL_MOTION_PRESETS: dict[str, dict[str, str | int]] = {
    "detail_to_full": {
        "label": "Makrodan Bütüne Açılış (Ken Burns Zoom-Out)",
        "duration_sec": 9,
        "beats": "0-2.5s makro kanca · 2.5-7s yavaş geri çekilme · 7-9s tam eser & müze künyesi",
    },
    "slow_pan_horizontal": {
        "label": "Yatay Galeri Taraması (Slow Pan)",
        "duration_sec": 9,
        "beats": "0-2s sol kenar girişi · 2-7s yumuşak yatay kaydırma · 7-9s tam kompozisyon",
    },
    "palette_reveal": {
        "label": "Pigmentten Tabloya Geçiş (Palette Reveal)",
        "duration_sec": 8,
        "beats": "0-2.5s 5 gerçek pigment bloğu · 2.5-6s tablo açılışı · 6-8s künye & soru",
    },
    "chiaroscuro_zoom": {
        "label": "Işığa Doğru Yavaş Yakınlaşma (Chiaroscuro Push-In)",
        "duration_sec": 9,
        "beats": "0-2s karanlık galeri açılışı · 2-7s ışık odağına yakınlaşma · 7-9s başlık & künye",
    },
}


def render_reel_9x16_cover(
    grid_4x5_cover_path: str | Path,
    output_path: str | Path,
    *,
    show_safe_guides: bool = False,
) -> dict:
    """Wrap an existing 1080x1350 Astra/Neon cover into a 1080x1920 (9:16) Instagram Reels cover
    so the exact 4:5 center crop on the 3x3 profile grid remains pixel-identical.
    """
    src_path = Path(grid_4x5_cover_path).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"4:5 grid cover not found: {src_path}")

    with Image.open(src_path) as raw:
        grid_im = raw.convert("RGB")
        if grid_im.size != (GRID_W, GRID_H):
            grid_im = grid_im.resize((GRID_W, GRID_H), Image.Resampling.LANCZOS)

    # Create dark atmospheric 9:16 backdrop derived from the cover itself
    backdrop = cover_crop(grid_im, REEL_W, REEL_H, focus=(0.5, 0.5))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(36))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.38)

    canvas = backdrop.copy()
    canvas.paste(grid_im, (0, GRID_TOP_OFFSET))

    # Subtle hairline transition between the 4:5 profile crop and 9:16 vertical extensions
    draw = ImageDraw.Draw(canvas)
    draw.line((0, GRID_TOP_OFFSET, REEL_W, GRID_TOP_OFFSET), fill=(255, 249, 237), width=1)
    draw.line((0, GRID_TOP_OFFSET + GRID_H, REEL_W, GRID_TOP_OFFSET + GRID_H), fill=(255, 249, 237), width=1)

    if show_safe_guides:
        # Visualize Instagram Reels HUD safe margins (top 220px, bottom 420px, right 150px)
        draw.rectangle((48, GRID_TOP_OFFSET, REEL_W - 150, GRID_TOP_OFFSET + GRID_H), outline=GOLD, width=2)

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".9x16.tmp.jpg")
    try:
        canvas.save(tmp, format="JPEG", quality=92, optimize=True, subsampling=0)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)

    return {
        "output_path": str(dest),
        "width": REEL_W,
        "height": REEL_H,
        "grid_crop_top": GRID_TOP_OFFSET,
        "grid_crop_bottom": GRID_TOP_OFFSET + GRID_H,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }


def _build_storyboard_frames(
    artwork: AstraArtworkInput,
    editorial_title: str,
    preset: ReelMotionPreset,
    work_dir: Path,
    *,
    zoom_start: tuple[float, float] | None = None,
    zoom_end: tuple[float, float] | None = None,
) -> list[Path]:
    """Build 3 crisp 1080x1920 museum-grade keyframe stills with auto-focus, border trim & custom zoom path."""
    pil_raw = load_source_image(artwork.validated_path())
    pil, _ = trim_museum_scan_borders(pil_raw)
    auto_focus = artwork.focal_point() if artwork.focus is not None else detect_smart_focus(pil)
    start_pt = zoom_start or auto_focus
    end_pt = zoom_end or auto_focus
    focus = start_pt
    pal = [c for c, _ in extract_pigments(pil, 5)]
    frames: list[Path] = []

    # Keyframe 1: 0-3s Hook Frame (safe-zone aware: title sits inside y=340..1460, x=64..900)
    if preset == "palette_reveal":
        f1 = Image.new("RGB", (REEL_W, REEL_H), OBSIDIAN)
        d1 = ImageDraw.Draw(f1)
        band_h = 160
        for idx, col in enumerate(pal):
            y0 = 380 + idx * (band_h + 22)
            d1.rectangle((64, y0, REEL_W - 160, y0 + band_h), fill=col)
            fg = PURE_WHITE if ink(col) != "#1a1915" else INK
            d1.text((96, y0 + 58), hexcol(col), font=F("mono_b", 32), fill=fg)
        draw_brand_tag(d1, 64, 250, GOLD, "5 REAL PIGMENTS FROM ONE MASTERWORK")
        draw_fit_text(d1, editorial_title, (64, 1340), "didot", 58, REEL_W - 220, PURE_WHITE, max_lines=2)
    elif preset == "slow_pan_horizontal":
        f1 = cover_crop(pil, REEL_W, REEL_H, focus=(max(0.18, focus[0] - 0.22), focus[1]), zoom=1.45)
        apply_scrim(f1, "bottom", 0.40, (12, 10, 9), 220)
        apply_scrim(f1, "top", 0.22, (12, 10, 9), 150)
        d1 = ImageDraw.Draw(f1)
        draw_brand_tag(d1, 64, 290, GOLD, "ART.FOLIO  //  GALLERY DRIFT")
        draw_fit_text(d1, editorial_title, (64, 1240), "didot", 66, REEL_W - 220, PURE_WHITE, max_lines=2)
    elif preset == "chiaroscuro_zoom":
        f1 = Image.new("RGB", (REEL_W, REEL_H), (12, 11, 10))
        paste_centered(f1, pil, (56, 360, REEL_W - 56, 1340))
        d1 = ImageDraw.Draw(f1)
        draw_brand_tag(d1, 64, 280, GOLD, "ART.FOLIO  //  LIGHT STUDY")
        draw_fit_text(d1, editorial_title, (64, 1380), "didot", 60, REEL_W - 220, SOFT_IVORY, max_lines=2)
    else:  # detail_to_full
        f1 = cover_crop(pil, REEL_W, REEL_H, focus=focus, zoom=2.35)
        apply_scrim(f1, "bottom", 0.42, (12, 10, 9), 230)
        apply_scrim(f1, "top", 0.22, (12, 10, 9), 160)
        d1 = ImageDraw.Draw(f1)
        draw_brand_tag(d1, 64, 290, GOLD, "LOOK CLOSELY  ·  01")
        draw_fit_text(d1, editorial_title, (64, 1240), "didot", 68, REEL_W - 220, PURE_WHITE, max_lines=2)

    p1 = work_dir / "kf_01.jpg"
    f1.save(p1, format="JPEG", quality=92)
    frames.append(p1)

    # Keyframe 2: Mid reveal frame (tracks zoom_end when custom zoom path is set)
    if preset == "slow_pan_horizontal":
        f2 = cover_crop(pil, REEL_W, REEL_H, focus=(min(0.82, end_pt[0] + 0.22), end_pt[1]), zoom=1.45)
    elif preset == "chiaroscuro_zoom":
        f2 = cover_crop(pil, REEL_W, REEL_H, focus=end_pt, zoom=2.1)
    else:
        f2 = cover_crop(pil, REEL_W, REEL_H, focus=end_pt, zoom=1.18)
    apply_scrim(f2, "bottom", 0.36, (12, 10, 9), 215)
    d2 = ImageDraw.Draw(f2)
    draw_fit_text(d2, artwork.title, (64, 1320), "serif", 48, REEL_W - 220, PURE_WHITE, max_lines=2)
    draw_fit_text(d2, f"{artwork.artist} ({artwork.date or 'Archive'})", (64, 1460), "mono", 24, REEL_W - 220, GOLD, max_lines=1)
    p2 = work_dir / "kf_02.jpg"
    f2.save(p2, format="JPEG", quality=92)
    frames.append(p2)

    # Keyframe 3: Full uncropped museum artwork + placard + CTA
    f3 = Image.new("RGB", (REEL_W, REEL_H), OBSIDIAN)
    paste_centered(f3, pil, (56, 320, REEL_W - 56, 1260), shadow=True)
    d3 = ImageDraw.Draw(f3)
    draw_brand_tag(d3, 64, 240, GOLD, f"ART.FOLIO  ·  {short_museum(artwork.institution)}")
    draw_fit_text(d3, artwork.title, (64, 1300), "didot", 48, REEL_W - 220, SOFT_IVORY, max_lines=2)
    draw_fit_text(d3, byline_for(artwork), (64, 1434), "mono", 22, REEL_W - 220, MUTED_LIGHT, max_lines=1)
    draw_fit_text(d3, "Which detail caught your eye first?", (64, 1500), "georgia_i", 32, REEL_W - 220, GOLD, max_lines=1)
    p3 = work_dir / "kf_03.jpg"
    f3.save(p3, format="JPEG", quality=92)
    frames.append(p3)

    return frames


def verify_reel_mp4(video_path: str | Path) -> dict:
    """Verify an exported/rendered Reel MP4 with ffprobe."""
    path = Path(video_path).resolve()
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError(f"Reel MP4 file missing or too small: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"path": str(path), "bytes": path.stat().st_size, "probed": False}
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,pix_fmt,duration",
        "-of", "default=noprint_wrappers=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    info = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "probed": True,
        "width": int(info.get("width", 0)),
        "height": int(info.get("height", 0)),
        "codec": info.get("codec_name", ""),
        "pix_fmt": info.get("pix_fmt", ""),
        "duration_sec": round(float(info.get("duration", 0.0)), 2),
    }


def default_kinetic_hook(artwork: AstraArtworkInput, preset: ReelMotionPreset) -> str:
    """Concise, mute-friendly 1-sentence observation shown during the first 3 seconds of a Reel."""
    if preset == "palette_reveal":
        return f"Five real pigments drawn directly from {artwork.artist}'s surface."
    if preset == "slow_pan_horizontal":
        return f"Follow the eye slowly across {artwork.title}."
    if preset == "chiaroscuro_zoom":
        return "Notice where the brightest light cuts through the shadow."
    return "Look closely at this detail before the full canvas opens."


def _render_kinetic_caption_png(caption_text: str, work_dir: Path) -> Path:
    """Render a transparent 1080x1920 safe-zone serif caption bar for mute Reel viewers (first 3s)."""
    overlay = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Safe zone: y=1470..1620 sits above Reels bottom UI & away from right-edge icons
    box_x0, box_y0, box_x1, box_y1 = 56, 1472, REEL_W - 148, 1618
    draw.rounded_rectangle(
        (box_x0, box_y0, box_x1, box_y1),
        radius=16,
        fill=(14, 14, 13, 206),
        outline=(212, 175, 95, 190),
        width=2,
    )
    draw.text((box_x0 + 24, box_y0 + 18), "CURATOR'S NOTE · 0:03", font=F("mono_b", 16), fill=(212, 175, 95, 255))
    clean = (caption_text or "").strip()[:110]
    draw_fit_text(
        draw,
        clean,
        (box_x0 + 24, box_y0 + 50),
        "georgia_i",
        30,
        (box_x1 - box_x0) - 48,
        (250, 248, 242, 255),
        max_lines=2,
    )
    out = work_dir / "kinetic_caption.png"
    overlay.save(out, format="PNG")
    return out


def render_local_reel_mp4(
    artwork: AstraArtworkInput,
    editorial_title: str,
    output_path: str | Path,
    *,
    preset: ReelMotionPreset = "detail_to_full",
    fps: int = 30,
    zoom_start: tuple[float, float] | None = None,
    zoom_end: tuple[float, float] | None = None,
    seamless_loop: bool = True,
    kinetic_hook: str | None = None,
) -> dict:
    """Render a real Instagram-ready 1080x1920 H.264 MP4 Reel locally using FFmpeg,
    supporting custom zoom paths (#06), auto-focus (#09), seamless loop (#07),
    and 3-second safe-zone Kinetic Caption overlay for mute viewers.
    """
    if preset not in REEL_MOTION_PRESETS:
        raise ValueError(f"Unknown Reel motion preset: {preset}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for local Reel MP4 rendering but was not found on PATH")

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = int(REEL_MOTION_PRESETS[preset]["duration_sec"])
    seg = max(2.4, duration_sec / 3.0)
    xfade = 0.65
    loop_tail = 0.55 if seamless_loop else 0.0
    hook_text = (kinetic_hook if kinetic_hook is not None else default_kinetic_hook(artwork, preset)).strip()

    with tempfile.TemporaryDirectory(prefix=".reel-render-") as tmp:
        work = Path(tmp)
        kf1, kf2, kf3 = _build_storyboard_frames(
            artwork,
            editorial_title,
            preset,
            work,
            zoom_start=zoom_start,
            zoom_end=zoom_end,
        )
        cap_png = _render_kinetic_caption_png(hook_text, work) if hook_text else None
        tmp_mp4 = dest.with_suffix(".render.tmp.mp4")

        d1 = seg + xfade
        d2 = seg + xfade
        d3 = seg + (xfade if seamless_loop else 0.0)
        o1 = seg
        o2 = seg * 2.0
        o3 = seg * 3.0 - 0.15

        inputs: list[str] = [
            "-loop", "1", "-t", f"{d1:.2f}", "-i", str(kf1),
            "-loop", "1", "-t", f"{d2:.2f}", "-i", str(kf2),
            "-loop", "1", "-t", f"{d3:.2f}", "-i", str(kf3),
        ]
        next_idx = 3
        if seamless_loop:
            inputs.extend(["-loop", "1", "-t", f"{loop_tail + 0.15:.2f}", "-i", str(kf1)])
            loop_idx = next_idx
            next_idx += 1
        else:
            loop_idx = -1

        if cap_png is not None:
            inputs.extend(["-loop", "1", "-t", "3.10", "-i", str(cap_png)])
            cap_idx = next_idx
            next_idx += 1
        else:
            cap_idx = -1

        audio_dur = duration_sec + (1.0 if seamless_loop else 0.2)
        inputs.extend(["-f", "lavfi", "-t", f"{audio_dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"])
        audio_idx = next_idx

        filters = [
            f"[0:v]scale=1140:2026,crop=1080:1920:'(iw-ow)/2+18*t/{d1:.2f}':'(ih-oh)/2',setsar=1,scale=out_range=tv,format=yuv420p[v0]",
            f"[1:v]scale=1140:2026,crop=1080:1920:'(iw-ow)/2-18*t/{d2:.2f}':'(ih-oh)/2',setsar=1,scale=out_range=tv,format=yuv420p[v1]",
            "[2:v]scale=1080:1920:out_range=tv,setsar=1,format=yuv420p[v2]",
            f"[v0][v1]xfade=transition=fade:duration={xfade:.2f}:offset={o1:.2f}[v01]",
        ]
        if seamless_loop:
            filters.extend([
                f"[{loop_idx}:v]scale=1140:2026,crop=1080:1920:'(iw-ow)/2':'(ih-oh)/2',setsar=1,scale=out_range=tv,format=yuv420p[vloop]",
                f"[v01][v2]xfade=transition=fade:duration={xfade:.2f}:offset={o2:.2f}[v012]",
                f"[v012][vloop]xfade=transition=fade:duration={loop_tail:.2f}:offset={o3:.2f}[vbase]",
            ])
        else:
            filters.append(f"[v01][v2]xfade=transition=fade:duration={xfade:.2f}:offset={o2:.2f}[vbase]")

        if cap_idx >= 0:
            filters.append(
                f"[{cap_idx}:v]format=rgba,fade=t=in:st=0.20:d=0.40:alpha=1,fade=t=out:st=2.50:d=0.50:alpha=1[vcap]"
            )
            filters.append("[vbase][vcap]overlay=0:0:eof_action=pass,scale=out_range=tv,format=yuv420p[vout]")
        else:
            filters.append("[vbase]scale=out_range=tv,format=yuv420p[vout]")

        cmd = [
            ffmpeg, "-y",
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-map", f"{audio_idx}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(tmp_mp4),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg Reel render failed: {proc.stderr[-600:]}")
            tmp_mp4.replace(dest)
        finally:
            tmp_mp4.unlink(missing_ok=True)

    verified = verify_reel_mp4(dest)
    return {
        **verified,
        "preset": preset,
        "preset_label": REEL_MOTION_PRESETS[preset]["label"],
        "beats": REEL_MOTION_PRESETS[preset]["beats"],
        "seamless_loop": seamless_loop,
        "kinetic_hook": hook_text,
        "zoom_start": list(zoom_start) if zoom_start else None,
        "zoom_end": list(zoom_end) if zoom_end else None,
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }
