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
    jump_m: float = 50.0,
) -> list[list[TelemetryFrame]]:
    """Cut a capture into runs of continuous driving.

    A run ends when ``IsRaceOn`` goes false, when the stream stops for
    longer than ``gap_s``, when the car teleports further than ``jump_m``
    between two frames (the game moving it to an event's start line, a
    rewind), or when it enters or leaves an event (``RacePosition`` going
    between 0 and 1-or-more). All of these are session boundaries rather
    than errors: a recording that starts in free roam, drives to a race and
    races it holds two drives, and only one of them is the stage.
    """
    segments: list[list[TelemetryFrame]] = []
    current: list[TelemetryFrame] = []

    def cut() -> None:
        nonlocal current
        if len(current) >= min_points:
            segments.append(current)
        current = []

    for f in frames:
        if not f.race_on:
            cut()
            continue
        if current:
            last = current[-1]
            if (
                f.t - last.t > gap_s
                or math.hypot(f.x - last.x, f.z - last.z) > jump_m
                or (f.race_position >= 1) != (last.race_position >= 1)
            ):
                cut()
        current.append(f)
    cut()
    return segments


def splice_rewinds(
    segments: Sequence[Sequence[TelemetryFrame]],
    max_off_m: float = 30.0,
    max_gap_s: float = 120.0,
) -> list[list[TelemetryFrame]]:
    """Sew a drive back together across rewinds and restarts.

    A rewind cuts a drive in two: the segment before it, and one that
    resumes somewhere the car had already been. The road between the rewind
    target and the cut was driven twice; only the second time counts. So
    when a segment starts on the previous segment's line (within
    ``max_off_m``, both inside an event, within ``max_gap_s``), the previous
    one is cut at that point and the new one continues from there. A
    restart resumes at the start, which replaces the first attempt whole.
    """
    out: list[list[TelemetryFrame]] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if is_event(prev) and is_event(seg) and 0.0 <= seg[0].t - prev[-1].t <= max_gap_s:
                cut = _latest_point_near(prev, seg[0], max_off_m)
                if cut is not None:
                    out[-1] = list(prev[:cut]) + list(seg)
                    continue
        out.append(list(seg))
    return out


def _latest_point_near(frames: Sequence[TelemetryFrame], target: TelemetryFrame, max_off_m: float) -> int | None:
    """Index of the last frame within ``max_off_m`` of ``target``, the most
    recent pass on a circuit, or None."""
    best: int | None = None
    best_d = max_off_m
    for i, f in enumerate(frames):
        d = math.hypot(f.x - target.x, f.z - target.z)
        if d <= best_d:
            best, best_d = i, d
    return best


def is_event(segment: Sequence[TelemetryFrame]) -> bool:
    """True when most of the frames were driven inside an event (a race, a
    stage), not in free roam. The game reports a race position only there."""
    if not segment:
        return False
    return sum(1 for f in segment if f.race_position >= 1) * 2 > len(segment)


def pick_segment(segments: Sequence[Sequence[TelemetryFrame]]) -> int:
    """Which segment is the stage: the longest drive inside an event, or, in
    a recording with no event at all (a free-roam route), the longest drive.
    Never the drive *to* the race just because it took longer."""
    if not segments:
        raise ValueError("no segments to pick from")
    events = [i for i, s in enumerate(segments) if is_event(s)]
    pool = events or list(range(len(segments)))
    return max(pool, key=lambda i: len(segments[i]))


def lap_boundaries(segment: Sequence[TelemetryFrame]) -> list[int]:
    """Frame indices where the game's lap counter went up: the start/finish
    line crossings of a circuit race. Empty for a point-to-point stage."""
    return [i for i in range(1, len(segment)) if segment[i].lap > segment[i - 1].lap]


def first_full_lap(segment: Sequence[TelemetryFrame]) -> tuple[list[TelemetryFrame], int, int]:
    """One lap of a circuit: the frames between the first two line crossings.

    Returns ``(frames, lap_used, laps_seen)``. A recording with fewer than
    two crossings (a point-to-point stage, or a single lap from the grid) is
    returned whole with ``lap_used`` -1. Lap 0 runs from the grid to the
    line and is not a full lap; the first line-to-line lap is.
    """
    crossings = lap_boundaries(segment)
    if len(crossings) < 2:
        return list(segment), -1, len(crossings)
    return list(segment[crossings[0]:crossings[1]]), 1, len(crossings)


def closes_on_itself(line: Sequence[LinePoint], close_m: float = 60.0, min_length_m: float = 300.0) -> bool:
    """Does the line end where it began? Then it is a circuit and the
    co-driver must keep going round instead of falling silent at the seam."""
    if len(line) < 3:
        return False
    return total_length(line) >= min_length_m and ground_distance(line[0], line[-1]) <= close_m


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
