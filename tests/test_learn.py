"""Merging flickering corners, "long", and learning from recorded runs."""

from __future__ import annotations

import math

import pytest

from codriver.config import Config, find_config_dir
from codriver.record.synth import SynthSpec, write_synth
from codriver.stage.build import build_stage
from codriver.stage.curvature import STRAIGHT, Direction, Marking
from codriver.stage.learn import learn_stage, runs_for_stage
from codriver.stage.line import LinePoint
from codriver.stage.notes import Candidate, generate, merge_same_label, reduce_candidates
from codriver.stage.schema import load, save


def R(sev):
    return Marking(Direction.RIGHT, sev, 50.0)


def _corners(markings, **kw):
    line = [LinePoint(x=0.0, y=0.0, z=i * 3.0, susp_max=0.5) for i in range(len(markings))]
    cumulative = [i * 3.0 for i in range(len(markings))]
    return generate(line, markings, cumulative, hazards=False,
                    distance_call_min_m=1e9, **kw)


# --------------------------------------------------------------------------
# step 4b: one corner, one note
# --------------------------------------------------------------------------


def test_a_flickering_long_corner_is_one_note_not_two():
    """Seen live: a long right-hander classified R4, R5, R4 lost its R5 to
    step 3 and was called as two '4 right' notes 100 m apart, in quick
    succession. It is one corner."""
    markings = [STRAIGHT] * 5 + [R(4)] * 15 + [R(5)] * 10 + [R(4)] * 15 + [STRAIGHT] * 5
    out = reduce_candidates(markings, collapse_window_points=20)
    assert [(c.index, c.marking.label) for c in out] == [(5, "R4")]
    assert out[0].end_index == 45, "the survivor spans the whole corner"


def test_merge_keeps_the_earliest_index_and_apex():
    cands = [Candidate(10, R(3), apex_index=14), Candidate(30, R(3), apex_index=33)]
    merged = merge_same_label(cands)
    assert len(merged) == 1
    assert merged[0].index == 10
    assert merged[0].apex_index == 14


def test_a_corner_that_runs_long_is_called_long():
    short = [STRAIGHT] * 5 + [R(4)] * 10 + [STRAIGHT] * 5   # 30 m
    long_ = [STRAIGHT] * 5 + [R(4)] * 50 + [STRAIGHT] * 5   # 150 m
    assert _corners(short, long_min_m=120.0)[0].tokens == ["4", "right"]
    note = _corners(long_, long_min_m=120.0)[0]
    assert note.tokens == ["4", "right", "long"]
    assert note.length_m == pytest.approx(150.0, abs=3.0)


def test_tightens_is_not_also_long():
    """'right tightens 3 long' would be two modifiers fighting; the tightens
    call already says the corner develops."""
    markings = [STRAIGHT] * 5 + [R(5)] * 20 + [R(3)] * 30 + [STRAIGHT] * 5
    note = _corners(markings, long_min_m=60.0, tightens_min_run_points=12)[0]
    assert note.tokens == ["right", "tightens", "3"]


def test_length_and_observed_speed_survive_the_stage_file(tmp_path):
    from codriver.stage.notes import Note
    from codriver.stage.schema import Stage

    stage = Stage(
        name="t",
        line=[LinePoint(x=float(i), y=0.0, z=0.0) for i in range(5)],
        notes=[Note(at_m=0.0, tokens=["4", "right", "long"], length_m=150.0,
                    observed_kmh=97.0, severity=4, direction="right")],
    )
    back = load(save(stage, tmp_path / "s.json"))
    assert back.notes[0].length_m == 150.0
    assert back.notes[0].observed_kmh == 97.0


# --------------------------------------------------------------------------
# learning from runs
# --------------------------------------------------------------------------


@pytest.fixture
def cfg():
    return Config.load(find_config_dir())


def _circle(path, radius, speed, origin=(-4210.0, 6180.0)):
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=40.0,
            speed_mps=speed,
            size_m=radius,
            origin=origin,
            pause_at_s=None,
            jump_at_s=None,
            stationary_s=0.0,
        ),
    )
    return path


def _mean_radius(line, origin):
    return sum(math.hypot(p.x - origin[0], p.z - origin[1]) for p in line) / len(line)


def test_learn_moves_the_line_toward_where_the_car_actually_went(tmp_path, cfg):
    """Recon at r=80 m, one later run at r=84 m on the same circle: the learned
    line must sit between them (median of recon + run), and the run's speed
    must show up as observed speed on the notes."""
    origin = (-4210.0, 6180.0)
    recon = _circle(tmp_path / "recon.fzr", 80.0, 20.0, origin)
    stage, _ = build_stage(recon, cfg, name="circle")
    assert _mean_radius(stage.line, origin) == pytest.approx(80.0, abs=0.5)

    run = _circle(tmp_path / "circle_run1.fzr", 84.0, 26.0, origin)
    learned, report = learn_stage(stage, cfg, [run])

    assert report.runs_used == ["circle_run1.fzr"]
    assert report.runs_skipped == []
    assert report.samples > 500
    r = _mean_radius(learned.line, origin)
    assert 81.0 < r < 84.0, f"learned radius {r:.1f} should lie between recon and run"
    assert learned.generator["learned_from_runs"] == ["circle_run1.fzr"]


