"""Circuits, and the drive to the race.

What the first outside tester hit: a recording that started in free roam
built the stage from the drive *to* the race (the longer segment won); a
circuit race gave a stage of every lap on top of each other; and the
co-driver fell silent at the start/finish line, because the stage ended
there. Now: the event's segment, one lap of it, and the runtime goes round.
"""

from __future__ import annotations

import math
import struct
import time
from pathlib import Path

import pytest

from codriver.adapters.fh6 import pack_fields
from codriver.config import Config, find_config_dir
from codriver.record.capture import CaptureWriter, read_all
from codriver.record.synth import SynthSpec, synth_records
from codriver.runtime.locate import Locator, StageIndex
from codriver.runtime.run import CoDriver
from codriver.runtime.scheduler import Scheduler
from codriver.stage import line as line_mod
from codriver.stage.build import build_stage, frames_from_capture
from codriver.stage.line import LinePoint, cumulative_distance
from codriver.stage.notes import Note
from codriver.stage.schema import from_dict, load, save, to_dict

RADIUS = 90.0
SPEED = 20.0
LAP_M = 2 * math.pi * RADIUS
ORIGIN = (-4210.0, 6180.0)


@pytest.fixture
def cfg():
    cfg = Config.load(find_config_dir())
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"
    return cfg


