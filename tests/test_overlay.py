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
    assert cfg.get("overlay.width") >= 40 and cfg.get("overlay.height") >= 40
    assert 0.2 <= cfg.get("overlay.opacity") <= 1.0


# --------------------------------------------------------------------------
# headless rendering
# --------------------------------------------------------------------------


def test_test_frame_is_transparent_where_nothing_is_drawn():
    img = render_test_frame(360, 300, Style())
    assert img.mode == "RGBA" and img.size == (360, 300)
    for corner in ((0, 0), (359, 0), (0, 299), (359, 299)):
        assert img.getpixel(corner)[3] == 0, f"corner {corner} must be fully transparent"
    # the arrow shaft sits left of centre, the text at the bottom: both opaque
    assert img.getpixel((int(360 * 0.42), int(300 * 0.6)))[3] == 255
    assert max(img.getpixel((x, int(300 * 0.90)))[3] for x in range(150, 210)) > 200


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
    def __init__(self, x, y, width, height, *, hotkey=None, on_hotkey=None, on_geometry=None, opacity=1.0):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.hotkey, self.on_hotkey, self.on_geometry, self.opacity = hotkey, on_hotkey, on_geometry, opacity
        self.edit_mode = False
        self.frames = []

    def create(self):
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
    ov.render()
    assert len(ov.window.frames) == 1 and ov.window.frames[0].size == (cfg.get("overlay.width"), cfg.get("overlay.height"))

    ov.toggle_edit_mode()
    assert ov.window.edit_mode is True
    ov.window.on_geometry(500, 120, 420, 330, False)   # mid-drag: re-render only
    ov.idle()
    assert ov.window.frames[-1].size == (420, 330)
    assert not (cfg_dir / "local.yaml").exists(), "nothing written until the drag ends"
    ov.window.on_geometry(512, 128, 420, 330, True)    # drag ends
    ov.idle()
    saved = yaml.safe_load((cfg_dir / "local.yaml").read_text(encoding="utf-8"))
    assert saved["overlay"] == {"x": 512, "y": 128, "width": 420, "height": 330}
    assert cfg.get("overlay.x") == 512, "the config sees its own write"
    ov.toggle_edit_mode()
    assert ov.window.edit_mode is False


def test_config_changes_are_picked_up_while_running(cfg_dir):
    from codriver.overlay.app import Overlay

    cfg = Config.load(cfg_dir)
    ov = Overlay(cfg, window_factory=FakeWindow)
    ov.idle()
    before = len(ov.window.frames)
    ov.idle()
    assert len(ov.window.frames) == before, "no change, no redraw"
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
    w = LayeredWindow(60, 60, 240, 180, hotkey=parse_hotkey("ctrl+shift+f12"), opacity=0.9)
    w.create()
    try:
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
