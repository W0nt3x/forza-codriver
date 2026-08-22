"""The note algorithm steps 2-6: the note reduction, and the stage file itself.

Each reduction step gets a test built from hand-written markings, so a step
that stops firing is caught by name rather than by the note count quietly
drifting.
"""

from __future__ import annotations

import json

import pytest

from codriver.stage.curvature import STRAIGHT, Direction, Marking
from codriver.stage.line import LinePoint
from codriver.stage.notes import (
    Candidate,
    Note,
    add_distance_calls,
    candidates,
    collapse_ascending,
    detect_crests_and_dips,
    detect_jumps,
    distance_token,
    drop_straights,
    filter_descending,
    link_notes,
    reduce_candidates,
    required_tokens,
)
from codriver.stage.schema import (
    Stage,
    StageError,
    from_dict,
    load,
    render_notes,
    save,
    to_dict,
)

BUCKETS = (30, 50, 70, 100, 150, 200, 250, 300, 400, 500)


def R(sev):
    return Marking(Direction.RIGHT, sev, 50.0)


def L(sev):
    return Marking(Direction.LEFT, sev, 50.0)


def corner(at_m, tokens, index=0, **kw):
    return Note(at_m=at_m, tokens=list(tokens), index=index, **kw)


# --------------------------------------------------------------------------
# step 2
# --------------------------------------------------------------------------


def test_candidates_appear_only_where_the_classification_changes():
    markings = [R(4)] * 5 + [R(3)] * 5 + [R(3)] * 3 + [STRAIGHT] * 4
    out = candidates(markings)
    assert [(c.index, c.marking.label) for c in out] == [
        (0, "R4"),
        (5, "R3"),
        (13, "S"),
    ]


# --------------------------------------------------------------------------
# step 3
# --------------------------------------------------------------------------


def test_descending_severity_in_the_same_direction_is_dropped():
    """Coming out of a corner you have already slowed and can see it opening."""
    cands = [Candidate(0, R(2)), Candidate(10, R(4))]
    assert [c.marking.label for c in filter_descending(cands)] == ["R2"]


def test_descending_severity_in_the_other_direction_is_kept():
    cands = [Candidate(0, R(2)), Candidate(10, L(4))]
    assert len(filter_descending(cands)) == 2


def test_a_straight_after_a_corner_survives_step_3():
    """Straight is not 'the same direction' as a right, so it is not dropped
    here, step 5 is what removes it."""
    cands = [Candidate(0, R(2)), Candidate(10, STRAIGHT)]
    assert len(filter_descending(cands)) == 2


# --------------------------------------------------------------------------
# step 4
# --------------------------------------------------------------------------


def test_a_tightening_corner_is_called_at_its_entry():
    """When a corner starts turning, what matters is how tight it ends up."""
    cands = [Candidate(0, R(4)), Candidate(10, R(2))]
    out = collapse_ascending(cands, collapse_window_points=20)
    assert len(out) == 1
    assert out[0].index == 0, "the call must move to the entry, not stay late"
    assert out[0].marking.label == "R2", "and carry the severity it ends up at"


def test_a_distant_tightening_corner_stays_a_separate_call():
    """An R2 after a long R4 is genuinely worth its own note."""
    cands = [Candidate(0, R(4)), Candidate(80, R(2))]
    assert len(collapse_ascending(cands, collapse_window_points=20)) == 2


def test_collapse_does_not_cross_directions():
    cands = [Candidate(0, R(4)), Candidate(5, L(2))]
    assert len(collapse_ascending(cands, collapse_window_points=20)) == 2


def test_collapse_chains_through_a_progressively_tightening_corner():
    cands = [Candidate(0, R(5)), Candidate(6, R(4)), Candidate(12, R(2))]
    out = collapse_ascending(cands, collapse_window_points=20)
    assert len(out) == 1
    assert (out[0].index, out[0].marking.label) == (0, "R2")


# --------------------------------------------------------------------------
# step 5
# --------------------------------------------------------------------------


