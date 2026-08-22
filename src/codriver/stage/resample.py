"""The note algorithm, step 0, resampling the raw line to consistent spacing.

A recon recording is a ~60 Hz position stream: dense where you were slow,
sparse where you were fast, and noisy throughout. Every later step assumes
even spacing, the +/-7 point window in ``curvature.py`` only means "20 m
each side" if the points really are 3 m apart, and the 20-point guard in
``notes.py`` only means "60 m" for the same reason. Skipping this step does
not degrade the output, it invalidates it.

Three stages:

1. Linearly subdivide any gap longer than ``max_gap_m``, so a fast straight
   does not smuggle a 40 m jump into the spline.
2. Densify with a quadratic Bezier through the *segment midpoints*, not
   Catmull-Rom. Catmull-Rom passes through every recorded point and throws
   visible artefacts at 90-degree turns; the midpoint spline cuts corners,
   which is what a driver does anyway.
3. Walk the dense polyline and emit a point every ``spacing_m``. You cannot
   get even spacing by stepping the curve parameter uniformly, equal steps
   in t give unequal steps in distance.

Distances here are in the X/Z ground plane. Altitude rides along
and is interpolated with everything else.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from .line import LinePoint, ground_distance


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_point(a: LinePoint, b: LinePoint, t: float) -> LinePoint:
    """Interpolate position and every carried telemetry value."""
    return LinePoint(
        x=_lerp(a.x, b.x, t),
        y=_lerp(a.y, b.y, t),
        z=_lerp(a.z, b.z, t),
        t=_lerp(a.t, b.t, t),
        speed=_lerp(a.speed, b.speed, t),
        # Suspension and surface flags are events, not curves. Take the
        # extreme rather than the average, or a one-frame jump interpolates
        # itself out of existence.
        susp_max=min(a.susp_max, b.susp_max),
        steer=_lerp(a.steer, b.steer, t),
        wet_wheels=max(a.wet_wheels, b.wet_wheels),
        surface_rumble=max(a.surface_rumble, b.surface_rumble),
    )


def subdivide_long_gaps(
    line: Sequence[LinePoint],
    max_gap_m: float = 30.0,
) -> list[LinePoint]:
    """Split any segment longer than ``max_gap_m`` into equal linear pieces."""
    if len(line) < 2:
        return list(line)
    out: list[LinePoint] = [line[0]]
    for a, b in zip(line, line[1:]):
        gap = ground_distance(a, b)
        if gap > max_gap_m:
            pieces = int(math.ceil(gap / max_gap_m))
            for i in range(1, pieces):
                out.append(lerp_point(a, b, i / pieces))
        out.append(b)
    return out


def midpoint_bezier(
    p0: LinePoint,
    p1: LinePoint,
    p2: LinePoint,
    t: float,
) -> LinePoint:
    """The note algorithm's midpoint interpolation.

    Runs from midpoint(p0, p1) at t=0 to midpoint(p1, p2) at t=1, bending
    around p1 without passing through it. That is a quadratic Bezier with p1
    as the control point, and the corner-cutting is deliberate.
    """
    p01 = lerp_point(p0, p1, t * 0.5 + 0.5)
    p12 = lerp_point(p1, p2, t * 0.5)
    return lerp_point(p01, p12, t)


def densify(
    line: Sequence[LinePoint],
    walk_step_m: float = 0.25,
) -> list[LinePoint]:
    """Sample the midpoint spline finely enough to walk by arc length."""
    if len(line) < 3:
        return list(line)

    out: list[LinePoint] = [lerp_point(line[0], line[1], 0.0)]
    for i in range(1, len(line) - 1):
        p0, p1, p2 = line[i - 1], line[i], line[i + 1]
        # The span is roughly half of each neighbouring segment. Enough steps
        # that no single one exceeds walk_step_m.
        span = 0.5 * (ground_distance(p0, p1) + ground_distance(p1, p2))
        steps = max(2, int(math.ceil(span / max(walk_step_m, 1e-6))))
        for s in range(1, steps + 1):
            out.append(midpoint_bezier(p0, p1, p2, s / steps))
    return out


def walk_even_spacing(
    dense: Sequence[LinePoint],
    spacing_m: float = 3.0,
) -> list[LinePoint]:
    """Emit a point every ``spacing_m`` of arc length along a dense polyline."""
    if len(dense) < 2:
        return list(dense)

    total = sum(ground_distance(a, b) for a, b in zip(dense, dense[1:]))
    if total <= 0.0:
        return [dense[0]]

    # Stretch the step so it divides the line exactly. Walking with a fixed
    # spacing leaves a short remainder at the end, and a final segment of
    # 1.9 m in a 3 m line quietly breaks the invariant everything downstream
    # depends on, the +/-7 point window only means 20 m if every step is
    # the same size. The adjustment is under half a step across a whole stage.
    steps = max(1, round(total / spacing_m))
    step = total / steps

    out: list[LinePoint] = [dense[0]]
    carried = 0.0
    for a, b in zip(dense, dense[1:]):
        seg = ground_distance(a, b)
        if seg <= 0.0:
            continue
        # There may be room for several output points inside one dense
        # segment, if the recon lap was moving quickly here.
        pos = step - carried
        while pos <= seg and len(out) < steps:
            out.append(lerp_point(a, b, pos / seg))
            pos += step
        carried = (carried + seg) % step
    out.append(dense[-1])
    return out


def resample(
    line: Sequence[LinePoint],
    spacing_m: float = 3.0,
    max_gap_m: float = 30.0,
    walk_step_m: float = 0.25,
) -> list[LinePoint]:
    """The whole of step 0 of the note algorithm: subdivide, smooth, space evenly."""
    if len(line) < 3:
        return list(line)
    subdivided = subdivide_long_gaps(line, max_gap_m=max_gap_m)
    dense = densify(subdivided, walk_step_m=walk_step_m)
    return walk_even_spacing(dense, spacing_m=spacing_m)


def spacing_stats(line: Sequence[LinePoint]) -> tuple[float, float, float]:
    """(min, mean, max) spacing. A sanity check on the resampler's own output."""
    if len(line) < 2:
        return (0.0, 0.0, 0.0)
    gaps = [ground_distance(a, b) for a, b in zip(line, line[1:])]
    return (min(gaps), sum(gaps) / len(gaps), max(gaps))