def _laps(path: Path, laps: float = 3.0, race_position: int = 3) -> Path:
    """A circuit race: ``laps`` times round a circle, the lap counter going
    up at every crossing of the start point, as the game's does."""
    spec = SynthSpec(shape="circle", duration_s=laps * LAP_M / SPEED + 1.0, speed_mps=SPEED, size_m=RADIUS,
                     origin=ORIGIN, pause_at_s=None, jump_at_s=None, stationary_s=1.0)
    with CaptureWriter(path, header={"note": "laps"}) as w:
        for t_ns, data in synth_records(spec):
            raw = bytearray(data)
            dist = struct.unpack_from("<f", raw, 292)[0]
            struct.pack_into("<H", raw, 312, int(dist // LAP_M))
            raw[314] = race_position
            w.add(bytes(raw), t_ns)
    return path


def _free_roam(seconds: float, x0: float, rate: float = 60.0) -> list[tuple[int, bytes]]:
    """Driving along a straight in free roam: IsRaceOn set, no race position."""
    out = []
    for i in range(int(seconds * rate)):
        t = i / rate
        out.append((int(t * 1e9), pack_fields({
            "IsRaceOn": 1, "RacePosition": 0, "PositionX": x0 + i * SPEED / rate, "PositionZ": 100.0,
            "Speed": SPEED, "TimestampMS": int(t * 1000)})))
    return out


# --------------------------------------------------------------------------
# the build picks the race
# --------------------------------------------------------------------------


def test_the_race_wins_over_the_longer_drive_to_it(tmp_path, cfg):
    _, race = read_all(_laps(tmp_path / "race.fzr", laps=1.2))
    roam = _free_roam(40.0, x0=5000.0)  # longer than the race, somewhere else
    t_end = roam[-1][0] + int(0.1e9)  # no stream gap: the cut is the teleport and the event
    path = tmp_path / "evening.fzr"
    with CaptureWriter(path, header={}) as w:
        for t_ns, d in roam:
            w.add(d, t_ns)
        for t_ns, d in race:
            w.add(d, t_end + t_ns)

    segments = line_mod.split_segments(frames_from_capture(path), gap_s=0.5, min_points=60, jump_m=50.0)
    assert len(segments) == 2 and len(segments[0]) > len(segments[1])
    assert not line_mod.is_event(segments[0]) and line_mod.is_event(segments[1])
    assert line_mod.pick_segment(segments) == 1

    stage, report = build_stage(path, cfg, name="race")
    assert report.segments_found == 2 and report.segment_used == 1 and report.segment_event
    assert math.hypot(stage.line[0].x - (ORIGIN[0] + RADIUS), stage.line[0].z - ORIGIN[1]) < 30.0, \
        "the stage starts at the race, not in free roam"
    assert not stage.loop, "1.2 laps: the end is not the start"
    assert "in an event" in report.render()


def test_a_teleport_alone_splits_a_recording(tmp_path):
    a = _free_roam(5.0, x0=0.0)
    b = [(t + a[-1][0] + int(0.02e9), d) for t, d in _free_roam(5.0, x0=9000.0)]
    path = tmp_path / "jump.fzr"
    with CaptureWriter(path, header={}) as w:
        for t_ns, d in a + b:
            w.add(d, t_ns)
    frames = frames_from_capture(path)
    assert len(line_mod.split_segments(frames, jump_m=50.0)) == 2
    assert len(line_mod.split_segments(frames, jump_m=1e9)) == 1


# --------------------------------------------------------------------------
# one lap, that repeats
# --------------------------------------------------------------------------


def test_three_laps_become_one_lap_that_repeats(tmp_path, cfg):
    stage, report = build_stage(_laps(tmp_path / "laps.fzr", laps=3.0), cfg, name="ring")
    assert report.laps_seen >= 2 and report.lap_used == 1
    assert stage.loop is True and report.loop
    assert abs(stage.length_m - LAP_M) < 0.05 * LAP_M, "one lap, not three"
    assert line_mod.ground_distance(stage.line[0], stage.line[-1]) < 60.0
    assert stage.source["lap"] == 1 and stage.source["laps_in_capture"] >= 2
    assert "circuit" in report.render() and "line to line" in report.render()


def test_a_single_lap_still_loops_and_half_a_lap_does_not(tmp_path, cfg):
    ring, report = build_stage(_laps(tmp_path / "one.fzr", laps=0.98), cfg, name="one")
    assert report.lap_used == -1, "no second line crossing: the whole drive is the lap"
    assert ring.loop, "it ends where it started"
    half, _ = build_stage(_laps(tmp_path / "half.fzr", laps=0.5), cfg, name="half")
    assert not half.loop


def test_the_loop_flag_survives_the_file_and_old_files_have_none(tmp_path, cfg):
    ring, _ = build_stage(_laps(tmp_path / "laps.fzr"), cfg, name="ring")
    save(ring, tmp_path / "ring.json")
    assert load(tmp_path / "ring.json").loop is True
    d = to_dict(ring)
    del d["loop"]
    assert from_dict(d).loop is False


# --------------------------------------------------------------------------
# the runtime goes round
# --------------------------------------------------------------------------


def _ring_index(loop: bool, n: int = 200) -> StageIndex:
    pts = [LinePoint(x=RADIUS * math.cos(2 * math.pi * i / n), y=0.0, z=RADIUS * math.sin(2 * math.pi * i / n))
           for i in range(n)]
    return StageIndex(pts, cumulative_distance(pts), loop=loop)


def _drive_ring(locator: Locator, steps: int, n: int = 200) -> tuple[float, list[float]]:
    """Two laps round the ring at one point per step: (worst off-line
    distance reported, along_m per step)."""
    worst, alongs = 0.0, []
    for k in range(steps):
        a = 2 * math.pi * k / n
        fix = locator.update(RADIUS * math.cos(a), RADIUS * math.sin(a), k * 0.1)
        worst = max(worst, fix.off_line_m)
        alongs.append(fix.along_m)
    return worst, alongs


def test_tracking_carries_across_the_seam_on_a_circuit():
    index = _ring_index(loop=True)
    assert abs(index.length_m - LAP_M) < 1.0, "the seam segment counts towards the lap"
    worst, alongs = _drive_ring(Locator(index), steps=400)
    assert worst < 1.0, "two laps on the line, never off it"
    assert alongs[199] > LAP_M - 10.0 and alongs[200] < 10.0, "along wraps at the line"
    assert max(alongs) < index.length_m

    worst, alongs = _drive_ring(Locator(_ring_index(loop=False)), steps=400)
    assert worst > 20.0 and alongs[210] > LAP_M - 10.0, \
        "without the loop the tracker clings to the last point until it is lost and re-acquired"


def _flat(tokens) -> float:
    return 0.4 * len(tokens)


def test_the_scheduler_repeats_the_notes_every_lap():
    notes = [Note(at_m=100.0, tokens=["3", "right"], severity=3, kind="corner"),
             Note(at_m=300.0, tokens=["2", "left"], severity=2, kind="corner")]
    s = Scheduler(notes=notes, duration_fn=_flat, loop_m=500.0)
    s.speed_curve_kmh, s.speed_curve_mult = (0, 200), (1.0, 1.0)
    s.relocate(0.0)
    speed, dt, t = 20.0, 1 / 30, 0.0
    spoken: list[tuple[float, float]] = []
    while t * speed < 1500.0:  # three laps
        for e in s.tick((t * speed) % 500.0, speed, t):
            spoken.append((e.note.at_m, t * speed))
        t += dt
    assert [at for at, _ in spoken] == [100.0, 300.0] * 3
    assert s.dropped == 0
    for (at, where), lap in zip(spoken, [0, 0, 1, 1, 2, 2]):
        assert where < at + lap * 500.0, "fired ahead of the corner on every lap"

    # what the overlay sees near the end of a lap: the next lap's corners
    s.tick(450.0, speed, t)
    assert [n.at_m for n in s.upcoming(2)] == [100.0, 300.0]
    assert s.distance_to(100.0) == pytest.approx(150.0)
    assert s.next_note is not None and s.next_note.at_m == 100.0

    plain = Scheduler(notes=notes, duration_fn=_flat)
    plain.relocate(450.0)
    assert plain.upcoming(2) == [] and plain.next_note is None


def test_relocating_past_the_last_note_points_at_the_next_lap():
    s = Scheduler(notes=[Note(at_m=100.0, tokens=["3", "right"], severity=3, kind="corner")],
                  duration_fn=_flat, loop_m=500.0)
    s.relocate(200.0)
    assert s.next_note is not None
    assert s.distance_to(100.0) == pytest.approx(400.0)


def test_the_codriver_calls_every_lap(tmp_path, cfg):
    rec = _laps(tmp_path / "laps.fzr", laps=3.0)
    stage, _ = build_stage(rec, cfg, name="ring")
    assert stage.loop and stage.notes
    events: list[dict] = []
    co = CoDriver(stage, cfg, silent=True, hud=False, record_dir=None, on_event=events.append)
    _, records = read_all(rec)
    t0 = time.monotonic()
    for t_ns, data in records:
        co.on_datagram(data, t_ns, t0 + t_ns / 1e9)
    stats = co.finish()
    assert stats.reacquires == 1 and stats.rewinds == 0, "the start line is not a re-localisation"
    assert stats.fixes == stats.frames, "never lost across three laps"
    notes = [e for e in events if e["kind"] == "note"]
    assert len(notes) >= 2 * len(stage.notes), "called again on the later laps"
    status = [e for e in events if e["kind"] == "status" and e.get("next_in_m") is not None]
    assert status and all(0.0 <= e["next_in_m"] <= stage.length_m + 5.0 for e in status)
    assert all(0.0 <= n["in_m"] <= stage.length_m + 5.0 for e in status for n in e["upcoming"])
    waiting = next(e for e in events if e["kind"] == "waiting")
    assert waiting["loop"] is True
