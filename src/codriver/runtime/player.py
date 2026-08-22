"""The audio design, audio.

Everything is pre-rendered; nothing synthesises in the hot path. A phrase is
assembled by butt-joining per-token clips with a short crossfade, which takes
microseconds on numpy arrays, and handed to a persistent low-latency output
stream. The scheduler has already guaranteed phrases never overlap.

Until a voice pack exists, the bank is **placeholder beeps with realistic
per-token durations**, a number takes about as long to beep as to say, a
linked phrase takes as long as its words. Timing is the whole point of the
placeholder: the lead-time maths in the scheduler uses the same durations the
mouth does, so trigger feel can be tuned before a single word is recorded.
The pitches are deterministic per token, so with a little practice "high
beep, low beep" is genuinely readable as "1, left".

Both banks share ``ConcatBank``: ``duration`` and ``render`` are one
implementation, so the scheduler's lead and the player's output can never
disagree about how long a phrase is.
"""

from __future__ import annotations

import logging
import threading
import zlib
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from ..audio import ConcatBank

log = logging.getLogger(__name__)


class ClipBank(Protocol):
    """What the scheduler and player need from a voice."""

    samplerate: int

    def duration(self, tokens: Sequence[str]) -> float:
        """Seconds the assembled phrase will take. Must match render()."""
        ...

    def render(self, tokens: Sequence[str]) -> np.ndarray:
        """The phrase as float32 mono samples in [-1, 1]."""
        ...


# --------------------------------------------------------------------------
# placeholder beeps
# --------------------------------------------------------------------------

# Relative spoken lengths per token category, scaled by
# audio.placeholder_clip_s. Rough word lengths, not precision, what matters
# is that a six-token phrase costs about six tokens of time.
_LINKS = {"into", "and", "then"}
_HAZARDS = {"jump", "crest", "dip", "bump", "narrows", "over"}
_DISTANCES = {"30", "50", "70", "100", "150", "200", "250", "300", "400", "500"}
_MODIFIERS = {"tightens", "opens", "long", "short", "caution", "care"}
_SEVERITIES = {"1", "2", "3", "4", "5", "6"}

_CATEGORY_SCALE = {
    "number": 0.8,
    "direction": 0.9,
    "distance": 1.2,
    "link": 0.55,
    "hazard": 1.1,
    "modifier": 1.3,
    "other": 1.0,
}


def _category(token: str) -> str:
    if token in _DISTANCES:
        return "distance"
    if token.isdigit():
        return "number"
    if token in ("left", "right"):
        return "direction"
    if token in _LINKS:
        return "link"
    if token in _HAZARDS:
        return "hazard"
    if token in _MODIFIERS:
        return "modifier"
    return "other"


def _pitch(token: str) -> float:
    """Deterministic pitch per token, audibly structured.

    Severity numbers ride a scale with 1 highest, the tighter the corner,
    the more urgent the beep. Left sits low, right sits high, so direction is
    readable without looking. Everything else hashes into a mid band, with
    crc32, not ``hash()``, which Python randomises per process and would
    give a token a different pitch every run.
    """
    if token in _SEVERITIES:
        return 1000.0 - (int(token) - 1) * 90.0
    if token == "left":
        return 330.0
    if token == "right":
        return 620.0
    if token in _DISTANCES:
        return 260.0
    if token in _LINKS:
        return 210.0
    if token == "tightens":
        return 1150.0
    if token in _HAZARDS:
        return 1400.0
    return 400.0 + (zlib.crc32(token.encode("utf-8")) % 17) * 25.0


