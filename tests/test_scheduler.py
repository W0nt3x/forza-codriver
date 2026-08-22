"""The scheduler, the runtime design's trigger timing and queue discipline.

Everything runs against a fake clock and a fixed token-duration function, so
these tests assert timing exactly rather than approximately.
"""

from __future__ import annotations

import pytest

from codriver.runtime.scheduler import PlayEvent, Scheduler, interp_curve
from codriver.stage.notes import Note


def note(at_m, tokens=("3", "right"), severity=3, kind="corner"):
    return Note(at_m=at_m, tokens=list(tokens), severity=severity, kind=kind)


def flat_duration(tokens):
    return 0.4 * len(tokens)


def make(notes, **kw):
    s = Scheduler(notes=notes, duration_fn=flat_duration)
    s.speed_curve_kmh = (0, 200)
    s.speed_curve_mult = (1.0, 1.0)
    for key, value in kw.items():
        setattr(s, key, value)
    return s


def drive(s, speed, until_m, dt=1 / 30, start_m=0.0, start_t=0.0):
    """Advance the car at constant speed, collecting (event, fired_at_m, t)."""
    events = []
    along, now = start_m, start_t
    while along < until_m:
        for e in s.tick(along, speed, now):
            events.append((e, along, now))
        along += speed * dt
        now += dt
    return events


# --------------------------------------------------------------------------
# lead distance
# --------------------------------------------------------------------------


def test_the_note_finishes_reaction_buffer_before_the_corner():
    """The the runtime design contract, verified with a stopwatch rather than by reading the
    formula back: finish time = corner arrival - reaction buffer."""
    s = make([note(500.0)], reaction_buffer_s=1.8)
    speed = 30.0
    events = drive(s, speed, 500.0)
    assert len(events) == 1
    e, fired_at_m, fired_t = events[0]

    corner_arrival_t = fired_t + (500.0 - fired_at_m) / speed
    finish_t = fired_t + e.duration_s
    margin = corner_arrival_t - finish_t
    # One 30 Hz tick of slack: the threshold is crossed between ticks.
    assert margin == pytest.approx(1.8, abs=0.05)


def test_faster_cars_hear_the_note_further_out():
    slow = make([note(500.0)])
    fast = make([note(500.0)])
    _, slow_at, _ = drive(slow, 15.0, 500.0)[0]
    _, fast_at, _ = drive(fast, 45.0, 500.0)[0]
    assert 500.0 - fast_at > (500.0 - slow_at) * 2.5


def test_speed_curve_multiplies_the_lead():
    s = make([note(500.0)])
    s.speed_curve_kmh = (0, 100)
    s.speed_curve_mult = (1.0, 2.0)
    # 30 m/s = 108 km/h -> mult 2.0 (flat above the last point)
    assert s.lead_m(30.0, 1.0) == pytest.approx(30.0 * (1.0 + 1.8) * 2.0)


def test_lead_is_clamped():
    s = make([note(500.0)], min_lead_m=20.0, max_lead_m=100.0)
    assert s.lead_m(0.5, 0.5) == 20.0
    assert s.lead_m(80.0, 3.0) == 100.0


def test_interp_curve_is_linear_between_and_flat_outside():
    xs, ys = (0, 60, 120), (1.0, 1.0, 1.2)
    assert interp_curve(-10, xs, ys) == 1.0
    assert interp_curve(90, xs, ys) == pytest.approx(1.1)
    assert interp_curve(500, xs, ys) == pytest.approx(1.2)


# --------------------------------------------------------------------------
# queue discipline
# --------------------------------------------------------------------------


def test_two_phrases_never_overlap():
    """The second note fires while the first is still being spoken; it must
    wait for the mouth, not talk over it."""
    s = make([note(200.0), note(203.0, tokens=("2", "left"), severity=2)])
    events = drive(s, 25.0, 210.0)
    assert len(events) == 2
    (e1, _, t1), (e2, _, t2) = events
    assert t2 >= t1 + e1.duration_s, "second phrase started before the first ended"


def test_contention_drops_the_less_severe_note():
    """Two notes fire in the same tick; severity decides who gets the mouth."""
    s = make(
        [
            note(200.0, tokens=("5", "right"), severity=5),
            note(202.0, tokens=("1", "left"), severity=1),
        ]
    )
    # Start already within lead distance of both, so they fire together.
    events = drive(s, 60.0, 220.0, start_m=50.0)
    spoken = [e.note.severity for e, _, _ in events]
    assert spoken == [1], f"the hairpin must win the mouth, got {spoken}"
    assert s.dropped == 1


