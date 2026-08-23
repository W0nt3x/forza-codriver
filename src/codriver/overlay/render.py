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
    """The look, all of it from overlay.* in the config. The defaults follow
    the game's own HUD: a Helvetica-style bold (the Horizon UI font is a
    custom Helvetica; Arial Bold is the Windows stand-in), white text with a
    soft shadow rather than a heavy outline, a dark rounded tag behind the
    call like the game's button prompts, and Horizon lime as the accent."""

    font: str = "arialbd.ttf"
    accent_rgb: tuple[int, int, int] = (200, 255, 0)
    arrow_rgb: tuple[int, int, int] = (200, 255, 0)
    text_rgb: tuple[int, int, int] = (255, 255, 255)
    outline_rgb: tuple[int, int, int] = (0, 0, 0)
    muted_rgb: tuple[int, int, int] = (225, 225, 225)
    panel: bool = True
    opacity: float = 0.9  # applied by the window as the constant alpha; kept here for tests


_FONT_FALLBACKS = ("arialbd.ttf", "segoeuib.ttf", "seguisb.ttf", "DejaVuSans-Bold.ttf")


@lru_cache(maxsize=64)
def load_font(px: int, preferred: str = "") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """``preferred`` (a file name in C:/Windows/Fonts, or a path) first, then
    the fallbacks, then Pillow's built-in. Never fails: a missing font must
    not kill the overlay."""
    names: list[str] = []
    if preferred:
        names += [preferred, str(Path("C:/Windows/Fonts") / preferred)]
    for fb in _FONT_FALLBACKS:
        names += [fb, str(Path("C:/Windows/Fonts") / fb)]
    for name in names:
        try:
            return ImageFont.truetype(name, max(4, int(px)))
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=max(4, int(px)))
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


def parse_rgb(value: object, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """'#c8ff00' or '200,255,0' from the config; anything else is the default."""
    text = str(value or "").strip()
    try:
        if text.startswith("#") and len(text) == 7:
            return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)
        if "," in text:
            r, g, b = (int(p) for p in text.split(","))
            if all(0 <= c <= 255 for c in (r, g, b)):
                return r, g, b
    except ValueError:
        pass
    return default


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


