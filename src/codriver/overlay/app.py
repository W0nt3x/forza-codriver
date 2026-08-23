"""Config, window and renderer, wired. Stage 1: the static test arrow.

Everything the user can change lives under ``overlay.*`` in the config and
is hot-reloaded the way the rest of the project does it: the idle callback
polls the config and re-renders when it changed. Position and size are
written back to config/local.yaml when a drag or resize ends, so the overlay
comes back where it was left.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

from ..config import Config
from .hotkey import describe, parse_hotkey
from .render import Style, render_test_frame

log = logging.getLogger(__name__)

WindowFactory = Callable[..., object]


class Overlay:
    def __init__(self, cfg: Config, window_factory: WindowFactory | None = None) -> None:
        self.cfg = cfg
        self.edit_mode = False
        self._dirty = True
        self._pending_geometry: tuple[int, int, int, int] | None = None
        mods, vk = parse_hotkey(str(cfg.get("overlay.hotkey")))
        self.hotkey_text = describe(mods, vk)
        if window_factory is None:
            from .win32 import LayeredWindow, make_dpi_aware
            make_dpi_aware()
            window_factory = LayeredWindow
        self.window = window_factory(
            int(cfg.get("overlay.x")), int(cfg.get("overlay.y")),
            int(cfg.get("overlay.width")), int(cfg.get("overlay.height")),
            hotkey=(mods, vk), on_hotkey=self.toggle_edit_mode, on_geometry=self._on_geometry,
            opacity=float(cfg.get("overlay.opacity")),
        )

    # -- what the window calls back ------------------------------------------

    def toggle_edit_mode(self) -> None:
        self.edit_mode = not self.edit_mode
        self.window.set_edit_mode(self.edit_mode)
        log.info("overlay edit mode %s (%s toggles)", "on" if self.edit_mode else "off", self.hotkey_text)
        self._dirty = True

    def _on_geometry(self, x: int, y: int, w: int, h: int, final: bool) -> None:
        """During a drag or resize: re-render at the new size. When it ends:
        remember the placement in local.yaml."""
        self.window.x, self.window.y = x, y
        self.window.width, self.window.height = max(40, w), max(40, h)
        self._dirty = True
        if final:
            self._pending_geometry = (x, y, self.window.width, self.window.height)

    def persist_geometry(self) -> None:
        if self._pending_geometry is None:
            return
        x, y, w, h = self._pending_geometry
        self._pending_geometry = None
        for key, value in (("overlay.x", x), ("overlay.y", y), ("overlay.width", w), ("overlay.height", h)):
            self.cfg.set_local(key, int(value))
        log.info("overlay placed at %d,%d size %dx%d (saved)", x, y, w, h)

    # -- frames ------------------------------------------------------------------

    def style(self) -> Style:
        return Style(
            font_px=int(self.cfg.get("overlay.font_px", 64)),
            opacity=float(self.cfg.get("overlay.opacity")),
        )

    def render(self) -> None:
        caption = f"edit: drag to move, corner resizes, {self.hotkey_text} locks"
        img = render_test_frame(self.window.width, self.window.height, self.style(),
                                edit_mode=self.edit_mode, caption=caption)
        self.window.opacity = float(self.cfg.get("overlay.opacity"))
        self.window.present(img)
        self._dirty = False

    def idle(self) -> None:
        """Called by the window's message loop every few dozen ms."""
        self.persist_geometry()
        if self.cfg.poll():
            self._dirty = True
        if self._dirty:
            self.render()

    def run(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("the overlay needs Windows")
        log.info("overlay: %s toggles edit mode; Forza must run in Borderless Windowed", self.hotkey_text)
        self.window.create()
        self.render()
        self.window.run(on_idle=self.idle)