def test_learn_skips_a_run_of_a_different_stage(tmp_path, cfg):
    recon = _circle(tmp_path / "recon.fzr", 80.0, 20.0)
    stage, _ = build_stage(recon, cfg, name="circle")
    elsewhere = _circle(tmp_path / "circle_other.fzr", 80.0, 20.0, origin=(5000.0, -3000.0))
    learned, report = learn_stage(stage, cfg, [elsewhere])
    assert report.runs_used == []
    assert len(report.runs_skipped) == 1
    assert _mean_radius(learned.line, (-4210.0, 6180.0)) == pytest.approx(80.0, abs=0.5)


def test_learn_records_observed_speed_on_corner_notes(tmp_path, cfg):
    recon = _circle(tmp_path / "recon.fzr", 60.0, 15.0)
    stage, _ = build_stage(recon, cfg, name="circle")
    run = _circle(tmp_path / "circle_run1.fzr", 60.0, 25.0)
    learned, report = learn_stage(stage, cfg, [run])
    corners = [n for n in learned.notes if n.kind == "corner"]
    assert corners and report.notes_with_speed == len(corners)
    # median of recon (15 m/s) and run (25 m/s) samples per point is in
    # between; the slowest point of the corner is reported.
    for n in corners:
        assert 50 < n.observed_kmh < 95


def test_runs_are_found_by_stage_name(tmp_path):
    from codriver.stage.schema import Stage

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "alpha_20260822_1.fzr").write_bytes(b"")
    (runs / "alpha_20260822_2.fzr").write_bytes(b"")
    (runs / "beta_20260822_1.fzr").write_bytes(b"")
    found = runs_for_stage(Stage(name="alpha"), runs)
    assert [p.name for p in found] == ["alpha_20260822_1.fzr", "alpha_20260822_2.fzr"]
    assert runs_for_stage(Stage(name="gamma"), runs) == []
    assert runs_for_stage(Stage(name="alpha"), tmp_path / "missing") == []


def test_run_loop_records_what_it_hears(tmp_path, cfg):
    """Every drive is training data: the run loop saves a capture that
    `learn` can read back."""
    import socket
    import threading
    import time

    from codriver.record.capture import read_all
    from codriver.record.replay import replay_file
    from codriver.runtime.run import run_stage

    recon = _circle(tmp_path / "recon.fzr", 80.0, 20.0)
    stage, _ = build_stage(recon, cfg, name="circle")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg.data["telemetry"]["port"] = port
    cfg.data["telemetry"]["bind_host"] = "127.0.0.1"

    record_dir = tmp_path / "runs"
    result = {}
    runner = threading.Thread(
        target=lambda: result.update(
            stats=run_stage(stage, cfg, silent=True, hud=False, max_frames=300,
                            record_dir=record_dir)
        ),
        daemon=True,
    )
    runner.start()
    time.sleep(0.3)
    threading.Thread(
        target=lambda: replay_file(recon, host="127.0.0.1", port=port, speed=4.0),
        daemon=True,
    ).start()
    runner.join(timeout=30.0)

    stats = result["stats"]
    assert stats.recorded_to is not None
    assert stats.recorded_to.parent == record_dir
    assert stats.recorded_to.name.startswith("circle_")
    _, records = read_all(stats.recorded_to)
    assert len(records) == stats.recorded_packets >= 300
    assert runs_for_stage(stage, record_dir) == [stats.recorded_to]


def test_learn_keeps_the_jumps_and_water_the_geometry_cannot_see(tmp_path, cfg):
    """Learn rebuilds the stage from its own saved line. The first stage
    format dropped the per-point suspension and water telemetry, so the jump
    a recon lap had recorded vanished the first time Learn ran."""
    path = tmp_path / "hazards.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=40.0,
            speed_mps=25.0,
            size_m=120.0,
            pause_at_s=None,
            jump_at_s=15.0,
            jump_len_s=0.5,
            water_at_s=25.0,
            water_len_s=0.6,
        ),
    )

    def kinds(stage):
        return {n.kind for n in stage.notes} | {
            p["kind"] for n in stage.notes for p in n.parts
        }

    stage, _ = build_stage(path, cfg)
    assert {"jump", "water"} <= kinds(stage)
    save(stage, tmp_path / "h.json")
    learned, _ = learn_stage(load(tmp_path / "h.json"), cfg, [])
    assert {"jump", "water"} <= kinds(learned)


def test_run_recordings_and_learn_agree_on_a_safe_file_name(tmp_path):
    """A hand-edited or downloaded stage can carry any name. The recorder
    reduces it to a safe stem, and Learn looks for runs under the same stem,
    so the two never disagree and no name can point outside the runs folder."""
    from codriver.stage.schema import Stage, safe_stem

    assert safe_stem("stage2") == "stage2"
    assert safe_stem("coast-road-sprint") == "coast-road-sprint"
    assert safe_stem("../../evil") == "evil"
    assert safe_stem("C:\\Users\\x") == "C-Users-x"
    assert safe_stem("Coast Road Sprint") == "Coast-Road-Sprint"
    assert safe_stem("...") == "stage"

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "evil_20260101_000000.fzr").write_bytes(b"")
    st = Stage(name="../../evil")
    assert [p.name for p in runs_for_stage(st, runs)] == ["evil_20260101_000000.fzr"]

