"""Voice packs, the audio design's swappable word banks.

A pack is a directory::

    voices/<name>/
      manifest.yaml     token -> wav filename, plus metadata
      one.wav
      left.wav
      ...

Everything is loaded into memory as float32 numpy buffers at startup,
resampled to the output rate if needed. Playback never touches the disk.

Two loud-failure rules, both from the audio design:

* A token the pack is missing is reported at **load time**, not discovered
  as silence at 140 km/h. At runtime a missing token falls back to its
  placeholder beep, so the phrase stays audible and correctly timed.
* The manifest naming a file that does not exist, or a file that will not
  decode, is an error worth stopping for.

``WavBank`` shares ``ConcatBank`` with the beeps, the scheduler's lead
maths automatically uses real word lengths the moment a pack loads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from ..audio import ConcatBank, load_clip
from ..config import Config
from ..runtime.player import BeepBank

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.yaml"


class VoicePackError(Exception):
    pass


@dataclass
class PackReport:
    """What `codriver voice check` prints."""

    name: str
    path: Path
    tokens: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    bad_files: list[str] = field(default_factory=list)
    total_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.missing_files and not self.bad_files


@dataclass
class WavBank(ConcatBank):
    """A loaded voice pack.

    Tokens the pack lacks are spoken by the fallback beep bank, audibly a
    beep in the middle of speech, which is exactly the right amount of wrong:
    the phrase stays timed and the gap is impossible to miss.
    """

    samplerate: int
    crossfade_s: float
    clips: dict[str, np.ndarray]
    name: str = "voice"
    fallback: BeepBank | None = None

    _warned: set = field(default_factory=set, repr=False)

    def clip(self, token: str) -> np.ndarray:
        clip = self.clips.get(token)
        if clip is not None:
            return clip
        if token not in self._warned:
            self._warned.add(token)
            log.warning(
                "voice pack '%s' has no clip for token '%s'; using a beep",
                self.name,
                token,
            )
        if self.fallback is None:
            return np.zeros(int(0.3 * self.samplerate), dtype=np.float32)
        return self.fallback.clip(token)


def read_manifest(pack_dir: Path) -> dict:
    manifest_path = pack_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise VoicePackError(f"{pack_dir} has no {MANIFEST_NAME}")
    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = yaml.safe_load(fh)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tokens"), dict):
        raise VoicePackError(
            f"{manifest_path} must be a mapping with a 'tokens' section"
        )
    return manifest


def check_pack(pack_dir: Path | str, samplerate: int = 48000) -> PackReport:
    """Validate a pack without keeping it: every manifest entry must exist
    and decode."""
    pack_dir = Path(pack_dir)
    manifest = read_manifest(pack_dir)
    report = PackReport(name=manifest.get("name", pack_dir.name), path=pack_dir)
    for token, filename in sorted(manifest["tokens"].items()):
        report.tokens.append(str(token))
        wav = pack_dir / str(filename)
        if not wav.is_file():
            report.missing_files.append(f"{token} -> {filename}")
            continue
        try:
            clip = load_clip(wav, samplerate)
            report.total_seconds += len(clip) / samplerate
        except Exception as exc:
            report.bad_files.append(f"{token} -> {filename} ({exc})")
    return report


def load_pack(
    pack_dir: Path | str,
    samplerate: int,
    crossfade_s: float,
    fallback: BeepBank | None = None,
) -> WavBank:
    """Load a pack into memory. Raises VoicePackError on a broken manifest."""
    pack_dir = Path(pack_dir)
    manifest = read_manifest(pack_dir)
    clips: dict[str, np.ndarray] = {}
    broken: list[str] = []
    for token, filename in manifest["tokens"].items():
        wav = pack_dir / str(filename)
        try:
            clips[str(token)] = load_clip(wav, samplerate)
        except Exception as exc:
            broken.append(f"{token} -> {filename} ({exc})")
    if broken:
        raise VoicePackError(
            f"voice pack {pack_dir} has unreadable clips:\n  " + "\n  ".join(broken)
        )
    if not clips:
        raise VoicePackError(f"voice pack {pack_dir} contains no clips")
    log.info(
        "voice pack '%s': %d clips, %.1f s of audio",
        manifest.get("name", pack_dir.name),
        len(clips),
        sum(len(c) for c in clips.values()) / samplerate,
    )
    return WavBank(
        samplerate=samplerate,
        crossfade_s=crossfade_s,
        clips=clips,
        name=manifest.get("name", pack_dir.name),
        fallback=fallback,
    )


def load_configured_bank(cfg: Config, fallback: BeepBank) -> ConcatBank:
    """The pack named by ``audio.voice_pack``, or the beeps with a loud hint.

    A missing pack is a normal state (none generated yet), so it degrades; a
    *broken* pack is a mistake worth stopping for, so it raises.
    """
    pack_name = cfg.get("audio.voice_pack")
    # Against the project root, never the working directory: the UI writes
    # packs next to config/, and wherever the process was started from, this
    # has to find them there.
    pack_dir = cfg.path("audio.voices_dir") / str(pack_name)
    if not (pack_dir / MANIFEST_NAME).is_file():
        log.warning(
            "no voice pack at %s, using placeholder beeps. "
            "Generate one with: python -m codriver voice generate",
            pack_dir,
        )
        return fallback
    return load_pack(
        pack_dir,
        samplerate=cfg.get("audio.samplerate"),
        crossfade_s=cfg.get("audio.crossfade_ms") / 1000.0,
        fallback=fallback,
    )


def stage_coverage(
    pack_dir: Path | str, stage_tokens: set[str]
) -> tuple[set[str], set[str]]:
    """(covered, missing) tokens for a stage. What `voice check --stage` uses:
    the question that matters is never 'is the pack complete', it is 'can it
    speak *this* stage'."""
    manifest = read_manifest(Path(pack_dir))
    have = {str(t) for t in manifest["tokens"]}
    return stage_tokens & have, stage_tokens - have
