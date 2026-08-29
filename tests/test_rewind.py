"""Rewinds. The game keeps sending while you rewind, but blank packets
(IsRaceOn 0), and resumes a few seconds back up the road. Two things must
survive that: a recording built into a stage, and a co-driver mid-stage."""

from __future__ import annotations

import time

import pytest

from codriver.adapters.fh6 import pack_fields
from codriver.config import Config, find_config_dir
from codriver.record.capture import CaptureWriter
from codriver.record.synth import SynthSpec, synth_records
from codriver.runtime.run import CoDriver
from codriver.stage.build import build_stage
from codriver.stage.line import LinePoint
from codriver.stage.notes import Note
from codriver.stage.schema import Stage

RATE = 60


@pytest.fixture
def cfg():
    cfg = Config.load(find_config_dir())
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"
    return cfg


def _blank(t_s: float) -> bytes:
    return pack_fields({"IsRaceOn": 0, "TimestampMS": int(t_s * 1000)})


def _write(path, records) -> None:
    with CaptureWriter(path, header={}) as w:
        for t_ns, d in records:
            w.add(d, t_ns)


def _rewound(records, cut_s: float, resume_s: float, blank_s: float = 4.0):
    """The drive up to ``cut_s``, ``blank_s`` of blank packets, then the
    drive again from ``resume_s`` on, with the capture clock running on."""
    a = records[: int(cut_s * RATE)]
    b = records[int(resume_s * RATE):]
    t = a[-1][0]
    out = list(a)
    for i in range(1, int(blank_s * RATE)):
        out.append((t + int(i * 1e9 / RATE), _blank((t + i * 1e9 / RATE) / 1e9)))
    shift = t + int(blank_s * 1e9) - b[0][0]
    out += [(t_ns + shift, d) for t_ns, d in b]
    return out


def test_a_rewound_recording_builds_the_whole_road_once(tmp_path, cfg):
    spec = SynthSpec(shape="slalom", duration_s=60.0, speed_mps=18.0, size_m=60.0,
                     pause_at_s=None, jump_at_s=None, stationary_s=1.0)
    records = synth_records(spec)
    _write(tmp_path / "whole.fzr", records)
    whole, _ = build_stage(tmp_path / "whole.fzr", cfg, name="whole")

    # a ten-second rewind at 40 s
    _write(tmp_path / "rewound.fzr", _rewound(records, cut_s=40.0, resume_s=30.0))
    stage, report = build_stage(tmp_path / "rewound.fzr", cfg, name="rewound")
    assert report.segments_found == 2 and report.rewinds_spliced == 1
    assert stage.length_m == pytest.approx(whole.length_m, rel=0.02), "the road once, not the longest piece"
    assert len(stage.notes) == len(whole.notes)
    assert "spliced" in report.render()

    # a restart at 40 s: back to the line, the second attempt replaces the first
    _write(tmp_path / "restart.fzr", _rewound(records, cut_s=40.0, resume_s=0.0))
    stage, report = build_stage(tmp_path / "restart.fzr", cfg, name="restart")
    assert report.rewinds_spliced == 1
    assert stage.length_m == pytest.approx(whole.length_m, rel=0.02)


def _straight(length_m: float = 600.0, note_at_m: float = 300.0) -> Stage:
    pts = [LinePoint(x=float(i * 3), y=0.0, z=0.0) for i in range(int(length_m / 3) + 1)]
    return Stage(name="straight", line=pts, length_m=length_m,
                 notes=[Note(at_m=note_at_m, tokens=["3", "right"], severity=3, kind="corner")])


def _racing(x: float, t_s: float, speed: float) -> bytes:
    return pack_fields({"IsRaceOn": 1, "RacePosition": 1, "PositionX": x, "PositionZ": 0.0,
                        "Speed": speed, "TimestampMS": int(t_s * 1000), "CurrentRaceTime": t_s})


def test_a_short_rewind_re_arms_the_notes(cfg):
    """A rewind shorter than the position-jump threshold keeps the tracker
    locked; it must still notice the car went backwards and say the corner
    again on the second approach."""
    events: list[dict] = []
    co = CoDriver(_straight(), cfg, silent=True, hud=False, record_dir=None, on_event=events.append)
    t0 = time.monotonic()
    speed, dt = 20.0, 1.0 / RATE

    def feed(data: bytes, t: float) -> None:
        co.on_datagram(data, int(t * 1e9), t0 + t)

    t = 0.0
    while speed * t < 280.0:  # the 300 m corner has been called by here
        feed(_racing(speed * t, t, speed), t)
        t += dt
    assert [e["at_m"] for e in events if e["kind"] == "note"] == [300.0]

    for _ in range(3 * RATE):  # the rewind: three seconds of blank packets
        feed(_blank(t), t)
        t += dt
    assert any(e["kind"] == "suspended" for e in events), "blank packets are a suspension"

    x = 250.0  # 30 m back: no jump, the windowed search still has it
    while x < 400.0:
        feed(_racing(x, t, speed), t)
        x += speed * dt
        t += dt
    stats = co.finish()
    assert stats.rewinds == 1 and stats.suspends == 1
    assert any(e["kind"] == "rewind" and e["metres"] == 30 for e in events)
    assert [e["at_m"] for e in events if e["kind"] == "note"] == [300.0, 300.0], "called again"
