"""Pure drawing: a View in, an RGBA image out. No window, no Win32, so every
visual decision is testable headless and the Win32 layer stays thin.

Stage 2: the arrow points the way of the next corner, the text is the crew
shorthand, the call after next sits below in small type, the distance in the
corner. Colours are placeholders; stage 3 brings the shared severity scale.
Everything scales with the window height.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .state import NoteBrief, View

SUPERSAMPLE = 2
"""Draw at twice the size and downsample: Pillow's polygons are not
anti-aliased, the downsample makes them so."""

DIM_ALPHA = 0.35
"""When the picture is not live (paused, lost, stale): faded, not frozen."""


@dataclass(frozen=True)
class Style:
    arrow_rgb: tuple[int, int, int] = (255, 204, 0)
    text_rgb: tuple[int, int, int] = (255, 255, 255)
    outline_rgb: tuple[int, int, int] = (0, 0, 0)
    muted_rgb: tuple[int, int, int] = (200, 200, 200)
    opacity: float = 0.9  # applied by the window as the constant alpha; kept here for tests


@lru_cache(maxsize=32)
def load_font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A bold sans. Segoe on Windows, DejaVu where that exists, Pillow's
    built-in otherwise. Never fails: a missing font must not kill the overlay."""
    candidates = [
        "segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
        Path("C:/Windows/Fonts/segoeuib.ttf"), Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(str(name), max(4, int(px)))
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=max(4, int(px)))
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# -- words ----------------------------------------------------------------------

_SHORT = {
    "left": "L", "right": "R", "into": "into", "and": "+", "then": "then",
    "tightens": "tightens", "opens": "opens", "long": "long", "short": "short",
    "cut": "cut", "dont_cut": "don't cut", "caution": "caution", "care": "care",
    "keep_left": "keep L", "keep_right": "keep R",
    "jump": "JUMP", "crest": "CREST", "over_crest": "OVER CREST", "dip": "DIP", "bump": "BUMP",
    "narrows": "NARROWS", "water": "WATER", "hairpin": "HAIRPIN", "square": "SQUARE",
    "start": "START", "finish": "FINISH",
}
_DISTANCES = {"30", "50", "70", "100", "150", "200", "250", "300", "400", "500"}


def shorthand(tokens: tuple[str, ...] | list[str]) -> str:
    """Crew shorthand: "3 right into 2 left" -> "3 R into 2 L". A leading
    distance word is dropped: the distance is its own element on screen."""
    toks = list(tokens)
    if toks and toks[0] in _DISTANCES:
        toks = toks[1:]
    return " ".join(_SHORT.get(t, t) for t in toks)


def _is_hazard(note: NoteBrief | None) -> bool:
    return note is not None and note.kind != "corner" and note.direction is None


# -- frames -----------------------------------------------------------------------

def render_frame(view: View, width: int, height: int, style: Style,
                 edit_mode: bool = False, caption: str = "") -> Image.Image:
    width, height = max(40, int(width)), max(40, int(height))
    ss = SUPERSAMPLE
    w, h = width * ss, height * ss
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if edit_mode:
        _draw_edit_chrome(draw, w, h, ss, caption)

    if view.mode == "idle" and not edit_mode:
        # No run: the overlay is invisible, not a box waiting on the screen.
        return _finish(img, width, height, ss)

    if view.next is None:
        _hint(draw, w, h, ss, style, view)
        return _finish(img, width, height, ss, dim=view.mode not in ("tracking", "waiting") and not edit_mode)

    nxt = view.next
    # 1. the arrow (or the hazard word) in the upper two thirds
    if _is_hazard(nxt):
        _text(draw, (w * 0.5, h * 0.40), shorthand(nxt.tokens[:1]), load_font(int(h * 0.20)),
              style.arrow_rgb, style.outline_rgb, ss)
    else:
        direction = nxt.direction or _first_direction(nxt.tokens) or "right"
        _arrow(draw, w, h, ss, style, direction)

    # 2. the call in shorthand, big
    _text(draw, (w * 0.5, h * 0.79), shorthand(nxt.tokens), load_font(int(h * 0.15)),
          style.text_rgb, style.outline_rgb, ss)

    # 3. the call after next, small, as a preview
    if view.after is not None:
        _text(draw, (w * 0.5, h * 0.93), "then " + shorthand(view.after.tokens), load_font(int(h * 0.075)),
              style.muted_rgb, style.outline_rgb, ss)

    # 4. the distance, top right, secondary
    if view.distance_m is not None:
        _text(draw, (w * 0.86, h * 0.10), f"{int(round(view.distance_m / 5.0) * 5)} m",
              load_font(int(h * 0.10)), style.muted_rgb, style.outline_rgb, ss)

    live = view.mode == "tracking"
    return _finish(img, width, height, ss, dim=(not live) and not edit_mode)


