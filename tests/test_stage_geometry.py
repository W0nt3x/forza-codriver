"""Resampling and curvature classification, the note algorithm, steps 0 and 1.

Circles are the useful test shape here: a circle of radius r has curvature
1/r everywhere, so the classifier has one right answer at every point and a
window that is too wide or a formula that is subtly wrong shows up as a
number, not a vibe.
"""

from __future__ import annotations

import math

import pytest

from codriver.stage.curvature import (
    G,
    STRAIGHT_SEVERITY,
    Direction,
    classify,
    direction_agreement,
    fit_circle,
    histogram,
    invert,
    severity_for_radius,
)
from codriver.stage.line import (
    LinePoint,
    cumulative_distance,
    ground_distance,
    split_segments,
    to_line,
    total_length,
    trim_stationary,
)
from codriver.stage.resample import (
    densify,
    midpoint_bezier,
    resample,
    spacing_stats,
    subdivide_long_gaps,
)

BANDS = (30, 40, 50, 70, 90, 130)


def arc(radius: float, sweep_deg: float, step_m: float = 2.0, clockwise=False, y=0.0):
    """Points along a circular arc in the X/Z plane."""
    span = math.radians(sweep_deg)
    n = max(3, int(radius * span / step_m))
    sign = -1.0 if clockwise else 1.0
    return [
        LinePoint(
            x=radius * math.cos(sign * span * i / n),
            y=y,
            z=radius * math.sin(sign * span * i / n),
        )
        for i in range(n + 1)
    ]


def straight(length: float, step_m: float = 2.0):
    n = int(length / step_m)
    return [LinePoint(x=0.0, y=0.0, z=i * step_m) for i in range(n + 1)]


# --------------------------------------------------------------------------
# line extraction
# --------------------------------------------------------------------------


def test_to_line_drops_points_the_car_did_not_move_between():
    from codriver.adapters.base import TelemetryFrame

    frames = [TelemetryFrame(t=i / 60, x=0.0, y=0.0, z=0.0) for i in range(200)]
    frames += [TelemetryFrame(t=(200 + i) / 60, x=0.0, y=0.0, z=i * 1.0) for i in range(50)]
    line = to_line(frames, min_step_m=0.25)
    # 200 identical stationary frames collapse to one.
    assert len(line) == 50


def test_split_segments_cuts_on_race_off_and_on_stream_gaps():
    from codriver.adapters.base import TelemetryFrame

    def run(t0, n, race_on=True, x0=0.0):
        return [
            TelemetryFrame(t=t0 + i / 60, x=x0 + float(i), y=0.0, z=0.0, race_on=race_on)
            for i in range(n)
        ]

    # Each run spans 100/60 s, so the starts are far enough apart that the
    # silences between them are real gaps rather than ordinary frame spacing.
    frames = run(0.0, 100) + run(5.0, 100) + run(10.0, 5, race_on=False) + run(20.0, 100)
    segments = split_segments(frames, gap_s=0.5, min_points=60)
    assert len(segments) == 3
    assert all(len(s) == 100 for s in segments)

    # A silence shorter than the threshold is just frame jitter, not a break,
    # as long as the car is still where it was (it carries on from x=100).
    merged = split_segments(run(0.0, 100) + run(2.0, 100, x0=100.0), gap_s=0.5, min_points=60)
    assert len(merged) == 1
    # ...but a teleport back to the start in that same short silence is one.
    teleported = split_segments(run(0.0, 100) + run(2.0, 100), gap_s=0.5, min_points=60)
    assert len(teleported) == 2


def test_trim_stationary_removes_both_ends():
    slow = [LinePoint(x=float(i), y=0.0, z=0.0, speed=0.0) for i in range(10)]
    fast = [LinePoint(x=float(i), y=0.0, z=0.0, speed=20.0) for i in range(10, 40)]
    assert len(trim_stationary(slow + fast + slow)) == 30


# --------------------------------------------------------------------------
# resampling
# --------------------------------------------------------------------------


