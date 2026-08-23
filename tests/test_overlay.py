"""The overlay, stage 1: hotkey parsing, headless rendering, geometry
persistence, and (on a Windows desktop) the real window's styles."""

from __future__ import annotations

import ctypes
import sys

import pytest
import yaml

from codriver.config import Config, find_config_dir
from codriver.overlay.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, describe, parse_hotkey
from codriver.overlay.render import Style, render_test_frame, to_premultiplied_bgra


# --------------------------------------------------------------------------
# hotkey strings
# --------------------------------------------------------------------------


def test_hotkey_strings_parse_to_modifiers_and_a_key():
    assert parse_hotkey("ctrl+shift+o") == (MOD_CONTROL | MOD_SHIFT, ord("O"))
    assert parse_hotkey("Ctrl + Alt + F9") == (MOD_CONTROL | MOD_ALT, 0x70 + 8)
    assert parse_hotkey("alt+space") == (MOD_ALT, 0x20)
    assert describe(*parse_hotkey("ctrl+shift+o")) == "Ctrl+Shift+O"
    assert describe(*parse_hotkey("ctrl+f9")) == "Ctrl+F9"


@pytest.mark.parametrize("bad", ["", "o", "ctrl", "ctrl+shift", "ctrl+o+p", "ctrl+bogus", "ctrl+f25"])
def test_bad_hotkeys_fail_with_a_message_not_a_default(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_shipped_hotkey_is_valid():
    cfg = Config.load(find_config_dir())
    parse_hotkey(cfg.get("overlay.hotkey"))
    assert 0.05 <= cfg.get("overlay.size") <= 1.0 and 0.4 <= cfg.get("overlay.aspect") <= 4.0
    assert 0.2 <= cfg.get("overlay.opacity") <= 1.0


# --------------------------------------------------------------------------
# headless rendering
# --------------------------------------------------------------------------


def test_test_frame_is_transparent_where_nothing_is_drawn():
    img = render_test_frame(360, 300, Style())
    assert img.mode == "RGBA" and img.size == (360, 300)
    for corner in ((0, 0), (359, 0), (0, 299), (359, 299)):
        assert img.getpixel(corner)[3] == 0, f"corner {corner} must be fully transparent"
    # the arrow shaft in the middle band and the text near the bottom: both opaque
    assert max(img.getpixel((x, int(300 * 0.5)))[3] for x in range(360)) == 255
    assert max(img.getpixel((x, int(300 * 0.79)))[3] for x in range(360)) == 255


def test_edit_mode_makes_the_window_bounds_visible():
    plain = render_test_frame(200, 160, Style(), edit_mode=False)
    edit = render_test_frame(200, 160, Style(), edit_mode=True, caption="edit")
    assert plain.getpixel((5, 80))[3] == 0
    assert edit.getpixel((5, 80))[3] > 0, "edit mode shows a frame so the window can be placed"
    assert edit.getpixel((197, 157))[3] > 150, "the resize grip is drawn"


def test_premultiplied_bgra_is_what_updatelayeredwindow_wants():
    from PIL import Image

    img = Image.new("RGBA", (2, 1))
    img.putpixel((0, 0), (255, 0, 0, 128))   # half-transparent red
    img.putpixel((1, 0), (0, 0, 255, 255))   # opaque blue
    data = to_premultiplied_bgra(img)
    assert len(data) == 2 * 1 * 4
    b, g, r, a = data[0:4]
    assert (r, g, b, a) == (128, 0, 0, 128), "premultiplied: red scaled by alpha, channels BGRA"
    assert tuple(data[4:8]) == (255, 0, 0, 255)


def test_tiny_sizes_do_not_crash_the_renderer():
    img = render_test_frame(1, 1, Style())
    assert img.size[0] >= 40 and img.size[1] >= 40


# --------------------------------------------------------------------------
# the app: geometry persistence, with a fake window
# --------------------------------------------------------------------------


class FakeWindow:
    SCREEN = (1920, 1080)

    def __init__(self, x, y, width, height, *, hotkey=None, on_hotkey=None, on_geometry=None, opacity=1.0):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.hotkey, self.on_hotkey, self.on_geometry, self.opacity = hotkey, on_hotkey, on_geometry, opacity
        self.edit_mode = False
        self.frames = []

    @classmethod
    def screen_size(cls):
        return cls.SCREEN

    def create(self):
        pass

    def request_close(self):
        pass

    def present(self, img):
        self.frames.append(img)

    def set_edit_mode(self, on):
        self.edit_mode = on

    def run(self, on_idle=None, idle_ms=33):
        pass


@pytest.fixture
def cfg_dir(tmp_path):
    import shutil

    src = find_config_dir()
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(src / "defaults.yaml", d / "defaults.yaml")
    return d


def test_dragging_in_edit_mode_persists_the_placement(cfg_dir):
    from codriver.overlay.app import Overlay

    cfg = Config.load(cfg_dir)
    ov = Overlay(cfg, window_factory=FakeWindow)
    ov.toggle_edit_mode()   # no data yet: edit mode shows the sample picture
    ov.render()
    # 26 % of a 1080 screen, aspect 1.25: 280 x 350, placed at 76 % / 14 %
    assert ov.window.frames[0].size == (350, 280)
    assert (ov.window.x, ov.window.y) == (int(0.76 * 1920), int(0.14 * 1080))

    assert ov.window.edit_mode is True
    ov.window.on_geometry(500, 120, 420, 330, False)   # mid-drag: re-render only
    ov.idle()
    assert ov.window.frames[-1].size == (420, 330)
    assert not (cfg_dir / "local.yaml").exists(), "nothing written until the drag ends"
    ov.window.on_geometry(512, 128, 420, 330, True)    # drag ends
    ov.idle()
    saved = yaml.safe_load((cfg_dir / "local.yaml").read_text(encoding="utf-8"))
    assert saved["overlay"] == {"x": round(512 / 1920, 4), "y": round(128 / 1080, 4),
                                "size": round(330 / 1080, 4), "aspect": round(420 / 330, 4)}
    assert cfg.get("overlay.size") == round(330 / 1080, 4), "the config sees its own write"
    ov.toggle_edit_mode()
    assert ov.window.edit_mode is False


def test_config_changes_are_picked_up_while_running(cfg_dir):
    from codriver.overlay.app import Overlay

    cfg = Config.load(cfg_dir)
    ov = Overlay(cfg, window_factory=FakeWindow)
    ov.idle()
    before = len(ov.window.frames)
    ov.idle()
    assert len(ov.window.frames) == before, "idle and unchanged: no redraw"
    (cfg_dir / "local.yaml").write_text("overlay:\n  opacity: 0.5\n", encoding="utf-8")
    cfg.poll(immediate=True)
    ov._dirty = True  # what idle() does after a successful poll
    ov.idle()
    assert ov.window.opacity == 0.5


# --------------------------------------------------------------------------
# the real window, only where there is a Windows desktop
# --------------------------------------------------------------------------


def _has_windows_desktop() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetDesktopWindow()) and bool(ctypes.windll.user32.GetShellWindow())
    except Exception:
        return False


