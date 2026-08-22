"""Learning from drives: many runs in, one better stage out.

A single recon lap is one driver's one line on one day. Every later drive
of the same stage is recorded by ``codriver run`` for free, and this module
folds those runs back in:

* **The line** becomes the median of where the car actually went, recon
  plus every run, point by point along the stage. Recon wobble averages
  out; the line converges on the road, or at least on the driver's habitual
  line, which is what pace notes should describe anyway.
* **Each corner remembers the slowest speed driven through it**, median
  over runs. That is information the geometry cannot produce: it folds in
  grip, car, camber, visibility and nerve. It is printed next to the note,
  not spoken, a "3" stays a "3"; you just get to see that you take your
  3s at ~95 km/h in this car.

Everything is localised with the same ``Locator`` the runtime uses, so a run
that cut a corner by 40 m is rejected the same way it would be live, and a
run of a different stage tracks nothing and is skipped outright.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Sequence

import numpy as np

from ..config import Config
from ..runtime.locate import Locator, StageIndex
from .build import BuildReport, frames_from_capture, stage_from_line
from .line import LinePoint, cumulative_distance
from .schema import Stage

log = logging.getLogger(__name__)


@dataclass
class LearnReport:
    runs_used: list[str] = field(default_factory=list)
    runs_skipped: list[str] = field(default_factory=list)
    samples: int = 0
    points_shifted: int = 0
    mean_abs_shift_m: float = 0.0
    max_shift_m: float = 0.0
    notes_with_speed: int = 0
    build: BuildReport | None = None

    def render(self) -> str:
        out = [
            f"  runs            {len(self.runs_used)} used, {len(self.runs_skipped)} skipped",
            f"  samples         {self.samples} localised frames",
            f"  line shift      {self.points_shifted} points moved, "
            f"mean {self.mean_abs_shift_m:.2f} m, max {self.max_shift_m:.2f} m",
            f"  observed speed  on {self.notes_with_speed} notes",
        ]
        for r in self.runs_skipped:
            out.append(f"  SKIPPED         {r}")
        if self.build is not None:
            out.append("  -- rebuilt --")
            out.append(self.build.render())
        return "\n".join(out)


def runs_for_stage(stage: Stage, runs_dir: Path | str) -> list[Path]:
    """Every ``<stage>_*.fzr`` that ``codriver run`` has saved, oldest first."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted(runs_dir.glob(f"{stage.name}_*.fzr"))


def _normals(line: Sequence[LinePoint]) -> np.ndarray:
    """Unit left-normal at every point, from the central-difference tangent."""
    x = np.array([p.x for p in line])
    z = np.array([p.z for p in line])
    tx = np.gradient(x)
    tz = np.gradient(z)
    length = np.hypot(tx, tz)
    length[length == 0] = 1.0
    return np.column_stack([-tz / length, tx / length])


def _collect(
    index: StageIndex,
    normals: np.ndarray,
    frames,
    locator_kwargs: dict,
    max_shift_m: float,
) -> tuple[list[list[float]], list[list[float]], int, int]:
    """Localise one run. Returns per-point offsets, per-point speeds, tracked, total."""
    n = index.n
    offsets: list[list[float]] = [[] for _ in range(n)]
    speeds: list[list[float]] = [[] for _ in range(n)]
    locator = Locator(index, **locator_kwargs)
    tracked = total = 0
    for f in frames:
        if not f.race_on:
            continue
        total += 1
        fix = locator.update(f.x, f.z, f.t)
        if not fix.ok or fix.index < 0:
            continue
        i = fix.index
        nx, nz = normals[i]
        signed = (f.x - index.x[i]) * nx + (f.z - index.z[i]) * nz
        if abs(signed) > max_shift_m:
            continue  # a cut or a spin is not where the road is
        tracked += 1
        offsets[i].append(float(signed))
        speeds[i].append(float(f.speed))
    return offsets, speeds, tracked, total


def _smooth(values: np.ndarray, points: int) -> np.ndarray:
    if points <= 1:
        return values
    kernel = np.ones(points) / points
    return np.convolve(values, kernel, mode="same")


