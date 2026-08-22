"""Localisation, the runtime design.

The test that earns its keep is the self-crossing one: an unconstrained
nearest-neighbour query snaps to the wrong arm wherever the stage crosses
itself, and your real stage has a switchback staircase where arms run 20 m
apart. The windowed search must hold the correct arm straight through the
crossing.
"""

from __future__ import annotations

import math

import pytest

from codriver.runtime.locate import Fix, Locator, StageIndex, TrackState
from codriver.stage.line import LinePoint, cumulative_distance


def _index(points):
    line = [LinePoint(x=x, y=0.0, z=z) for x, z in points]
    return StageIndex(line, cumulative_distance(line))


def straight_line(n=100, step=3.0):
    return _index([(i * step, 0.0) for i in range(n)])


def crossing_line():
    """A path that crosses itself at right angles at (0, 0).

    Leg A runs along z=0 through the origin; a connector loops around; leg C
    runs along x=0 back through the same origin. Indices on A and C are far
    apart, but the *positions* at the crossing are identical, the exact
    situation that breaks a global nearest query.
    """
    points = []
    for i in range(41):  # leg A: x -60..60, z 0
        points.append((-60.0 + i * 3.0, 0.0))
    # connector: quarter arc from (60, 0) around to (0, -60)
    for i in range(1, 21):
        a = (i / 20.0) * (math.pi / 2)
        points.append((60.0 * math.cos(a), -60.0 * math.sin(a)))
    for i in range(1, 41):  # leg C: x 0, z -60..60, crossing (0,0) at i=20
        points.append((0.0, -60.0 + i * 3.0))
    return _index(points), 41, 61  # index ranges: A ends at 40, C starts at 61


# --------------------------------------------------------------------------
# StageIndex
# --------------------------------------------------------------------------


def test_nearest_finds_the_closest_point():
    idx = straight_line()
    i, d = idx.nearest(30.0, 4.0)
    assert i == 10
    assert d == pytest.approx(4.0)


def test_nearest_respects_the_window():
    idx = straight_line()
    i, _ = idx.nearest(30.0, 0.0, lo=50, hi=80)
    assert i == 50, "constrained search must not leave its window"


def test_projection_beats_snapping_to_the_grid():
    """3 m point spacing is 75 ms of trigger jitter at 40 m/s. Projection
    onto the segment removes it."""
    idx = straight_line()
    along, off = idx.project(31.7, 2.0, 11)
    assert along == pytest.approx(31.7, abs=0.01)
    assert off == pytest.approx(2.0, abs=0.01)


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------


def _drive(locator, positions, t0=0.0, dt=1 / 30):
    fixes = []
    for k, (x, z) in enumerate(positions):
        fixes.append(locator.update(x, z, t0 + k * dt))
    return fixes


def test_acquire_then_track_a_straight():
    locator = Locator(straight_line())
    fixes = _drive(locator, [(i * 1.0, 0.3) for i in range(90)])
    assert fixes[0].ok
    assert all(f.ok for f in fixes)
    assert fixes[-1].along_m == pytest.approx(89.0, abs=0.1)
    assert fixes[-1].off_line_m == pytest.approx(0.3, abs=0.05)


def test_windowed_search_holds_its_arm_through_a_crossing():
    """THE test. At (0,0) legs A and C are the same place; only history says
    which one the car is on."""
    idx, a_end, c_start = crossing_line()
    locator = Locator(idx)

    # Drive leg C from its start: x=0, z climbing through the crossing.
    path = [(0.0, -55.0 + k * 1.0) for k in range(111)]
    # Localise once near C's start so the window is anchored there.
    first = locator.update(*path[0], 0.0)
    assert first.ok and first.index >= c_start

    fixes = _drive(locator, path[1:], t0=1 / 30)
    assert all(f.index >= c_start for f in fixes), (
        "tracking jumped to the other arm at the self-crossing"
    )
    assert all(f.ok for f in fixes)


def test_cold_start_far_from_the_stage_stays_cold():
    locator = Locator(straight_line())
    fix = locator.update(500.0, 400.0, 0.0)
    assert not fix.ok
    assert fix.state is TrackState.COLD


def test_wandering_off_line_is_tolerated_briefly_then_reacquired():
    """A cut or a spin puts the car wide for a moment; tracking must not be
    thrown away for that. Staying wide for lost_after_packets must declare
    LOST, and coming back onto the line anywhere must reacquire.

    The excursion is 15 m, believable for a cut. 80 m in one frame would
    (correctly) trip the teleport detector instead; that path has its own
    test below.
    """
    locator = Locator(straight_line(), lost_distance_m=10.0, lost_after_packets=5)
    _drive(locator, [(i * 1.0, 0.0) for i in range(30)])

    # Two wide packets: still tracking on the old fix.
    wide = _drive(locator, [(31.0, 15.0), (32.0, 15.0)], t0=1.0)
    assert all(f.state is TrackState.TRACKING for f in wide)

    # Persistently wide: the streak runs out and LOST is declared.
    fixes = _drive(locator, [(33.0 + k, 15.0) for k in range(5)], t0=1.07)
    assert fixes[-1].state is TrackState.LOST

    # Back on the line, somewhere well ahead, and reacquired globally.
    back = locator.update(60.0, 1.0, 1.5)
    assert back.ok
    assert back.along_m == pytest.approx(60.0, abs=1.5)


def test_stream_gap_is_flagged_and_survivable():
    """Unpause: same place, tracking resumes, queue owner is told."""
    locator = Locator(straight_line())
    _drive(locator, [(i * 1.0, 0.0) for i in range(30)])
    resumed = locator.update(30.0, 0.0, 10.0)  # ~9 s of silence
    assert resumed.resumed_from_gap
    assert not resumed.jumped
    assert resumed.ok


def test_rewind_is_a_jump_and_forces_relocalisation():
    """Gap + teleport = rewind/restart. Nothing queued can be trusted."""
    locator = Locator(straight_line())
    _drive(locator, [(100.0 + i, 0.0) for i in range(30)])
    fix = locator.update(3.0, 0.0, 10.0)
    assert fix.resumed_from_gap
    assert fix.jumped
    assert fix.ok, "must re-localise immediately at the new position"
    assert fix.along_m == pytest.approx(3.0, abs=1.5)


def test_teleport_without_gap_is_still_a_jump():
    locator = Locator(straight_line())
    _drive(locator, [(i * 1.0, 0.0) for i in range(30)])
    fix = locator.update(250.0, 0.0, 30 / 30)
    assert fix.jumped
    assert fix.ok
    assert fix.along_m == pytest.approx(250.0, abs=1.5)