def test_straights_are_dropped_last():
    cands = [Candidate(0, R(3)), Candidate(5, STRAIGHT), Candidate(9, L(4))]
    assert [c.marking.label for c in drop_straights(cands)] == ["R3", "L4"]


def test_the_full_reduction_runs_in_order():
    markings = [STRAIGHT] * 5 + [R(4)] * 5 + [R(2)] * 5 + [R(5)] * 10 + [STRAIGHT] * 5
    out = reduce_candidates(markings, collapse_window_points=20)
    # R4 tightens to R2 (step 4 folds it back to the entry), then R5 opens out
    # again and is dropped by step 3. One note.
    assert [(c.index, c.marking.label) for c in out] == [(5, "R2")]


# --------------------------------------------------------------------------
# step 6
# --------------------------------------------------------------------------


def test_distance_rounds_down_to_a_callable_bucket():
    """Hearing 'one hundred' and finding 140 m is a pleasant surprise. The
    other way round is not."""
    assert distance_token(140.0, BUCKETS) == "100"
    assert distance_token(100.0, BUCKETS) == "100"
    assert distance_token(99.0, BUCKETS) == "70"
    assert distance_token(9000.0, BUCKETS) == "500"
    assert distance_token(5.0, BUCKETS) == "30"


def test_close_notes_link_with_into():
    notes = [corner(0.0, ["3", "right"]), corner(15.0, ["2", "left"])]
    out = link_notes(notes, link_into_max_m=20.0, link_and_max_m=50.0)
    assert len(out) == 1
    assert out[0].text == "3 right into 2 left"
    assert out[0].at_m == 0.0, "a phrase is spoken before its FIRST corner"


def test_moderately_spaced_notes_link_with_and():
    notes = [corner(0.0, ["3", "right"]), corner(40.0, ["2", "left"])]
    out = link_notes(notes, link_into_max_m=20.0, link_and_max_m=50.0)
    assert out[0].text == "3 right and 2 left"


def test_distant_notes_do_not_link():
    notes = [corner(0.0, ["3", "right"]), corner(200.0, ["2", "left"])]
    assert len(link_notes(notes, 20.0, 50.0)) == 2


def test_linking_stops_at_the_configured_ceiling():
    """A slalom would otherwise chain indefinitely, and a twelve-corner phrase
    is no longer a pace note."""
    notes = [corner(i * 10.0, [str(i + 1), "left"], index=i) for i in range(8)]
    out = link_notes(notes, link_into_max_m=20.0, link_and_max_m=50.0, max_linked_notes=3)
    assert all(len(n.parts) <= 3 for n in out)
    assert len(out) == 3


def test_linked_phrase_keeps_its_parts_for_hand_editing():
    notes = [corner(0.0, ["3", "right"]), corner(15.0, ["2", "left"])]
    merged = link_notes(notes, 20.0, 50.0)[0]
    assert [p["tokens"] for p in merged.parts] == [["3", "right"], ["2", "left"]]


def test_distance_is_called_only_across_a_real_gap():
    notes = [corner(0.0, ["3", "right"]), corner(220.0, ["2", "left"])]
    out = add_distance_calls(notes, distance_call_min_m=60.0, distance_buckets_m=BUCKETS)
    assert out[0].tokens == ["3", "right"]
    assert out[1].tokens == ["200", "2", "left"], "distance leads the phrase"


def test_no_distance_call_when_notes_are_close():
    notes = [corner(0.0, ["3", "right"]), corner(55.0, ["2", "left"])]
    out = add_distance_calls(notes, distance_call_min_m=60.0, distance_buckets_m=BUCKETS)
    assert out[1].tokens == ["2", "left"]


# --------------------------------------------------------------------------
# hazards from telemetry, not geometry
# --------------------------------------------------------------------------


