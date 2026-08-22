"""Voice packs: the loader, the post-processing, and the coverage report.

TTS engines are deliberately not under test, they need a network or a
Windows speech stack. Everything after the engine is: the the audio design clip rules
(trim hard, normalise, no clicks) and the loader's loud-failure behaviour.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
import soundfile as sf
import yaml

from codriver.runtime.player import BeepBank
from codriver.voice.generate import (
    edge_fades,
    normalize_rms,
    post_process,
    trim_silence,
)
from codriver.voice.pack import (
    VoicePackError,
    WavBank,
    check_pack,
    load_pack,
    stage_coverage,
)
from codriver.voice.vocab import VOCABULARY, spoken_text

SR = 48000


def tone(seconds=0.3, hz=440.0, sr=SR, amp=0.5):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def write_pack(directory, clips, sr=SR, manifest_extra=None):
    directory.mkdir(parents=True, exist_ok=True)
    tokens = {}
    for token, samples in clips.items():
        sf.write(directory / f"{token}.wav", samples, sr, subtype="PCM_16")
        tokens[token] = f"{token}.wav"
    manifest = {"name": directory.name, "samplerate": sr, "tokens": tokens}
    manifest.update(manifest_extra or {})
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )
    return directory


# --------------------------------------------------------------------------
# post-processing
# --------------------------------------------------------------------------


def test_trim_silence_cuts_both_ends_and_keeps_the_word():
    """Leading silence is what makes concatenated speech sound like a
    broken train announcer."""
    word = tone(0.3)
    padded = np.concatenate([np.zeros(SR // 2, np.float32), word, np.zeros(SR, np.float32)])
    trimmed = trim_silence(padded, SR)
    assert len(trimmed) < len(word) + int(0.05 * SR)
    assert len(trimmed) > len(word) * 0.9


def test_trim_silence_never_returns_nothing():
    quiet = np.full(SR, 1e-5, dtype=np.float32)
    assert len(trim_silence(quiet, SR)) == len(quiet)


def test_normalize_hits_the_target_loudness():
    quiet = tone(amp=0.01)
    loud = tone(amp=0.9)
    n_quiet = normalize_rms(quiet, target_db=-20.0)
    n_loud = normalize_rms(loud, target_db=-20.0)
    rms = lambda x: float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    assert rms(n_quiet) == pytest.approx(rms(n_loud), rel=0.05), (
        "identical loudness across the whole bank"
    )


def test_normalize_respects_the_peak_ceiling():
    spiky = tone(amp=0.01)
    spiky[100] = 0.02
    out = normalize_rms(spiky, target_db=-3.0, peak_ceiling=0.98)
    assert float(np.max(np.abs(out))) <= 0.98 + 1e-6


def test_fades_remove_edge_clicks():
    clip = tone(0.2)
    faded = edge_fades(clip, SR)
    assert abs(float(faded[0])) < 1e-4
    assert abs(float(faded[-1])) < 1e-4


def test_post_process_resamples_trims_and_normalises_together():
    sr_in = 24000
    word = (0.05 * np.sin(2 * np.pi * 300 * np.arange(int(0.3 * sr_in)) / sr_in)).astype(
        np.float32
    )
    raw = np.concatenate([np.zeros(sr_in, np.float32), word, np.zeros(sr_in, np.float32)])
    out = post_process(raw, sr_in, SR)
    assert out.dtype == np.float32
    # ~0.3s of content at 48k, not 2.3s
    assert int(0.2 * SR) < len(out) < int(0.45 * SR)
    rms = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
    assert rms == pytest.approx(10 ** (-20 / 20), rel=0.15)


# --------------------------------------------------------------------------
# the loader
# --------------------------------------------------------------------------


def test_load_pack_round_trips_clips(tmp_path):
    pack = write_pack(tmp_path / "v", {"3": tone(0.25), "right": tone(0.3, 550)})
    bank = load_pack(pack, samplerate=SR, crossfade_s=0.025)
    assert set(bank.clips) == {"3", "right"}
    assert bank.duration(["3", "right"]) == pytest.approx(0.55 - 0.025, abs=0.01)


def test_load_pack_resamples_to_the_output_rate(tmp_path):
    pack = write_pack(tmp_path / "v", {"3": tone(0.5, sr=24000)}, sr=24000)
    bank = load_pack(pack, samplerate=SR, crossfade_s=0.025)
    assert len(bank.clips["3"]) == pytest.approx(0.5 * SR, rel=0.01)


def test_duration_equals_render_for_wav_banks(tmp_path):
    """Same contract as the beeps: the scheduler leads with duration(), the
    mouth speaks render(). They must be the same number."""
    pack = write_pack(
        tmp_path / "v",
        {"100": tone(0.5), "left": tone(0.3), "tightens": tone(0.45), "1": tone(0.2)},
    )
    bank = load_pack(pack, samplerate=SR, crossfade_s=0.025)
    tokens = ["100", "left", "tightens", "1"]
    assert bank.duration(tokens) == pytest.approx(
        len(bank.render(tokens)) / SR, abs=1e-3
    )


def test_missing_token_falls_back_to_a_beep_and_warns_once(tmp_path, caplog):
    """Missing tokens warn loudly, not fail silently at 140 km/h. And the
    phrase must stay correctly timed, so the beep stands in."""
    pack = write_pack(tmp_path / "v", {"3": tone(0.25)})
    bank = load_pack(pack, samplerate=SR, crossfade_s=0.025, fallback=BeepBank())
    with caplog.at_level(logging.WARNING):
        rendered = bank.render(["3", "right"])
        bank.render(["3", "right"])
    assert len(rendered) > len(bank.clips["3"])
    assert caplog.text.count("no clip for token 'right'") == 1
    assert bank.duration(["3", "right"]) == pytest.approx(len(rendered) / SR, abs=1e-3)


def test_broken_manifest_raises(tmp_path):
    directory = tmp_path / "v"
    directory.mkdir()
    (directory / "manifest.yaml").write_text("just_a_list: true", encoding="utf-8")
    with pytest.raises(VoicePackError, match="tokens"):
        load_pack(directory, samplerate=SR, crossfade_s=0.025)


def test_manifest_naming_a_missing_file_raises(tmp_path):
    pack = write_pack(tmp_path / "v", {"3": tone(0.25)})
    manifest = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["tokens"]["ghost"] = "ghost.wav"
    (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(VoicePackError, match="ghost"):
        load_pack(pack, samplerate=SR, crossfade_s=0.025)


# --------------------------------------------------------------------------
# check and coverage
# --------------------------------------------------------------------------


def test_check_pack_reports_missing_and_unreadable_files(tmp_path):
    pack = write_pack(tmp_path / "v", {"3": tone(0.25), "left": tone(0.3)})
    (pack / "left.wav").write_bytes(b"this is not audio")
    manifest = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["tokens"]["ghost"] = "ghost.wav"
    (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    report = check_pack(pack, samplerate=SR)
    assert not report.ok
    assert any("ghost" in m for m in report.missing_files)
    assert any("left" in b for b in report.bad_files)
    assert report.total_seconds == pytest.approx(0.25, abs=0.02)


def test_stage_coverage_answers_the_question_that_matters(tmp_path):
    """Not 'is the pack complete' but 'can it speak this stage'."""
    pack = write_pack(
        tmp_path / "v", {"3": tone(0.2), "right": tone(0.2), "100": tone(0.2)}
    )
    covered, missing = stage_coverage(pack, {"3", "right", "100", "jump", "left"})
    assert covered == {"3", "right", "100"}
    assert missing == {"jump", "left"}


def test_vocabulary_covers_everything_the_generator_can_emit():
    """Every token stage/notes.py can produce must have a spoken form, or a
    generated pack would ship with holes."""
    from codriver.stage.notes import CREST, DIP, JUMP

    emitted = {str(n) for n in range(1, 7)}
    emitted |= {"left", "right", "tightens", "into", "and", JUMP, CREST, DIP}
    emitted |= {"30", "50", "70", "100", "150", "200", "250", "300", "400", "500"}
    assert emitted <= set(VOCABULARY)


def test_unknown_tokens_are_spoken_literally():
    assert spoken_text("3") == "three"
    assert spoken_text("dont_cut") == "don't cut"
    assert spoken_text("gravel") == "gravel"


# --------------------------------------------------------------------------
# languages
# --------------------------------------------------------------------------


def test_german_vocabulary_covers_the_same_tokens_as_english():
    """Token keys are language-neutral: any stage works with any pack. A
    language missing a token would silently beep where English speaks."""
    from codriver.voice.vocab import VOCABULARIES

    assert set(VOCABULARIES["de"]) == set(VOCABULARIES["en"])


def test_spoken_text_is_per_language():
    assert spoken_text("3", "de") == "drei"
    assert spoken_text("tightens", "de") == "zieht zu"
    assert spoken_text("jump", "de") == "Sprung"
    assert spoken_text("150", "de") == "hundertfünfzig"
    assert spoken_text("3", "en") == "three"


def test_unknown_language_fails_loudly():
    from codriver.voice.vocab import vocabulary

    with pytest.raises(ValueError, match="available"):
        vocabulary("fr")


def test_each_language_has_a_default_edge_voice():
    from codriver.voice.vocab import DEFAULT_VOICES, VOCABULARIES

    assert set(DEFAULT_VOICES) == set(VOCABULARIES)