def render_test_frame(width: int, height: int, style: Style, edit_mode: bool = False,
                      caption: str = "") -> Image.Image:
    """The stage-1 picture: a right-hander ahead, a left after it, 120 m out.
    Used by tests and by the overlay before any data arrives in edit mode."""
    view = View(
        mode="tracking",
        next=NoteBrief("3 right", ("3", "right"), 3, "right", "corner", 120.0),
        after=NoteBrief("2 left", ("2", "left"), 2, "left", "corner", 260.0),
        distance_m=120.0, speed_kmh=90.0, connected=True,
    )
    return render_frame(view, width, height, style, edit_mode=edit_mode, caption=caption)


# -- pieces ---------------------------------------------------------------------------

def _arrow(draw: ImageDraw.ImageDraw, w: int, h: int, ss: int, style: Style, direction: str) -> None:
    """A shaft rising from the lower middle, bending to the side, with a head.
    Mirrored for left. Stage 3 bends it by severity."""
    shaft = max(8 * ss, int(h * 0.085))
    sign = 1 if direction == "right" else -1
    cx = w * 0.5 - sign * w * 0.08
    pts = [
        (cx, h * 0.66),
        (cx, h * 0.36),
        (cx + sign * w * 0.04, h * 0.27),
        (cx + sign * w * 0.14, h * 0.22),
        (cx + sign * w * 0.28, h * 0.22),
    ]
    draw.line(pts, fill=style.outline_rgb + (255,), width=shaft + 4 * ss, joint="curve")
    draw.line(pts, fill=style.arrow_rgb + (255,), width=shaft, joint="curve")
    hx, hy = pts[-1]
    head = [(hx, hy - shaft * 1.4), (hx + sign * shaft * 1.9, hy), (hx, hy + shaft * 1.4)]
    draw.polygon(head, fill=style.outline_rgb + (255,))
    inner = [(hx + sign * 2 * ss, hy - shaft * 1.1), (hx + sign * shaft * 1.6, hy), (hx + sign * 2 * ss, hy + shaft * 1.1)]
    draw.polygon(inner, fill=style.arrow_rgb + (255,))


def _first_direction(tokens: tuple[str, ...]) -> str | None:
    for t in tokens:
        if t in ("left", "right"):
            return t
    return None


def _hint(draw: ImageDraw.ImageDraw, w: int, h: int, ss: int, style: Style, view: View) -> None:
    """Small words for the moments without a next call."""
    if not view.connected:
        msg = "codriver overlay: start the UI (start.bat)"
    elif view.mode == "waiting":
        msg = "waiting for telemetry: drive"
    elif view.mode in ("suspended", "stale"):
        msg = "paused"
    elif view.mode == "lost":
        msg = "off the stage"
    else:
        msg = "no more calls"
    _text(draw, (w * 0.5, h * 0.5), msg, load_font(int(h * 0.07)), style.muted_rgb, style.outline_rgb, ss)


def _draw_edit_chrome(draw: ImageDraw.ImageDraw, w: int, h: int, ss: int, caption: str) -> None:
    draw.rectangle([0, 0, w - 1, h - 1], fill=(20, 24, 32, 110), outline=(255, 255, 255, 230), width=2 * ss)
    grip = max(16 * ss, int(h * 0.08))
    draw.polygon([(w - 1, h - 1), (w - 1 - grip, h - 1), (w - 1, h - 1 - grip)], fill=(255, 255, 255, 230))
    font = load_font(max(10 * ss, int(h * 0.045)))
    text = caption or "edit: drag to move, corner resizes, hotkey locks"
    draw.text((10 * ss, 8 * ss), text, font=font, fill=(255, 255, 255, 255))


def _text(draw: ImageDraw.ImageDraw, center: tuple[float, float], text: str, font,
          fill: tuple[int, int, int], outline: tuple[int, int, int], ss: int) -> None:
    """Centred text with a dark halo, readable over any game frame."""
    x, y = center
    stroke = max(2, 3 * ss)
    try:
        draw.text((x, y), text, font=font, fill=fill + (255,), anchor="mm",
                  stroke_width=stroke, stroke_fill=outline + (255,))
    except (ValueError, TypeError):  # bitmap fonts know no anchors
        draw.text((x, y), text, font=font, fill=fill + (255,))


def _finish(img: Image.Image, width: int, height: int, ss: int, dim: bool = False) -> Image.Image:
    out = img.resize((width, height), Image.LANCZOS) if ss > 1 else img
    if dim:
        alpha = out.getchannel("A").point(lambda a: int(a * DIM_ALPHA))
        out.putalpha(alpha)
    return out


def to_premultiplied_bgra(img: Image.Image) -> bytes:
    """What UpdateLayeredWindow wants: 32-bit BGRA, colour premultiplied by
    alpha, top-down rows. Pillow's 'RGBa' mode is premultiplied RGBA; only
    the channel order differs."""
    pm = img.convert("RGBa")
    r, g, b, a = pm.split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()
