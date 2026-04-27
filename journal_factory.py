#!/usr/bin/env python3
"""
Multimedia Journal Factory — GUI edition
Full-bleed photos with per-page text overlay. No pages.txt needed.
"""

import base64
import re
import shutil
import subprocess
import threading
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk


# ── constants ──────────────────────────────────────────────────────────────────

SPREAD_W, SPREAD_H = 1920, 1080
PAGE_W,   PAGE_H   = 960,  1080

PAGE_DUR  = 4
TRANS_DUR = 1

SUPPORTED_IMG = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".bmp", ".tiff", ".tif", ".gif",
}

FONT_CHOICES = [
    "Georgia",
    "Arial",
    "Times New Roman",
    "Courier New",
    "Palatino Linotype",
    "Custom (.ttf file)…",
]

# ── JS library cache (downloaded once, embedded in every HTML output) ──────────

_JS_CACHE = Path.home() / ".journal_factory" / "js_cache"
_JS_LIBS  = {
    "page-flip":   "https://cdn.jsdelivr.net/npm/page-flip@2.0.7/dist/js/page-flip.browser.js",
    "jspdf":       "https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js",
    "html2canvas": "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js",
}

def _get_js(name, log=None):
    """Return JS library text, downloading and caching on first use."""
    _JS_CACHE.mkdir(parents=True, exist_ok=True)
    cached = _JS_CACHE / f"{name}.js"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    url = _JS_LIBS[name]
    if log:
        log(f"  Downloading {name} (one-time) …")
    with urllib.request.urlopen(url, timeout=30) as r:
        content = r.read().decode("utf-8")
    cached.write_text(content, encoding="utf-8")
    return content


_FFMPEG_FALLBACKS = [r"C:\Program Files\ShareX\ffmpeg.exe"]

def _find_ffmpeg():
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    for fb in _FFMPEG_FALLBACKS:
        if Path(fb).is_file():
            return fb
    return None

FFMPEG = _find_ffmpeg()


# ── image helpers ──────────────────────────────────────────────────────────────

def get_images(folder):
    return sorted(
        [f for f in Path(folder).iterdir() if f.suffix.lower() in SUPPORTED_IMG],
        key=lambda f: f.name,
    )


def find_file(folder, stems, exts):
    for stem in stems:
        for ext in exts:
            c = Path(folder) / f"{stem}{ext}"
            if c.exists():
                return c
    return None


