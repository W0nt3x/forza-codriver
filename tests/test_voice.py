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
    from codriver.stage.notes import CREST, DIP, JUMP, WATER

    emitted = {str(n) for n in range(1, 7)}
    emitted |= {"left", "right", "tightens", "long", "into", "and", JUMP, CREST, DIP, WATER}
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


def test_configured_bank_is_found_from_any_working_directory(tmp_path, monkeypatch):
    """Regression for the UI bug: pack generated next to config/, runtime
    started elsewhere, result beeps. The pack must load regardless of cwd."""
    from codriver.config import Config
    from codriver.voice.pack import WavBank, load_configured_bank

    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "defaults.yaml").write_text(
        yaml.safe_dump({"audio": {"voices_dir": "voices", "voice_pack": "mine",
                                  "samplerate": SR, "crossfade_ms": 25}}),
        encoding="utf-8",
    )
    write_pack(root / "voices" / "mine", {"3": tone(0.25), "right": tone(0.3)})
    cfg = Config.load(root / "config")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    bank = load_configured_bank(cfg, BeepBank())
    assert isinstance(bank, WavBank)
    assert bank.name == "mine"


# --------------------------------------------------------------------------
# hostile input at the voice boundaries
# --------------------------------------------------------------------------


def test_manifest_clip_names_cannot_leave_the_pack(tmp_path):
    """A manifest is foreign input like a stage file. "../x.wav" must be
    reported as a bad entry, never read."""
    import yaml as _yaml

    from codriver.voice.pack import VoicePackError, check_pack, load_pack

    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"RIFF")
    pack = tmp_path / "evil"
    pack.mkdir()
    (pack / "manifest.yaml").write_text(_yaml.safe_dump({
        "name": "evil", "language": "en",
        "tokens": {"1": "../secret.wav", "2": "..\\secret.wav", "3": "/etc/passwd", "left": "left.wav"},
    }), encoding="utf-8")
    report = check_pack(pack)
    assert len(report.bad_files) == 3 and "left" not in " ".join(report.bad_files)
    assert any("left.wav" in m for m in report.missing_files)
    with pytest.raises(VoicePackError):
        load_pack(pack, 48000, 0.02)


def test_sapi_script_never_takes_a_voice_name_it_did_not_allowlist(tmp_path):
    """The SAPI path is the one place a value reaches an interpreter. The
    voice name is allowlisted and quoted, both; texts are quoted."""
    from codriver.voice.generate import GenerationError, _sapi_script

    for hostile in ("Zira'); Start-Process calc; #", "x; y", "a`nb", "x' + (Get-Content secret) + '"):
        with pytest.raises(GenerationError):
            _sapi_script({"1": "one"}, hostile, tmp_path)
    script = _sapi_script({"1": "don't cut", "2": "two"}, "Microsoft Zira Desktop", tmp_path)
    assert "$s.SelectVoice('Microsoft Zira Desktop');" in script
    assert "$s.Speak('don''t cut');" in script, "quotes are doubled, not passed through"
    assert script.count("SetOutputToWaveFile") == 2


def test_generate_pack_refuses_hostile_parameters_before_doing_anything(tmp_path):
    from codriver.voice.generate import GenerationError, generate_pack

    for kwargs in (
        dict(engine="bash"),
        dict(engine="edge", voice="x'; rm -rf /"),
        dict(engine="edge", rate="; calc"),
        dict(engine="edge", language="../../etc"),
        dict(engine="sapi", voice="Zira'); Start-Process calc; #"),
    ):
        with pytest.raises(GenerationError):
            generate_pack(tmp_path / "p", **kwargs)
    assert not (tmp_path / "p").exists(), "refused before the folder was even made"



def test_edge_generation_can_be_stopped_and_does_not_hang(monkeypatch):
    """A voice job that never ends sits on the UI's one job slot, and every
    other button with it. Stop must end it, and a silent endpoint must not
    hold it forever."""
    import asyncio
    import sys
    import time
    import types

    from codriver.voice.generate import GenerationError, _synthesize_edge

    class Hanging:
        def __init__(self, *args, **kwargs):
            pass

        async def save(self, path):
            await asyncio.sleep(3600)

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Hanging))
    with pytest.raises(GenerationError, match="stopped"):
        _synthesize_edge({"1": "one"}, "en-GB-RyanNeural", "+15%", should_stop=lambda: True)
    t0 = time.monotonic()
    with pytest.raises(GenerationError, match="no answer"):
        _synthesize_edge({"1": "one"}, "en-GB-RyanNeural", "+15%", chunk_timeout_s=0.2)
    assert time.monotonic() - t0 < 5.0
