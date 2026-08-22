"""End-to-end: capture in, stage out, GPX out.

These run the real pipeline over a synthetic drive with a known shape, so the
assertions are about what the stage *should* say rather than about whatever
it happened to produce.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

import pytest

from codriver.config import Config, find_config_dir
from codriver.record.synth import SynthSpec, write_synth
from codriver.stage.build import build_stage
from codriver.stage.gpx import to_gpx
from codriver.stage.schema import load, save

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


@pytest.fixture
def cfg():
    return Config.load(find_config_dir())


@pytest.fixture
def slalom(tmp_path):
    path = tmp_path / "slalom.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="slalom",
            duration_s=70.0,
            speed_mps=70 / 3.6,
            size_m=60.0,
            pause_at_s=None,
            jump_at_s=None,
        ),
    )
    return path


def test_build_produces_a_usable_stage(slalom, cfg):
    stage, report = build_stage(slalom, cfg, name="slalom")

    assert stage.name == "slalom"
    assert stage.length_m > 400
    assert len(stage.line) > 100
    assert stage.notes, "a slalom must produce notes"
    # A slalom alternates. Consecutive corner notes should not all turn the
    # same way.
    directions = [n.direction for n in stage.notes if n.direction]
    assert len(set(directions)) == 2


def test_resampling_is_uniform_end_to_end(slalom, cfg):
    """The invariant the whole of the note algorithm rests on."""
    _, report = build_stage(slalom, cfg)
    assert report.spacing_max - report.spacing_min < 0.05
    assert report.spacing_mean == pytest.approx(cfg.get("stage.resample.spacing_m"), abs=0.05)


def test_orientation_is_confirmed_against_recorded_steering(slalom, cfg):
    """The note algorithm's 'Right if divisor > 0' is frame-dependent; the recon lap's
    own steering says which way is which."""
    _, report = build_stage(slalom, cfg)
    assert report.direction_samples > 50
    assert report.direction_agreement > 0.9, (
        "after auto-orientation, classified direction should match the wheel"
    )


def test_a_circle_is_all_one_direction(tmp_path, cfg):
    path = tmp_path / "circle.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=40.0,
            speed_mps=20.0,
            size_m=70.0,
            pause_at_s=None,
            jump_at_s=None,
        ),
    )
    stage, report = build_stage(path, cfg)
    directions = {n.direction for n in stage.notes if n.direction}
    assert len(directions) == 1, f"a circle turned both ways: {directions}"
    assert report.direction_agreement > 0.9


def test_a_jump_in_the_telemetry_becomes_a_jump_note(tmp_path, cfg):
    path = tmp_path / "jump.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=40.0,
            speed_mps=25.0,
            size_m=120.0,
            pause_at_s=None,
            jump_at_s=20.0,
            jump_len_s=0.5,
        ),
    )
    stage, _ = build_stage(path, cfg)
    assert any("jump" in n.tokens for n in stage.notes)


def test_the_stage_pauses_are_split_into_segments(tmp_path, cfg):
    path = tmp_path / "paused.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=60.0,
            speed_mps=25.0,
            size_m=100.0,
            pause_at_s=30.0,
            pause_len_s=3.0,
            jump_at_s=None,
        ),
    )
    _, report = build_stage(path, cfg)
    assert report.segments_found == 2, "a pause is a session boundary"

    first, _ = build_stage(path, cfg, segment_index=0)
    second, _ = build_stage(path, cfg, segment_index=1)
    assert first.length_m != second.length_m

    with pytest.raises(ValueError, match="does not exist"):
        build_stage(path, cfg, segment_index=9)


def test_build_refuses_a_capture_with_no_driving(tmp_path, cfg):
    path = tmp_path / "idle.fzr"
    write_synth(
        path,
        SynthSpec(duration_s=3.0, stationary_s=99.0, pause_at_s=None, jump_at_s=None),
    )
    with pytest.raises(ValueError, match="no continuous driving|not enough movement"):
        build_stage(path, cfg)


def test_the_stage_records_the_hash_of_its_recording(slalom, cfg, tmp_path):
    stage, _ = build_stage(slalom, cfg)
    assert len(stage.source["sha256"]) == 64
    assert stage.source["capture"] == "slalom.fzr"
    assert stage.config["stage"]["resample"]["spacing_m"] == cfg.get(
        "stage.resample.spacing_m"
    )


def test_config_changes_change_the_output(slalom, cfg):
    """The whole point of the config being hot-reloadable is that these values
    move the result. If they do not, tuning is theatre."""
    base, base_report = build_stage(slalom, cfg)

    cfg.data["stage"]["curvature"]["class_speed_bands_kmh"] = [10, 15, 20, 25, 30, 35]
    narrow, narrow_report = build_stage(slalom, cfg)

    # The count need not drop, an alternating slalom still calls one note
    # per direction change however severe each one is. What must change is
    # how severe they are called, and how much of the line is a straight.
    assert narrow_report.markings["S"] > base_report.markings["S"] * 2
    assert max(n.severity for n in narrow.notes if n.severity) > max(
        n.severity for n in base.notes if n.severity
    )

    # Widening the window smooths harder, which really does merge notes.
    cfg.data["stage"]["curvature"]["class_speed_bands_kmh"] = base.config["stage"][
        "curvature"
    ]["class_speed_bands_kmh"]
    cfg.data["stage"]["curvature"]["window_points"] = 30
    smoothed, _ = build_stage(slalom, cfg)
    assert len(smoothed.notes) < len(base.notes)


def test_stage_survives_a_save_load_round_trip(slalom, cfg, tmp_path):
    stage, _ = build_stage(slalom, cfg)
    back = load(save(stage, tmp_path / "s.json"))
    assert [n.text for n in back.notes] == [n.text for n in stage.notes]
    assert back.length_m == pytest.approx(stage.length_m, abs=0.01)
    assert len(back.line) == len(stage.line)


# --------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------


def test_gpx_is_wellformed_and_carries_the_notes(slalom, cfg):
    stage, _ = build_stage(slalom, cfg)
    root = ET.fromstring(to_gpx(stage))

    waypoints = root.findall("g:wpt", GPX_NS)
    assert len(waypoints) == len(stage.notes)
    assert {w.find("g:name", GPX_NS).text for w in waypoints} == {
        n.text for n in stage.notes
    }

    tracks = root.findall("g:trk", GPX_NS)
    assert len(tracks) > 1, "one track per corner class, so they colour separately"
    assert all(t.find("g:name", GPX_NS).text for t in tracks)


def test_gpx_keeps_the_shape_undistorted(slalom, cfg):
    """The projection is a debug fiction, but it has to preserve shape or
    there is no point looking at it."""
    stage, _ = build_stage(slalom, cfg)
    root = ET.fromstring(to_gpx(stage, by_class=False))
    pts = root.findall(".//g:trkpt", GPX_NS)

    def latlon(p):
        return (float(p.get("lat")), float(p.get("lon")))

    a, b, c = latlon(pts[0]), latlon(pts[len(pts) // 2]), latlon(pts[-1])
    p0, p1, p2 = stage.line[0], stage.line[len(stage.line) // 2], stage.line[-1]

    def ratio(u, v, pu, pv):
        deg = math.hypot(v[0] - u[0], v[1] - u[1])
        metres = math.hypot(pv.z - pu.z, pv.x - pu.x)
        return deg / metres if metres else 0.0

    # Same metres-per-degree on both axes means the ratio is constant
    # whichever pair of points you pick. The tolerance is set by the 7-decimal
    # lat/lon in the file, which is ~1 cm, plenty for looking at a stage.
    assert ratio(a, b, p0, p1) == pytest.approx(ratio(b, c, p1, p2), rel=1e-3)


def test_gpx_single_track_mode(slalom, cfg):
    stage, _ = build_stage(slalom, cfg)
    root = ET.fromstring(to_gpx(stage, by_class=True))
    single = ET.fromstring(to_gpx(stage, by_class=False))
    assert len(root.findall("g:trk", GPX_NS)) > 1
    assert len(single.findall("g:trk", GPX_NS)) == 1
    assert len(single.findall(".//g:trkpt", GPX_NS)) == len(stage.line)


def test_gpx_refuses_an_empty_stage():
    from codriver.stage.schema import Stage

    with pytest.raises(ValueError, match="no line"):
        to_gpx(Stage(name="empty"))


def test_water_in_the_telemetry_becomes_a_water_note(tmp_path, cfg):
    path = tmp_path / "ford.fzr"
    write_synth(
        path,
        SynthSpec(
            shape="circle",
            duration_s=40.0,
            speed_mps=25.0,
            size_m=120.0,
            pause_at_s=None,
            jump_at_s=None,
            water_at_s=20.0,
            water_len_s=0.6,
        ),
    )
    stage, _ = build_stage(path, cfg)
    water = [n for n in stage.notes if "water" in n.tokens]
    assert len(water) == 1, [n.text for n in stage.notes]