def test_subdivide_splits_only_the_long_gaps():
    line = [LinePoint(0, 0, 0), LinePoint(0, 0, 5), LinePoint(0, 0, 105)]
    out = subdivide_long_gaps(line, max_gap_m=30.0)
    gaps = [ground_distance(a, b) for a, b in zip(out, out[1:])]
    assert gaps[0] == pytest.approx(5.0)
    assert max(gaps[1:]) <= 30.0 + 1e-9


def test_midpoint_bezier_runs_between_segment_midpoints():
    """The note algorithm's spline: it cuts the corner rather than passing through it,
    which is what a driver does anyway."""
    p0 = LinePoint(0, 0, 0)
    p1 = LinePoint(0, 0, 10)
    p2 = LinePoint(10, 0, 10)

    start = midpoint_bezier(p0, p1, p2, 0.0)
    end = midpoint_bezier(p0, p1, p2, 1.0)
    middle = midpoint_bezier(p0, p1, p2, 0.5)

    assert (start.x, start.z) == pytest.approx((0.0, 5.0))
    assert (end.x, end.z) == pytest.approx((5.0, 10.0))
    # Must NOT pass through the corner point itself.
    assert ground_distance(middle, p1) > 1.0


def test_resample_produces_uniform_spacing():
    """Everything downstream reads distance off the point index. If the
    spacing is not uniform, a 7-point window is not 20 m and the severity
    classes mean nothing."""
    line = arc(radius=80.0, sweep_deg=180, step_m=1.5)
    out = resample(line, spacing_m=3.0)
    lo, mean, hi = spacing_stats(out)
    assert hi - lo < 0.02, f"spacing varies: {lo:.3f}..{hi:.3f}"
    assert mean == pytest.approx(3.0, abs=0.02)


def test_resample_keeps_both_ends_of_the_line():
    line = straight(100.0, step_m=1.0)
    out = resample(line, spacing_m=3.0)
    assert ground_distance(out[0], line[0]) < 1.0
    assert ground_distance(out[-1], line[-1]) < 1.0


def test_resample_preserves_length_within_a_percent():
    line = arc(radius=50.0, sweep_deg=270, step_m=1.0)
    before = total_length(line)
    after = total_length(resample(line, spacing_m=3.0))
    assert after == pytest.approx(before, rel=0.02)


def test_resample_carries_altitude_through():
    line = [LinePoint(x=0.0, y=i * 0.5, z=i * 2.0) for i in range(60)]
    out = resample(line, spacing_m=3.0)
    assert out[0].y == pytest.approx(0.0, abs=1.0)
    assert out[-1].y == pytest.approx(line[-1].y, abs=1.0)
    assert all(a.y <= b.y + 1e-6 for a, b in zip(out, out[1:]))


def test_densify_leaves_short_lines_alone():
    assert len(densify([LinePoint(0, 0, 0), LinePoint(0, 0, 1)])) == 2


# --------------------------------------------------------------------------
# curvature
# --------------------------------------------------------------------------


def test_fit_circle_recovers_a_known_radius():
    points = arc(radius=100.0, sweep_deg=40, step_m=5.0)
    mid = len(points) // 2
    radius, direction = fit_circle(points[0], points[mid], points[-1])
    assert radius == pytest.approx(100.0, rel=0.01)
    assert direction in (Direction.LEFT, Direction.RIGHT)


def test_fit_circle_calls_collinear_points_straight():
    radius, direction = fit_circle(
        LinePoint(0, 0, 0), LinePoint(0, 0, 10), LinePoint(0, 0, 20)
    )
    assert direction is Direction.STRAIGHT
    assert radius == math.inf


