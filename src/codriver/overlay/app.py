"""Config, window, state and renderer, wired.

Everything the user can change lives under ``overlay.*`` and is hot-reloaded
the way the rest of the project does it: the idle callback polls the config
and re-renders when it changed. Placement is stored as shares of the screen
(``x``, ``y``, ``size`` = height, ``aspect`` = width/height), so the overlay
is the same size on a 1080p and a 4K monitor and comes back where it was
left. A drag or resize in edit mode writes those back to config/local.yaml.

The overlay consumes the runtime's event dicts through ``state.handle_event``;
who delivers them (the UI process in-process, or a socket client) is the
caller's business, see ``feed.py`` and the UI server.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable

from ..config import Config
from .hotkey import describe, parse_hotkey
from .render import Style, render_frame
from .state import OverlayState

log = logging.getLogger(__name__)

WindowFactory = Callable[..., object]
FRAME_MS = 66
"""Redraw cadence while live: 15 frames a second is plenty for a distance
that shrinks and an arrow that changes a few times a minute."""


class Overlay:
    def __init__(self, cfg: Config, window_factory: WindowFactory | None = None) -> None:
        self.cfg = cfg
        self.state = OverlayState()
        self.edit_mode = False
        self._dirty = True
        self._last_view = None
        self._pending_geometry: tuple[int, int, int, int] | None = None
        self._thread: threading.Thread | None = None
        mods, vk = parse_hotkey(str(cfg.get("overlay.hotkey")))
        self.hotkey_text = describe(mods, vk)
        if window_factory is None:
            from .win32 import LayeredWindow, make_dpi_aware
            make_dpi_aware()
            window_factory = LayeredWindow
        self._window_cls = window_factory
        self.screen_w, self.screen_h = window_factory.screen_size()
        x, y, w, h = self._pixels_from_config()
        self.window = window_factory(
            x, y, w, h,
            hotkey=(mods, vk), on_hotkey=self.toggle_edit_mode, on_geometry=self._on_geometry,
            opacity=float(cfg.get("overlay.opacity")),
        )

    # -- geometry in shares of the screen -----------------------------------------

    def _pixels_from_config(self) -> tuple[int, int, int, int]:
        self._migrate_legacy_placement()
        size = min(1.0, max(0.05, float(self.cfg.get("overlay.size"))))
        aspect = min(4.0, max(0.4, float(self.cfg.get("overlay.aspect"))))
        h = max(40, int(size * self.screen_h))
        w = max(40, int(h * aspect))
        # On the screen, whatever the file says: a window placed off-screen is
        # "the overlay does nothing" to the person in front of it.
        xf = min(max(0.0, float(self.cfg.get("overlay.x"))), max(0.0, 1.0 - w / self.screen_w))
        yf = min(max(0.0, float(self.cfg.get("overlay.y"))), max(0.0, 1.0 - h / self.screen_h))
        return int(xf * self.screen_w), int(yf * self.screen_h), w, h

    def _migrate_legacy_placement(self) -> None:
        """The first version stored x, y, width, height in pixels. Read as
        shares those put the window a thousand screens to the right. Convert
        once and drop the keys nothing reads any more."""
        x, y = float(self.cfg.get("overlay.x")), float(self.cfg.get("overlay.y"))
        if x > 2.0 or y > 2.0:
            # That placement was made for the first version's 45-pixel box;
            # it says nothing about where this window should go. Back to the
            # defaults, the hotkey places it again in two seconds.
            self.cfg.unset_local("overlay.x")
            self.cfg.unset_local("overlay.y")
            log.info("overlay: dropped the old pixel placement, using the default position")
        for key in ("overlay.width", "overlay.height", "overlay.font_px"):
            if self.cfg.get(key, None) is not None:
                self.cfg.unset_local(key)

    def _on_geometry(self, x: int, y: int, w: int, h: int, final: bool) -> None:
        """During a drag or resize: re-render at the new size. When it ends:
        remember the placement in local.yaml, as shares of the screen."""
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
        values = {
            "overlay.x": round(x / self.screen_w, 4),
            "overlay.y": round(y / self.screen_h, 4),
            "overlay.size": round(h / self.screen_h, 4),
            "overlay.aspect": round(w / h, 4),
        }
        for key, value in values.items():
            self.cfg.set_local(key, value)
        log.info("overlay placed at %d,%d size %dx%d (saved)", x, y, w, h)

    # -- edit mode -------------------------------------------------------------------

    def toggle_edit_mode(self) -> None:
        self.edit_mode = not self.edit_mode
        self.window.set_edit_mode(self.edit_mode)
        log.info("overlay edit mode %s (%s toggles)", "on" if self.edit_mode else "off", self.hotkey_text)
        self._dirty = True

    # -- frames ------------------------------------------------------------------------

    def style(self) -> Style:
        from .render import parse_rgb

        base = Style()
        cfg = self.cfg
        palette = cfg.section("display.colours") if "display" in cfg.data else {}
        severity = tuple(parse_rgb(palette.get(f"class_{n}"), base.severity_rgb[n - 1]) for n in range(1, 7))
        bend = cfg.get("overlay.bend_degrees", list(base.bend_degrees))
        try:
            bend = tuple(float(b) for b in bend)[:6]
            if len(bend) < 6:
                bend = base.bend_degrees
        except (TypeError, ValueError):
            bend = base.bend_degrees
        return Style(
            font=str(cfg.get("overlay.font", base.font) or base.font),
            accent_rgb=parse_rgb(cfg.get("overlay.accent", ""), base.accent_rgb),
            panel=bool(cfg.get("overlay.panel", True)),
            opacity=float(cfg.get("overlay.opacity")),
            severity_rgb=severity,
            hazard_rgb=parse_rgb(palette.get("hazard"), base.hazard_rgb),
            water_rgb=parse_rgb(palette.get("water"), base.water_rgb),
            bend_degrees=bend,
            bar_full_m=float(cfg.get("overlay.bar_full_m", base.bar_full_m) or base.bar_full_m),
            text_scale=float(cfg.get("overlay.text_scale", 1.0) or 1.0),
            arrow_scale=float(cfg.get("overlay.arrow_scale", 1.0) or 1.0),
        )

    def render(self, now: float | None = None) -> None:
        view = self.state.view(now)
        if self.edit_mode and view.next is None:
            # Nothing to show yet: a sample picture, so the user places a
            # window that looks like the real thing.
            from .render import render_test_frame
            img = render_test_frame(self.window.width, self.window.height, self.style(), edit_mode=True,
                                    caption=self._caption())
        else:
            img = render_frame(view, self.window.width, self.window.height, self.style(),
                               edit_mode=self.edit_mode, caption=self._caption())
        self.window.opacity = float(self.cfg.get("overlay.opacity"))
        self.window.present(img)
        self._last_view = view
        self._dirty = False

    def _caption(self) -> str:
        return f"edit: drag to move, corner resizes, {self.hotkey_text} locks"

    def idle(self) -> None:
        """Called by the window's message loop every few dozen ms."""
        self.persist_geometry()
        if self.cfg.poll():
            self._dirty = True
        view = self.state.view()
        # Live: redraw every tick, the distance moves. Otherwise only on change.
        if self._dirty or view != self._last_view or view.mode == "tracking":
            self.render()

    # -- running ---------------------------------------------------------------------------

    def run(self) -> None:
        """Create the window and pump it on the calling thread until closed."""
        if sys.platform != "win32":
            raise RuntimeError("the overlay needs Windows")
        log.info("overlay: %s toggles edit mode; Forza must run in Borderless Windowed", self.hotkey_text)
        self.window.create()
        self.render()
        self.window.run(on_idle=self.idle, idle_ms=FRAME_MS)

    def start_in_thread(self) -> threading.Thread:
        """For hosts that have their own main loop (the UI server)."""
        self._thread = threading.Thread(target=self.run, name="codriver-overlay", daemon=True)
        self._thread.start()
        return self._thread

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float = 3.0) -> None:
        self.window.request_close()
        if self._thread is not None:
            self._thread.join(timeout_s)
        if self.running:
            log.warning("overlay thread did not stop within %.0fs", timeout_s)

    def handle_event(self, event: dict) -> None:
        """The one entry point for runtime events, whatever carries them."""
        self.state.handle_event(event, time.monotonic())
