"""On-screen overlay: a transparent, click-through, always-on-top window that
draws the next corner over Forza Horizon 6 (Borderless Windowed).

A second renderer of the same note stream the web HUD shows. Nothing in
here talks to the game; it only draws what the runtime already produces.

    win32.py    the layered window: WS_EX_LAYERED + UpdateLayeredWindow for
                per-pixel alpha, WS_EX_TRANSPARENT for click-through,
                WS_EX_TOPMOST, a global hotkey, the message loop. ctypes only.
    render.py   pure Pillow: a state in, an RGBA image out. Testable headless.
    hotkey.py   "ctrl+shift+o" -> (modifiers, virtual key)
    app.py      wires config, window and renderer together
"""