def test_jump_is_read_off_the_suspension_not_the_altitude():
    """The note algorithm: all four wheels at max stretch at once means airborne. Far
    more reliable than inferring it from the altitude profile."""
    line = [LinePoint(x=0.0, y=100.0, z=i * 3.0, speed=30.0, susp_max=0.5) for i in range(60)]
    for i in range(30, 36):
        line[i] = LinePoint(x=0.0, y=100.0, z=i * 3.0, speed=30.0, susp_max=0.02)
    cumulative = [i * 3.0 for i in range(60)]

    jumps = detect_jumps(line, cumulative, susp_max_stretch=0.05, min_duration_s=0.15)
    assert len(jumps) == 1
    assert jumps[0].tokens == ["jump"]
    assert jumps[0].at_m == pytest.approx(90.0)


def test_a_single_unloaded_frame_is_not_a_jump():
    line = [LinePoint(x=0.0, y=100.0, z=i * 3.0, speed=40.0, susp_max=0.5) for i in range(60)]
    line[30] = LinePoint(x=0.0, y=100.0, z=90.0, speed=40.0, susp_max=0.02)
    cumulative = [i * 3.0 for i in range(60)]
    assert detect_jumps(line, cumulative, 0.05, min_duration_s=0.15) == []


def test_a_crest_needs_a_rise_and_a_fall_not_just_a_slope():
    """A long climb must not be called as a crest every twenty metres."""
    cumulative = [i * 3.0 for i in range(80)]
    climb = [LinePoint(x=0.0, y=i * 0.5, z=i * 3.0) for i in range(80)]
    assert detect_crests_and_dips(climb, cumulative, window_points=7) == []

    brow = [
        LinePoint(x=0.0, y=(i * 0.5 if i < 40 else (80 - i) * 0.5), z=i * 3.0)
        for i in range(80)
    ]
    found = detect_crests_and_dips(brow, cumulative, window_points=7)
    assert [n.kind for n in found] == ["crest"]
    assert found[0].at_m == pytest.approx(120.0, abs=30.0)


def test_a_dip_is_the_mirror_of_a_crest():
    cumulative = [i * 3.0 for i in range(80)]
    hollow = [
        LinePoint(x=0.0, y=(-i * 0.5 if i < 40 else -(80 - i) * 0.5), z=i * 3.0)
        for i in range(80)
    ]
    assert [n.kind for n in detect_crests_and_dips(hollow, cumulative, 7)] == ["dip"]


# --------------------------------------------------------------------------
# the stage file
# --------------------------------------------------------------------------


def _stage():
    return Stage(
        name="test",
        line=[LinePoint(x=float(i), y=10.0, z=0.0, speed=25.0) for i in range(20)],
        markings=[R(3)] * 10 + [STRAIGHT] * 10,
        notes=[
            corner(0.0, ["3", "right"], direction="right", severity=3, radius_m=48.0),
            corner(120.0, ["100", "2", "left"], index=9, direction="left", severity=2),
        ],
        length_m=19.0,
    )


def test_stage_round_trips_through_json(tmp_path):
    path = save(_stage(), tmp_path / "s.json")
    back = load(path)
    assert back.name == "test"
    assert [n.tokens for n in back.notes] == [["3", "right"], ["100", "2", "left"]]
    assert [m.label for m in back.markings] == ["R3"] * 10 + ["S"] * 10
    assert len(back.line) == 20
    assert back.line[5].speed == pytest.approx(25.0, abs=0.3)