def test_opposite_arcs_get_opposite_directions():
    left = arc(radius=60.0, sweep_deg=40, clockwise=False)
    right = arc(radius=60.0, sweep_deg=40, clockwise=True)
    _, dl = fit_circle(left[0], left[len(left) // 2], left[-1])
    _, dr = fit_circle(right[0], right[len(right) // 2], right[-1])
    assert dl is not dr
    assert {dl, dr} == {Direction.LEFT, Direction.RIGHT}


@pytest.mark.parametrize("severity,upper_kmh", list(enumerate(BANDS, start=1)))
def test_severity_bands_match_the_documented_speed_table(severity, upper_kmh):
    """A class is the speed band at which the corner reaches 0.3 g."""
    # Radius that reaches exactly 0.3 g at the top of this band.
    radius = (upper_kmh / 3.6) ** 2 / (0.3 * G)
    assert severity_for_radius(radius - 0.5, BANDS) == severity


def test_anything_faster_than_the_last_band_is_a_straight():
    radius = (200 / 3.6) ** 2 / (0.3 * G)
    assert severity_for_radius(radius, BANDS) == STRAIGHT_SEVERITY
    assert severity_for_radius(math.inf, BANDS) == STRAIGHT_SEVERITY


def test_classify_gives_a_constant_class_around_a_constant_radius():
    line = resample(arc(radius=45.0, sweep_deg=200, step_m=1.0), spacing_m=3.0)
    markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
    # Ignore the ends, which have no window and are reported straight.
    interior = markings[8:-8]
    labels = {m.label for m in interior}
    assert len(labels) == 1, f"a constant-radius arc classified as {labels}"
    assert interior[0].is_corner


def test_a_straight_road_produces_no_corners():
    line = resample(straight(300.0, step_m=1.0), spacing_m=3.0)
    markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
    assert not any(m.is_corner for m in markings)


def test_tighter_corners_get_more_severe_classes():
    severities = []
    for radius in (20.0, 60.0, 150.0):
        line = resample(arc(radius=radius, sweep_deg=170, step_m=1.0), spacing_m=3.0)
        markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
        corners = [m for m in markings if m.is_corner]
        severities.append(corners[len(corners) // 2].severity)
    assert severities[0] < severities[1] < severities[2]


def test_straight_is_never_the_same_direction_as_a_corner():
    """The note algorithm: the straight sentinel counts as 'same direction' only with
    other straights."""
    line = resample(arc(radius=40.0, sweep_deg=120, step_m=1.0), spacing_m=3.0)
    markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
    corner = next(m for m in markings if m.is_corner)
    straight_m = next(m for m in markings if not m.is_corner)
    assert not straight_m.same_direction_as(corner)
    assert straight_m.same_direction_as(straight_m)


def test_invert_swaps_left_and_right_and_leaves_straights_alone():
    line = resample(arc(radius=40.0, sweep_deg=120, step_m=1.0), spacing_m=3.0)
    markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
    flipped = invert(markings)
    for a, b in zip(markings, flipped):
        assert a.severity == b.severity
        if a.is_corner:
            assert a.direction is not b.direction
        else:
            assert b.direction is a.direction


def test_direction_agreement_detects_an_inverted_convention():
    """Whether divisor > 0 means right depends on the handedness of the frame,
    and nothing guarantees FH6 matches the frame the note algorithm was written for.
    The recon lap's own steering input settles it."""
    line = resample(arc(radius=40.0, sweep_deg=200, step_m=1.0), spacing_m=3.0)
    markings = classify(line, window_points=7, class_speed_bands_kmh=BANDS)
    direction = next(m.direction for m in markings if m.is_corner)

    steered = [
        LinePoint(p.x, p.y, p.z, steer=(0.5 if direction is Direction.RIGHT else -0.5))
        for p in line
    ]
    agreement, samples = direction_agreement(steered, markings)
    assert samples > 20
    assert agreement == pytest.approx(1.0)

    backwards, _ = direction_agreement(steered, invert(markings))
    assert backwards == pytest.approx(0.0)


def test_histogram_labels_are_compact_and_sorted():
    line = resample(arc(radius=40.0, sweep_deg=120, step_m=1.0), spacing_m=3.0)
    counts = histogram(classify(line, window_points=7, class_speed_bands_kmh=BANDS))
    assert all(k == "S" or (k[0] in "LR" and k[1:].isdigit()) for k in counts)
    assert list(counts) == sorted(counts)


def test_cumulative_distance_is_ground_plane_only():
    """The coordinate notes: distance is hypot(dx, dz). Altitude is not part of it."""
    climb = [LinePoint(x=0.0, y=i * 10.0, z=i * 3.0) for i in range(11)]
    assert cumulative_distance(climb)[-1] == pytest.approx(30.0)
