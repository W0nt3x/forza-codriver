"""Shared sample-level helpers: resampling, crossfading, clip assembly.

Used by the placeholder beeps, the voice-pack loader and the TTS generator.
One implementation of each, so the duration the scheduler plans with and the
audio the player emits can never drift apart, they are computed by the
same code path.
"""

from __future__ import annotations

from math import gcd
from pathlib import Path
from typing import Sequence

import numpy as np


def resample(samples: np.ndarray, samplerate: int, target_sr: int) -> np.ndarray:
    """Polyphase resample to ``target_sr``; a no-op when rates already match."""
    if samplerate == target_sr:
        return np.asarray(samples, dtype=np.float32)
    from scipy.signal import resample_poly

    g = gcd(int(samplerate), int(target_sr))
    out = resample_poly(samples, target_sr // g, samplerate // g)
    return np.ascontiguousarray(out, dtype=np.float32)


def fade_edges(samples: np.ndarray, samplerate: int, ms: float = 6.0) -> np.ndarray:
    """A few ms of linear fade at both ends, so butt-joints cannot click."""
    n = min(len(samples) // 2, max(1, int(ms / 1000.0 * samplerate)))
    if n <= 0:
        return samples
    out = samples.copy()
    out[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
    return out


def load_clip(path: Path | str, target_sr: int) -> np.ndarray:
    """Read any soundfile-readable file to float32 mono at ``target_sr``."""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return resample(data.mean(axis=1), sr, target_sr)


def concat_crossfade(
    clips: Sequence[np.ndarray], samplerate: int, crossfade_s: float
) -> np.ndarray:
    """Butt-join clips with a short linear crossfade between neighbours."""
    if not clips:
        return np.zeros(0, dtype=np.float32)
    xfade = int(crossfade_s * samplerate)
    out = clips[0].copy()
    for clip in clips[1:]:
        k = min(xfade, len(out), len(clip))
        if k > 0:
            ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
            out[-k:] = out[-k:] * (1.0 - ramp) + clip[:k] * ramp
            out = np.concatenate([out, clip[k:]])
        else:
            out = np.concatenate([out, clip])
    return out


def concat_duration(
    clips: Sequence[np.ndarray], samplerate: int, crossfade_s: float
) -> float:
    """Seconds ``concat_crossfade`` will produce for the same inputs.

    Kept next to it on purpose: the scheduler leads with this number, the
    player speaks the other, and they must agree to the sample.
    """
    if not clips:
        return 0.0
    xfade = int(crossfade_s * samplerate)
    # Mirror the concatenation exactly, including the per-join clamp for
    # clips shorter than the crossfade.
    total = len(clips[0])
    for clip in clips[1:]:
        total += len(clip) - min(xfade, total, len(clip))
    return total / samplerate


class ConcatBank:
    """Base for anything that speaks by concatenating per-token clips.

    Subclasses provide ``samplerate``, ``crossfade_s`` and ``clip(token)``;
    ``duration`` and ``render`` come from here and therefore always agree.
    """

    samplerate: int
    crossfade_s: float

    def clip(self, token: str) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError

    def duration(self, tokens: Sequence[str]) -> float:
        return concat_duration(
            [self.clip(t) for t in tokens], self.samplerate, self.crossfade_s
        )

    def render(self, tokens: Sequence[str]) -> np.ndarray:
        return concat_crossfade(
            [self.clip(t) for t in tokens], self.samplerate, self.crossfade_s
        )
