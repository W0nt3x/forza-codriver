"""The note algorithm, step 1, classifying every point by corner severity.

For each point, fit a circle through it and its neighbours ``window_points``
either side (7 points at 3 m spacing is about 20 m each way). The window is
also the smoothing: too small and road noise becomes phantom corners, too
large and real corners get averaged away. It is a tuning parameter and it
lives in the config.

Radius becomes severity through lateral acceleration. Treating 0.3 g as
comfortable cornering, a corner's class is the speed band at which it reaches
0.3 g, from ``a = v^2 / r``. There is **no industry standard** for what 1-6
mean, it varies per driver/co-driver pair, so the bands are config, not
code.

Everything happens in the X/Z plane. Y is altitude and has no business in a
radius.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .line import LinePoint

G = 9.80665

STRAIGHT_SEVERITY = 7
"""One past the last corner class. Straights sort as the least severe thing,
which makes the "more severe than" comparisons in notes.py plain integer
comparisons."""


class Direction(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    STRAIGHT = "straight"


@dataclass(frozen=True, slots=True)
class Marking:
    """A corner class, or the straight sentinel.

    The note algorithm: ``STRAIGHT`` counts as "same direction" only with other
    straights, never with a left or a right. That falls out of comparing the
    direction field directly, as long as nothing special-cases it.
    """

    direction: Direction
    severity: int
    radius_m: float = math.inf

    @property
    def is_corner(self) -> bool:
        return self.direction is not Direction.STRAIGHT

    def same_direction_as(self, other: "Marking") -> bool:
        return self.direction is other.direction

    def more_severe_than(self, other: "Marking") -> bool:
        return self.severity < other.severity

    @property
    def label(self) -> str:
        """Compact form used in stage files and GPX track names: R3, L5, S."""
        if not self.is_corner:
            return "S"
        return f"{'R' if self.direction is Direction.RIGHT else 'L'}{self.severity}"


STRAIGHT = Marking(Direction.STRAIGHT, STRAIGHT_SEVERITY)


def fit_circle(
    prev: LinePoint,
    cur: LinePoint,
    nxt: LinePoint,
    min_divisor: float = 1e-4,
) -> tuple[float, Direction]:
    """Radius and turn direction of the circle through three points.

    Works in local space with ``cur`` at the origin, exactly as the note algorithm
    writes it. A near-zero divisor means the three points are collinear:
    that is a straight, reported as an infinite radius.
    """
    pvx, pvz = prev.x - cur.x, prev.z - cur.z
    nvx, nvz = nxt.x - cur.x, nxt.z - cur.z

    divisor = 2.0 * (pvx * nvz - nvx * pvz)
    if abs(divisor) <= min_divisor:
        return (math.inf, Direction.STRAIGHT)

    pv2 = pvx * pvx + pvz * pvz
    nv2 = nvx * nvx + nvz * nvz
    xc = (pv2 * nvz - nv2 * pvz) / divisor
    zc = (pv2 * nvx - nv2 * pvx) / -divisor
    radius = math.hypot(xc, zc)

    return (radius, Direction.RIGHT if divisor > 0 else Direction.LEFT)


def severity_for_radius(
    radius_m: float,
    class_speed_bands_kmh: Sequence[float],
    comfortable_lateral_g: float = 0.3,
) -> int:
    """Which speed band this radius reaches ``comfortable_lateral_g`` in.

    ``a = v^2 / r`` gives ``v = sqrt(a * r)``; the class is the first band
    that speed falls into. Anything above the last band is a straight.
    """
    if not math.isfinite(radius_m) or radius_m <= 0.0:
        return STRAIGHT_SEVERITY
    speed_kmh = math.sqrt(comfortable_lateral_g * G * radius_m) * 3.6
    for i, upper in enumerate(class_speed_bands_kmh, start=1):
        if speed_kmh <= upper:
            return i
    return STRAIGHT_SEVERITY


def classify(
    line: Sequence[LinePoint],
    window_points: int = 7,
    class_speed_bands_kmh: Sequence[float] = (30, 40, 50, 70, 90, 130),
    comfortable_lateral_g: float = 0.3,
    min_divisor: float = 1e-4,
) -> list[Marking]:
    """Classify every point on a resampled line. One marking per point."""
    n = len(line)
    markings: list[Marking] = []
    for i in range(n):
        lo, hi = i - window_points, i + window_points
        if lo < 0 or hi >= n:
            # The ends have no window. Calling a corner there would be
            # inventing one out of a half-sized sample.
            markings.append(STRAIGHT)
            continue
        radius, direction = fit_circle(line[lo], line[i], line[hi], min_divisor)
        if direction is Direction.STRAIGHT:
            markings.append(STRAIGHT)
            continue
        severity = severity_for_radius(
            radius, class_speed_bands_kmh, comfortable_lateral_g
        )
        if severity >= STRAIGHT_SEVERITY:
            markings.append(STRAIGHT)
        else:
            markings.append(Marking(direction, severity, radius))
    return markings


def direction_agreement(
    line: Sequence[LinePoint],
    markings: Sequence[Marking],
    min_steer: float = 0.08,
) -> tuple[float, int]:
    """Fraction of corners whose classified direction matches recorded steering.

    Whether ``divisor > 0`` means right or left depends on the handedness of
    the coordinate frame, and the note algorithm's formula was written for a different
    one than FH6 might use. Rather than reason about it, check: the recon lap
    recorded which way the wheel was actually turned. Agreement near 1.0 means
    the convention is right, near 0.0 means it is inverted.

    Returns (agreement, samples).
    """
    agree = 0
    total = 0
    for point, marking in zip(line, markings):
        if not marking.is_corner or abs(point.steer) < min_steer:
            continue
        total += 1
        steering_right = point.steer > 0
        if steering_right == (marking.direction is Direction.RIGHT):
            agree += 1
    return (agree / total if total else float("nan"), total)


def invert(markings: Sequence[Marking]) -> list[Marking]:
    """Swap left and right. For when ``direction_agreement`` says we are backwards."""
    flip = {Direction.LEFT: Direction.RIGHT, Direction.RIGHT: Direction.LEFT}
    return [
        Marking(flip[m.direction], m.severity, m.radius_m) if m.is_corner else m
        for m in markings
    ]


def histogram(markings: Sequence[Marking]) -> dict[str, int]:
    """Count of each marking label. A quick smell test on the thresholds."""
    counts: dict[str, int] = {}
    for m in markings:
        counts[m.label] = counts.get(m.label, 0) + 1
    return dict(sorted(counts.items()))
