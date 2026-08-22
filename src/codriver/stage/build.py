"""Capture in, stage out.

Wires the note algorithm together: pick the driving out of a recording, resample it,
classify it, reduce it to notes. Every threshold comes from the config, this
module holds no numbers of its own.

Split in two on purpose: ``build_stage`` turns a capture into a raw line,
``stage_from_line`` turns any raw line into a stage. ``codriver learn`` uses
the second half on a line averaged over several drives.

It also checks one thing the geometry cannot check for itself. The note algorithm's
circle fit says "Right if divisor > 0", which is only true for one handedness
of the coordinate frame, and nothing guarantees FH6 uses the same one the
formula was written for. Rather than reason about it, we compare the
classified direction against the steering input the recon lap actually
recorded, and say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..adapters import get_adapter
from ..adapters.base import PacketError, TelemetryFrame
from ..config import Config
from ..record.capture import CaptureReader
from . import curvature, line as line_mod, notes as notes_mod, resample as resample_mod
from .line import LinePoint
from .schema import Stage, file_digest

log = logging.getLogger(__name__)


@dataclass
class BuildReport:
    """What the build did, for the human watching it happen."""

    segments_found: int = 0
    segment_used: int = 0
    frames_used: int = 0
    raw_points: int = 0
    raw_length_m: float = 0.0
    resampled_points: int = 0
    spacing_min: float = 0.0
    spacing_mean: float = 0.0
    spacing_max: float = 0.0
    length_m: float = 0.0
    direction_agreement: float = float("nan")
    direction_samples: int = 0
    inverted: bool = False
    markings: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    notes: int = 0
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = []
        if self.segments_found:
            out.append(
                f"  segments        {self.segments_found} found, using "
                f"#{self.segment_used} ({self.frames_used} frames)"
            )
        out += [
            f"  raw line        {self.raw_points} points, {self.raw_length_m:.0f} m",
            f"  resampled       {self.resampled_points} points, "
            f"spacing {self.spacing_min:.2f}/{self.spacing_mean:.2f}/"
            f"{self.spacing_max:.2f} m (min/mean/max)",
            f"  length          {self.length_m / 1000:.2f} km",
        ]
        if self.direction_samples:
            out.append(
                f"  orientation     {self.direction_agreement * 100:.0f}% of "
                f"{self.direction_samples} corners agree with recorded steering"
                + ("  [INVERTED to match]" if self.inverted else "")
            )
        counts = ", ".join(f"{k} {v}" for k, v in self.markings.items())
        out.append(f"  classification  {counts}")
        out.append(f"  notes           {self.notes} (from {self.candidates} corners)")
        for w in self.warnings:
            out.append(f"  WARNING         {w}")
        return "\n".join(out)


def frames_from_capture(path: Path | str, adapter_name: str = "fh6") -> list[TelemetryFrame]:
    adapter = get_adapter(adapter_name)
    out: list[TelemetryFrame] = []
    with CaptureReader(Path(path)) as reader:
        for t_ns, payload in reader:
            try:
                out.append(adapter.parse(payload, t_ns / 1e9))
            except PacketError:
                continue
    return out


def _stage_config_snapshot(cfg: Config) -> dict[str, Any]:
    """The config that produced this stage, stored with it.

    Without it you cannot tell whether a stage you built last week disagrees
    with today's because you changed the algorithm or because you changed a
    threshold.
    """
    return {"stage": cfg.section("stage")}


def stage_from_line(
    raw: Sequence[LinePoint],
    cfg: Config,
    name: str,
    source: dict[str, Any] | None = None,
    generator: dict[str, Any] | None = None,
    report: BuildReport | None = None,
) -> tuple[Stage, BuildReport]:
    """Resample, classify and reduce an already-extracted raw line."""
    report = report or BuildReport()
    report.raw_points = len(raw)
    report.raw_length_m = line_mod.total_length(raw)
    if len(raw) < 3:
        raise ValueError("not enough movement in this line to build a stage")

    spacing_m = cfg.get("stage.resample.spacing_m")
    resampled = resample_mod.resample(
        raw,
        spacing_m=spacing_m,
        max_gap_m=cfg.get("stage.resample.max_gap_before_subdivide_m"),
        walk_step_m=cfg.get("stage.resample.walk_step_m"),
    )
    report.resampled_points = len(resampled)
    (
        report.spacing_min,
        report.spacing_mean,
        report.spacing_max,
    ) = resample_mod.spacing_stats(resampled)

    window = cfg.get("stage.curvature.window_points")
    markings = curvature.classify(
        resampled,
        window_points=window,
        class_speed_bands_kmh=cfg.get("stage.curvature.class_speed_bands_kmh"),
        comfortable_lateral_g=cfg.get("stage.curvature.comfortable_lateral_g"),
        min_divisor=cfg.get("stage.curvature.min_divisor"),
    )

    agreement, samples = curvature.direction_agreement(resampled, markings)
    report.direction_agreement = agreement
    report.direction_samples = samples
    if cfg.get("stage.curvature.auto_orient") and samples >= 20:
        # Only act on a convincing answer. Agreement near 0.5 is a coin flip,
        # and flipping left and right on a coin flip is far worse than leaving
        # them alone and saying so.
        if agreement < 0.3:
            markings = curvature.invert(markings)
            report.inverted = True
            report.direction_agreement = 1.0 - agreement
            log.warning(
                "left/right were inverted for this coordinate frame "
                "(%.0f%% agreement with recorded steering); flipped them",
                agreement * 100,
            )
        elif agreement < 0.7:
            report.warnings.append(
                f"orientation is inconclusive: only {agreement * 100:.0f}% of "
                f"{samples} corners agree with recorded steering, where a "
                f"clear answer is above 70% or below 30%. Left and right were "
                f"left as the geometry called them, check them against the "
                f"GPX before trusting the notes."
            )

    report.markings = curvature.histogram(markings)
    cumulative = line_mod.cumulative_distance(resampled)
    report.length_m = cumulative[-1] if cumulative else 0.0
    report.candidates = len(
        notes_mod.reduce_candidates(
            markings, cfg.get("stage.notes.collapse_window_points")
        )
    )

    generated = notes_mod.generate(
        resampled,
        markings,
        cumulative,
        collapse_window_points=cfg.get("stage.notes.collapse_window_points"),
        tightens_min_run_points=cfg.get("stage.notes.tightens_min_run_points"),
        tightens_max_severity=cfg.get("stage.notes.tightens_max_severity"),
        link_into_max_m=cfg.get("stage.notes.link_into_max_m"),
        link_and_max_m=cfg.get("stage.notes.link_and_max_m"),
        max_linked_notes=cfg.get("stage.notes.max_linked_notes"),
        distance_call_min_m=cfg.get("stage.notes.distance_call_min_m"),
        distance_buckets_m=cfg.get("stage.notes.distance_buckets_m"),
        long_min_m=cfg.get("stage.notes.long_min_m"),
        window_points=window,
        jump_susp_max_stretch=cfg.get("stage.hazards.jump_susp_max_stretch"),
        jump_min_duration_s=cfg.get("stage.hazards.jump_min_duration_s"),
        crest_gradient=cfg.get("stage.hazards.crest_gradient"),
        dip_gradient=cfg.get("stage.hazards.dip_gradient"),
        water_min_wheels=cfg.get("stage.hazards.water_min_wheels"),
        water_min_length_m=cfg.get("stage.hazards.water_min_length_m"),
        water_merge_gap_m=cfg.get("stage.hazards.water_merge_gap_m"),
    )
    report.notes = len(generated)

    if report.notes == 0:
        report.warnings.append(
            "no notes generated. Either the stage really is a straight line, "
            "or stage.curvature.class_speed_bands_kmh is calling everything a "
            "straight."
        )

    gen = {"direction_inverted": report.inverted}
    if generator:
        gen.update(generator)
    stage = Stage(
        name=name,
        line=resampled,
        markings=markings,
        notes=generated,
        spacing_m=spacing_m,
        length_m=report.length_m,
        source=dict(source or {}),
        config=_stage_config_snapshot(cfg),
        generator=gen,
    )
    return stage, report


def build_stage(
    capture_path: Path | str,
    cfg: Config,
    name: str | None = None,
    segment_index: int | None = None,
) -> tuple[Stage, BuildReport]:
    capture_path = Path(capture_path)
    report = BuildReport()

    frames = frames_from_capture(capture_path, cfg.get("telemetry.adapter"))
    if not frames:
        raise ValueError(f"{capture_path} contains no decodable telemetry")

    segments = line_mod.split_segments(
        frames,
        gap_s=cfg.get("runtime.gaps.suspend_after_s"),
        min_points=cfg.get("stage.line.min_segment_frames"),
    )
    if not segments:
        raise ValueError(
            f"{capture_path} contains no continuous driving. Was the car "
            f"moving with IsRaceOn set?"
        )
    report.segments_found = len(segments)

    if segment_index is None:
        chosen = max(range(len(segments)), key=lambda i: len(segments[i]))
    else:
        if not 0 <= segment_index < len(segments):
            raise ValueError(
                f"segment {segment_index} does not exist; "
                f"the capture has {len(segments)}"
            )
        chosen = segment_index
    report.segment_used = chosen
    report.frames_used = len(segments[chosen])

    raw = line_mod.to_line(
        segments[chosen], min_step_m=cfg.get("stage.line.min_step_m")
    )
    raw = line_mod.trim_stationary(
        raw, speed_threshold=cfg.get("stage.line.trim_below_kmh") / 3.6
    )
    if len(raw) < 3:
        raise ValueError("not enough movement in this capture to build a stage")

    return stage_from_line(
        raw,
        cfg,
        name=name or capture_path.stem,
        source={
            "capture": capture_path.name,
            "sha256": file_digest(capture_path),
            "frames": len(frames),
            "segment": chosen,
            "segments_in_capture": len(segments),
        },
        report=report,
    )
