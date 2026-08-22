"""Tests for the the development rules layout checks.

The interesting test is ``test_verify_catches_a_missing_fh6_insert``. A check
that passes on good data proves nothing on its own, it has to fail on the
specific bug it was written to catch, which here is a parser copied from FM7
or FH5 reading everything from PositionX on 12 bytes early.
"""

from __future__ import annotations

import pytest

from codriver.record.capture import CaptureWriter
from codriver.record.synth import SynthSpec, as_fh5_layout, synth_records
from codriver.record.verify import FAIL, INFO, PASS, SKIP, verify_capture


def _write(path, records, **header):
    with CaptureWriter(path, header={"adapter": "fh6", **header}) as writer:
        for t_ns, payload in records:
            writer.add(payload, t_ns)
    return path


@pytest.fixture
def good_capture(tmp_path):
    spec = SynthSpec(duration_s=45.0, pause_at_s=None, jump_at_s=None)
    return _write(tmp_path / "good.fzr", synth_records(spec))


def _status(report, name):
    return next(c.status for c in report.checks if c.name == name)


def test_a_correct_capture_passes_every_check(good_capture):
    report = verify_capture(good_capture)
    assert report.ok, report.render()
    assert _status(report, "packet_size") == PASS
    assert _status(report, "speed_vs_position") == PASS
    assert _status(report, "speed_vs_velocity") == PASS
    assert _status(report, "distance_traveled") == PASS
    assert _status(report, "stationary") == PASS
    assert _status(report, "rpm_band") == PASS


def test_verify_catches_a_missing_fh6_insert(tmp_path):
    """The classic parser bug, as a test.

    Strip the 12 FH6-only bytes and everything from PositionX on shifts. The
    packet is still 324 bytes and every float still looks like a float, only
    physics gives it away.
    """
    spec = SynthSpec(duration_s=45.0, pause_at_s=None, jump_at_s=None)
    records = [(t, as_fh5_layout(p)) for t, p in synth_records(spec)]
    report = verify_capture(_write(tmp_path / "bad.fzr", records))

    assert not report.ok
    assert _status(report, "speed_vs_position") == FAIL
    assert _status(report, "packet_size") == PASS, "size alone cannot catch this"
    assert any("232" in c.detail for c in report.failed)


def test_wrong_packet_size_fails_loudly(tmp_path):
    records = [(i * 16_666_667, b"\x00" * 311) for i in range(120)]
    report = verify_capture(_write(tmp_path / "short.fzr", records))
    assert _status(report, "packet_size") == FAIL


def test_checks_skip_rather_than_lie_when_data_is_thin(tmp_path):
    """A capture taken in a menu should report SKIP, not a false PASS."""
    spec = SynthSpec(duration_s=2.0, stationary_s=99.0, pause_at_s=None, jump_at_s=None)
    report = verify_capture(_write(tmp_path / "idle.fzr", synth_records(spec)))
    assert _status(report, "speed_vs_position") == SKIP
    assert _status(report, "distance_traveled") == SKIP
    assert report.ok


def test_report_renders_without_crashing(good_capture):
    text = verify_capture(good_capture).render()
    assert "all checks passed" in text
    assert "speed_vs_position" in text


def test_an_empty_capture_does_not_blame_the_offset_table(tmp_path):
    """A capture with no datagrams is a port problem, not a layout problem.

    Reporting it as a layout failure sends you off fixing the one thing that
    is definitely not broken.
    """
    path = _write(tmp_path / "empty.fzr", [])
    report = verify_capture(path)

    assert report.packets == 0
    assert not report.ok
    text = report.render()
    assert "EMPTY" in text
    assert "codriver scan" in text
    assert "offset table" not in text


def test_distance_traveled_flags_scatter_not_a_constant_factor(tmp_path):
    """A field in its own units holds a steady ratio; a misaligned one does not.

    Measured on a real FH6 capture, DistanceTraveled tracks the position
    stream at a tight ~0.79. That is a units/semantics difference, not a
    layout bug, and must not fail the run, but a ratio that scatters is a
    layout bug and must.
    """
    import struct

    from codriver.adapters.fh6 import FH6Adapter

    spec = SynthSpec(duration_s=45.0, pause_at_s=None, jump_at_s=None)
    base = synth_records(spec)
    adapter = FH6Adapter()

    def rewrite(scale, jitter=0.0):
        out = []
        for i, (t_ns, payload) in enumerate(base):
            buf = bytearray(payload)
            true_d = adapter.parse(payload).distance_traveled
            noise = ((i * 7919) % 1000 / 1000.0 - 0.5) * jitter * max(true_d, 1.0)
            struct.pack_into("<f", buf, 292, true_d * scale + noise)
            out.append((t_ns, bytes(buf)))
        return out

    # Constant factor, as the real game does: reported, not failed.
    steady = verify_capture(_write(tmp_path / "steady.fzr", rewrite(0.788)))
    assert _status(steady, "distance_traveled") == INFO
    assert steady.ok
    assert "not metres" in next(
        c.detail for c in steady.checks if c.name == "distance_traveled"
    )

    # Scattering ratio, as a misaligned field would: failed.
    noisy = verify_capture(_write(tmp_path / "noisy.fzr", rewrite(1.0, jitter=0.6)))
    assert _status(noisy, "distance_traveled") == FAIL
    assert "292" in next(c.detail for c in noisy.failed if c.name == "distance_traveled")
