"""The layered window, ctypes only. Windows only.

Per-pixel alpha the one way Win32 offers it: the window is WS_EX_LAYERED and
every frame is handed over with UpdateLayeredWindow as a 32-bit premultiplied
BGRA bitmap (BLENDFUNCTION AC_SRC_ALPHA). No colour key, no whole-window
alpha, so an anti-aliased arrow edge blends with whatever the game draws.

Click-through is WS_EX_TRANSPARENT: the mouse never sees the window. Edit
mode clears that bit, and WM_NCHITTEST answers HTCAPTION (drag) or
HTBOTTOMRIGHT (resize) so Windows itself moves and sizes the window.
WS_EX_TOPMOST keeps it over a borderless game window; WS_EX_TOOLWINDOW keeps
it out of the taskbar and Alt+Tab; WS_EX_NOACTIVATE plus SW_SHOWNOACTIVATE
means the game keeps focus when the overlay appears.

One thread owns the window: it is created, pumped and destroyed on the
thread that calls ``run``. RegisterHotKey posts WM_HOTKEY to that thread.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Callable

from PIL import Image

from .render import to_premultiplied_bgra

log = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover - the module is imported for its constants elsewhere
    user32 = gdi32 = kernel32 = None
else:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# -- constants -----------------------------------------------------------------
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
GWL_EXSTYLE = -20
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HWND_TOPMOST = -1
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_NCHITTEST = 0x0084
WM_EXITSIZEMOVE = 0x0232
WM_HOTKEY = 0x0312
WM_APP = 0x8000
HTTRANSPARENT = -1
HTCLIENT = 1
HTCAPTION = 2
HTBOTTOMRIGHT = 17
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
HOTKEY_ID = 1
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
CLASS_NAME = "CodriverOverlayWindow"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE), ("AlphaFormat", wintypes.BYTE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _bind() -> None:
    """argtypes/restype for everything used, so a wrong call fails loudly
    instead of silently truncating a 64-bit handle."""
    u, g, k = user32, gdi32, kernel32
    u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    u.RegisterClassExW.restype = wintypes.ATOM
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = LRESULT
    u.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, wintypes.LPVOID, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.RegisterHotKey.restype = wintypes.BOOL
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SetWindowPos.restype = wintypes.BOOL
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
                                      wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                      ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    u.UpdateLayeredWindow.restype = wintypes.BOOL
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.GetForegroundWindow.restype = wintypes.HWND
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindow.restype = wintypes.BOOL
    if hasattr(u, "GetWindowLongPtrW"):
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = wintypes.LONG
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    u.SetWindowLongW.restype = wintypes.LONG
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    g.CreateDIBSection.restype = wintypes.HBITMAP
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE


if user32 is not None:
    _bind()


def get_exstyle(hwnd: int) -> int:
    if hasattr(user32, "GetWindowLongPtrW"):
        return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
    return int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF


def set_exstyle(hwnd: int, style: int) -> None:
    if hasattr(user32, "SetWindowLongPtrW"):
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    else:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(style).value)


def make_dpi_aware() -> None:
    """Per-monitor DPI awareness, so sizes in config are real pixels and a
    150 % desktop does not get a blurry, doubled overlay. Harmless if it was
    already set (returns false)."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except (AttributeError, OSError):  # pre-1703 Windows
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass


