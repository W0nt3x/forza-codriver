"""Capture file format, replay scheduling, and a real socket round trip."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from codriver.adapters.fh6 import pack_fields
from codriver.net.udp import UdpListener
from codriver.record.capture import (
    CaptureError,
    CaptureReader,
    CaptureWriter,
    default_capture_path,
    read_all,
    summarize,
)
from codriver.record.replay import build_schedule, replay_records
from codriver.record.synth import SynthSpec, synth_records, write_synth

NS = 1_000_000_000


def _records(n: int = 10, dt_ns: int = 16_666_667) -> list[tuple[int, bytes]]:
    return [
        (i * dt_ns, pack_fields({"Speed": float(i), "IsRaceOn": 1}))
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# capture format
# --------------------------------------------------------------------------


def test_round_trip_preserves_bytes_and_timing(tmp_path):
    path = tmp_path / "a.fzr"
    records = _records(50)
    with CaptureWriter(path, header={"adapter": "fh6", "note": "hello"}) as writer:
        for t_ns, payload in records:
            writer.add(payload, t_ns)
        assert writer.count == 50

    header, back = read_all(path)
    assert header["adapter"] == "fh6"
    assert header["note"] == "hello"
    assert header["format"] == "fzr"
    assert back == records


def test_timestamps_are_relative_to_the_first_datagram(tmp_path):
    """Arrival times come from perf_counter_ns, whose epoch is arbitrary."""
    path = tmp_path / "a.fzr"
    base = 987_654_321_000
    with CaptureWriter(path) as writer:
        writer.add(b"x" * 324, base)
        writer.add(b"y" * 324, base + 500_000_000)

    _, records = read_all(path)
    assert records[0][0] == 0
    assert records[1][0] == 500_000_000


def test_truncated_file_reads_back_its_complete_prefix(tmp_path):
    """A capture killed mid-drive must not lose the part that was written."""
    path = tmp_path / "a.fzr"
    with CaptureWriter(path) as writer:
        for t_ns, payload in _records(20):
            writer.add(payload, t_ns)

    blob = path.read_bytes()
    path.write_bytes(blob[:-200])  # chop the tail mid-record

    with CaptureReader(path) as reader:
        recovered = list(reader)
        assert reader.truncated is True
    assert 15 <= len(recovered) < 20
    assert all(len(p) == 324 for _, p in recovered)


def test_non_capture_file_is_rejected_clearly(tmp_path):
    path = tmp_path / "nope.fzr"
    path.write_bytes(b"this is not a capture")
    with pytest.raises(CaptureError, match="not a capture file"):
        with CaptureReader(path):
            pass


def test_summary_reports_sizes_and_gaps(tmp_path):
    """Gaps are signal, not error: the game stops sending when you pause."""
    path = tmp_path / "a.fzr"
    with CaptureWriter(path) as writer:
        for i in range(30):
            writer.add(b"\x00" * 324, i * 16_666_667)
        # A two-second silence, as a pause or a rewind would produce.
        for i in range(30):
            writer.add(b"\x00" * 324, 2 * NS + i * 16_666_667)
        writer.add(b"\x00" * 300, 3 * NS)  # an odd size, to be reported

    summary = summarize(path, gap_threshold_s=0.5)
    assert summary.packets == 61
    assert summary.size_histogram == {324: 60, 300: 1}
    assert len(summary.gaps) == 2
    assert summary.gaps[0].duration_s == pytest.approx(1.52, abs=0.02)
    assert summary.rate_hz == pytest.approx(20.0, rel=0.05)
    assert summary.truncated is False


def test_default_capture_path_uses_a_timestamp(tmp_path):
    assert default_capture_path(tmp_path, "stage-one").name == "stage-one.fzr"
    assert default_capture_path(tmp_path, "stage-one.fzr").name == "stage-one.fzr"
    assert default_capture_path(tmp_path).name.endswith(".fzr")


# --------------------------------------------------------------------------
# replay scheduling
# --------------------------------------------------------------------------


def test_schedule_preserves_original_spacing():
    times = [0, 1 * NS, 2 * NS, 5 * NS]
    assert build_schedule(times) == times


def test_schedule_scales_with_speed():
    times = [0, 2 * NS, 4 * NS]
    assert build_schedule(times, speed=2.0) == [0, 1 * NS, 2 * NS]
    assert build_schedule(times, speed=0.5) == [0, 4 * NS, 8 * NS]


def test_schedule_clamps_only_long_gaps():
    """max_gap compresses a menu pause without touching the 60 Hz cadence."""
    times = [0, 16_666_667, 33_333_334, 30 * NS, 30 * NS + 16_666_667]
    clamped = build_schedule(times, max_gap_s=1.0)
    assert clamped[:3] == times[:3]
    assert clamped[3] == 33_333_334 + NS
    assert clamped[4] - clamped[3] == 16_666_667


def test_schedule_rejects_nonpositive_speed():
    with pytest.raises(ValueError, match="positive"):
        build_schedule([0, NS], speed=0.0)


# --------------------------------------------------------------------------
# replay over a real socket
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_two_listeners_cannot_share_a_port():
    """SO_REUSEADDR must stay off.

    On Windows it would let a second socket bind the same UDP port, with
    indeterminate delivery between the two, so a capture started while a
    listen is still running would silently record a fraction of the stream.
    A refused bind beats a half-recorded recon lap.
    """
    port = _free_port()
    first = UdpListener(host="127.0.0.1", port=port).open()
    try:
        with pytest.raises(OSError, match="cannot bind UDP"):
            UdpListener(host="127.0.0.1", port=port).open()
    finally:
        first.close()


def test_replay_delivers_every_packet_in_order():
    port = _free_port()
    records = _records(30)
    received: list[tuple[bytes, int]] = []

    listener = UdpListener(host="127.0.0.1", port=port, timeout_s=0.25).open()

    def pump() -> None:
        deadline = time.monotonic() + 5.0
        while len(received) < len(records) and time.monotonic() < deadline:
            got = listener.recv()
            if got is not None:
                received.append(got)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    time.sleep(0.05)  # let the listener reach recv before the first send

    stats = replay_records(records, host="127.0.0.1", port=port, speed=4.0)
    thread.join(timeout=5.0)
    listener.close()

    assert stats.sent == 30
    assert [payload for payload, _ in received] == [p for _, p in records]


def test_replay_holds_its_schedule():
    """If replay timing is sloppy, every tuning session measures the
    scheduler instead of the game. Assert it is not."""
    port = _free_port()
    records = _records(40, dt_ns=20_000_000)  # 50 Hz for 0.8 s

    stats = replay_records(records, host="127.0.0.1", port=port)

    assert stats.sent == 40
    assert stats.wall_s == pytest.approx(0.78, abs=0.15)
    # Generous enough not to flake on a loaded CI box, tight enough to catch
    # the 15.6 ms Windows timer granularity this code exists to avoid.
    assert stats.late_max_ms < 10.0


def test_replay_rejects_an_empty_capture():
    with pytest.raises(ValueError, match="no records"):
        replay_records([], host="127.0.0.1", port=_free_port())


# --------------------------------------------------------------------------
# synthetic captures
# --------------------------------------------------------------------------


def test_synth_produces_wellformed_packets():
    records = synth_records(SynthSpec(duration_s=5.0, pause_at_s=None, jump_at_s=None))
    assert len(records) == pytest.approx(300, abs=2)
    assert all(len(p) == 324 for _, p in records)
    assert [t for t, _ in records] == sorted(t for t, _ in records)


def test_synth_pause_appears_as_a_stream_gap(tmp_path):
    path = tmp_path / "s.fzr"
    write_synth(path, SynthSpec(duration_s=12.0, pause_at_s=6.0, pause_len_s=2.0))
    summary = summarize(path, gap_threshold_s=0.5)
    assert len(summary.gaps) == 1
    assert summary.gaps[0].duration_s == pytest.approx(2.0, abs=0.1)


def test_one_unloaded_wheel_is_not_a_jump():
    """A jump is all four wheels at max stretch, not any one of them.

    Cresting a kerb or dropping a wheel into a rut unloads one corner. If that
    scored as flight, the stage builder would call a jump at every pothole.
    """
    from codriver.adapters.fh6 import FH6Adapter, pack_fields

    adapter = FH6Adapter()

    one_wheel = adapter.parse(
        pack_fields({"NormalizedSuspensionTravel": [0.0, 0.5, 0.5, 0.5]})
    )
    assert one_wheel.airborne_score == pytest.approx(0.5)

    all_four = adapter.parse(
        pack_fields({"NormalizedSuspensionTravel": [0.0, 0.0, 0.0, 0.0]})
    )
    assert all_four.airborne_score == pytest.approx(1.0)

    # A car at rest sits at its static ride height and must not score high.
    at_rest = adapter.parse(
        pack_fields({"NormalizedSuspensionTravel": [0.48] * 4})
    )
    assert at_rest.airborne_score == pytest.approx(0.52)


def test_synth_jump_unloads_all_four_wheels():
    from codriver.adapters.fh6 import FH6Adapter

    adapter = FH6Adapter()
    records = synth_records(SynthSpec(duration_s=30.0, jump_at_s=25.0, jump_len_s=0.5))
    airborne = [
        f
        for f in (adapter.parse(p, t / 1e9) for t, p in records)
        if f.airborne_score > 0.9
    ]
    assert len(airborne) == pytest.approx(30, abs=3)
    assert all(24.9 < f.t < 25.6 for f in airborne)
