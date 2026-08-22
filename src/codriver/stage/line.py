"""Turning a recon recording into a raw driving line.

A capture is not a stage. It contains menu frames, stream gaps where the game
stopped sending, a stationary wait at the start, and possibly several runs.
This module cuts it into contiguous driving segments and reduces each to a
sequence of positions with just enough telemetry attached for the hazard
detection in the note algorithm to work later.

Everything downstream measures distance in the **X/Z ground plane**
(``hypot(dx, dz)``), per the coordinate notes. Y is altitude: carried along, used for
crests and dips, never part of a distance or a radius.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..adapters.base import TelemetryFrame


@dataclass(frozen=True, slots=True)
class LinePoint:
    """One point on a stage line, with the telemetry worth keeping.

    The extra fields are what let the note algorithm detect jumps and surface changes
    empirically from the recon lap instead of guessing them from geometry.
    """

    x: float
    y: float
    z: float
    t: float = 0.0
    speed: float = 0.0
    """Speed recorded on the recon lap. Not a target speed, just what the
    recon driver happened to be doing."""
    susp_max: float = 1.0
    """Highest of the four NormalizedSuspensionTravel values. Near 0 means
    every wheel is extended, which means airborne."""
    steer: float = 0.0
    wet_wheels: int = 0
    """How many of the four wheels the game reports as in water (0..4). Any
    nonzero reading on a wheel counts, whichever way the game encodes the
    field, so a ford shows up here whether the packet says "flag" or
    "depth"."""
    surface_rumble: float = 0.0

    @property
    def ground(self) -> tuple[float, float]:
        return (self.x, self.z)


def ground_distance(a: LinePoint, b: LinePoint) -> float:
    return math.hypot(b.x - a.x, b.z - a.z)


def cumulative_distance(line: Sequence[LinePoint]) -> list[float]:
    """Distance along the line at each point, starting at 0."""
    out = [0.0]
    for a, b in zip(line, line[1:]):
        out.append(out[-1] + ground_distance(a, b))
    return out


def total_length(line: Sequence[LinePoint]) -> float:
    return cumulative_distance(line)[-1] if len(line) > 1 else 0.0


def frame_to_point(f: TelemetryFrame) -> LinePoint:
    return LinePoint(
        x=f.x,
        y=f.y,
        z=f.z,
        t=f.t,
        speed=f.speed,
        susp_max=max(f.susp),
        steer=f.steer,
        wet_wheels=sum(1 for v in f.in_puddle if v != 0.0),
        surface_rumble=max(f.surface_rumble),
    )


def split_segments(
    frames: Sequence[TelemetryFrame],
    gap_s: float = 0.5,
    min_points: int = 60,
) -> list[list[TelemetryFrame]]:
    """Cut a capture into runs of continuous driving.

    A run ends when ``IsRaceOn`` goes false or when the stream stops for
    longer than ``gap_s``. Both are meaningful: the game sends nothing during
    menus, pauses, rewinds and after the finish line, so a gap is
    a session boundary rather than an error.
    """
    segments: list[list[TelemetryFrame]] = []
    current: list[TelemetryFrame] = []
    for f in frames:
        if not f.race_on:
            if len(current) >= min_points:
                segments.append(current)
            current = []
            continue
        if current and f.t - current[-1].t > gap_s:
            if len(current) >= min_points:
                segments.append(current)
            current = []
        current.append(f)
    if len(current) >= min_points:
        segments.append(current)
    return segments


def to_line(
    frames: Iterable[TelemetryFrame],
    min_step_m: float = 0.25,
) -> list[LinePoint]:
    """Reduce frames to positions, dropping ones the car did not move between.

    At 60 Hz a stationary car emits hundreds of identical positions. Left in,
    they make the circle fit in ``curvature.py`` degenerate and the resampler
    do a lot of work to produce nothing.
    """
    line: list[LinePoint] = []
    for f in frames:
        point = frame_to_point(f)
        if line and ground_distance(line[-1], point) < min_step_m:
            continue
        line.append(point)
    return line


def longest_segment(
    frames: Sequence[TelemetryFrame],
    gap_s: float = 0.5,
    min_points: int = 60,
) -> list[TelemetryFrame]:
    """The longest continuous run of driving in a capture.

    The usual case: you drove to the start, recorded the stage, and stopped.
    Use ``split_segments`` directly when a capture holds several runs.
    """
    segments = split_segments(frames, gap_s=gap_s, min_points=min_points)
    if not segments:
        return []
    return max(segments, key=len)


def trim_stationary(line: Sequence[LinePoint], speed_threshold: float = 1.0) -> list[LinePoint]:
    """Drop the waiting-at-the-line and rolling-to-a-stop ends of a run."""
    lo, hi = 0, len(line)
    while lo < hi and line[lo].speed < speed_threshold:
        lo += 1
    while hi > lo and line[hi - 1].speed < speed_threshold:
        hi -= 1
    return list(line[lo:hi])