def test_a_jump_hazard_outranks_a_mild_corner():
    s = make(
        [
            note(200.0, tokens=("jump",), severity=None, kind="jump"),
            note(201.0, tokens=("5", "right"), severity=5),
        ]
    )
    events = drive(s, 60.0, 220.0, start_m=50.0)
    assert [e.note.kind for e, _, _ in events] == ["jump"]


def test_a_mild_note_yields_when_speaking_it_would_make_a_hairpin_late():
    """The mouth is a resource booked into the future. A phrase that would
    still be playing when a tighter corner's call falls due, leaving that
    call no room to finish, is sacrificed for it."""
    s = make(
        [
            # A long linked phrase for a mild corner...
            note(200.0, tokens=("5", "right", "and", "5", "left", "then"), severity=5),
            # ...with a hairpin close enough behind that both cannot be said.
            note(230.0, tokens=("1", "left"), severity=1),
        ]
    )
    # At 80 m/s: 2.4s of mild phrase + 0.8s of hairpin call > the 2.9s until
    # the hairpin. At lower speeds both fit and both are said, see the
    # companion test below.
    events = drive(s, 80.0, 240.0)
    spoken = [e.note.severity for e, _, _ in events]
    assert spoken == [1], f"expected the hairpin alone, got {spoken}"
    assert s.dropped == 1


def test_a_mild_note_speaks_when_there_is_room_for_both():
    """Look-ahead must not turn into paranoia: when both phrases fit, both
    are said."""
    s = make(
        [
            note(200.0, tokens=("5", "right"), severity=5),
            note(320.0, tokens=("1", "left"), severity=1),
        ]
    )
    events = drive(s, 30.0, 340.0)
    assert [e.note.severity for e, _, _ in events] == [5, 1]
    assert s.dropped == 0


def test_a_note_that_cannot_finish_in_time_is_dropped_not_played_late():
    """Standing nearly on top of the corner: silence beats 'three right'
    delivered mid-corner."""
    s = make([note(30.0)], min_lead_m=100.0, drop_if_later_than_s=0.3)
    events = drive(s, 40.0, 40.0, start_m=20.0)
    assert events == []
    assert s.dropped == 1


def test_relocate_skips_notes_already_behind():
    s = make([note(100.0), note(400.0), note(700.0)])
    s.relocate(350.0)
    events = drive(s, 30.0, 720.0, start_m=350.0)
    assert [e.note.at_m for e, _, _ in events] == [400.0, 700.0]


def test_flush_clears_the_queue_but_not_progress():
    s = make([note(200.0), note(500.0)])
    drive(s, 60.0, 199.0)  # fires 200 into the queue (mouth busy or not)
    s.flush()
    events = drive(s, 30.0, 520.0, start_m=205.0, start_t=100.0)
    assert [e.note.at_m for e, _, _ in events] == [500.0], (
        "the flushed note must not come back, the future note must"
    )


def test_every_note_spoken_exactly_once_on_a_clean_run():
    notes = [note(m, severity=3 + (i % 3)) for i, m in enumerate(range(150, 1500, 150))]
    s = make(notes)
    events = drive(s, 25.0, 1520.0)
    assert [e.note.at_m for e, _, _ in events] == [n.at_m for n in notes]
    assert s.dropped == 0


def test_a_linked_phrase_ranks_by_its_most_important_part():
    """Observed live: '6 left and jump into 4 left' was dropped in favour of
    a bare jump call, because the phrase ranked as its head corner (a 6).
    The driver went over a jump into a 4 with no warning. A phrase is as
    important as the most important thing in it."""
    bare_five = note(150.0, tokens=("5", "right"), severity=5)
    linked = Note(
        at_m=250.0,
        tokens=["6", "left", "and", "jump", "into", "4", "left"],
        severity=6,
        kind="corner",
        parts=[
            {"tokens": ["6", "left"], "kind": "corner", "severity": 6, "at_m": 250.0},
            {"tokens": ["jump"], "kind": "jump", "at_m": 260.0},
            {"tokens": ["4", "left"], "kind": "corner", "severity": 4, "at_m": 268.0},
        ],
    )

    # At 60 m/s both fire on the first tick (the long phrase's lead is far
    # bigger), so they contend immediately.
    s = make([bare_five, linked])
    events = drive(s, 60.0, 280.0)
    spoken = [e.note.tokens[0] for e, _, _ in events]
    assert spoken == ["6"], (
        f"the phrase carrying the jump must win the mouth, got {spoken}"
    )
    assert s.dropped == 1
    # Head severity alone would have ranked it 6 and lost to the bare 5.
