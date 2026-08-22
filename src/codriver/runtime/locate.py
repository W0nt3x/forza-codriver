"""The runtime design, localising the car on the recorded stage line.

The live stream only says where the car *is*. This module answers where that
is *on the stage*, as an index into the resampled line plus a distance along
it, which is the coordinate every note is positioned in.

The one rule that matters: **never trust a bare nearest-neighbour query.**
The recorded stage crosses itself, doubles back through switchbacks and runs
parallel to itself; the globally nearest point is routinely the wrong lap of
a hairpin. So the search is constrained to a window around the last confirmed
index, and a global search happens only in two situations: when we have never
been localised, and when confidence has verifiably collapsed.

States:

    COLD       never localised (or deliberately reset) -> global search
    TRACKING   windowed search around the last confirmed index
    LOST       too far from the line for too long      -> global re-acquire
    SUSPENDED  the stream stopped (pause, rewind, finish line)

The stream gaps are handled here too, because they are a localisation
concern: the game sends nothing during pauses and rewinds, and
what happens *after* a gap, same place (unpause) or somewhere else entirely
(rewind/restart), decides whether the note queue survives.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import numpy as np

from ..stage.line import LinePoint


class TrackState(str, Enum):
    COLD = "cold"
    TRACKING = "tracking"
    LOST = "lost"
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class Fix:
    """One localisation result."""

    state: TrackState
    index: int = -1
    """Index into the stage line of the matched segment start. -1 = no fix."""
    along_m: float = 0.0
    """Distance along the stage, including projection within the segment.
    This is the coordinate notes are positioned in."""
    off_line_m: float = 0.0
    """Lateral distance from the stage line. Confidence, in metres."""
    resumed_from_gap: bool = False
    """True on the first fix after a stream gap."""
    jumped: bool = False
    """True when the car teleported (rewind/restart) and the queue must be
    rebuilt rather than resumed."""

    @property
    def ok(self) -> bool:
        return self.state is TrackState.TRACKING


class StageIndex:
    """The stage line as arrays, with windowed and global nearest-point search.

    A KD-tree is deliberately absent. The windowed search, the one that runs
    every packet, scans a few hundred points with numpy, which is both
    faster than a tree query at this size and immune to the tree's one real
    failure mode here: happily returning the wrong arm of a switchback. The
    global search scans everything, and runs only on acquire/re-acquire.
    """

    def __init__(self, line: Sequence[LinePoint], cumulative: Sequence[float]):
        if len(line) < 2:
            raise ValueError("a stage line needs at least two points")
        self.x = np.array([p.x for p in line])
        self.z = np.array([p.z for p in line])
        self.cumulative = np.asarray(cumulative, dtype=float)
        self.n = len(line)
        # Segment vectors, for projecting the car onto the line between points.
        self.seg_dx = np.diff(self.x)
        self.seg_dz = np.diff(self.z)
        self.seg_len2 = self.seg_dx**2 + self.seg_dz**2
        self.length_m = float(self.cumulative[-1])

    def nearest(self, x: float, z: float, lo: int = 0, hi: int | None = None) -> tuple[int, float]:
        """(index, distance) of the nearest line point within [lo, hi)."""
        hi = self.n if hi is None else min(hi, self.n)
        lo = max(0, lo)
        d2 = (self.x[lo:hi] - x) ** 2 + (self.z[lo:hi] - z) ** 2
        i = int(np.argmin(d2))
        return lo + i, float(math.sqrt(d2[i]))

    def project(self, x: float, z: float, index: int) -> tuple[float, float]:
        """Project onto the line near ``index``: (along_m, off_line_m).

        Snapping to the nearest 3 m point alone quantises the car's position;
        at 40 m/s that is 75 ms of trigger jitter. Projecting onto the two
        segments either side of the matched point removes it.
        """
        best_along = float(self.cumulative[index])
        best_off = math.hypot(x - self.x[index], z - self.z[index])
        for seg in (index - 1, index):
            if not 0 <= seg < self.n - 1 or self.seg_len2[seg] <= 0:
                continue
            t = ((x - self.x[seg]) * self.seg_dx[seg] + (z - self.z[seg]) * self.seg_dz[seg]) / self.seg_len2[seg]
            t = min(1.0, max(0.0, t))
            px = self.x[seg] + t * self.seg_dx[seg]
            pz = self.z[seg] + t * self.seg_dz[seg]
            off = math.hypot(x - px, z - pz)
            if off < best_off:
                best_off = off
                seg_len = math.sqrt(self.seg_len2[seg])
                best_along = float(self.cumulative[seg]) + t * seg_len
        return best_along, best_off


@dataclass
class Locator:
    """Stateful tracker: feed it positions, get ``Fix`` back.

    All thresholds are read at call time from the attributes, so a config
    hot-reload takes effect by assigning to them, no rebuild.
    """

    index: StageIndex
    search_back_points: int = 50
    search_forward_points: int = 300
    lost_distance_m: float = 25.0
    lost_after_packets: int = 30
    suspend_after_s: float = 0.5
    rewind_jump_m: float = 50.0

    state: TrackState = TrackState.COLD
    last_index: int = -1
    _bad_streak: int = field(default=0, repr=False)
    _last_t: float = field(default=float("nan"), repr=False)
    _last_x: float = field(default=0.0, repr=False)
    _last_z: float = field(default=0.0, repr=False)

    def reset(self) -> None:
        self.state = TrackState.COLD
        self.last_index = -1
        self._bad_streak = 0
        self._last_t = float("nan")

    def update(self, x: float, z: float, t: float) -> Fix:
        resumed = False
        jumped = False

        if not math.isnan(self._last_t):
            gap = t - self._last_t
            if gap > self.suspend_after_s:
                # The game went silent: pause, rewind, or the finish line.
                # We only find out which now that it is talking again.
                resumed = True
                if math.hypot(x - self._last_x, z - self._last_z) > self.rewind_jump_m:
                    # Rewind or restart: the car is somewhere else. Nothing
                    # about the previous fix can be trusted.
                    jumped = True
                    self.state = TrackState.COLD
                    self.last_index = -1
            elif math.hypot(x - self._last_x, z - self._last_z) > self.rewind_jump_m:
                # Teleport with no gap in the stream. In-game rewind can look
                # like this if it was shorter than the gap threshold.
                jumped = True
                self.state = TrackState.COLD
                self.last_index = -1

        self._last_t = t
        self._last_x = x
        self._last_z = z

        if self.state in (TrackState.COLD, TrackState.LOST):
            fix = self._acquire(x, z)
        else:
            fix = self._track(x, z)

        return Fix(
            state=fix.state,
            index=fix.index,
            along_m=fix.along_m,
            off_line_m=fix.off_line_m,
            resumed_from_gap=resumed,
            jumped=jumped,
        )

    # -- internals ---------------------------------------------------------

    def _acquire(self, x: float, z: float) -> Fix:
        index, dist = self.index.nearest(x, z)
        if dist > self.lost_distance_m:
            # Off the stage entirely (driving to the start line, wrong road).
            self.state = TrackState.LOST if self.last_index >= 0 else TrackState.COLD
            return Fix(state=self.state, off_line_m=dist)
        self.state = TrackState.TRACKING
        self.last_index = index
        self._bad_streak = 0
        along, off = self.index.project(x, z, index)
        return Fix(TrackState.TRACKING, index, along, off)

    def _track(self, x: float, z: float) -> Fix:
        lo = self.last_index - self.search_back_points
        hi = self.last_index + self.search_forward_points
        index, dist = self.index.nearest(x, z, lo, hi)

        if dist > self.lost_distance_m:
            self._bad_streak += 1
            if self._bad_streak >= self.lost_after_packets:
                # Confidence has verifiably collapsed; only now is a global
                # search allowed.
                self.state = TrackState.LOST
                return self._acquire(x, z)
            # Keep the previous fix and say how bad things look; a couple of
            # wide moments (a cut, a spin) should not throw tracking away.
            along = float(self.index.cumulative[self.last_index])
            return Fix(TrackState.TRACKING, self.last_index, along, dist)

        self._bad_streak = 0
        self.last_index = index
        along, off = self.index.project(x, z, index)
        return Fix(TrackState.TRACKING, index, along, off)
