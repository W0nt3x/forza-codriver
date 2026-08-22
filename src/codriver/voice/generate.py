"""Generate a voice pack with TTS, at build time, never at runtime.

Two engines:

* ``edge``  -- Microsoft's neural voices via the edge-tts package. Needs
  network *while generating only*; the output is ordinary WAV files. Best
  quality by far. Default voice is a British male, which is simply what a
  rally co-driver sounds like.
* ``sapi``  -- the offline Windows voices via System.Speech (PowerShell).
  No network, no extra packages, audibly synthetic. The fallback.

Whatever the engine produces goes through the same post-processing, which is
where the audio design's clip requirements actually happen:

* resample to the output rate, mono
* **trim silence hard** at both ends, leading silence is what makes
  concatenated speech sound like a broken train announcer
* normalise to identical RMS loudness across the whole bank
* a few ms of fade at each end so butt-joints cannot click

The result is a ``voices/<name>/`` directory with a manifest, immediately
loadable by ``pack.py``.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from ..audio import fade_edges, resample
from .vocab import DEFAULT_VOICES, spoken_text, vocabulary

log = logging.getLogger(__name__)


class GenerationError(Exception):
    pass


# --------------------------------------------------------------------------
# post-processing, the audio design's clip requirements
# --------------------------------------------------------------------------


def trim_silence(
    samples: np.ndarray,
    samplerate: int,
    threshold_db: float = -38.0,
    keep_ms: float = 12.0,
) -> np.ndarray:
    """Cut leading and trailing silence, keeping a few ms of breath.

    Envelope-based: a short RMS window against a dB threshold, so a quiet
    fricative onset survives while actual silence does not.
    """
    if len(samples) == 0:
        return samples
    window = max(1, int(0.008 * samplerate))
    padded = np.concatenate([samples, np.zeros(window, dtype=samples.dtype)])
    energy = np.sqrt(
        np.convolve(padded.astype(np.float64) ** 2, np.ones(window) / window, "same")
    )
    threshold = 10.0 ** (threshold_db / 20.0)
    loud = np.nonzero(energy[: len(samples)] > threshold)[0]
    if len(loud) == 0:
        return samples  # never return emptiness for a quiet take
    keep = int(keep_ms / 1000.0 * samplerate)
    lo = max(0, int(loud[0]) - keep)
    hi = min(len(samples), int(loud[-1]) + keep)
    return samples[lo:hi]


def normalize_rms(
    samples: np.ndarray, target_db: float = -20.0, peak_ceiling: float = 0.98
) -> np.ndarray:
    """Equal perceived loudness across the bank, with a hard peak ceiling."""
    if len(samples) == 0:
        return samples
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    if rms <= 1e-9:
        return samples
    gain = 10.0 ** (target_db / 20.0) / rms
    peak = float(np.max(np.abs(samples))) * gain
    if peak > peak_ceiling:
        gain *= peak_ceiling / peak
    return (samples * gain).astype(np.float32)


# Kept under its historical name for callers; the implementation lives in
# codriver.audio alongside the crossfade it pairs with.
edge_fades = fade_edges


def post_process(
    samples: np.ndarray,
    samplerate: int,
    target_sr: int,
    trim_db: float = -38.0,
    target_rms_db: float = -20.0,
) -> np.ndarray:
    samples = resample(np.asarray(samples, dtype=np.float32), samplerate, target_sr)
    samples = trim_silence(samples, target_sr, threshold_db=trim_db)
    samples = normalize_rms(samples, target_db=target_rms_db)
    return edge_fades(samples, target_sr)


# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------


def _synthesize_edge(texts: dict[str, str], voice: str, rate: str) -> dict[str, tuple[np.ndarray, int]]:
    """All tokens through edge-tts. Returns token -> (samples, samplerate)."""
    import asyncio

    import soundfile as sf

    try:
        import edge_tts
    except ImportError as exc:
        raise GenerationError("edge-tts is not installed: pip install edge-tts") from exc

    async def one(token: str, text: str, out_dir: Path) -> tuple[str, Path]:
        path = out_dir / f"{token}.mp3"
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(path))
        return token, path

    async def all_tokens(out_dir: Path):
        # A few at a time: fast, without hammering the endpoint.
        results = []
        items = list(texts.items())
        for i in range(0, len(items), 6):
            chunk = items[i : i + 6]
            results += await asyncio.gather(
                *(one(tok, txt, out_dir) for tok, txt in chunk)
            )
            log.info("edge-tts: %d/%d", min(i + 6, len(items)), len(items))
        return results

    out: dict[str, tuple[np.ndarray, int]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            results = asyncio.run(all_tokens(tmp_dir))
        except Exception as exc:
            raise GenerationError(
                f"edge-tts failed ({exc}). No network? Try --engine sapi."
            ) from exc
        for token, path in results:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
            out[token] = (data.mean(axis=1), sr)
    return out


def _synthesize_sapi(texts: dict[str, str], voice: str | None, rate: str) -> dict[str, tuple[np.ndarray, int]]:
    """All tokens through the offline Windows System.Speech voices."""
    import soundfile as sf

    out: dict[str, tuple[np.ndarray, int]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        select = (
            f"$s.SelectVoice('{voice}');" if voice else ""
        )
        lines = ["Add-Type -AssemblyName System.Speech;",
                 "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;",
                 select,
                 "$s.Rate = 1;"]
        for token, text in texts.items():
            wav = tmp_dir / f"{token}.wav"
            safe = text.replace("'", "''")
            lines.append(f"$s.SetOutputToWaveFile('{wav}');")
            lines.append(f"$s.Speak('{safe}');")
        lines.append("$s.SetOutputToNull(); $s.Dispose();")
        script = tmp_dir / "gen.ps1"
        script.write_text("\n".join(lines), encoding="utf-8")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GenerationError(f"SAPI synthesis failed: {result.stderr.strip()}")
        for token in texts:
            wav = tmp_dir / f"{token}.wav"
            if not wav.is_file():
                raise GenerationError(f"SAPI produced no file for '{token}'")
            data, sr = sf.read(wav, dtype="float32", always_2d=True)
            out[token] = (data.mean(axis=1), sr)
    return out


# --------------------------------------------------------------------------
# the pack builder
# --------------------------------------------------------------------------


@dataclass
class GenerateResult:
    pack_dir: Path
    engine: str
    voice: str
    clips: int = 0
    total_seconds: float = 0.0
    durations: dict[str, float] = field(default_factory=dict)


def generate_pack(
    out_dir: Path | str,
    engine: str = "edge",
    voice: str | None = None,
    samplerate: int = 48000,
    rate: str = "+15%",
    language: str = "en",
    tokens: dict[str, str] | None = None,
) -> GenerateResult:
    """Generate, post-process and write a complete voice pack.

    ``language`` picks the vocabulary (what words are spoken for each token)
    and the default TTS voice. Token keys never change with language, a
    stage built once works with every pack.

    ``rate`` leans the delivery slightly fast, co-drivers talk briskly, and
    a brisk word bank buys lead-time headroom at speed.
    """
    import soundfile as sf

    out_dir = Path(out_dir)
    vocab = vocabulary(language)
    texts = dict(tokens or {t: spoken_text(t, language) for t in vocab})

    if engine == "edge":
        voice = voice or DEFAULT_VOICES.get(language)
        if voice is None:
            raise GenerationError(
                f"no default edge voice for language {language!r}; pass --voice"
            )
        raw = _synthesize_edge(texts, voice, rate)
    elif engine == "sapi":
        raw = _synthesize_sapi(texts, voice, rate)
        voice = voice or "system default"
    else:
        raise GenerationError(f"unknown engine {engine!r}; use 'edge' or 'sapi'")

    out_dir.mkdir(parents=True, exist_ok=True)
    result = GenerateResult(pack_dir=out_dir, engine=engine, voice=voice)
    manifest_tokens: dict[str, str] = {}
    for token, (samples, sr) in raw.items():
        processed = post_process(samples, sr, samplerate)
        if len(processed) < samplerate * 0.05:
            log.warning("clip for '%s' is suspiciously short after trimming", token)
        filename = f"{token}.wav"
        sf.write(out_dir / filename, processed, samplerate, subtype="PCM_16")
        manifest_tokens[token] = filename
        result.clips += 1
        result.durations[token] = len(processed) / samplerate
        result.total_seconds += len(processed) / samplerate

    manifest = {
        "name": out_dir.name,
        "engine": engine,
        "voice": voice,
        "language": language,
        "samplerate": samplerate,
        "tokens": manifest_tokens,
    }
    with (out_dir / "manifest.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=True)
    log.info(
        "voice pack '%s': %d clips, %.1f s, engine %s (%s)",
        out_dir.name,
        result.clips,
        result.total_seconds,
        engine,
        voice,
    )
    return result