class LayeredWindow:
    """One transparent, click-through, topmost window. Create, present frames,
    toggle edit mode, pump messages; all on one thread."""

    GRIP_PX = 28  # bottom-right corner that resizes in edit mode

    def __init__(self, x: int, y: int, width: int, height: int, *,
                 hotkey: tuple[int, int] | None = None,
                 on_hotkey: Callable[[], None] | None = None,
                 on_geometry: Callable[[int, int, int, int, bool], None] | None = None,
                 opacity: float = 1.0) -> None:
        if user32 is None:
            raise RuntimeError("the overlay needs Windows")
        self.x, self.y, self.width, self.height = int(x), int(y), max(40, int(width)), max(40, int(height))
        self.hotkey = hotkey
        self.on_hotkey = on_hotkey
        self.on_geometry = on_geometry
        self.opacity = opacity
        self.hwnd: int | None = None
        self.edit_mode = False
        self._alive = False
        self._wndproc = WNDPROC(self._handle)  # keep the callback alive
        self._class_atom: int | None = None

    # -- lifecycle -----------------------------------------------------------

    def create(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = CLASS_NAME
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))  # IDC_ARROW
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise ctypes.WinError(ctypes.get_last_error())
        exstyle = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(exstyle, CLASS_NAME, "codriver overlay", WS_POPUP,
                                      self.x, self.y, self.width, self.height, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd
        self._alive = True
        if self.hotkey is not None:
            mods, vk = self.hotkey
            from .hotkey import MOD_NOREPEAT
            if not user32.RegisterHotKey(hwnd, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                log.warning("overlay hotkey could not be registered (in use by another program?); "
                            "edit mode is unavailable until you change overlay.hotkey")
        # Shown without activation: the game keeps keyboard and focus.
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def destroy(self) -> None:
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            user32.DestroyWindow(self.hwnd)
        self.hwnd = None
        self._alive = False

    def request_close(self) -> None:
        """Thread-safe: ask the window thread to shut down."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # -- drawing ---------------------------------------------------------------

    def present(self, img: Image.Image) -> None:
        """Hand one RGBA frame to the compositor. The frame's size becomes the
        window's size."""
        if not self.hwnd:
            return
        w, h = img.size
        data = to_premultiplied_bgra(img)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # negative: top-down rows, like the image
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bmp or not bits.value:
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(mem, bmp)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(round(self.opacity * 255)))), AC_SRC_ALPHA)
            size = wintypes.SIZE(w, h)
            src = wintypes.POINT(0, 0)
            ok = user32.UpdateLayeredWindow(self.hwnd, None, None, ctypes.byref(size), mem,
                                            ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem, old)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            self.width, self.height = w, h
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)

    # -- edit mode -------------------------------------------------------------

    def set_edit_mode(self, on: bool) -> None:
        """Edit mode: the mouse reaches the window (drag, resize). Off: every
        click goes through to the game."""
        if not self.hwnd:
            return
        self.edit_mode = bool(on)
        style = get_exstyle(self.hwnd)
        style = (style & ~WS_EX_TRANSPARENT) if on else (style | WS_EX_TRANSPARENT)
        set_exstyle(self.hwnd, style)
        user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)

    def geometry(self) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def move(self, x: int, y: int) -> None:
        if self.hwnd:
            user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            self.x, self.y = int(x), int(y)

    # -- message loop ----------------------------------------------------------

    def run(self, on_idle: Callable[[], None] | None = None, idle_ms: int = 33) -> None:
        """Pump messages until the window closes; call ``on_idle`` about every
        ``idle_ms`` and after every burst of messages."""
        if self.hwnd is None:
            self.create()
        msg = wintypes.MSG()
        while self._alive:
            user32.MsgWaitForMultipleObjects(0, None, False, idle_ms, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._alive = False
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if self._alive and on_idle is not None:
                try:
                    on_idle()
                except Exception:  # a render bug must not kill the loop
                    log.exception("overlay idle handler failed")
        self.destroy()

    def _handle(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                if self.on_hotkey:
                    self.on_hotkey()
                return 0
            if msg == WM_NCHITTEST and self.edit_mode:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                left, top, w, h = self.geometry()
                if x >= left + w - self.GRIP_PX and y >= top + h - self.GRIP_PX:
                    return HTBOTTOMRIGHT
                return HTCAPTION
            if msg == WM_SIZE:
                if self.on_geometry:
                    l, t, w, h = self.geometry()
                    self.on_geometry(l, t, w, h, False)
                return 0
            if msg == WM_EXITSIZEMOVE:
                if self.on_geometry:
                    l, t, w, h = self.geometry()
                    self.on_geometry(l, t, w, h, True)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("overlay window procedure failed")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
