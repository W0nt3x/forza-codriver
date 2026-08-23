"""The one way a value from outside becomes a file path.

Stage files, voice-pack manifests, request bodies and URL parameters all
carry names that end up joined onto a folder. Every such join goes through
``resolve_inside``: the name must be a plain file name (no separators of
either kind, no ``..``, nothing but letters, digits, space, dot, dash and
underscore), and the *resolved* result must still be under the base folder.
The second check is the one that counts; the first only gives a readable
error. A regex alone is not enough: ``..\\`` looks harmless to a
POSIX-minded check and is a separator on Windows.

Nothing in here is specific to any caller. Callers translate ``UnsafePath``
into whatever their boundary speaks (HTTP 400, a skipped manifest entry, a
GenerationError).
"""

from __future__ import annotations

import re
from pathlib import Path

PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,120}$")
"""A file name and nothing else. Allows the names the app itself produces
("coast-road-sprint.json", "stage2_20260822_231027.fzr", "left.wav")."""


class UnsafePath(ValueError):
    """A name that may not become a path under the given base."""


def inside(base: Path | str, candidate: Path | str) -> bool:
    """True when ``candidate``, fully resolved, is ``base`` or lives under it."""
    try:
        base_r = Path(base).resolve()
        cand_r = Path(candidate).resolve()
    except (OSError, RuntimeError):
        return False
    return cand_r == base_r or base_r in cand_r.parents


def resolve_inside(base: Path | str, name: object, what: str = "file") -> Path:
    """``base/<name>`` for a plain name from outside, or ``UnsafePath``.

    ``what`` only shapes the error message ("recording", "stage", "clip").
    """
    text = str(name if name is not None else "").strip()
    if not text:
        raise UnsafePath(f"no {what} name given")
    if ".." in text or not PLAIN_NAME.match(text):
        raise UnsafePath(f"not a usable {what} name: {text[:80]!r}")
    path = Path(base) / text
    if not inside(base, path):
        raise UnsafePath(f"{what} {text[:80]!r} is outside its folder")
    return path
