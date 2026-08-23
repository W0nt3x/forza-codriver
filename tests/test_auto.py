"""Evening mode: a CoDriver fed from outside, a matcher that knows which
stage starts where, and the whole thing over UDP: one evening, two races,
one of them a stage we have."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

from codriver.adapters.fh6 import FH6Adapter, pack_fields
from codriver.config import Config, find_config_dir
from codriver.record.capture import CaptureWriter, read_all
from codriver.record.synth import SynthSpec, synth_records, write_synth
from codriver.runtime.auto import StageMatcher, load_stages, session_auto
from codriver.runtime.run import CoDriver
from codriver.stage.build import build_stage
from codriver.stage.schema import save


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.load(find_config_dir())
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"
    return cfg


def _circle(path: Path, origin=(-4210.0, 6180.0), duration=24.0, race_position=1):
    """A synthetic circle drive, positioned anywhere, as a capture file."""
    spec = SynthSpec(shape="circle", duration_s=duration, speed_mps=20.0, size_m=90.0,
                     pause_at_s=None, jump_at_s=None, origin=origin, stationary_s=1.0)
    write_synth(path, spec)
    return path


# --------------------------------------------------------------------------
# CoDriver, fed directly
# --------------------------------------------------------------------------


def test_codriver_can_be_fed_without_a_socket(tmp_path):
    cfg = _cfg(tmp_path)
    recon = _circle(tmp_path / "recon.fzr")
    stage, _ = build_stage(recon, cfg, name="circle")
    events: list[dict] = []
    co = CoDriver(stage, cfg, silent=True, hud=False, record_dir=tmp_path / "runs", on_event=events.append)
    assert events[0]["kind"] == "waiting" and events[0]["stage"] == "circle"
    _, records = read_all(recon)
    t0 = time.monotonic()
    for t_ns, data in records:
        co.on_datagram(data, t_ns, t0 + t_ns / 1e9)
    stats = co.finish()
    kinds = [e["kind"] for e in events]
    assert "localised" in kinds and "status" in kinds and kinds[-1] == "done"
    assert stats.frames == len(records) and stats.fixes > 0
    assert stats.spoken >= 1, "a circle has corners to call"
    assert stats.recorded_to is not None and stats.recorded_to.parent == tmp_path / "runs"
    assert stats.recorded_packets == len(records)
    assert co.finish() is stats, "finishing twice is harmless"


# --------------------------------------------------------------------------
# which stage starts here?
# --------------------------------------------------------------------------


def test_matcher_picks_the_stage_that_starts_where_the_car_is(tmp_path):
    cfg = _cfg(tmp_path)
    a, _ = build_stage(_circle(tmp_path / "a.fzr", origin=(0.0, 0.0)), cfg, name="alpha")
    b, _ = build_stage(_circle(tmp_path / "b.fzr", origin=(5000.0, 0.0)), cfg, name="bravo")
    m = StageMatcher([a, b], radius_m=40.0, head_m=120.0)
    ax, az = a.line[0].x, a.line[0].z
    hit = m.match(ax + 5.0, az - 3.0)
    assert hit is not None and hit[0].name == "alpha" and hit[1] < 10.0
    bx, bz = b.line[0].x, b.line[0].z
    assert m.match(bx, bz)[0].name == "bravo"
    assert m.match(2500.0, 0.0) is None, "nowhere near either start"
    assert m.match(ax + 200.0, az) is None, "far past the first metres of alpha"
    assert StageMatcher([], 40.0).match(0.0, 0.0) is None


def test_load_stages_skips_broken_files(tmp_path):
    cfg = _cfg(tmp_path)
    st, _ = build_stage(_circle(tmp_path / "a.fzr"), cfg, name="alpha")
    stages = tmp_path / "stages"
    stages.mkdir()
    save(st, stages / "alpha.json")
    (stages / "broken.json").write_text("{not json", encoding="utf-8")
    (stages / "other.json").write_text('{"format": "gpx"}', encoding="utf-8")
    assert [s.name for s in load_stages(stages)] == ["alpha"]


# --------------------------------------------------------------------------
# the evening, over UDP
# --------------------------------------------------------------------------


def _evening(path: Path, known_origin, unknown_origin) -> None:
    """Free roam far away, a race on the known stage, a menu, a race somewhere
    new, a menu. Races carry a race position; free roam and menus do not."""
    ad_rate = 30.0
    t = 0.0
    dt = 1.0 / ad_rate

    def other(race_on, seconds, x):
        nonlocal t
        out = []
        for _ in range(int(seconds * ad_rate)):
            out.append((int(t * 1e9), pack_fields({"IsRaceOn": race_on, "RacePosition": 0, "PositionX": x,
                                                   "PositionZ": 9000.0, "Speed": 20.0 if race_on else 0.0,
                                                   "TimestampMS": int(t * 1000)})))
            t += dt
        return out

    def race(origin, position, duration):
        nonlocal t
        out = []
        for rel_ns, data in synth_records(SynthSpec(shape="circle", duration_s=duration, speed_mps=20.0, size_m=90.0,
                                                      pause_at_s=None, jump_at_s=None, origin=origin, stationary_s=1.0)):
            # synth frames carry RacePosition 1 already; set the requested one
            raw = bytearray(data)
            raw[314] = position
            out.append((int(t * 1e9) + rel_ns, bytes(raw)))
        t += duration
        return out

    records = other(1, 2.0, 0.0) + race(known_origin, 4, 16.0) + other(0, 3.0, 0.0) + race(unknown_origin, 2, 12.0) + other(0, 1.0, 0.0)
    with CaptureWriter(path, header={"note": "evening"}) as w:
        for t_ns, data in records:
            w.add(data, t_ns)


def test_an_evening_calls_the_known_stage_and_records_the_unknown_race(tmp_path):
    from codriver.record.replay import replay_file

    cfg = _cfg(tmp_path)
    known_origin, unknown_origin = (-4210.0, 6180.0), (3000.0, -2000.0)
    stage, _ = build_stage(_circle(tmp_path / "recon.fzr", origin=known_origin), cfg, name="known-circle")
    stages_dir = tmp_path / "stages"
    stages_dir.mkdir()
    save(stage, stages_dir / "known-circle.json")
    evening = tmp_path / "evening.fzr"
    _evening(evening, known_origin, unknown_origin)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg.data["telemetry"]["port"] = port
    # replay at 4x, detector clocks are wall time: scaled windows
    cfg.data["capture"]["auto"] = {"start_frames": 15, "end_s": 0.5, "gap_s": 1.0, "preroll_s": 0.5, "min_seconds": 1.0}
    cfg.data.setdefault("runtime", {})["auto"] = {"match_radius_m": 40.0, "match_head_m": 120.0, "match_window_s": 4.0}

    recordings, runs = tmp_path / "recordings", tmp_path / "recordings" / "runs"
    events: list[dict] = []
    stop = threading.Event()
    result: dict = {}
    runner = threading.Thread(
        target=lambda: result.update(r=session_auto(
            cfg, load_stages(stages_dir), recordings, runs, silent=True, hud=False,
            on_event=events.append, should_stop=stop.is_set)),
        daemon=True)
    runner.start()
    time.sleep(0.3)
    replay_file(evening, host="127.0.0.1", port=port, speed=4.0)
    time.sleep(2.0)
    stop.set()
    runner.join(timeout=15.0)
    assert not runner.is_alive()

    r = result["r"]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "auto_started" and kinds[-1] == "auto_done"
    assert kinds.count("race_started") == 2, kinds
    assert kinds.count("auto_matched") == 1 and kinds.count("auto_unmatched") == 1
    matched = next(e for e in events if e["kind"] == "auto_matched")
    assert matched["stage"] == "known-circle" and matched["distance_m"] < 40.0
    assert any(e["kind"] == "note" for e in events), "the known stage was called"
    assert kinds.count("done") == 1, "one CoDriver ran and finished"
    saved = [e for e in events if e["kind"] == "race_saved"]
    assert len(saved) == 2 and r.discarded == 0
    assert saved[0]["stage"] == "known-circle" and Path(saved[0]["path"]).parent == runs
    assert Path(saved[0]["path"]).name.startswith("known-circle_"), "a run of the stage: Learn finds it"
    assert saved[1]["stage"] is None and Path(saved[1]["path"]).parent == recordings
    assert Path(saved[1]["path"]).name.startswith("race-"), "an unknown race: build it later"
    ad = FH6Adapter()
    for e, pos in zip(saved, (4, 2)):
        _, records = read_all(Path(e["path"]))
        frames = [ad.parse(d, 0.0) for _, d in records]
        assert sum(1 for f in frames if f.race_position == pos) > 0.9 * 30 * (16.0 if pos == 4 else 12.0)