def test_a_hand_edited_note_survives_a_reload(tmp_path):
    """The architecture notes: hand-editing a generated stage must be a supported workflow.
    Generation gets 80% of the way; the last 20% is a human fixing three
    corners."""
    path = save(_stage(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["notes"][0]["tokens"] = ["hairpin", "right", "don't cut"]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert load(path).notes[0].tokens == ["hairpin", "right", "don't cut"]


def test_stage_file_records_where_it_came_from(tmp_path):
    """The architecture notes: source recording hash, generator version, config snapshot.
    Without them you cannot tell whether last week's stage differs because the
    algorithm changed or because a threshold did."""
    stage = _stage()
    stage.source = {"capture": "x.fzr", "sha256": "abc"}
    stage.config = {"stage": {"resample": {"spacing_m": 3.0}}}
    data = to_dict(stage)
    assert data["source"]["sha256"] == "abc"
    assert data["config"]["stage"]["resample"]["spacing_m"] == 3.0
    assert "generated_utc" in data["generator"]


def test_loading_something_that_is_not_a_stage_fails_clearly(tmp_path):
    path = tmp_path / "nope.json"
    path.write_text('{"format": "something-else"}', encoding="utf-8")
    with pytest.raises(StageError, match="not a stage file"):
        load(path)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StageError, match="not valid JSON"):
        load(path)


def test_a_future_version_is_refused_rather_than_misread():
    with pytest.raises(StageError, match="version"):
        from_dict({"format": "codriver-stage", "version": 99})


def test_render_notes_reads_like_a_co_driver():
    text = render_notes(_stage())
    assert "3 right" in text
    assert "100 2 left" in text
    assert "0.120 km" in text


def test_required_tokens_lists_what_a_voice_pack_must_provide():
    tokens = required_tokens(_stage().notes)
    assert tokens == {"3", "right", "100", "2", "left"}


# --------------------------------------------------------------------------
# "tightens" phrasing
# --------------------------------------------------------------------------


def _generate_corners(markings, **kw):
    from codriver.stage.notes import generate

    line = [LinePoint(x=0.0, y=0.0, z=i * 3.0, susp_max=0.5) for i in range(len(markings))]
    cumulative = [i * 3.0 for i in range(len(markings))]
    return generate(line, markings, cumulative, hazards=False,
                    distance_call_min_m=1e9, **kw)


def test_a_corner_that_develops_far_is_phrased_as_tightens():
    """The apex severity was pulled back well past the classification
    window's own ramp-in: the corner keeps coming after the driver has
    committed, and the phrasing must say so."""
    markings = (
        [STRAIGHT] * 5 + [R(5)] * 8 + [R(3)] * 6 + [R(1)] * 6 + [STRAIGHT] * 5
    )
    notes = _generate_corners(markings, tightens_min_run_points=12)
    assert [n.tokens for n in notes] == [["right", "tightens", "1"]]
    assert notes[0].severity == 1, "queue decisions still see the apex severity"


def test_a_short_ramp_into_a_corner_is_not_tightens():
    """Every corner approached off a straight ramps through the milder
    classes over roughly one window. That is an artifact of the sliding
    window, not a corner that develops."""
    markings = [STRAIGHT] * 5 + [R(5)] * 3 + [R(2)] * 10 + [STRAIGHT] * 5
    notes = _generate_corners(markings, tightens_min_run_points=12)
    assert [n.tokens for n in notes] == [["2", "right"]]


def test_a_mild_corner_never_says_tightens():
    """'tightens 5' is not a warning anyone needs."""
    markings = (
        [STRAIGHT] * 5 + [R(6)] * 10 + [R(5)] * 8 + [R(4)] * 8 + [STRAIGHT] * 5
    )
    notes = _generate_corners(
        markings, tightens_min_run_points=12, tightens_max_severity=3
    )
    assert [n.tokens for n in notes] == [["4", "right"]]


def test_chained_collapse_keeps_the_original_apex_index():
    """A corner that tightens in several steps must measure its development
    from where the call sits to where the apex originally was, not to the
    last intermediate merge."""
    cands = [Candidate(0, R(5)), Candidate(8, R(3)), Candidate(16, R(1))]
    out = collapse_ascending(cands, collapse_window_points=20)
    assert len(out) == 1
    assert out[0].index == 0
    assert out[0].marking.label == "R1"
    assert out[0].apex_index == 16


def test_tightens_adds_its_token_to_the_voice_pack_requirements():
    markings = (
        [STRAIGHT] * 5 + [L(5)] * 8 + [L(3)] * 6 + [L(1)] * 6 + [STRAIGHT] * 5
    )
    notes = _generate_corners(markings, tightens_min_run_points=12)
    assert "tightens" in required_tokens(notes)


# --------------------------------------------------------------------------
# water, read off the wheel flags, not the map
# --------------------------------------------------------------------------


def _wet_line(wet_counts):
    line = [
        LinePoint(x=0.0, y=0.0, z=i * 3.0, susp_max=0.5, wet_wheels=w)
        for i, w in enumerate(wet_counts)
    ]
    return line, [i * 3.0 for i in range(len(line))]


def test_a_ford_is_called_water_where_the_water_starts():
    from codriver.stage.notes import detect_water

    line, cum = _wet_line([0] * 10 + [4] * 5 + [0] * 10)
    notes = detect_water(line, cum, min_wheels=2, min_length_m=5.0)
    assert [(n.kind, n.index, n.tokens) for n in notes] == [("water", 10, ["water"])]
    assert notes[0].length_m == pytest.approx(12.0)


def test_one_wet_wheel_is_a_puddle_not_a_water_call():
    from codriver.stage.notes import detect_water

    line, cum = _wet_line([0] * 10 + [1] * 5 + [0] * 10)
    assert detect_water(line, cum, min_wheels=2) == []


def test_a_splash_is_too_short_to_be_called():
    from codriver.stage.notes import detect_water

    line, cum = _wet_line([0] * 10 + [4] + [0] * 10)
    assert detect_water(line, cum, min_length_m=5.0) == []


def test_two_wet_stretches_close_together_are_one_crossing():
    from codriver.stage.notes import detect_water

    near = _wet_line([0] * 5 + [4] * 3 + [0] * 2 + [4] * 3 + [0] * 5)
    notes = detect_water(*near, min_length_m=5.0, merge_gap_m=15.0)
    assert [n.index for n in notes] == [5]
    far = _wet_line([0] * 5 + [4] * 3 + [0] * 20 + [4] * 3 + [0] * 5)
    assert len(detect_water(*far, min_length_m=5.0, merge_gap_m=15.0)) == 2


def test_generate_emits_water_alongside_the_corners():
    from codriver.stage.notes import generate

    line, cum = _wet_line([0] * 30 + [4] * 6 + [0] * 30)
    notes = generate(line, [STRAIGHT] * len(line), cum, distance_call_min_m=1e9)
    assert [n.tokens for n in notes] == [["water"]]


def test_per_point_telemetry_survives_the_stage_file(tmp_path):
    """Learn rebuilds a stage from its saved line. If the file dropped the
    suspension, water and steering per point, every jump and ford vanished
    the first time Learn ran, and the orientation check went blind."""
    from codriver.stage.schema import Stage, load, save

    line = [
        LinePoint(
            x=float(i), y=0.0, z=0.0, speed=10.0, steer=0.5,
            susp_max=0.02 if i == 3 else 0.5, wet_wheels=4 if i == 5 else 0,
        )
        for i in range(8)
    ]
    save(Stage(name="t", line=line, markings=[STRAIGHT] * 8), tmp_path / "t.json")
    back = load(tmp_path / "t.json")
    assert [p.wet_wheels for p in back.line] == [p.wet_wheels for p in line]
    assert [p.susp_max for p in back.line] == pytest.approx([p.susp_max for p in line], abs=0.01)
    assert [p.steer for p in back.line] == pytest.approx([0.5] * 8)


def test_a_stage_file_without_per_point_telemetry_still_loads():
    from codriver.stage.schema import Stage, from_dict, to_dict

    data = to_dict(Stage(name="old", line=[LinePoint(x=float(i), y=0.0, z=0.0) for i in range(5)],
                         markings=[STRAIGHT] * 5))
    del data["telemetry"]
    back = from_dict(data)
    assert len(back.line) == 5
    assert all(p.susp_max == 1.0 and p.wet_wheels == 0 for p in back.line)
