"""Pure drawing: a state in, an RGBA image out. No window, no Win32, so every
visual decision is testable headless and the Win32 layer stays thin.

Stage 1 draws one static test arrow. The arrow shape and the severity
colours arrive in stage 3; until then nothing here knows about notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SUPERSAMPLE = 2
"""Draw at twice the size and downsample: Pillow's polygons are not
anti-aliased, the downsample makes them so."""


@dataclass(frozen=True)
class Style:
    arrow_rgb: tuple[int, int, int] = (255, 204, 0)
    text_rgb: tuple[int, int, int] = (255, 255, 255)
    outline_rgb: tuple[int, int, int] = (0, 0, 0)
    font_px: int = 64
    opacity: float = 0.9  # applied by the window as the constant alpha; kept here for tests


@lru_cache(maxsize=16)
def load_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A bold sans. Segoe on Windows, DejaVu where that exists, Pillow's
    built-in otherwise. Never fails: a missing font must not kill the overlay."""
    candidates = [
        "segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
        Path("C:/Windows/Fonts/segoeuib.ttf"), Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(str(name), px)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


def render_test_frame(width: int, height: int, style: Style, edit_mode: bool = False,
                      caption: str = "") -> Image.Image:
    """The stage-1 frame: a big right-turn arrow with '3 R' under it. In edit
    mode the window's bounds become visible so it can be placed."""
    width, height = max(40, int(width)), max(40, int(height))
    ss = SUPERSAMPLE
    img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = width * ss, height * ss

    if edit_mode:
        _draw_edit_chrome(draw, w, h, ss, caption)

    # arrow: shaft rising from the bottom, bending right, head at the end
    shaft = max(8 * ss, int(min(w, h) * 0.11))
    cx = w * 0.42
    pts = [
        (cx, h * 0.78),
        (cx, h * 0.40),
        (cx + w * 0.04, h * 0.30),
        (cx + w * 0.14, h * 0.24),
        (cx + w * 0.30, h * 0.24),
    ]
    draw.line(pts, fill=style.outline_rgb + (255,), width=shaft + 4 * ss, joint="curve")
    draw.line(pts, fill=style.arrow_rgb + (255,), width=shaft, joint="curve")
    hx, hy = pts[-1]
    head = [(hx, hy - shaft * 1.4), (hx + shaft * 1.9, hy), (hx, hy + shaft * 1.4)]
    draw.polygon(head, fill=style.outline_rgb + (255,))
    inner = [(hx + 2 * ss, hy - shaft * 1.1), (hx + shaft * 1.6, hy), (hx + 2 * ss, hy + shaft * 1.1)]
    draw.polygon(inner, fill=style.arrow_rgb + (255,))

    _text_with_outline(draw, (w * 0.5, h * 0.90), "3 R", load_font(style.font_px * ss),
                       style.text_rgb, style.outline_rgb, ss)

    return img.resize((width, height), Image.LANCZOS) if ss > 1 else img


def _draw_edit_chrome(draw: ImageDraw.ImageDraw, w: int, h: int, ss: int, caption: str) -> None:
    draw.rectangle([0, 0, w - 1, h - 1], fill=(20, 24, 32, 110), outline=(255, 255, 255, 230), width=2 * ss)
    grip = 26 * ss
    draw.polygon([(w - 1, h - 1), (w - 1 - grip, h - 1), (w - 1, h - 1 - grip)], fill=(255, 255, 255, 230))
    font = load_font(14 * ss)
    text = caption or "edit: drag to move, corner resizes, hotkey locks"
    draw.text((10 * ss, 8 * ss), text, font=font, fill=(255, 255, 255, 255))


def _text_with_outline(draw: ImageDraw.ImageDraw, center: tuple[float, float], text: str,
                       font, fill: tuple[int, int, int], outline: tuple[int, int, int], ss: int) -> None:
    """Centred text with a dark halo, readable over any game frame."""
    x, y = center
    stroke = max(2, 3 * ss)
    try:
        draw.text((x, y), text, font=font, fill=fill + (255,), anchor="mm",
                  stroke_width=stroke, stroke_fill=outline + (255,))
    except (ValueError, TypeError):  # bitmap fonts know no anchors
        draw.text((x, y), text, font=font, fill=fill + (255,))


def to_premultiplied_bgra(img: Image.Image) -> bytes:
    """What UpdateLayeredWindow wants: 32-bit BGRA, colour premultiplied by
    alpha, top-down rows. Pillow's 'RGBa' mode is premultiplied RGBA; only
    the channel order differs."""
    pm = img.convert("RGBa")
    r, g, b, a = pm.split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()