@pytest.mark.skipif(not _has_windows_desktop(), reason="needs a Windows desktop")
def test_the_window_is_layered_topmost_clickthrough_and_does_not_take_focus():
    from codriver.overlay import win32
    from codriver.overlay.win32 import LayeredWindow

    foreground_before = win32.user32.GetForegroundWindow()
    sw, sh = LayeredWindow.screen_size()
    assert sw > 0 and sh > 0
    reported = []
    w = LayeredWindow(60, 60, 240, 180, hotkey=parse_hotkey("ctrl+shift+f12"), opacity=0.9,
                      on_geometry=lambda *g: reported.append(g))
    w.create()
    try:
        assert all(g[2] > 0 and g[3] > 0 for g in reported),             f"creation must never report a zero-size rectangle: {reported}"
        assert w.geometry() == (60, 60, 240, 180), "the requested size survives creation"
        style = win32.get_exstyle(w.hwnd)
        for flag, name in ((win32.WS_EX_LAYERED, "LAYERED"), (win32.WS_EX_TOPMOST, "TOPMOST"),
                           (win32.WS_EX_TRANSPARENT, "TRANSPARENT"), (win32.WS_EX_TOOLWINDOW, "TOOLWINDOW"),
                           (win32.WS_EX_NOACTIVATE, "NOACTIVATE")):
            assert style & flag, f"WS_EX_{name} missing"
        w.present(render_test_frame(240, 180, Style()))
        assert win32.user32.IsWindowVisible(w.hwnd)
        assert win32.user32.GetForegroundWindow() != w.hwnd, "the overlay must not steal focus"
        assert win32.user32.GetForegroundWindow() == foreground_before
        assert w.geometry() == (60, 60, 240, 180)

        w.set_edit_mode(True)
        assert not (win32.get_exstyle(w.hwnd) & win32.WS_EX_TRANSPARENT), "edit mode takes the mouse"
        w.set_edit_mode(False)
        assert win32.get_exstyle(w.hwnd) & win32.WS_EX_TRANSPARENT, "locked again: click-through"

        w.present(render_test_frame(300, 200, Style()))
        assert w.geometry()[2:] == (300, 200), "a frame's size is the window's size"
    finally:
        w.destroy()
    assert not win32.user32.IsWindow(w.hwnd or 0)


