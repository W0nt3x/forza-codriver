"""Hotkey strings to Win32: "ctrl+shift+o" -> (modifier flags, virtual key).

Config is where the hotkey lives, so it arrives as text typed by a person.
Unknown words are an error with the list of what is known, not a silent
default that makes the overlay un-toggleable.
"""

from __future__ import annotations

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "win": MOD_WIN, "super": MOD_WIN,
}

_NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "scrolllock": 0x91, "numlock": 0x90, "printscreen": 0x2C,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    **{f"numpad{i}": 0x60 + i for i in range(10)},
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+shift+o' -> (MOD_CONTROL | MOD_SHIFT, 0x4F). Case-insensitive,
    spaces ignored. Raises ValueError with a usable message."""
    parts = [p.strip().lower() for p in str(text or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("hotkey is empty; try ctrl+shift+o")
    mods = 0
    key: int | None = None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
            continue
        if key is not None:
            raise ValueError(f"hotkey {text!r} names two keys; one key plus modifiers")
        key = _key_code(part, text)
    if key is None:
        raise ValueError(f"hotkey {text!r} has no key, only modifiers")
    if mods == 0:
        raise ValueError(f"hotkey {text!r} needs a modifier (ctrl, alt, shift, win), "
                         "or every keystroke in the game would trigger it")
    return mods, key


def _key_code(part: str, whole: str) -> int:
    if len(part) == 1 and (part.isascii() and (part.isalpha() or part.isdigit())):
        return ord(part.upper())
    if part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
        return 0x70 + int(part[1:]) - 1
    if part in _NAMED_KEYS:
        return _NAMED_KEYS[part]
    raise ValueError(
        f"unknown key {part!r} in hotkey {whole!r}; use a letter, a digit, f1-f24 or one of "
        + ", ".join(sorted(_NAMED_KEYS))
    )


def describe(mods: int, key: int) -> str:
    """The other way round, for log lines and the edit-mode caption."""
    words = []
    if mods & MOD_CONTROL:
        words.append("Ctrl")
    if mods & MOD_ALT:
        words.append("Alt")
    if mods & MOD_SHIFT:
        words.append("Shift")
    if mods & MOD_WIN:
        words.append("Win")
    for name, code in _NAMED_KEYS.items():
        if code == key:
            words.append(name.capitalize())
            break
    else:
        if 0x70 <= key <= 0x87:
            words.append(f"F{key - 0x70 + 1}")
        else:
            words.append(chr(key))
    return "+".join(words)