def load_font(name_or_path, size):
    if isinstance(name_or_path, Path):
        try:
            return ImageFont.truetype(str(name_or_path), size)
        except (OSError, IOError):
            pass
    else:
        for candidate in [
            f"C:/Windows/Fonts/{name_or_path}.ttf",
            f"C:/Windows/Fonts/{name_or_path.replace(' ', '')}.ttf",
            f"C:/Windows/Fonts/{name_or_path.replace(' ', '').lower()}.ttf",
        ]:
            try:
                return ImageFont.truetype(candidate, size)
            except (OSError, IOError):
                pass
    return ImageFont.load_default()


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def fit_bg(img_path, w, h):
    """Fill w×h with the image, blurry-bg fill for mismatched aspect ratios."""
    src = Image.open(img_path).convert("RGB")
    r   = src.width / src.height
    tr  = w / h
    bw, bh = (w, int(w / r)) if r > tr else (int(h * r), h)
    bg = src.resize((bw * 2, bh * 2), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
    bg = bg.resize((w, h), Image.LANCZOS)
    fw, fh = (w, int(w / r)) if r > tr else (int(h * r), h)
    fg = src.resize((fw, fh), Image.LANCZOS)
    canvas = bg.copy()
    canvas.paste(fg, ((w - fw) // 2, (h - fh) // 2))
    return canvas


def wrap_lines(text, font, draw, max_w):
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
            test = f"{cur} {word}".strip()
            if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def wrap_and_fit(text, font_path, max_pt, min_pt, box_w, box_h):
    """Return (font, lines) at largest pt that fits in box."""
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for pt in range(max_pt, min_pt - 1, -2):
        font  = load_font(font_path, pt)
        lines = wrap_lines(text, font, dummy, box_w)
        if len(lines) * (font.size * 1.65) <= box_h:
            return font, lines
    return load_font(font_path, min_pt), wrap_lines(text, load_font(font_path, min_pt), dummy, box_w)


def gradient_band(w, h, alpha_max=195):
    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd   = ImageDraw.Draw(band)
    for row in range(h):
        bd.line([(0, row), (w, row)], fill=(0, 0, 0, int(alpha_max * row / h)))
    return band


# ── page renderers ─────────────────────────────────────────────────────────────

def render_journal_page(img_path, text, font_path, color_hex, page_num):
    """960×1080 full-bleed photo with optional text overlay at bottom."""
    img = fit_bg(img_path, PAGE_W, PAGE_H).convert("RGBA")

    if text and text.strip():
        font, lines = wrap_and_fit(text, font_path,
                                   max_pt=24, min_pt=11,
                                   box_w=PAGE_W - 60,
                                   box_h=PAGE_H // 2 - 40)
        lh      = int(font.size * 1.65)
        band_h  = len(lines) * lh + 56
        img.alpha_composite(gradient_band(PAGE_W, band_h), (0, PAGE_H - band_h))
        img  = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        color = hex_to_rgb(color_hex)
        y = PAGE_H - band_h + 24
        for line in lines:
            if line:
                draw.text((30, y), line, font=font, fill=color)
            y += lh if line else lh // 2
    else:
        img  = img.convert("RGB")
        draw = ImageDraw.Draw(img)

    num_font = load_font(font_path, 14)
    nb  = draw.textbbox((0, 0), str(page_num), font=num_font)
    draw.text(((PAGE_W - (nb[2] - nb[0])) // 2, PAGE_H - 18),
              str(page_num), font=num_font, fill=(200, 190, 170))
    return img


def apply_frame(img, style, color_hex, thickness, frame_img_path=None):
    """
    Draw a decorative frame onto a PIL Image (in-place) and return it.
    style: "None" | "Simple" | "Double" | "Vintage" | "Ornate Corners" | "3D Bevel"
    frame_img_path: optional PNG overlay (transparent centre) — composited last.
    """
    if style == "None" and not frame_img_path:
        return img

    w, h   = img.size
    draw   = ImageDraw.Draw(img)
    color  = hex_to_rgb(color_hex)
    t      = max(1, thickness)
    pad    = 18   # inset from edge

    if style == "Simple":
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1],
                           outline=color)

    elif style == "Double":
        gap = max(4, t * 2)
        for i in range(t):
            draw.rectangle([pad+i,       pad+i,       w-pad-i-1,       h-pad-i-1],
                           outline=color)
            draw.rectangle([pad+gap+i,   pad+gap+i,   w-pad-gap-i-1,   h-pad-gap-i-1],
                           outline=color)

    elif style == "Vintage":
        # Outer double line + small corner squares
        gap = max(5, t * 2)
        for i in range(t):
            draw.rectangle([pad+i,     pad+i,     w-pad-i-1,     h-pad-i-1],     outline=color)
            draw.rectangle([pad+gap+i, pad+gap+i, w-pad-gap-i-1, h-pad-gap-i-1], outline=color)
        sq = t * 4 + gap + pad
        for cx, cy in [(pad, pad), (w-pad-gap*2, pad),
                       (pad, h-pad-gap*2), (w-pad-gap*2, h-pad-gap*2)]:
            draw.rectangle([cx, cy, cx+gap*2, cy+gap*2], outline=color, width=t)

    elif style == "Ornate Corners":
        # Light border + decorative L-shapes at each corner
        for i in range(t):
            draw.rectangle([pad+i, pad+i, w-pad-i-1, h-pad-i-1], outline=color)
        arm = min(80, w // 8)   # length of each corner arm
        corners = [
            (pad, pad, pad+arm, pad, pad, pad+arm),           # top-left
            (w-pad-arm, pad, w-pad, pad, w-pad, pad+arm),     # top-right
            (pad, h-pad-arm, pad, h-pad, pad+arm, h-pad),     # bottom-left
            (w-pad-arm, h-pad, w-pad, h-pad, w-pad, h-pad-arm),  # bottom-right
        ]
        for x1,y1,x2,y2,x3,y3 in corners:
            draw.line([(x1,y1),(x2,y2)], fill=color, width=t*2)
            draw.line([(x1,y1),(x3,y3)], fill=color, width=t*2)
        # small diamond centre-of-each-edge
        mid_pts = [(w//2, pad), (w//2, h-pad), (pad, h//2), (w-pad, h//2)]
        d = max(5, t*3)
        for mx, my in mid_pts:
            draw.polygon([(mx,my-d),(mx+d,my),(mx,my+d),(mx-d,my)], fill=color)

    elif style == "3D Bevel":
        # Raised picture-frame look: 4 trapezoid faces with light top/left
        # and dark bottom/right to simulate depth.
        bw    = t * 5 + 12          # total bevel width (px)
        o, i_ = pad, pad + bw       # outer edge, inner edge
        # Derive light (highlight) and dark (shadow) variants of the base colour
        light = tuple(min(255, int(v * 1.40 + 55)) for v in color)
        dark  = tuple(max(0,   int(v * 0.40))       for v in color)
        mid   = color

        # Fill the frame band with base colour first (covers gaps between faces)
        draw.rectangle([o, o, w-o, h-o], outline=mid, width=bw)

        # Top face (light — catches the light from above)
        draw.polygon([(o, o), (w-o, o), (w-i_, i_), (i_, i_)], fill=light)
        # Left face (light — catches the light from the left)
        draw.polygon([(o, o), (i_, i_), (i_, h-i_), (o, h-o)], fill=light)
        # Bottom face (dark — in shadow)
        draw.polygon([(o, h-o), (i_, h-i_), (w-i_, h-i_), (w-o, h-o)], fill=dark)
        # Right face (dark — in shadow)
        draw.polygon([(w-o, o), (w-o, h-o), (w-i_, h-i_), (w-i_, i_)], fill=dark)

        # Thin inner-edge bevel (sunken inner lip for extra depth)
        lip = max(1, t // 2)
        for j in range(lip):
            draw.line([(i_+j, i_+j), (w-i_-j, i_+j)],   fill=dark)   # inner top
            draw.line([(i_+j, i_+j), (i_+j, h-i_-j)],   fill=dark)   # inner left
            draw.line([(i_+j, h-i_-j), (w-i_-j, h-i_-j)], fill=light) # inner bottom
            draw.line([(w-i_-j, i_+j), (w-i_-j, h-i_-j)], fill=light) # inner right

    # ── custom PNG frame overlay ──────────────────────────────────────────────
    if frame_img_path and Path(frame_img_path).exists():
        try:
            frame = Image.open(frame_img_path).convert("RGBA").resize(
                (w, h), Image.LANCZOS)
            base = img.convert("RGBA")
            base.alpha_composite(frame)
            img = base.convert("RGB")
        except Exception:
            pass

    return img


def render_video_spread(img_path, text, font_path, color_hex, page_num,
                        page_bg="#fcf8f0", font_size=0,
                        page_bg_img=None, page_img_opacity=80,
                        frame_style="None", frame_color="#8B7355",
                        frame_thickness=4, frame_img=None,
                        page_num_pos="Bottom Center",
                        frame_padding=0,
                        page_num_size=14):
    """
    1920×1080 book-spread video frame.
    Left 960 px  = photo (blurry-bg fill).
    Right 960 px = journal text page — flat colour OR a background image.
    font_size: override pt size (0 = auto-fit, min 28 pt for readability).
    page_num_pos: "Bottom Left/Center/Right" or "Top Left/Center/Right"
    """
    spread = Image.new("RGB", (SPREAD_W, SPREAD_H))

    # ── left: photo ───────────────────────────────────────────────────────────
    spread.paste(fit_bg(img_path, PAGE_W, PAGE_H), (0, 0))

    # ── right: text page ──────────────────────────────────────────────────────
    bg = hex_to_rgb(page_bg) if page_bg else (252, 248, 240)
    txt_img  = Image.new("RGB", (PAGE_W, PAGE_H), bg)

    # Optional background image — composited at chosen opacity
    if page_bg_img and Path(page_bg_img).exists():
        try:
            bg_src = fit_bg(page_bg_img, PAGE_W, PAGE_H).convert("RGBA")
            alpha  = int(255 * page_img_opacity / 100)
            # Apply overall opacity by scaling the alpha channel
            r, g, b, a = bg_src.split()
            a = a.point(lambda x: int(x * alpha / 255))
            bg_src = Image.merge("RGBA", (r, g, b, a))
            base   = txt_img.convert("RGBA")
            base.alpha_composite(bg_src)
            txt_img = base.convert("RGB")
        except Exception:
            pass   # fall back to flat colour silently
    draw     = ImageDraw.Draw(txt_img)

    # Thin decorative border
    draw.rectangle([18, 18, PAGE_W - 18, PAGE_H - 18],
                   outline=(200, 192, 180), width=1)

    # Margins: expand inward to keep text inside any active frame
    _fi  = _calc_frame_inset(frame_style, frame_thickness, frame_img,
                              user_extra=frame_padding)
    mx   = max(72, _fi + 12)
    my   = max(80, _fi + 12)
    box_w    = PAGE_W - mx * 2
    box_h    = PAGE_H - my * 2 - 60   # 60 px for page number row

    if text and text.strip():
        if font_size > 0:
            # user-set size — still word-wrap but don't shrink
            font  = load_font(font_path, font_size)
            lines = wrap_lines(text, font, draw, box_w)
        else:
            # auto-fit: start big (52 pt), never go below 28 pt for video legibility
            font, lines = wrap_and_fit(text, font_path,
                                       max_pt=52, min_pt=28,
                                       box_w=box_w, box_h=box_h)
    else:
        # blank page — just decorative lines like a real notebook
        font  = load_font(font_path, 32)
        lines = []
        line_color = (220, 212, 200)
        line_gap   = 52
        for ly in range(my + 20, PAGE_H - 80, line_gap):
            draw.line([(mx, ly), (PAGE_W - mx, ly)],
                      fill=line_color, width=1)

    color = hex_to_rgb(color_hex)
    lh    = int(font.size * 1.65)
    y     = my
    for line in lines:
        if line:
            draw.text((mx, y), line, font=font, fill=color)
        y += lh if line else lh // 2

    # Page number — position and size follow user settings
    num_font = load_font(font_path, max(8, page_num_size))
    nb  = draw.textbbox((0, 0), str(page_num), font=num_font)
    nw, nh = nb[2] - nb[0], nb[3] - nb[1]
    if "Left"  in page_num_pos: nx = mx
    elif "Right" in page_num_pos: nx = PAGE_W - nw - mx
    else:                          nx = (PAGE_W - nw) // 2
    ny = 28 if "Top" in page_num_pos else PAGE_H - 46
    draw.text((nx, ny), str(page_num), font=num_font, fill=(158, 148, 135))

    # ── 3D depth effects on the text page ────────────────────────────────────
    # Wider spine shadow — curved falloff for a realistic depth gradient
    for i in range(32):
        shade = int(130 * ((1 - i / 32) ** 1.6))
        draw.line([(i, 0), (i, PAGE_H)], fill=(35, 22, 10, shade))

    # Subtle spine-light highlight (bright edge right after the darkest shadow)
    for i in range(10):
        shade = int(35 * (1 - i / 10))
        draw.line([(32 + i, 0), (32 + i, PAGE_H)], fill=(255, 245, 225, shade))

    # Right-edge shadow — page curl / thickness illusion
    for i in range(10):
        shade = int(70 * (1 - i / 10))
        draw.line([(PAGE_W - 1 - i, 0), (PAGE_W - 1 - i, PAGE_H)],
                  fill=(0, 0, 0, shade))

    # Top-edge darkening (paper thickness / book binding)
    for i in range(6):
        shade = int(45 * (1 - i / 6))
        draw.line([(0, i), (PAGE_W, i)], fill=(20, 14, 8, shade))

    # Bottom-edge darkening
    for i in range(6):
        shade = int(35 * (1 - i / 6))
        draw.line([(0, PAGE_H - 1 - i), (PAGE_W, PAGE_H - 1 - i)],
                  fill=(0, 0, 0, shade))

    spread.paste(txt_img, (PAGE_W, 0))

    # ── 3D depth on the full spread ───────────────────────────────────────────
    sd = ImageDraw.Draw(spread)

    # Dramatic centre spine — wider gradient + bright highlight line
    for i in range(22):
        shade = int(160 * ((1 - i / 22) ** 1.5))
        sd.line([(PAGE_W - 6 + i, 0), (PAGE_W - 6 + i, SPREAD_H)],
                fill=(25, 15, 6, shade))
    for i in range(5):
        shade = int(55 * (1 - i / 5))
        sd.line([(PAGE_W - 8 - i, 0), (PAGE_W - 8 - i, SPREAD_H)],
                fill=(220, 195, 155, shade))

    # Outer left edge of spread — book cover curving away
    for i in range(8):
        shade = int(60 * (1 - i / 8))
        sd.line([(i, 0), (i, SPREAD_H)], fill=(0, 0, 0, shade))

    # Outer right edge of spread
    for i in range(8):
        shade = int(50 * (1 - i / 8))
        sd.line([(SPREAD_W - 1 - i, 0), (SPREAD_W - 1 - i, SPREAD_H)],
                fill=(0, 0, 0, shade))

    # Top-edge of full spread (book binding shadow)
    for i in range(8):
        shade = int(55 * (1 - i / 8))
        sd.line([(0, i), (SPREAD_W, i)], fill=(15, 10, 5, shade))

    # Bottom shadow band — simulates book resting on a surface
    for i in range(14):
        shade = int(50 * (1 - i / 14))
        sd.line([(0, SPREAD_H - 1 - i), (SPREAD_W, SPREAD_H - 1 - i)],
                fill=(0, 0, 0, shade))

    # Apply decorative frame to the text page only (right half)
    txt_img = apply_frame(txt_img, frame_style, frame_color,
                          frame_thickness, frame_img)
    spread.paste(txt_img, (PAGE_W, 0))   # re-paste with frame on top

    return spread


def render_cover(img_path, title, subtitle, font_path, w, h):
    img  = fit_bg(img_path, w, h).convert("RGBA")
    img.alpha_composite(gradient_band(w, 320, 180), (0, h - 320))
    img  = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    tf = load_font(font_path, 64 if w < 1000 else 82)
    tb = draw.textbbox((0, 0), title, font=tf)
    draw.text(((w - (tb[2]-tb[0])) // 2, h - 258), title, font=tf, fill=(255, 252, 245))

    if subtitle:
        sf = load_font(font_path, 32 if w < 1000 else 40)
        sb = draw.textbbox((0, 0), subtitle, font=sf)
        draw.text(((w - (sb[2]-sb[0])) // 2, h - 152), subtitle, font=sf, fill=(215, 207, 190))
    return img


def render_back_cover(img_path, author_name, author_bio, author_photo, font_path, w, h):
    img    = fit_bg(img_path, w, h).convert("RGBA")
    pw     = min(PAGE_W, w // 2)
    panel  = Image.new("RGBA", (pw, h), (252, 248, 240, 228))
    img.alpha_composite(panel, (w - pw, 0))
    img  = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    px, py = w - pw + 50, 70

    if author_photo and Path(author_photo).exists():
        ap   = Image.open(author_photo).convert("RGB").resize((160, 160), Image.LANCZOS)
        mask = Image.new("L", (160, 160), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 160, 160), fill=255)
        img.paste(ap, (w - pw + (pw - 160) // 2, py), mask)
        py += 190

    draw.text((px, py), "About the Author", font=load_font(font_path, 30), fill=(108, 80, 50))
    py += 52
    draw.text((px, py), author_name, font=load_font(font_path, 44), fill=(38, 26, 14))
    py += 68

    bio_font, bio_lines = wrap_and_fit(author_bio, font_path,
                                       max_pt=28, min_pt=18,
                                       box_w=pw - 100, box_h=h - py - 50)
    lh = int(bio_font.size * 1.65)
    for line in bio_lines:
        draw.text((px, py), line, font=bio_font, fill=(52, 38, 22))
        py += lh if line else lh // 2
    return img


# ── output filename helper ─────────────────────────────────────────────────────

def _safe_stem(title: str) -> str:
    """
    Turn a journal title into a filesystem-safe stem and append a timestamp
    so every build produces a unique file — no accidental overwrites.
    e.g. "Run Free Run Wild" → "Run_Free_Run_Wild_2026-04-27_19-34-02"
    """
    stem  = re.sub(r'[\\/:*?"<>|]+', '', title)   # strip illegal chars
    stem  = re.sub(r'\s+', '_', stem.strip())       # spaces → underscores
    stem  = stem[:60]                               # cap length
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{stem}_{stamp}" if stem else f"journal_{stamp}"


# ── asset helpers ──────────────────────────────────────────────────────────────

def _img_mime(path):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            }.get(Path(path).suffix.lower(), "image/jpeg")

def _audio_mime(path):
    return {".mp3": "audio/mpeg", ".ogg": "audio/ogg",
            ".wav": "audio/wav",  ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            }.get(Path(path).suffix.lower(), "audio/mpeg")

def _asset_src(path, mime, embed, assets_dir):
    if embed:
        return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"
    dst = assets_dir / Path(path).name
    if not dst.exists():
        shutil.copy2(path, dst)
    return f"assets/{Path(path).name}"


# ── frame inset helper ────────────────────────────────────────────────────────

def _calc_frame_inset(frame_style, frame_thickness, frame_img_path,
                      user_extra=0, base_pad=18):
    """
    Return how many pixels of content inset the frame needs so text/writing
    sits fully *inside* the frame and is not covered by its borders or corner
    ornaments.  user_extra px are added on top of the auto value.
    """
    t   = max(1, frame_thickness)
    pad = base_pad          # frame's own inset from page edge
    gap = max(4, t * 2)    # used by Double / Vintage

    if frame_style == "Simple":
        auto = pad + t + 14
    elif frame_style == "Double":
        auto = pad + t * 2 + gap + 16
    elif frame_style == "Vintage":
        auto = pad + t * 2 + gap + gap + 16
    elif frame_style == "Ornate Corners":
        arm  = min(80, 960 // 8)   # matches apply_frame() arm length
        auto = pad + arm + 24
    elif frame_style == "3D Bevel":
        bw   = t * 5 + 12
        auto = pad + bw + 16
    else:
        auto = 0

    # Custom PNG: we can't know corner size — use a generous default
    if frame_img_path and Path(frame_img_path).exists():
        auto = max(auto, 70)

    return auto + user_extra


# ── HTML builder ───────────────────────────────────────────────────────────────

def _hex_to_rgba(hex_color, opacity_pct):
    """Convert #rrggbb + opacity 0-100 to CSS rgba()."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{opacity_pct/100:.2f})"


def build_html(cfg, log):
    photos     = cfg["photos"]
    embed      = cfg["embed_assets"]
    output_dir = cfg["output_dir"]
    assets_dir = output_dir / "assets"
    fn   = cfg["font_name"]
    tc   = cfg["text_color"]
    N    = len(photos)
    pg_bg  = _hex_to_rgba(cfg["page_bg_color"], cfg["page_opacity"])

    # page background image (optional)
    _pg_img_path = cfg.get("page_bg_img", "")
    _pg_img_opacity = cfg.get("page_img_opacity", 80) / 100.0   # 0.0–1.0
    has_pg_img = bool(_pg_img_path and Path(_pg_img_path).exists())
    pg_dark = cfg["page_darkness"] / 100.0  # 0.0-0.4 black overlay for contrast

    if not embed:
        assets_dir.mkdir(exist_ok=True)

    def isrc(p):
        return _asset_src(p, _img_mime(p), embed, assets_dir)

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    title_e    = esc(cfg["title"])
    sub_e      = esc(cfg["subtitle"])
    cover_src  = isrc(cfg["cover_img"])
    back_src   = isrc(cfg["back_img"])

    # page background image CSS
    if has_pg_img:
        pg_img_src = isrc(_pg_img_path)
        pg_img_css = (
            f"background-image:url('{pg_img_src}');"
            f"background-size:cover;background-position:center;"
        )
        pg_img_overlay = (
            f"position:absolute;inset:0;z-index:0;"
            f"background:{pg_bg};opacity:{_pg_img_opacity:.2f};pointer-events:none"
        )
    else:
        pg_img_css     = f"background:{pg_bg};"
        pg_img_overlay = None

    # ── frame / border CSS ────────────────────────────────────────────────────
    _frame_style = cfg.get("frame_style",    "None")
    _frame_color = cfg.get("frame_color",    "#8B7355")
    _frame_thick = cfg.get("frame_thickness", 4)
    _frame_img_p = cfg.get("frame_img",      "")
    _frame_pad   = 18   # px inset from page edge

    # ── frame content inset — keeps text inside the frame border ─────────────
    # (must come AFTER _frame_style / _frame_thick / _frame_img_p are defined)
    _frame_inset = _calc_frame_inset(
        _frame_style, _frame_thick, _frame_img_p,
        user_extra=cfg.get("frame_padding", 0)
    )
    # Build the inline style override for write-inner when a frame is active.
    if _frame_inset > 0:
        _fp = _frame_inset
        _inner_pad_override = (f"padding:{_fp}px {_fp}px {max(28,_fp)}px {_fp}px;")
    else:
        _inner_pad_override = ""   # use CSS class defaults

    # ── page number position & size (writing pages only) ──────────────────────
    _pnum_pos   = cfg.get("page_num_pos",  "Bottom Center")
    _pnum_sz    = cfg.get("page_num_size", 14)
    _pnum_vert  = "bottom:8px;top:auto" if "Bottom" in _pnum_pos else "top:8px;bottom:auto"
    _pnum_align = ("left"  if "Left"  in _pnum_pos else
                   "right" if "Right" in _pnum_pos else "center")
    # z-index:4 keeps the number above the frame div (z=2) and frame PNG (z=3)
    _pnum_style = (f"position:absolute;{_pnum_vert};left:0;right:0;width:100%;"
                   f"text-align:{_pnum_align};font-size:{_pnum_sz}px;opacity:.60;"
                   f"z-index:4;pointer-events:none;padding:0 18px")

    def _css_frame():
        """Return inline CSS for the frame overlay div."""
        t  = _frame_thick
        p  = _frame_pad
        c  = _frame_color
        g  = max(4, t * 2)   # gap for double/vintage
        if _frame_style == "Simple":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};")
        elif _frame_style == "Double":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};"
                    f"outline:{t}px solid {c};outline-offset:{g}px;")
        elif _frame_style == "Vintage":
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};"
                    f"outline:{t}px solid {c};outline-offset:{g}px;"
                    f"box-shadow:inset 0 0 0 {g+t*2}px transparent;")
        elif _frame_style == "Ornate Corners":
            # CSS can't do corner ornaments natively — use a thick box-shadow trick
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border:{t}px solid {c};"
                    f"box-shadow:inset 0 0 0 {t*2}px {c},"
                    f"inset {t*4}px {t*4}px 0 {t*2}px transparent,"
                    f"inset -{t*4}px -{t*4}px 0 {t*2}px transparent;")
        elif _frame_style == "3D Bevel":
            # Raised picture-frame: top+left borders are lighter, bottom+right darker.
            # Compute light/dark hex variants of the chosen colour.
            _h = c.lstrip("#")
            _r, _g, _b = int(_h[0:2],16), int(_h[2:4],16), int(_h[4:6],16)
            light_c = (f"#{min(255,int(_r*1.40+55)):02x}"
                       f"{min(255,int(_g*1.40+55)):02x}"
                       f"{min(255,int(_b*1.40+55)):02x}")
            dark_c  = (f"#{max(0,int(_r*.40)):02x}"
                       f"{max(0,int(_g*.40)):02x}"
                       f"{max(0,int(_b*.40)):02x}")
            bw = t * 5 + 12    # match Pillow bevel width
            # CSS border-color order: top right bottom left
            return (f"position:absolute;inset:{p}px;z-index:2;pointer-events:none;"
                    f"border-style:solid;border-width:{bw}px;"
                    f"border-color:{light_c} {dark_c} {dark_c} {light_c};"
                    f"box-shadow:4px 4px 14px rgba(0,0,0,.65),"
                    f"-1px -1px 5px rgba(255,255,255,.18),"
                    f"inset 0 0 0 2px rgba(0,0,0,.28),"
                    f"inset 0 0 0 {bw-2}px rgba(255,255,255,.07);")
        return ""   # "None"

    frame_div_css  = _css_frame()
    frame_div_html = (f'<div style="{frame_div_css}"></div>'
                      if frame_div_css else "")

    # Custom PNG frame overlay
    frame_img_html = ""
    if _frame_img_p and Path(_frame_img_p).exists():
        fi_src = isrc(_frame_img_p)
        frame_img_html = (
            f'<img src="{fi_src}" style="position:absolute;inset:0;'
            f'width:100%;height:100%;object-fit:fill;z-index:3;'
            f'pointer-events:none;" alt="">'
        )

    # ── flipbook pages ─────────────────────────────────────────────────────────
    # Layout: for each photo we emit TWO pages (photo + writing page) so that
    # the PageFlip library shows them as a natural left/right spread.

    pages = []

    # cover (counts as one page — shown alone as the front cover)
    pages.append(f"""
  <div class="page page-cover" data-density="hard"><div class="pc">
    <img class="full" src="{cover_src}" alt="Cover">
    <div class="cov-ov">
      <h1 class="cov-title">{title_e}</h1>
      {"<p class='cov-sub'>" + sub_e + "</p>" if sub_e else ""}
    </div>
  </div></div>""")

    # photo + writing pairs
    for i, ph in enumerate(photos, 1):
        log(f"  HTML: page {i}/{N} …")
        src     = isrc(ph["path"])
        prompt  = esc(ph["text"]).replace("\n", "<br>") if ph["text"].strip() else ""
        prompt_html = f'<div class="prompt">{prompt}</div>' if prompt else ""

        # photo page (left)
        pages.append(f"""
  <div class="page" data-density="soft"><div class="pc">
    <img class="full" src="{src}" alt="Page {i}">
    <div class="pnum">{i}</div>
  </div></div>""")

        # writing page (right)
        # Build the colour-wash overlay div only when a bg image is active
        _img_wash = (
            f'<div style="{pg_img_overlay}"></div>'
            if pg_img_overlay else ""
        )
        pages.append(f"""
  <div class="page write-pg" data-density="soft" data-idx="{i}"><div class="pc">
    <div class="write-wrap" style="{pg_img_css}">
      {_img_wash}
      <div class="dark-ov" style="background:rgba(0,0,0,{pg_dark:.2f})"></div>
      <div class="write-inner" style="{_inner_pad_override}font-family:'{fn}',Georgia,serif;color:{tc}">
        {prompt_html}
        <textarea class="entry" data-key="jp{i}"
          placeholder="Write your thoughts here…"
          style="font-family:'{fn}',Georgia,serif;color:{tc}"></textarea>
      </div>
      <div style="{_pnum_style}">{i}</div>
      {frame_div_html}
      {frame_img_html}
    </div>
  </div></div>""")

    # back cover (single hard page)
    a_name = esc(cfg["author_name"])
    a_bio  = esc(cfg["author_bio"]).replace("\n", "<br>")
    pages.append(f"""
  <div class="page page-cover" data-density="hard"><div class="pc">
    <img class="full" src="{back_src}" alt="Back Cover">
    <div class="auth-panel" style="font-family:'{fn}',Georgia,serif">
      <h2 class="ab-head">About the Author</h2>
      <p class="ab-name">{a_name}</p>
      <p class="ab-bio">{a_bio}</p>
    </div>
  </div></div>""")

    # ── music ──────────────────────────────────────────────────────────────────
    mus_html = mus_btn = mus_js = ""
    if cfg["music_file"] and Path(cfg["music_file"]).exists():
        ms = _asset_src(cfg["music_file"], _audio_mime(cfg["music_file"]), embed, assets_dir)
        mus_html = f'<audio id="bgm" loop><source src="{ms}"></audio>'
        mus_btn  = "<button id='mb' onclick='tm()'>&#9834; Music</button>"
        mus_js   = ("function tm(){const a=document.getElementById('bgm'),b=document.getElementById('mb');"
                    "if(a.paused){a.play();b.textContent='\\u266a Pause';}"
                    "else{a.pause();b.textContent='\\u266a Music';}}")

    # ── fetch/load JS libraries (cached after first download) ─────────────────
    log("  Loading JS libraries …")
    js_pageflip    = _get_js("page-flip",   log)
    js_jspdf       = _get_js("jspdf",       log)
    js_html2canvas = _get_js("html2canvas", log)

    # ── journal data injected for PDF export ───────────────────────────────────
    import json as _json
    spreads_data = _json.dumps([
        {"src": isrc(ph["path"]), "prompt": ph["text"], "key": f"jp{i}"}
        for i, ph in enumerate(photos, 1)
    ])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_e}</title>
<script>{js_pageflip}</script>
<script>{js_jspdf}</script>
<script>{js_html2canvas}</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}

/* ── body: radial spotlight so the book floats in darkness ── */
body{{
  background:radial-gradient(ellipse at 50% 38%,#2e1b0e 0%,#140c05 52%,#050301 100%);
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:20px;
  font-family:'{fn}',Georgia,serif}}

/* ── book wrapper: multi-layer shadow makes it look lifted off the surface ── */
#book{{
  box-shadow:
    0 80px 100px rgba(0,0,0,.98),
    0 30px 55px  rgba(0,0,0,.80),
    -10px 15px 35px rgba(0,0,0,.55),
    10px  15px 35px rgba(0,0,0,.45),
    0 2px  8px  rgba(0,0,0,.9);
  border-radius:2px 4px 4px 2px;
}}

/* ── page shell ── */
.pc{{width:100%;height:100%;overflow:hidden;position:relative;
  /* right-edge darkening — simulates the page curling away from viewer */
  box-shadow:inset -6px 0 18px rgba(0,0,0,.30),
             inset  0 -4px 12px rgba(0,0,0,.18);}}
img.full{{width:100%;height:100%;object-fit:cover;display:block}}

/* ── cover ── */
.cov-ov{{position:absolute;bottom:0;left:0;right:0;
  padding:36px 24px 28px;
  background:linear-gradient(transparent,rgba(0,0,0,.83));text-align:center}}
.cov-title{{font-size:clamp(16px,4vw,52px);color:#faf8f2;font-style:italic;
  text-shadow:2px 2px 10px rgba(0,0,0,.9);margin-bottom:8px}}
.cov-sub{{font-size:clamp(10px,2vw,28px);color:#ddd5be;
  text-shadow:1px 1px 6px rgba(0,0,0,.8)}}

/* ── photo page ── */
.pnum{{position:absolute;bottom:4px;width:100%;text-align:center;
  font-size:11px;color:rgba(220,205,180,.6)}}

/* ── writing page ── */
.write-wrap{{
  position:absolute;inset:0;display:flex;flex-direction:column;
  /* spine-side light reflection + far-edge shadow = depth between pages */
  box-shadow:
    inset  18px 0 30px rgba(255,245,225,.07),
    inset  -8px 0 20px rgba(0,0,0,.18),
    inset  0  -8px 20px rgba(0,0,0,.12);
}}
.dark-ov{{position:absolute;inset:0;pointer-events:none;z-index:0}}
.write-inner{{position:relative;z-index:1;flex:1;display:flex;flex-direction:column;
  padding:clamp(14px,3vw,36px) clamp(16px,3.5vw,40px) 28px;overflow:hidden}}
.prompt{{font-style:italic;font-size:clamp(10px,1.6vw,18px);line-height:1.65;
  margin-bottom:clamp(8px,1.5vw,18px);opacity:.85;flex-shrink:0;
  pointer-events:none;user-select:none}}
/* textarea MUST stop mouse events reaching PageFlip */
.entry{{flex:1;border:none;background:transparent;resize:none;outline:none;
  font-size:clamp(10px,1.5vw,17px);line-height:1.9;
  width:100%;min-height:0;
  cursor:text;
  position:relative;z-index:10}}
.entry:focus{{box-shadow:inset 0 0 0 1px rgba(200,180,140,.35)}}

/* ── back cover author panel ── */
.auth-panel{{position:absolute;top:0;right:0;bottom:0;width:46%;
  background:rgba(252,248,240,.93);padding:44px 28px;
  display:flex;flex-direction:column;gap:12px;overflow:hidden}}
.ab-head{{font-size:clamp(11px,1.4vw,20px);color:#8a5e38;font-style:italic;
  border-bottom:1px solid #d8cfc0;padding-bottom:8px}}
.ab-name{{font-size:clamp(13px,1.8vw,26px);font-weight:bold;color:#2c1810}}
.ab-bio{{font-size:clamp(9px,1.1vw,15px);line-height:1.8;color:#4a3520}}

/* ── controls bar — always visible against the dark background ── */
#controls{{
  margin-top:14px;
  display:flex;gap:8px;align-items:center;
  flex-wrap:wrap;justify-content:center;
  background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.18);
  border-radius:10px;
  padding:10px 20px;
  backdrop-filter:blur(6px);
  -webkit-backdrop-filter:blur(6px);
}}
/* base button — warm amber, clearly readable */
button{{
  background:#b87828;
  color:#fff8ee;
  border:1px solid rgba(255,255,255,.25);
  border-radius:6px;
  padding:8px 18px;
  font-size:13px;
  font-weight:600;
  cursor:pointer;
  font-family:'{fn}',Georgia,serif;
  transition:background .15s, box-shadow .15s;
  box-shadow:0 2px 6px rgba(0,0,0,.45);
  letter-spacing:.02em;
}}
button:hover{{background:#d4922a;box-shadow:0 3px 10px rgba(0,0,0,.55)}}
/* Save Entries — teal/green so it's unmistakably distinct */
#saveBtn{{background:#2a7a50}}
#saveBtn:hover{{background:#34a066}}
/* Export PDF — blue */
#dlBtn{{background:#2a5a9a}}
#dlBtn:hover{{background:#3470c0}}
#dlBtn:disabled{{background:#1e3a60;cursor:not-allowed;opacity:.55}}
/* Music toggle — purple */
#mb{{background:#6a3a9a}}
#mb:hover{{background:#8050b8}}
/* page counter text */
#pinfo{{
  color:#f5e8c8;
  font-size:14px;
  font-weight:600;
  text-shadow:0 1px 4px rgba(0,0,0,.8);
  min-width:110px;
  text-align:center;
}}
#save-note{{font-size:11px;font-style:italic;color:#c8d8c0;min-width:80px}}

/* ── hidden export panel ── */
#xpanel{{position:fixed;left:-9999px;top:0;width:1920px;height:1080px;
  overflow:hidden;background:#fff}}
</style>
</head>
<body>
{mus_html}
<div id="book">{"".join(pages)}</div>
<div id="controls">
  <button onclick="pf.flipPrev()">&#9664; Prev</button>
  <span id="pinfo">Cover</span>
  <button onclick="pf.flipNext()">Next &#9654;</button>
  {mus_btn}
  <button id="saveBtn" onclick="saveEntries()">&#128190; Save Entries</button>
  <button id="dlBtn"   onclick="dlPDF()">&#8659; Export PDF</button>
  <span id="save-note"></span>
</div>
<div id="xpanel"></div>

<script>
/* ── journal data ─────────────────────────────────────── */
const TITLE   = {_json.dumps(cfg["title"])};
const COVER   = {_json.dumps(cover_src)};
const BACK    = {_json.dumps(back_src)};
const PG_BG   = {_json.dumps(pg_bg)};
const PG_DARK = {pg_dark:.3f};
const FONT    = {_json.dumps(fn)};
const TC      = {_json.dumps(tc)};
const SPREADS = {spreads_data};

/* ── Block PageFlip from stealing textarea clicks ─────── */
// Must run BEFORE PageFlip initialises so our listeners fire first.
document.querySelectorAll('.entry').forEach(function(el){{
  ['mousedown','touchstart','pointerdown'].forEach(function(evt){{
    el.addEventListener(evt, function(e){{
      e.stopPropagation();   // PageFlip never sees this event
    }}, {{capture:true}});
  }});
  // Also block the parent write-page div from flipping when the
  // reader double-clicks anywhere on the writing area
  el.closest('.write-pg') && el.closest('.write-pg').addEventListener(
    'mousedown', function(e){{
      if(e.target.classList.contains('entry')){{
        e.stopPropagation();
      }}
    }}, {{capture:true}}
  );
}});

/* ── PageFlip ─────────────────────────────────────────── */
const pf=new St.PageFlip(document.getElementById('book'),{{
  width:480,height:640,size:'stretch',
  minWidth:260,maxWidth:840,minHeight:360,maxHeight:1180,
  showCover:true,mobileScrollSupport:false,
  flippingTime:820,useMouseEvents:true,swipeDistance:50
}});
pf.loadFromHTML(document.querySelectorAll('#book .page'));
const tot={N*2};
pf.on('flip',e=>{{
  const c=e.data;
  let lbl;
  if(c===0)lbl='Cover';
  else if(c>tot)lbl='Back Cover';
  else{{const pg=Math.ceil(c/2);lbl=`Page ${{pg}} of {N}`;}}
  document.getElementById('pinfo').textContent=lbl;
}});

/* ── localStorage: auto-save every keystroke, reload on open ─ */
const STORE_KEY = 'journal_{_json.dumps(cfg["title"])[1:-1].replace(" ","_")}';

function _allEntries(){{
  const out={{}};
  document.querySelectorAll('.entry').forEach(t=>out[t.dataset.key]=t.value);
  return out;
}}
function autoSave(){{
  localStorage.setItem(STORE_KEY, JSON.stringify(_allEntries()));
  const n=document.getElementById('save-note');
  n.textContent='\\u2713 auto-saved';
  n.style.color='#7a9a68';
  clearTimeout(autoSave._t);
  autoSave._t=setTimeout(()=>n.textContent='',3000);
}}
function loadSaved(){{
  try{{
    const raw=localStorage.getItem(STORE_KEY);
    if(!raw)return;
    const data=JSON.parse(raw);
    document.querySelectorAll('.entry').forEach(t=>{{
      if(data[t.dataset.key]!==undefined)t.value=data[t.dataset.key];
    }});
  }}catch(e){{}}
}}
document.querySelectorAll('.entry').forEach(t=>
  t.addEventListener('input', autoSave));
window.addEventListener('load', loadSaved);

/* ── Save Entries button: download as a .txt file ──────── */
function saveEntries(){{
  let out=TITLE+'\\n'+'='.repeat(TITLE.length)+'\\n\\n';
  SPREADS.forEach((sp,i)=>{{
    const text=(document.querySelector(`.entry[data-key="${{sp.key}}"]`)||{{}}).value||'';
    out+='--- Page '+(i+1)+' ---\\n';
    if(sp.prompt)out+='[Prompt] '+sp.prompt+'\\n\\n';
    out+=(text.trim()||'(blank)')+'\\n\\n';
  }});
  const blob=new Blob([out],{{type:'text/plain'}});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=TITLE.replace(/[^a-z0-9]/gi,'_')+'_entries.txt';
  a.click();
  URL.revokeObjectURL(a.href);
  const n=document.getElementById('save-note');
  n.textContent='\\u2713 Downloaded!';
  n.style.color='#7aaa88';
  setTimeout(()=>n.textContent='',3000);
}}

/* ── music ────────────────────────────────────────────── */
{mus_js}

/* ── PDF export ───────────────────────────────────────── */
async function dlPDF(){{
  const btn=document.getElementById('dlBtn');
  btn.disabled=true; btn.textContent='Building PDF…';

  const {{jsPDF}}=window.jspdf;
  const PW=1920,PH=1080;
  const pdf=new jsPDF({{orientation:'landscape',unit:'px',
    format:[PW,PH],hotfixes:['px_scaling']}});
  const panel=document.getElementById('xpanel');

  async function cap(){{
    return html2canvas(panel,{{
      scale:1,width:PW,height:PH,
      useCORS:true,allowTaint:true,
      backgroundColor:null
    }});
  }}

  async function addImg(el){{
    panel.innerHTML=''; panel.appendChild(el.cloneNode(true));
    const c=await cap();
    return c.toDataURL('image/jpeg',0.92);
  }}

  // helper: full-width image slide
  function imgSlide(src){{
    const d=document.createElement('div');
    d.style.cssText=`width:${{PW}}px;height:${{PH}}px;overflow:hidden;background:#111`;
    const im=document.createElement('img');
    im.src=src;
    im.style.cssText=`width:100%;height:100%;object-fit:cover`;
    d.appendChild(im); return d;
  }}

  // helper: spread (photo left + writing right)
  function spreadSlide(sp){{
    const entry=localStorage.getItem(sp.key)||'';
    const d=document.createElement('div');
    d.style.cssText=`width:${{PW}}px;height:${{PH}}px;display:flex;overflow:hidden`;

    // photo half
    const ph=document.createElement('div');
    ph.style.cssText=`width:960px;height:${{PH}}px;flex-shrink:0;overflow:hidden`;
    const im=document.createElement('img');
    im.src=sp.src; im.style.cssText=`width:100%;height:100%;object-fit:cover`;
    ph.appendChild(im);

    // writing half
    const wr=document.createElement('div');
    wr.style.cssText=`width:960px;height:${{PH}}px;position:relative;background:${{PG_BG}};`+
      `display:flex;flex-direction:column;padding:52px 48px 36px;overflow:hidden;`+
      `font-family:'${{FONT}}',Georgia,serif;color:${{TC}}`;
    const ov=document.createElement('div');
    ov.style.cssText=`position:absolute;inset:0;background:rgba(0,0,0,${{PG_DARK}});pointer-events:none`;
    wr.appendChild(ov);
    const inner=document.createElement('div');
    inner.style.cssText=`position:relative;z-index:1;flex:1;display:flex;flex-direction:column;overflow:hidden`;
    if(sp.prompt){{
      const pr=document.createElement('div');
      pr.style.cssText=`font-style:italic;font-size:20px;line-height:1.6;margin-bottom:20px;opacity:.8;white-space:pre-wrap`;
      pr.textContent=sp.prompt; inner.appendChild(pr);
    }}
    const et=document.createElement('div');
    et.style.cssText=`font-size:18px;line-height:1.9;white-space:pre-wrap;word-wrap:break-word;flex:1;overflow:hidden`;
    et.textContent=entry; inner.appendChild(et);
    wr.appendChild(inner);

    d.appendChild(ph); d.appendChild(wr); return d;
  }}

  try{{
    // cover
    let data=await addImg(imgSlide(COVER));
    pdf.addImage(data,'JPEG',0,0,PW,PH);

    // spreads
    for(const sp of SPREADS){{
      pdf.addPage([PW,PH],'landscape');
      data=await addImg(spreadSlide(sp));
      pdf.addImage(data,'JPEG',0,0,PW,PH);
    }}

    // back cover
    pdf.addPage([PW,PH],'landscape');
    data=await addImg(imgSlide(BACK));
    pdf.addImage(data,'JPEG',0,0,PW,PH);

    pdf.save(TITLE.replace(/[^a-z0-9]/gi,'_')+'.pdf');
  }}catch(e){{
    alert('PDF error: '+e.message);
    console.error(e);
  }}

  panel.innerHTML='';
  btn.disabled=false; btn.textContent='\\u8659 Download Journal PDF';
}}
</script>
</body>
</html>"""

    out = output_dir / f"{_safe_stem(cfg['title'])}_flipbook.html"
    out.write_text(html, encoding="utf-8")
    log(f"  Saved: {out}")
    if not embed:
        log("  Keep the 'assets' folder next to the HTML file.")


# ── video builder ──────────────────────────────────────────────────────────────

def build_video(cfg, log):
    photos  = cfg["photos"]
    tmp     = cfg["output_dir"] / "_frames"
    tmp.mkdir(exist_ok=True)

    log("  Rendering cover …")
    render_cover(cfg["cover_img"], cfg["title"], cfg["subtitle"],
                 cfg["font_path"], SPREAD_W, SPREAD_H).save(tmp / "f0000.png")

    for i, ph in enumerate(photos, 1):
        log(f"  Rendering frame {i}/{len(photos)} …")
        render_video_spread(
            ph["path"], ph["text"],
            cfg["font_path"], cfg["text_color"], i,
            page_bg         = cfg.get("page_bg_color",    "#fcf8f0"),
            font_size       = cfg.get("video_font_size",  0),
            page_bg_img     = cfg.get("page_bg_img",      None),
            page_img_opacity= cfg.get("page_img_opacity", 80),
            frame_style     = cfg.get("frame_style",      "None"),
            frame_color     = cfg.get("frame_color",      "#8B7355"),
            frame_thickness = cfg.get("frame_thickness",  4),
            frame_img       = cfg.get("frame_img",        None),
            page_num_pos    = cfg.get("page_num_pos",     "Bottom Center"),
            frame_padding   = cfg.get("frame_padding",    0),
            page_num_size   = cfg.get("page_num_size",    14),
        ).save(tmp / f"f{i:04d}.png")

    n_back = len(photos) + 1
    log("  Rendering back cover …")
    render_back_cover(cfg["back_img"], cfg["author_name"], cfg["author_bio"],
                      cfg["author_photo"], cfg["font_path"],
                      SPREAD_W, SPREAD_H).save(tmp / f"f{n_back:04d}.png")

    frames = sorted(tmp.glob("f*.png"))
    N       = len(frames)
    out_mp4 = cfg["output_dir"] / f"{_safe_stem(cfg['title'])}.mp4"

    # ── FFmpeg input durations ────────────────────────────────────────────────
    # xfade chain timing explanation:
    #   offset_n = (n+1)*PAGE_DUR + n*TRANS_DUR  (each page gets exactly PAGE_DUR seconds)
    #   The OUTPUT of xfade step n = offset_n + len(frame_n+1)
    #   Step n+1 needs that output to last until offset_{n+1} + TRANS_DUR, so:
    #     frame at position k (1 … N-2) needs: PAGE_DUR + 2*TRANS_DUR seconds
    #   First frame: PAGE_DUR + TRANS_DUR  (only needs to survive one transition start)
    #   Last frame:  PAGE_DUR + TRANS_DUR  (1s used in blend, PAGE_DUR shown alone)
    cmd = [FFMPEG, "-y"]
    for i, f in enumerate(frames):
        if i == 0 or i == N - 1:
            t = PAGE_DUR + TRANS_DUR          # 5 s  — first or last frame
        else:
            t = PAGE_DUR + 2 * TRANS_DUR      # 6 s  — middle frames need extra buffer
        cmd += ["-loop", "1", "-t", str(t), "-i", str(f)]

    has_music = cfg["music_file"] and Path(cfg["music_file"]).exists()
    if has_music:
        cmd += ["-stream_loop", "-1", "-i", str(cfg["music_file"])]

    # ── xfade filter chain ────────────────────────────────────────────────────
    # offset_n = (n+1)*PAGE_DUR + n*TRANS_DUR keeps each page visible for exactly
    # PAGE_DUR seconds before the blend begins.
    if N == 1:
        filter_str = "[0:v]copy[v]"
    elif N == 2:
        offset = PAGE_DUR      # frame 0 shows alone for PAGE_DUR, then 1s blend
        filter_str = (
            f"[0:v][1:v]xfade=transition=fadegrays:"
            f"duration={TRANS_DUR}:offset={offset}[v]"
        )
    else:
        parts = []
        for n in range(N - 1):
            in_a   = f"[x{n-1}]" if n > 0 else "[0:v]"
            in_b   = f"[{n+1}:v]"
            out    = "[v]" if n == N - 2 else f"[x{n}]"
            offset = (n + 1) * PAGE_DUR + n * TRANS_DUR
            parts.append(
                f"{in_a}{in_b}xfade=transition=fadegrays:"
                f"duration={TRANS_DUR}:offset={offset}{out}"
            )
        filter_str = ";".join(parts)

    cmd += ["-filter_complex", filter_str, "-map", "[v]"]
    if has_music:
        cmd += ["-map", f"{N}:a"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_mp4)]

    log("  Running FFmpeg …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(tmp)

    if result.returncode != 0:
        log("  FFmpeg error:")
        log(result.stderr[-2000:])
        return False

    log(f"  Saved: {out_mp4}")
    return True


# ── GUI constants ──────────────────────────────────────────────────────────────

BG     = "#1c1208"
PANEL  = "#2a1a0c"
CARD   = "#321e0e"
ENTRY  = "#3e2815"
FG     = "#f0e6d0"
ACCENT = "#c8954a"
MUTED  = "#9a7a50"
BTN    = "#7a4820"
BTNHOV = "#9a6030"
DANGER = "#a03828"
SEL    = "#5a3010"


def _row(parent, label, var, row, browse_fn=None):
    tk.Label(parent, text=label, bg=CARD, fg=FG,
             font=("Segoe UI", 9), anchor="w", width=24).grid(
        row=row, column=0, sticky="w", padx=(12, 4), pady=5)
    tk.Entry(parent, textvariable=var, bg=ENTRY, fg=FG,
             insertbackground=FG, relief="flat",
             font=("Segoe UI", 9)).grid(
        row=row, column=1, sticky="ew", padx=4, pady=5)
    if browse_fn:
        tk.Button(parent, text="Browse…", command=browse_fn,
                  bg=BTN, fg=FG, relief="flat", font=("Segoe UI", 8),
                  padx=9, cursor="hand2",
                  activebackground=BTNHOV, activeforeground=FG).grid(
            row=row, column=2, padx=(4, 12), pady=5)


def _btn(parent, text, cmd, danger=False, **kw):
    return tk.Button(parent, text=text, command=cmd,
                     bg=DANGER if danger else BTN, fg=FG,
                     relief="flat", font=("Segoe UI", 9),
                     padx=10, pady=4, cursor="hand2",
                     activebackground="#c04030" if danger else BTNHOV,
                     activeforeground=FG, **kw)


# ── main app ───────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Multimedia Journal Factory")
        self.geometry("1020x720")
        self.minsize(840, 580)
        self.configure(bg=BG)
        self._setup_style()
        self._init_vars()
        self._photos  = []    # [{"path": Path, "text": str}, ...]
        self._sel     = None  # selected index
        self._thumb   = None  # ImageTk ref
        self._build_ui()

    # ── style ──────────────────────────────────────────────────────────────────

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    padding=[16, 7], font=("Segoe UI", 9))
        s.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", ACCENT)])
        s.configure("TCheckbutton", background=CARD, foreground=FG,
                    font=("Segoe UI", 9))
        s.map("TCheckbutton", background=[("active", CARD)], foreground=[("active", FG)])
        s.configure("Vertical.TScrollbar", background=PANEL,
                    troughcolor=BG, borderwidth=0, arrowsize=12)

    def _init_vars(self):
        self.v_cover        = tk.StringVar()
        self.v_back         = tk.StringVar()
        self.v_music        = tk.StringVar()
        self.v_output       = tk.StringVar()
        self.v_title        = tk.StringVar()
        self.v_subtitle     = tk.StringVar()
        self.v_author_name  = tk.StringVar()
        self.v_author_photo = tk.StringVar()
        self.v_font         = tk.StringVar(value=FONT_CHOICES[0])
        self.v_font_path    = tk.StringVar()
        self.v_color        = tk.StringVar(value="#f5f0e8")
        self.b_html         = tk.BooleanVar(value=True)
        self.b_embed        = tk.BooleanVar(value=False)
        self.b_video        = tk.BooleanVar(value=True)
        self.v_page_bg        = tk.StringVar(value="#fcf8f0")
        self.v_pg_opacity     = tk.IntVar(value=96)
        self.v_pg_dark        = tk.IntVar(value=0)
        self.v_video_font_size = tk.IntVar(value=0)   # 0 = auto-fit
        self.v_page_bg_img     = tk.StringVar()        # optional background image
        self.v_page_img_opacity= tk.IntVar(value=80)  # how opaque the bg image is (40-100)
        # frame / border
        self.v_frame_style     = tk.StringVar(value="None")
        self.v_frame_color     = tk.StringVar(value="#8B7355")
        self.v_frame_thickness = tk.IntVar(value=4)
        self.v_frame_img       = tk.StringVar()        # optional PNG frame overlay
        self.v_frame_padding   = tk.IntVar(value=0)   # extra content inset (0 = auto)
        self.v_page_num_pos    = tk.StringVar(value="Bottom Center")  # page number position
        self.v_page_num_size   = tk.IntVar(value=14)  # page number font size (pt / px)
        self.v_font.trace_add("write", self._on_font_change)
        self.v_color.trace_add("write", lambda *_: self._refresh_swatch())
        self.v_page_bg.trace_add("write", lambda *_: self._refresh_pg_swatch())

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=PANEL, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Multimedia Journal Factory",
                 bg=PANEL, fg=ACCENT, font=("Georgia", 20, "italic")).pack()
        tk.Label(hdr, text="Create beautiful HTML flipbooks and MP4 video journals",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(pady=(2, 0))
        ffcolor = ACCENT if FFMPEG else DANGER
        ffmsg   = f"FFmpeg: {FFMPEG}" if FFMPEG else "FFmpeg: NOT FOUND — video export disabled"
        tk.Label(hdr, text=ffmsg, bg=PANEL, fg=ffcolor, font=("Consolas", 8)).pack(pady=(4, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=8)

        t_photos  = tk.Frame(nb, bg=CARD)
        t_details = self._card(nb)
        t_style   = tk.Frame(nb, bg=CARD)   # plain container — scroll added inside
        t_build   = self._card(nb)

        nb.add(t_photos,  text="   Photos   ")
        nb.add(t_details, text="   Details   ")
        nb.add(t_style,   text="   Style   ")
        nb.add(t_build,   text="   Build   ")

        self._tab_photos(t_photos)
        self._tab_details(t_details)
        self._tab_style(t_style)
        self._tab_build(t_build)

    def _card(self, parent):
        f = tk.Frame(parent, bg=CARD)
        f.columnconfigure(1, weight=1)
        return f

    # ── Photos tab ─────────────────────────────────────────────────────────────

    def _tab_photos(self, p):
        p.columnconfigure(1, weight=1)
        p.rowconfigure(1, weight=1)

        # ── top toolbar ───────────────────────────────────────────────────────
        bar = tk.Frame(p, bg=CARD, pady=6)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10)
        _btn(bar, "📁  Load Folder", self._load_folder).pack(side="left", padx=4)
        _btn(bar, "+ Add Photos",    self._add_photos).pack(side="left", padx=4)
        _btn(bar, "✕  Clear All",    self._clear_photos, danger=True).pack(side="left", padx=4)
        tk.Label(bar,
                 text="  Each photo = one page.  Select a page from the list, then write its journal entry on the right.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # ── left panel: page list ──────────────────────────────────────────────
        left = tk.Frame(p, bg=CARD, width=270)
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
        left.pack_propagate(False)

        # list header
        tk.Label(left, text="PAGES  (drag to reorder)",
                 bg="#1a0e06", fg=MUTED, font=("Segoe UI", 8),
                 pady=4).pack(fill="x")

        lf = tk.Frame(left, bg=CARD)
        lf.pack(fill="both", expand=True)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._lb = tk.Listbox(
            lf, bg="#1a0e06", fg=FG,
            selectbackground="#5a3010", selectforeground=ACCENT,
            activestyle="none",
            font=("Segoe UI", 10, "bold"),
            relief="flat", borderwidth=0, highlightthickness=0,
            cursor="hand2",
        )
        self._lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.bind("<<ListboxSelect>>", self._on_select)

        # reorder + remove buttons
        br = tk.Frame(left, bg=CARD, pady=4)
        br.pack(fill="x")
        _btn(br, "↑ Move Up",   self._move_up).pack(side="left", padx=3)
        _btn(br, "↓ Move Down", self._move_down).pack(side="left", padx=3)
        _btn(br, "✕ Remove",    self._remove, danger=True).pack(side="left", padx=3)

        # ── right panel: page editor ───────────────────────────────────────────
        right = tk.Frame(p, bg=CARD)
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 10), pady=(0, 10))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)   # row 2 = text area expands

        # page badge + nav  (row 0)
        nav_bar = tk.Frame(right, bg="#1a0e06", pady=6)
        nav_bar.grid(row=0, column=0, sticky="ew")
        nav_bar.columnconfigure(1, weight=1)

        self._prev_btn = tk.Button(
            nav_bar, text="◀ Prev", command=self._prev_page,
            bg=BTN, fg=FG, relief="flat", font=("Segoe UI", 9),
            padx=10, cursor="hand2",
            activebackground=BTNHOV, activeforeground=FG, state="disabled")
        self._prev_btn.grid(row=0, column=0, padx=(10, 4), pady=2)

        self._page_badge = tk.Label(
            nav_bar,
            text="← Load photos, then select a page to write its entry",
            bg="#1a0e06", fg=ACCENT,
            font=("Georgia", 11, "italic"))
        self._page_badge.grid(row=0, column=1, sticky="ew", padx=8)

        self._next_btn = tk.Button(
            nav_bar, text="Next ▶", command=self._next_page,
            bg=BTN, fg=FG, relief="flat", font=("Segoe UI", 9),
            padx=10, cursor="hand2",
            activebackground=BTNHOV, activeforeground=FG, state="disabled")
        self._next_btn.grid(row=0, column=2, padx=(4, 10), pady=2)

        # thumbnail + file info  (row 1)
        thumb_row = tk.Frame(right, bg=CARD)
        thumb_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 4))
        thumb_row.columnconfigure(1, weight=1)

        self._thumb_lbl = tk.Label(thumb_row, bg=CARD,
                                   text="No photo selected",
                                   fg=MUTED, font=("Segoe UI", 9))
        self._thumb_lbl.grid(row=0, column=0, rowspan=3, padx=(0, 12))

        self._fname_lbl = tk.Label(thumb_row, text="", bg=CARD, fg=FG,
                                   font=("Segoe UI", 9, "bold"), anchor="w")
        self._fname_lbl.grid(row=0, column=1, sticky="w")

        tk.Label(thumb_row,
                 text="Write this page's journal entry in the box below.\n"
                      "Leave it blank for a photo-only page.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8), justify="left",
                 ).grid(row=1, column=1, sticky="w", pady=(4, 0))

        # journal entry text box  (row 2 — expands to fill)
        txt_label = tk.Frame(right, bg=CARD)
        txt_label.grid(row=2, column=0, sticky="nsew", padx=10, pady=(6, 10))
        txt_label.columnconfigure(0, weight=1)
        txt_label.rowconfigure(1, weight=1)

        tk.Label(txt_label,
                 text="✏  Journal Entry for this Page",
                 bg=CARD, fg=ACCENT, font=("Georgia", 10, "italic"),
                 anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 4))

        tf = tk.Frame(txt_label, bg=ENTRY, bd=0)
        tf.grid(row=1, column=0, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)

        self._txt = tk.Text(
            tf, bg=ENTRY, fg=FG, insertbackground=FG,
            relief="flat", font=("Georgia", 12),
            wrap="word", padx=12, pady=10,
            state="disabled",
            spacing1=2, spacing3=2,
        )
        self._txt.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self._txt.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self._txt.configure(yscrollcommand=tsb.set)
        self._txt.bind("<<Modified>>", self._on_txt_edit)

        # char counter at the bottom
        self._char_lbl = tk.Label(right, text="", bg=CARD, fg=MUTED,
                                  font=("Segoe UI", 8), anchor="e")
        self._char_lbl.grid(row=3, column=0, sticky="e", padx=12, pady=(0, 4))

    # ── Details tab ────────────────────────────────────────────────────────────

    def _tab_details(self, p):
        img_t   = [("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All", "*.*")]
        audio_t = [("Audio", "*.mp3 *.wav *.ogg *.m4a *.aac *.flac"), ("All", "*.*")]

        self._sec(p, "Cover & Back Cover", 0)
        _row(p, "Cover image *",         self.v_cover,  1, lambda: self._file(self.v_cover, img_t))
        _row(p, "Back cover image *",    self.v_back,   2, lambda: self._file(self.v_back, img_t))

        self._sec(p, "Output", 3)
        _row(p, "Output folder *",       self.v_output, 4, lambda: self._folder(self.v_output))
        _row(p, "Music file (optional)", self.v_music,  5, lambda: self._file(self.v_music, audio_t))

        self._sec(p, "Journal Info", 6)
        _row(p, "Journal title *",       self.v_title,    7)
        _row(p, "Subtitle (optional)",   self.v_subtitle, 8)

        self._sec(p, "Author (back cover)", 9)
        _row(p, "Author name *",         self.v_author_name,  10)
        _row(p, "Author photo (optional)", self.v_author_photo, 11,
             lambda: self._file(self.v_author_photo, img_t))

        tk.Label(p, text="Short bio *", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w").grid(
            row=12, column=0, sticky="nw", padx=(12, 4), pady=(8, 0))

        bf = tk.Frame(p, bg=CARD)
        bf.grid(row=12, column=1, columnspan=2, sticky="ew", padx=(4, 12), pady=(8, 12))
        bf.columnconfigure(0, weight=1)
        self._bio = tk.Text(bf, bg=ENTRY, fg=FG, insertbackground=FG,
                            relief="flat", font=("Segoe UI", 9),
                            wrap="word", height=5, padx=6, pady=4)
        self._bio.grid(row=0, column=0, sticky="ew")
        bsb = ttk.Scrollbar(bf, orient="vertical", command=self._bio.yview)
        bsb.grid(row=0, column=1, sticky="ns")
        self._bio.configure(yscrollcommand=bsb.set)

    # ── Style tab ──────────────────────────────────────────────────────────────

    def _tab_style(self, outer):
        # ── scrollable wrapper ────────────────────────────────────────────────
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0, bd=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        p = tk.Frame(canvas, bg=CARD)          # inner frame — all grid() calls go here
        p.columnconfigure(1, weight=1)
        win_id = canvas.create_window((0, 0), window=p, anchor="nw")

        def _on_inner_cfg(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_cfg(e):
            canvas.itemconfig(win_id, width=e.width)
        p.bind("<Configure>", _on_inner_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)

        # Mousewheel scrolls the Style tab while the cursor is inside it
        def _on_enter(_):
            canvas.bind_all("<MouseWheel>",
                lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        def _on_leave(_):
            canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)
        # ── content below ─────────────────────────────────────────────────────

        self._sec(p, "Typography & Text Colour", 0)

        tk.Label(p, text="Font", bg=CARD, fg=FG, font=("Segoe UI", 9),
                 anchor="w", width=24).grid(row=1, column=0, sticky="w", padx=(12, 4), pady=5)
        ttk.Combobox(p, textvariable=self.v_font, values=FONT_CHOICES,
                     state="readonly", font=("Segoe UI", 9)).grid(
            row=1, column=1, sticky="ew", padx=4, pady=5)

        self._cfr = tk.Frame(p, bg=CARD)
        self._cfr.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._cfr.columnconfigure(1, weight=1)
        _row(self._cfr, ".ttf / .otf path *", self.v_font_path, 0,
             lambda: self._file(self.v_font_path, [("Fonts", "*.ttf *.otf"), ("All", "*.*")]))
        self._cfr.grid_remove()

        tk.Label(p, text="Text colour on photos", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=3, column=0, sticky="w", padx=(12, 4), pady=5)
        cr = tk.Frame(p, bg=CARD)
        cr.grid(row=3, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(cr, textvariable=self.v_color, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas", 9), width=10).pack(side="left")
        self._swatch = tk.Label(cr, width=3, bg=self.v_color.get(),
                                relief="flat", cursor="hand2")
        self._swatch.pack(side="left", padx=(8, 0))
        self._swatch.bind("<Button-1>", lambda e: self._pick_color())
        _btn(cr, "Pick colour…", self._pick_color).pack(side="left", padx=(8, 0))

        tk.Label(p, text="Presets", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8), width=24, anchor="w").grid(
            row=4, column=0, sticky="w", padx=(12, 4), pady=(0, 8))
        pr = tk.Frame(p, bg=CARD)
        pr.grid(row=4, column=1, sticky="w", padx=4, pady=(0, 8))
        for name, hx in [("Warm White", "#f5f0e8"), ("Dark Brown", "#2c1810"),
                          ("Deep Navy", "#1a1a2e"), ("Black", "#000000"),
                          ("Gold", "#d4a847")]:
            _btn(pr, name, lambda h=hx: self.v_color.set(h)).pack(side="left", padx=3)

        # ── writing page background ────────────────────────────────────────────
        # ── video font size ───────────────────────────────────────────────────
        self._sec(p, "Video Text Size", 5)
        tk.Label(p, text="Journal text size", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=6, column=0, sticky="w", padx=(12, 4), pady=5)
        vfs_row = tk.Frame(p, bg=CARD)
        vfs_row.grid(row=6, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(vfs_row, variable=self.v_video_font_size, from_=0, to=72,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(vfs_row,
                 text="pt  (0 = auto-fit, recommended 32–52 for readability)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        self._sec(p, "Writing Page Background  (HTML & Video)", 7)

        tk.Label(p, text="Page colour", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=8, column=0, sticky="w", padx=(12, 4), pady=5)
        pgcr = tk.Frame(p, bg=CARD)
        pgcr.grid(row=8, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(pgcr, textvariable=self.v_page_bg, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas", 9), width=10).pack(side="left")
        self._pg_swatch = tk.Label(pgcr, width=3, bg=self.v_page_bg.get(),
                                   relief="flat", cursor="hand2")
        self._pg_swatch.pack(side="left", padx=(8, 0))
        self._pg_swatch.bind("<Button-1>", lambda e: self._pick_pg_color())
        _btn(pgcr, "Pick…", self._pick_pg_color).pack(side="left", padx=(8, 0))

        # page colour presets
        pg_pre = tk.Frame(p, bg=CARD)
        pg_pre.grid(row=9, column=1, sticky="w", padx=4, pady=(0, 6))
        for name, hx in [("Cream", "#fcf8f0"), ("White", "#ffffff"),
                          ("Warm Grey", "#e8e4de"), ("Parchment", "#f4ead5"),
                          ("Slate", "#e4e8ec"), ("Black", "#111111")]:
            _btn(pg_pre, name, lambda h=hx: self.v_page_bg.set(h)).pack(side="left", padx=3)

        # ── background image (optional) ───────────────────────────────────────
        self._sec(p, "Page Background Image  (optional — overrides flat colour)", 10)

        img_t = [("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All", "*.*")]
        tk.Label(p, text="Background image", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=11, column=0, sticky="w", padx=(12, 4), pady=5)
        bi_row = tk.Frame(p, bg=CARD)
        bi_row.grid(row=11, column=1, sticky="ew", padx=4, pady=5)
        bi_row.columnconfigure(0, weight=1)
        tk.Entry(bi_row, textvariable=self.v_page_bg_img, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="ew")
        _btn(bi_row, "Browse…",
             lambda: self._file(self.v_page_bg_img, img_t)).grid(
            row=0, column=1, padx=(6, 0))
        _btn(bi_row, "✕ Clear",
             lambda: self.v_page_bg_img.set("")).grid(
            row=0, column=2, padx=(4, 0))

        tk.Label(p, text="Image opacity", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=12, column=0, sticky="w", padx=(12, 4), pady=5)
        imo_row = tk.Frame(p, bg=CARD)
        imo_row.grid(row=12, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(imo_row, variable=self.v_page_img_opacity, from_=20, to=100,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(imo_row,
                 text="% (lower = more see-through, keeps text readable)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        tk.Label(p, text="Flat colour opacity", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=13, column=0, sticky="w", padx=(12, 4), pady=5)
        op_row = tk.Frame(p, bg=CARD)
        op_row.grid(row=13, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(op_row, variable=self.v_pg_opacity, from_=40, to=100,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(op_row, text="% opaque  (lower = more transparent)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # darkness slider
        tk.Label(p, text="Background darkness", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=14, column=0, sticky="w", padx=(12, 4), pady=5)
        dk_row = tk.Frame(p, bg=CARD)
        dk_row.grid(row=14, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(dk_row, variable=self.v_pg_dark, from_=0, to=60,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(dk_row, text="% dark overlay  (increases contrast)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # ── page frame / border ────────────────────────────────────────────────
        self._sec(p, "Page Frame & Border", 15)

        FRAME_STYLES = ["None", "Simple", "Double", "Vintage", "Ornate Corners", "3D Bevel"]

        tk.Label(p, text="Frame style", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=16, column=0, sticky="w", padx=(12, 4), pady=5)
        ttk.Combobox(p, textvariable=self.v_frame_style,
                     values=FRAME_STYLES, state="readonly",
                     font=("Segoe UI", 9)).grid(
            row=16, column=1, sticky="w", padx=4, pady=5)

        tk.Label(p, text="Frame colour", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=17, column=0, sticky="w", padx=(12, 4), pady=5)
        fc_row = tk.Frame(p, bg=CARD)
        fc_row.grid(row=17, column=1, sticky="w", padx=4, pady=5)
        tk.Entry(fc_row, textvariable=self.v_frame_color, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Consolas", 9), width=10).pack(side="left")
        self._frame_swatch = tk.Label(fc_row, width=3,
                                      bg=self.v_frame_color.get(),
                                      relief="flat", cursor="hand2")
        self._frame_swatch.pack(side="left", padx=(8, 0))
        self.v_frame_color.trace_add("write", lambda *_: self._refresh_frame_swatch())
        self._frame_swatch.bind("<Button-1>", lambda e: self._pick_frame_color())
        _btn(fc_row, "Pick…", self._pick_frame_color).pack(side="left", padx=(8, 0))

        # colour presets for frames
        fp_row = tk.Frame(p, bg=CARD)
        fp_row.grid(row=18, column=1, sticky="w", padx=4, pady=(0, 6))
        for name, hx in [("Gold",    "#c9a84c"), ("Rose Gold", "#b76e79"),
                          ("Dusty Sage","#7a9e7e"), ("Warm Brown","#8B7355"),
                          ("Navy",    "#1a2a4a"), ("Black",     "#111111")]:
            _btn(fp_row, name, lambda h=hx: self.v_frame_color.set(h)).pack(
                side="left", padx=3)

        tk.Label(p, text="Frame thickness", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=19, column=0, sticky="w", padx=(12, 4), pady=5)
        ft_row = tk.Frame(p, bg=CARD)
        ft_row.grid(row=19, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(ft_row, variable=self.v_frame_thickness, from_=1, to=20,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(ft_row, text="px", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=4)

        # custom frame image (PNG overlay)
        self._sec(p, "Custom Frame Image  (optional — PNG with transparent centre)", 20)
        img_t = [("Images", "*.jpg *.jpeg *.png *.webp"), ("All", "*.*")]
        tk.Label(p, text="Frame PNG", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=21, column=0, sticky="w", padx=(12, 4), pady=5)
        fi_row = tk.Frame(p, bg=CARD)
        fi_row.grid(row=21, column=1, sticky="ew", padx=4, pady=5)
        fi_row.columnconfigure(0, weight=1)
        tk.Entry(fi_row, textvariable=self.v_frame_img, bg=ENTRY, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="ew")
        _btn(fi_row, "Browse…",
             lambda: self._file(self.v_frame_img, img_t)).grid(
            row=0, column=1, padx=(6, 0))
        _btn(fi_row, "✕ Clear",
             lambda: self.v_frame_img.set("")).grid(
            row=0, column=2, padx=(4, 0))
        tk.Label(p, text="Load any decorative PNG frame (e.g. from Creative Fabrica / Etsy).\n"
                          "Use a transparent-centre PNG for best results.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8), justify="left").grid(
            row=22, column=1, sticky="w", padx=4, pady=(0, 8))

        tk.Label(p, text="Frame content padding", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=23, column=0, sticky="w", padx=(12, 4), pady=5)
        fcp_row = tk.Frame(p, bg=CARD)
        fcp_row.grid(row=23, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(fcp_row, variable=self.v_frame_padding, from_=0, to=150,
                 orient="horizontal", length=220, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(fcp_row,
                 text="px extra  (0 = auto — increase for large PNG corners)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

        # ── page number position ───────────────────────────────────────────────
        self._sec(p, "Writing Page Numbers  (interactive & video)", 25)

        NUM_POS = ["Bottom Left", "Bottom Center", "Bottom Right",
                   "Top Left",    "Top Center",    "Top Right"]
        tk.Label(p, text="Page number position", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=26, column=0, sticky="w", padx=(12, 4), pady=5)
        ttk.Combobox(p, textvariable=self.v_page_num_pos,
                     values=NUM_POS, state="readonly",
                     font=("Segoe UI", 9)).grid(
            row=26, column=1, sticky="w", padx=4, pady=5)
        tk.Label(p, text="Only applies to the writing pages, not the photo pages.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(
            row=27, column=1, sticky="w", padx=4, pady=(0, 8))

        tk.Label(p, text="Page number size", bg=CARD, fg=FG,
                 font=("Segoe UI", 9), anchor="w", width=24).grid(
            row=28, column=0, sticky="w", padx=(12, 4), pady=5)
        pns_row = tk.Frame(p, bg=CARD)
        pns_row.grid(row=28, column=1, sticky="w", padx=4, pady=5)
        tk.Scale(pns_row, variable=self.v_page_num_size, from_=8, to=48,
                 orient="horizontal", length=200, bg=CARD, fg=FG,
                 troughcolor=ENTRY, highlightthickness=0,
                 relief="flat", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(pns_row, text="pt  (HTML & video)",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=8)

    # ── Build tab ──────────────────────────────────────────────────────────────

    def _tab_build(self, p):
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        opts = tk.Frame(p, bg=CARD)
        opts.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        tk.Label(opts, text="Output options:", bg=CARD, fg=ACCENT,
                 font=("Georgia", 10, "italic")).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(opts, text="HTML Flipbook",   variable=self.b_html).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="Embed assets",    variable=self.b_embed).pack(side="left", padx=8)
        ttk.Checkbutton(opts, text="MP4 Video",       variable=self.b_video).pack(side="left", padx=8)

        lw = tk.Frame(p, bg=CARD)
        lw.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        lw.columnconfigure(0, weight=1)
        lw.rowconfigure(0, weight=1)
        self._log_widget = tk.Text(lw, bg="#120c05", fg="#c8b090",
                                   font=("Consolas", 9), wrap="word",
                                   state="disabled", relief="flat", padx=8, pady=6)
        self._log_widget.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(lw, orient="vertical", command=self._log_widget.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._log_widget.configure(yscrollcommand=vsb.set)

        self._gen_btn = tk.Button(p, text="  Generate Journal  ",
                                  command=self._start_build,
                                  bg=BTN, fg="#fdf0da",
                                  font=("Georgia", 13, "italic"),
                                  relief="flat", padx=24, pady=11,
                                  cursor="hand2",
                                  activebackground=BTNHOV, activeforeground="#fff")
        self._gen_btn.grid(row=2, column=0, pady=(8, 14))

    # ── helpers ────────────────────────────────────────────────────────────────

    def _sec(self, parent, text, row):
        tk.Label(parent, text=text, bg=CARD, fg=ACCENT,
                 font=("Georgia", 11, "italic")).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(14, 2))

    def _folder(self, var):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _file(self, var, ft):
        f = filedialog.askopenfilename(filetypes=ft)
        if f:
            var.set(f)

    def _pick_color(self):
        r = colorchooser.askcolor(color=self.v_color.get(), title="Text colour")
        if r and r[1]:
            self.v_color.set(r[1])

    def _refresh_swatch(self):
        try:
            self._swatch.configure(bg=self.v_color.get())
        except Exception:
            pass

    def _refresh_pg_swatch(self):
        try:
            self._pg_swatch.configure(bg=self.v_page_bg.get())
        except Exception:
            pass

    def _pick_pg_color(self):
        r = colorchooser.askcolor(color=self.v_page_bg.get(), title="Writing page colour")
        if r and r[1]:
            self.v_page_bg.set(r[1])

    def _refresh_frame_swatch(self):
        try:
            self._frame_swatch.configure(bg=self.v_frame_color.get())
        except Exception:
            pass

    def _pick_frame_color(self):
        r = colorchooser.askcolor(color=self.v_frame_color.get(), title="Frame colour")
        if r and r[1]:
            self.v_frame_color.set(r[1])

    def _on_font_change(self, *_):
        if self.v_font.get() == FONT_CHOICES[-1]:
            self._cfr.grid()
        else:
            self._cfr.grid_remove()

    # ── photo management ───────────────────────────────────────────────────────

    def _load_folder(self):
        d = filedialog.askdirectory(title="Select photo folder")
        if not d:
            return
        imgs = get_images(d)
        if not imgs:
            messagebox.showinfo("No images", "No supported images found.", parent=self)
            return
        existing = {ph["path"] for ph in self._photos}
        for p in imgs:
            if p not in existing:
                self._photos.append({"path": p, "text": ""})
        self._refresh_lb()
        if self._photos and self._sel is None:
            self._lb.selection_set(0)
            self._on_select(None)

    def _add_photos(self):
        files = filedialog.askopenfilenames(
            title="Select photos",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff"), ("All", "*.*")])
        existing = {ph["path"] for ph in self._photos}
        for f in files:
            p = Path(f)
            if p not in existing:
                self._photos.append({"path": p, "text": ""})
        self._refresh_lb()

    def _clear_photos(self):
        if not self._photos:
            return
        if messagebox.askyesno("Clear all", "Remove all photos?", parent=self):
            self._photos.clear()
            self._sel = None
            self._refresh_lb()
            self._page_badge.configure(
                text="← Load photos, then select a page to write its entry")
            self._fname_lbl.configure(text="")
            self._thumb_lbl.configure(image="", text="No photo selected")
            self._txt.configure(state="disabled")
            self._txt.delete("1.0", "end")
            self._char_lbl.configure(text="")
            self._prev_btn.configure(state="disabled")
            self._next_btn.configure(state="disabled")

    def _remove(self):
        if self._sel is None:
            return
        self._save_txt()
        self._photos.pop(self._sel)
        if self._photos:
            self._sel = min(self._sel, len(self._photos) - 1)
            self._refresh_lb()
            self._lb.selection_set(self._sel)
            self._load_editor()
        else:
            self._sel = None
            self._refresh_lb()
            self._page_badge.configure(
                text="← Load photos, then select a page to write its entry")
            self._fname_lbl.configure(text="")
            self._thumb_lbl.configure(image="", text="No photo selected")
            self._txt.configure(state="disabled")
            self._txt.delete("1.0", "end")
            self._char_lbl.configure(text="")
            self._prev_btn.configure(state="disabled")
            self._next_btn.configure(state="disabled")

    def _move_up(self):
        if self._sel is None or self._sel == 0:
            return
        self._save_txt()
        i = self._sel
        self._photos[i-1], self._photos[i] = self._photos[i], self._photos[i-1]
        self._sel = i - 1
        self._refresh_lb()
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)

    def _move_down(self):
        if self._sel is None or self._sel >= len(self._photos) - 1:
            return
        self._save_txt()
        i = self._sel
        self._photos[i+1], self._photos[i] = self._photos[i], self._photos[i+1]
        self._sel = i + 1
        self._refresh_lb()
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)

    def _refresh_lb(self):
        self._lb.delete(0, "end")
        total = len(self._photos)
        for i, ph in enumerate(self._photos):
            has_text = "✎" if ph["text"].strip() else "  "
            self._lb.insert("end", f"  {has_text}  Page {i+1:02d} of {total}   {ph['path'].name}")
        if self._sel is not None and self._sel < len(self._photos):
            self._lb.selection_set(self._sel)
            self._lb.see(self._sel)

    def _save_txt(self):
        if self._sel is not None and self._sel < len(self._photos):
            self._photos[self._sel]["text"] = self._txt.get("1.0", "end-1c")

    def _go_to_page(self, idx):
        """Select and display the page at index idx."""
        if not self._photos or idx < 0 or idx >= len(self._photos):
            return
        self._save_txt()
        self._sel = idx
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(self._sel)
        self._lb.see(self._sel)
        self._load_editor()

    def _prev_page(self):
        if self._sel is not None and self._sel > 0:
            self._go_to_page(self._sel - 1)

    def _next_page(self):
        if self._sel is not None and self._sel < len(self._photos) - 1:
            self._go_to_page(self._sel + 1)

    def _load_editor(self):
        """Refresh the right-hand editor panel for self._sel."""
        ph    = self._photos[self._sel]
        total = len(self._photos)
        idx   = self._sel

        # page badge
        self._page_badge.configure(
            text=f"Page {idx+1} of {total}")

        # file name label
        self._fname_lbl.configure(text=ph["path"].name)

        # prev / next buttons
        self._prev_btn.configure(state="normal" if idx > 0           else "disabled")
        self._next_btn.configure(state="normal" if idx < total - 1   else "disabled")

        # thumbnail
        try:
            img = Image.open(ph["path"])
            img.thumbnail((220, 138), Image.LANCZOS)
            self._thumb = ImageTk.PhotoImage(img)
            self._thumb_lbl.configure(image=self._thumb, text="")
        except Exception:
            self._thumb_lbl.configure(image="", text="(preview unavailable)")

        # text box
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.insert("1.0", ph["text"])
        self._txt.edit_modified(False)
        self._txt.focus_set()

        # char counter
        self._update_char_count()
        self._refresh_lb()

    def _update_char_count(self):
        if self._sel is None:
            return
        chars = len(self._txt.get("1.0", "end-1c"))
        colour = "#e08040" if chars > 600 else MUTED
        self._char_lbl.configure(
            text=f"{chars} characters  {'(long — font will auto-shrink in output)' if chars > 600 else ''}",
            fg=colour)

    def _on_select(self, _):
        sel = self._lb.curselection()
        if not sel:
            return
        self._save_txt()
        self._sel = sel[0]
        self._load_editor()

    def _on_txt_edit(self, _):
        if self._txt.edit_modified():
            self._save_txt()
            self._txt.edit_modified(False)
            self._update_char_count()
            # refresh the ✎ indicator in the list
            self._refresh_lb()

    # ── build ──────────────────────────────────────────────────────────────────

    def _start_build(self):
        self._save_txt()
        errors = []
        if not self._photos:
            errors.append("  • No photos — add photos in the Photos tab")
        for label, var in [("Cover image", self.v_cover), ("Back cover image", self.v_back),
                           ("Output folder", self.v_output), ("Journal title", self.v_title),
                           ("Author name", self.v_author_name)]:
            if not var.get().strip():
                errors.append(f"  • {label} is required")
        if not self._bio.get("1.0", "end").strip():
            errors.append("  • Author bio is required (Details tab)")
        if self.v_font.get() == FONT_CHOICES[-1] and not self.v_font_path.get().strip():
            errors.append("  • Custom font path required")
        if not self.b_html.get() and not self.b_video.get():
            errors.append("  • Select at least one output type")
        if errors:
            messagebox.showerror("Missing fields", "\n".join(errors), parent=self)
            return

        is_custom = self.v_font.get() == FONT_CHOICES[-1]
        font_path = Path(self.v_font_path.get().strip()) if is_custom else self.v_font.get()
        font_name = Path(self.v_font_path.get().strip()).stem if is_custom else self.v_font.get()
        music     = self.v_music.get().strip()
        photo     = self.v_author_photo.get().strip()

        cfg = dict(
            photos       = self._photos,
            cover_img    = Path(self.v_cover.get().strip()),
            back_img     = Path(self.v_back.get().strip()),
            music_file   = Path(music) if music else None,
            output_dir   = Path(self.v_output.get().strip()),
            title        = self.v_title.get().strip(),
            subtitle     = self.v_subtitle.get().strip(),
            author_name  = self.v_author_name.get().strip(),
            author_bio   = self._bio.get("1.0", "end").strip(),
            author_photo = Path(photo) if photo else None,
            font_path    = font_path,
            font_name    = font_name,
            text_color   = self.v_color.get(),
            page_bg_color     = self.v_page_bg.get(),
            page_opacity      = self.v_pg_opacity.get(),
            page_darkness     = self.v_pg_dark.get(),
            video_font_size   = self.v_video_font_size.get(),
            page_bg_img       = self.v_page_bg_img.get().strip(),
            page_img_opacity  = self.v_page_img_opacity.get(),
            frame_style       = self.v_frame_style.get(),
            frame_color       = self.v_frame_color.get(),
            frame_thickness   = self.v_frame_thickness.get(),
            frame_img         = self.v_frame_img.get().strip(),
            frame_padding     = self.v_frame_padding.get(),
            page_num_pos      = self.v_page_num_pos.get(),
            page_num_size     = self.v_page_num_size.get(),
            build_html   = self.b_html.get(),
            build_video  = self.b_video.get(),
            embed_assets = self.b_embed.get(),
        )
        self._gen_btn.configure(state="disabled", text="  Working…  ")
        self._log_clear()
        self._log("=" * 52)
        self._log("    MULTIMEDIA JOURNAL FACTORY")
        self._log("=" * 52)
        self._log(f"  {len(self._photos)} photos")
        threading.Thread(target=self._run, args=(cfg,), daemon=True).start()

    def _run(self, cfg):
        try:
            cfg["output_dir"].mkdir(parents=True, exist_ok=True)
            if cfg["build_html"]:
                self._log("\n-- BUILDING HTML FLIPBOOK --")
                build_html(cfg, self._log)
            if cfg["build_video"]:
                if not FFMPEG:
                    self._log("\n  ERROR: FFmpeg not found — skipping video.")
                else:
                    self._log("\n-- BUILDING VIDEO --")
                    build_video(cfg, self._log)
            self._log("\n" + "=" * 52)
            self._log("  Done!  Opening output folder …")
            self._log("=" * 52)
            import os
            self.after(0, lambda: os.startfile(str(cfg["output_dir"])))
        except Exception:
            self._log("\n  ERROR:")
            self._log(traceback.format_exc())
        finally:
            self.after(0, lambda: self._gen_btn.configure(
                state="normal", text="  Generate Journal  "))

    def _log(self, msg):
        def _do():
            self._log_widget.configure(state="normal")
            self._log_widget.insert("end", msg + "\n")
            self._log_widget.see("end")
            self._log_widget.configure(state="disabled")
        self.after(0, _do)

    def _log_clear(self):
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.configure(state="disabled")


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
