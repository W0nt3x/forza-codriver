"""The placeholder beep bank, and the timing contract it must honour.

The load-bearing property: ``duration(tokens)`` must equal the length of
``render(tokens)``. The scheduler leads with one and the mouth speaks the
other, if they diverge, the trigger timing being tuned is a lie.
"""

from __future__ import annotations

import numpy as np
import pytest

from codriver.runtime.player import BeepBank, NullPlayer, _category, make_player


@pytest.fixture
def bank():
    return BeepBank(samplerate=48000, base_clip_s=0.35, crossfade_s=0.025)


def test_duration_matches_render_exactly(bank):
    for tokens in (
        ["3", "right"],
        ["100", "left", "tightens", "1"],
        ["3", "right", "into", "2", "left"],
        ["jump"],
    ):
        rendered = len(bank.render(tokens)) / bank.samplerate
        assert bank.duration(tokens) == pytest.approx(rendered, abs=1e-4), tokens


def test_empty_phrase_is_zero(bank):
    assert bank.duration([]) == 0.0
    assert len(bank.render([])) == 0


def test_crossfade_makes_a_phrase_shorter_than_its_clips(bank):
    tokens = ["3", "right", "into", "2", "left"]
    butt = sum(len(bank.clip(t)) for t in tokens) / bank.samplerate
    assert bank.duration(tokens) == pytest.approx(butt - 4 * 0.025, abs=1e-4)


def test_phrases_have_word_like_lengths(bank):
    """The placeholder exists to make timing tunable before recording: a
    typical call must take roughly as long as speaking it would."""
    assert 0.4 < bank.duration(["3", "right"]) < 1.0
    assert 1.0 < bank.duration(["100", "left", "tightens", "1"]) < 2.5


def test_output_is_clean_float32(bank):
    samples = bank.render(["100", "left", "tightens", "1"])
    assert samples.dtype == np.float32
    assert float(np.max(np.abs(samples))) <= 1.0
    # Starts and ends at silence: no clicks at the phrase boundary.
    assert abs(float(samples[0])) < 1e-3
    assert abs(float(samples[-1])) < 1e-3


def test_severity_numbers_ride_a_pitch_scale(bank):
    """1 must sound more urgent (higher) than 6, so severity is audible
    before the vocabulary is learnt."""

    def dominant_hz(token):
        clip = bank.clip(token).astype(np.float64)
        spectrum = np.abs(np.fft.rfft(clip))
        return float(np.fft.rfftfreq(len(clip), 1 / bank.samplerate)[np.argmax(spectrum)])

    pitches = [dominant_hz(str(n)) for n in range(1, 7)]
    assert pitches == sorted(pitches, reverse=True)


def test_clips_are_cached(bank):
    a = bank.clip("3")
    assert bank.clip("3") is a


def test_categories_cover_the_stage_vocabulary():
    for token, want in [
        ("3", "number"),
        ("left", "direction"),
        ("100", "distance"),
        ("into", "link"),
        ("jump", "hazard"),
        ("tightens", "modifier"),
    ]:
        assert _category(token) == want


def test_make_player_silent_returns_null():
    assert isinstance(make_player(48000, 256, None, 0.0, silent=True), NullPlayer)


def test_stream_player_fills_device_blocks_without_hardware():
    """The audio callback, fed by hand. A regression for a one-character slip
    (``out[: 0]`` instead of ``out[:, 0]``) that silently broke playback and
    could not be caught while the callback lived inside sounddevice."""
    import threading

    from codriver.runtime.player import StreamPlayer

    p = StreamPlayer.__new__(StreamPlayer)  # no device, no stream
    p._lock = threading.Lock()
    p._pending = []
    p._pos = 0
    p._gain = 1.0

    samples = np.linspace(-0.5, 0.5, 600, dtype=np.float32)
    p.play(samples)

    out = np.zeros((256, 1), dtype=np.float32)  # exactly what PortAudio hands over
    assert p.fill_block(out, 256) == 256
    assert np.array_equal(out[:, 0], samples[:256])
    assert p.fill_block(out, 256) == 256
    assert np.array_equal(out[:, 0], samples[256:512])
    assert p.fill_block(out, 256) == 88, "the tail of the phrase, then silence"
    assert np.array_equal(out[:88, 0], samples[512:])
    assert not out[88:, 0].any()
    assert p.fill_block(out, 256) == 0 and not out.any()

    p._gain = 0.5
    p.play(np.ones(256, dtype=np.float32))
    p.fill_block(out, 256)
    assert np.allclose(out[:, 0], 0.5)