def learn_stage(
    stage: Stage,
    cfg: Config,
    run_paths: Sequence[Path | str],
) -> tuple[Stage, LearnReport]:
    report = LearnReport()
    if len(stage.line) < 3:
        raise ValueError("stage has no usable line")

    min_samples = cfg.get("stage.learn.min_samples_per_point")
    smooth_points = cfg.get("stage.learn.smooth_points")
    max_shift = cfg.get("stage.learn.max_shift_m")
    min_tracked = cfg.get("stage.learn.min_tracked_fraction")
    locator_kwargs = dict(
        search_back_points=cfg.get("runtime.locate.search_back_points"),
        search_forward_points=cfg.get("runtime.locate.search_forward_points"),
        lost_distance_m=cfg.get("runtime.locate.lost_distance_m"),
        lost_after_packets=cfg.get("runtime.locate.lost_after_packets"),
        suspend_after_s=cfg.get("runtime.gaps.suspend_after_s"),
        rewind_jump_m=cfg.get("runtime.gaps.rewind_jump_m"),
    )

    line = stage.line
    n = len(line)
    index = StageIndex(line, cumulative_distance(line))
    normals = _normals(line)

    # The current line is itself a sample: offset 0, with the recon speeds.
    all_offsets: list[list[float]] = [[0.0] for _ in range(n)]
    all_speeds: list[list[float]] = [[p.speed] for p in line]

    for path in run_paths:
        path = Path(path)
        frames = frames_from_capture(path, cfg.get("telemetry.adapter"))
        offsets, speeds, tracked, total = _collect(
            index, normals, frames, locator_kwargs, max_shift
        )
        if total == 0 or tracked / total < min_tracked:
            report.runs_skipped.append(
                f"{path.name}: tracked {tracked}/{total} frames"
            )
            continue
        report.runs_used.append(path.name)
        report.samples += tracked
        for i in range(n):
            all_offsets[i].extend(offsets[i])
            all_speeds[i].extend(speeds[i])

    # Median lateral correction where there is enough evidence, smoothed so
    # a single wide moment does not kink the road, clamped to what a road
    # can plausibly be off by.
    shift = np.zeros(n)
    for i in range(n):
        if len(all_offsets[i]) >= min_samples:
            shift[i] = median(all_offsets[i])
    shift = np.clip(_smooth(shift, smooth_points), -max_shift, max_shift)
    moved = np.abs(shift) > 0.05
    report.points_shifted = int(moved.sum())
    report.mean_abs_shift_m = float(np.abs(shift[moved]).mean()) if moved.any() else 0.0
    report.max_shift_m = float(np.abs(shift).max()) if n else 0.0

    new_line = [
        LinePoint(
            x=p.x + shift[i] * normals[i][0],
            y=p.y,
            z=p.z + shift[i] * normals[i][1],
            t=p.t,
            speed=median(all_speeds[i]) if all_speeds[i] else p.speed,
            susp_max=p.susp_max,
            steer=p.steer,
            in_puddle=p.in_puddle,
            surface_rumble=p.surface_rumble,
        )
        for i, p in enumerate(line)
    ]

    learned, build = stage_from_line(
        new_line,
        cfg,
        name=stage.name,
        source=stage.source,
        generator={
            **{k: v for k, v in stage.generator.items() if k != "generated_utc"},
            "learned_from_runs": list(stage.generator.get("learned_from_runs", []))
            + report.runs_used,
            "learn_samples": int(stage.generator.get("learn_samples", 0)) + report.samples,
        },
    )
    report.build = build

    # Slowest speed through each corner, from the (now median) line speeds.
    spacing = learned.spacing_m or 3.0
    for note in learned.notes:
        if note.kind != "corner" or not note.length_m:
            continue
        span = max(1, int(round(note.length_m / spacing)))
        lo = min(note.index, len(learned.line) - 1)
        hi = min(lo + span + 1, len(learned.line))
        slowest = min(p.speed for p in learned.line[lo:hi])
        if slowest > 0.5:
            note.observed_kmh = round(slowest * 3.6)
            report.notes_with_speed += 1

    return learned, report