def fit_font(text: str, max_px: int, max_width: float, min_px: int = 8, preferred: str = ""):
    """The largest font up to ``max_px`` at which ``text`` is narrower than
    ``max_width``. A long phrase shrinks instead of running off the box."""
    px = max(min_px, int(max_px))
    font = load_font(px, preferred)
    while px > min_px:
        try:
            width = font.getlength(text)
        except AttributeError:  # bitmap fallback font
            width = len(text) * px * 0.6
        if width <= max_width:
            break
        px = max(min_px, int(px * 0.92))
        font = load_font(px, preferred)
    return font


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
    f = style.font
    usable = w * 0.90  # text never touches the edges of the tag
    # the dark tag behind the call and the preview, like the game's prompts
    if style.panel:
        draw.rounded_rectangle([w * 0.03, h * 0.70, w * 0.97, h * 0.985], radius=h * 0.035,
                               fill=(10, 12, 16, 150))

    # 1. the arrow (or the hazard word) in the upper two thirds
    if _is_hazard(nxt):
        word = shorthand(nxt.tokens[:1])
        _text(draw, (w * 0.5, h * 0.38), word, fit_font(word, int(h * 0.20), usable, preferred=f),
              style.arrow_rgb, style.outline_rgb, ss)
    else:
        direction = nxt.direction or _first_direction(nxt.tokens) or "right"
        _arrow(draw, w, h, ss, style, direction)

    # 2. the call in shorthand, big, shrunk to fit if it is a long one
    call = shorthand(nxt.tokens)
    _text(draw, (w * 0.5, h * 0.80), call, fit_font(call, int(h * 0.14), usable, preferred=f),
          style.text_rgb, style.outline_rgb, ss)

    # 3. the call after next, small, as a preview
    if view.after is not None:
        then = "then " + shorthand(view.after.tokens)
        _text(draw, (w * 0.5, h * 0.93), then, fit_font(then, int(h * 0.07), usable, preferred=f),
              style.muted_rgb, style.outline_rgb, ss)

    # 4. the distance, top right, in the accent, secondary
    if view.distance_m is not None:
        dist = f"{int(round(view.distance_m / 5.0) * 5)} m"
        _text(draw, (w * 0.96, h * 0.09), dist, fit_font(dist, int(h * 0.095), w * 0.4, preferred=f),
              style.accent_rgb, style.outline_rgb, ss, anchor="rm")

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
    """A flat shaft rising from the lower middle, bending to the side, with a
    head: a soft shadow and a hairline edge, not a heavy outline, like the
    game's own flat graphics. Mirrored for left. Stage 3 bends it by severity."""
    shaft = max(8 * ss, int(h * 0.08))
    sign = 1 if direction == "right" else -1
    cx = w * 0.5 - sign * w * 0.08
    pts = [
        (cx, h * 0.64),
        (cx, h * 0.36),
        (cx + sign * w * 0.04, h * 0.27),
        (cx + sign * w * 0.14, h * 0.22),
        (cx + sign * w * 0.27, h * 0.22),
    ]
    hx, hy = pts[-1]
    head = [(hx, hy - shaft * 1.35), (hx + sign * shaft * 1.8, hy), (hx, hy + shaft * 1.35)]
    off = 3 * ss
    shadow = (0, 0, 0, 110)
    draw.line([(x + off, y + off) for x, y in pts], fill=shadow, width=shaft + 2 * ss, joint="curve")
    draw.polygon([(x + off, y + off) for x, y in head], fill=shadow)
    edge = style.outline_rgb + (200,)
    draw.line(pts, fill=edge, width=shaft + 2 * ss, joint="curve")
    draw.polygon(head, fill=edge)
    draw.line(pts, fill=style.arrow_rgb + (255,), width=shaft, joint="curve")
    inner = [(hx + sign * ss, hy - shaft * 1.1), (hx + sign * shaft * 1.55, hy), (hx + sign * ss, hy + shaft * 1.1)]
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
    font = fit_font(msg, int(h * 0.07), w * 0.90, preferred=style.font)
    if style.panel:
        tw = font.getlength(msg) if hasattr(font, "getlength") else len(msg) * h * 0.04
        draw.rounded_rectangle([w * 0.5 - tw / 2 - h * 0.04, h * 0.44, w * 0.5 + tw / 2 + h * 0.04, h * 0.56],
                               radius=h * 0.03, fill=(10, 12, 16, 150))
    _text(draw, (w * 0.5, h * 0.5), msg, font, style.muted_rgb, style.outline_rgb, ss)


def _draw_edit_chrome(draw: ImageDraw.ImageDraw, w: int, h: int, ss: int, caption: str) -> None:
    draw.rectangle([0, 0, w - 1, h - 1], fill=(20, 24, 32, 110), outline=(255, 255, 255, 230), width=2 * ss)
    grip = max(16 * ss, int(h * 0.08))
    draw.polygon([(w - 1, h - 1), (w - 1 - grip, h - 1), (w - 1, h - 1 - grip)], fill=(255, 255, 255, 230))
    text = caption or "edit: drag to move, corner resizes, hotkey locks"
    font = fit_font(text, max(10 * ss, int(h * 0.045)), w - 20 * ss, preferred="arialbd.ttf")
    draw.text((10 * ss, 8 * ss), text, font=font, fill=(255, 255, 255, 255))


def _text(draw: ImageDraw.ImageDraw, center: tuple[float, float], text: str, font,
          fill: tuple[int, int, int], outline: tuple[int, int, int], ss: int,
          anchor: str = "mm") -> None:
    """Text the way the game sets it: a soft drop shadow and a hairline dark
    edge, readable over any frame without looking stencilled. Centred by
    default; "rm" right-aligns on the point."""
    x, y = center
    off = 2 * ss
    try:
        draw.text((x + off, y + off), text, font=font, fill=(0, 0, 0, 130), anchor=anchor,
                  stroke_width=ss, stroke_fill=(0, 0, 0, 130))
        draw.text((x, y), text, font=font, fill=fill + (255,), anchor=anchor,
                  stroke_width=ss, stroke_fill=outline + (190,))
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
