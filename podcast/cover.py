"""Show artwork: 3000x3000 JPG (Spotify and Apple want 1400-3000px square, RGB).

Deliberately plain and drawn in code: flat background, a soundwave, the logo
and the name. No photos, people or generated imagery, and none of the blog
banner backgrounds, so the show reads as its own thing next to the blog.
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

from .common import DOCS, ROOT

SIZE = 3000
BG = (11, 11, 12)
GREEN = (70, 218, 147)
WHITE = (245, 245, 245)
GREY = (140, 140, 140)
MARGIN = 260


def _font(name: str, size: int):
    path = ROOT / "assets" / "fonts" / f"Aeonik-{name}.ttf"
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size)


def _wave(draw: ImageDraw.ImageDraw, top: int, height: int) -> None:
    """A row of rounded bars shaped like a speech waveform."""
    bars, gap = 38, 26
    width = (SIZE - 2 * MARGIN - gap * (bars - 1)) / bars
    mid = top + height / 2
    for i in range(bars):
        t = i / (bars - 1)
        envelope = math.sin(math.pi * t) ** 0.6
        wiggle = 0.55 + 0.45 * abs(math.sin(i * 1.7) * math.cos(i * 0.6))
        h = max(width, height * envelope * wiggle)
        x = MARGIN + i * (width + gap)
        draw.rounded_rectangle((x, mid - h / 2, x + width, mid + h / 2), radius=width / 2, fill=GREEN)


def build() -> None:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)

    logo = Image.open(ROOT / "assets" / "logo" / "CF-Logo-White@4x.png").convert("RGBA")
    logo.thumbnail((1100, 300))
    img.paste(logo, (MARGIN, MARGIN), logo)

    title = _font("Bold", 470)
    draw.text((MARGIN - 20, 760), "CROSS-", font=title, fill=WHITE)
    draw.text((MARGIN - 20, 1190), "CHAIN", font=title, fill=WHITE)
    draw.text((MARGIN - 20, 1620), "DAILY", font=title, fill=GREEN)

    _wave(draw, 2200, 330)

    draw.text((MARGIN, SIZE - MARGIN - 60), "BITCOIN  ·  DEFI  ·  CRYPTO SWAPS", font=_font("Medium", 110), fill=GREY)
    DOCS.mkdir(exist_ok=True)
    img.save(DOCS / "cover.jpg", "JPEG", quality=92)