@dataclass
class BeepBank(ConcatBank):
    """Placeholder clips: pure tones with word-like durations.

    ``base_clip_s`` is ``audio.placeholder_clip_s``, one knob that scales
    every duration, so "the beeps feel rushed/slow" is a config edit.
    """

    samplerate: int = 48000
    base_clip_s: float = 0.35
    crossfade_s: float = 0.025
    gain: float = 0.4

    _cache: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    def clip(self, token: str) -> np.ndarray:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        seconds = self.base_clip_s * _CATEGORY_SCALE[_category(token)]
        n = max(1, int(seconds * self.samplerate))
        t = np.arange(n) / self.samplerate
        tone = np.sin(2 * np.pi * _pitch(token) * t)
        # Distances get a double pulse so "100" is audibly not a corner number.
        if _category(token) == "distance":
            tone *= 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 8.0 * t))
        # Quick fades kill the clicks butt-joining would otherwise produce.
        fade = max(1, int(0.008 * self.samplerate))
        env = np.ones(n)
        env[:fade] = np.linspace(0.0, 1.0, fade)
        env[-fade:] = np.linspace(1.0, 0.0, fade)
        out = (tone * env * self.gain).astype(np.float32)
        self._cache[token] = out
        return out

    def retune(self, base_clip_s: float) -> None:
        """Hot-reload hook: a new base length invalidates every cached clip."""
        if base_clip_s != self.base_clip_s:
            self.base_clip_s = base_clip_s
            self._cache.clear()


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


class Player(Protocol):
    def play(self, samples: np.ndarray) -> None: ...
    def stop_all(self) -> None: ...
    def close(self) -> None: ...


@dataclass
class NullPlayer:
    """No audio device (or --silent): phrases are visible in the HUD only."""

    def play(self, samples: np.ndarray) -> None:  # noqa: ARG002
        pass

    def stop_all(self) -> None:
        pass

    def close(self) -> None:
        pass


class StreamPlayer:
    """A persistent sounddevice output stream fed from a sample queue.

    One stream for the whole session, opening a stream per phrase would put
    device latency inside the trigger timing. The callback pulls from a plain
    list under a lock; at 48 kHz / 256 frames that is ~5 ms per callback,
    nowhere near contention.
    """

    def __init__(
        self,
        samplerate: int = 48000,
        blocksize: int = 256,
        device: int | str | None = None,
        gain_db: float = 0.0,
    ):
        import sounddevice as sd  # deferred: importable without an audio stack

        self.samplerate = samplerate
        self._gain = 10.0 ** (gain_db / 20.0)
        self._lock = threading.Lock()
        self._pending: list[np.ndarray] = []
        self._pos = 0
        self._stream = sd.OutputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=1,
            dtype="float32",
            device=device,
            callback=self._callback,
        )
        self._stream.start()
        log.info(
            "audio stream open: %d Hz, blocksize %d, ~%.1f ms/block",
            samplerate, blocksize, 1000.0 * blocksize / samplerate,
        )

    def _callback(self, out, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            log.debug("audio status: %s", status)
        self.fill_block(out, frames)

    def fill_block(self, out: np.ndarray, frames: int) -> int:
        """Copy queued samples into the device buffer ``out`` (frames x 1).

        Split out of the callback so it can be tested without a sound card.
        Returns how many frames carried audio; the rest is silence.
        """
        buf = out[:, 0] if out.ndim == 2 else out
        buf.fill(0.0)
        filled = 0
        with self._lock:
            while filled < frames and self._pending:
                head = self._pending[0]
                take = min(frames - filled, len(head) - self._pos)
                buf[filled : filled + take] = head[self._pos : self._pos + take]
                self._pos += take
                filled += take
                if self._pos >= len(head):
                    self._pending.pop(0)
                    self._pos = 0
        if self._gain != 1.0:
            buf *= self._gain
        return filled

    def play(self, samples: np.ndarray) -> None:
        with self._lock:
            self._pending.append(samples)

    def stop_all(self) -> None:
        """Cut whatever is playing. Used on suspend/rewind, a note for a
        corner that no longer exists must not keep talking."""
        with self._lock:
            self._pending.clear()
            self._pos = 0

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def make_player(
    samplerate: int,
    blocksize: int,
    device: int | str | None,
    gain_db: float,
    silent: bool = False,
) -> Player:
    if silent:
        return NullPlayer()
    try:
        return StreamPlayer(samplerate, blocksize, device, gain_db)
    except Exception as exc:
        log.warning("no audio output (%s); running silent", exc)
        return NullPlayer()