def test_legacy_pixel_placement_is_converted_and_kept_on_screen(cfg_dir):
    """Stage 1 stored pixels; read as shares they put the window a thousand
    screens to the right. Convert once, drop the dead keys, clamp on screen."""
    from codriver.overlay.app import Overlay

    (cfg_dir / "local.yaml").write_text(
        "overlay:\n  x: 1856\n  y: 64\n  width: 45\n  height: 47\n", encoding="utf-8")
    cfg = Config.load(cfg_dir)
    ov = Overlay(cfg, window_factory=FakeWindow)
    assert 0 <= ov.window.x <= 1920 - ov.window.width
    assert 0 <= ov.window.y <= 1080 - ov.window.height
    assert (ov.window.x, ov.window.y) == (int(cfg.get("overlay.x") * 1920), int(cfg.get("overlay.y") * 1080))
    saved = yaml.safe_load((cfg_dir / "local.yaml").read_text(encoding="utf-8")) or {}
    assert "overlay" not in saved, "the old placement is dropped entirely, defaults apply"
    assert cfg.get("overlay.x") == 0.76, "the shipped default: top right"


@pytest.mark.skipif(not _has_windows_desktop(), reason="needs a Windows desktop")
def test_a_second_window_gets_its_own_messages_after_the_first_is_gone():
    """The class is registered once per process. Its window procedure must
    route by hwnd, not to whichever instance registered it: that instance may
    be stopped and collected while a later overlay is alive (seen live: the
    hotkey crashed the thread with an access violation)."""
    import gc

    from codriver.overlay import win32
    from codriver.overlay.win32 import LayeredWindow

    first = LayeredWindow(50, 50, 100, 80)
    first.create()
    first.destroy()
    del first
    gc.collect()

    hits = []
    second = LayeredWindow(60, 60, 120, 90, hotkey=parse_hotkey("ctrl+shift+f11"), on_hotkey=lambda: hits.append(1))
    second.create()
    try:
        win32.user32.SendMessageW(second.hwnd, win32.WM_HOTKEY, win32.HOTKEY_ID, 0)
        assert hits == [1], "the second window's hotkey reaches the second window's handler"
    finally:
        second.destroy()

