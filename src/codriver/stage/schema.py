"""The stage file format.

JSON: the resampled line, the generated notes, and enough
metadata to know where it came from and how it was made, source recording
hash, generator version, and the config snapshot in force at the time.

Notes are positioned by **distance along the stage** and carry their token
list, so the file stays hand-editable. That is a supported workflow, not a
concession: automatic generation gets you 80% of the way, and the last 20% is
a human fixing three corners. Nothing in this format requires regenerating
from the recording to change a note.

The format is game-agnostic. Nothing here knows what FH6 is.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .curvature import STRAIGHT, Direction, Marking
from .line import LinePoint
from .notes import Note

STAGE_FORMAT = "codriver-stage"
STAGE_VERSION = 1


class StageError(Exception):
    pass


@dataclass
class Stage:
    name: str
    line: list[LinePoint] = field(default_factory=list)
    markings: list[Marking] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    spacing_m: float = 3.0
    length_m: float = 0.0
    source: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    generator: dict[str, Any] = field(default_factory=dict)

    @property
    def length_km(self) -> float:
        return self.length_m / 1000.0


def _marking_from_label(label: str) -> Marking:
    if label == "S":
        return STRAIGHT
    direction = Direction.RIGHT if label[0] == "R" else Direction.LEFT
    return Marking(direction, int(label[1:]))


def file_digest(path: Path | str) -> str:
    """SHA-256 of a capture, so a stage can name the recording it came from."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_dict(stage: Stage) -> dict[str, Any]:
    return {
        "format": STAGE_FORMAT,
        "version": STAGE_VERSION,
        "name": stage.name,
        "generator": {
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **stage.generator,
        },
        "source": stage.source,
        "config": stage.config,
        "length_m": round(stage.length_m, 2),
        "spacing_m": stage.spacing_m,
        "points": len(stage.line),
        "notes": [
            {
                "at_m": round(n.at_m, 2),
                "tokens": n.tokens,
                "kind": n.kind,
                **({"direction": n.direction} if n.direction else {}),
                **({"severity": n.severity} if n.severity is not None else {}),
                **({"radius_m": n.radius_m} if n.radius_m is not None else {}),
                **({"length_m": n.length_m} if n.length_m else {}),
                **({"observed_kmh": n.observed_kmh} if n.observed_kmh is not None else {}),
                **({"parts": n.parts} if n.parts else {}),
                "index": n.index,
            }
            for n in stage.notes
        ],
        # Kept after the notes: the notes are what you read and edit, the line
        # is bulk data.
        "line": [[round(p.x, 2), round(p.y, 2), round(p.z, 2)] for p in stage.line],
        "recon_speed_kmh": [round(p.speed * 3.6) for p in stage.line],
        # Per-point telemetry that the hazard detection and the orientation
        # check need again when a stage is rebuilt from its own line, which
        # is what Learn does. Without it, jumps and water vanished the first
        # time Learn ran.
        "telemetry": {
            "steer": [round(p.steer, 2) for p in stage.line],
            "susp_max": [round(p.susp_max, 2) for p in stage.line],
            "wet_wheels": [p.wet_wheels for p in stage.line],
        },
        "markings": [m.label for m in stage.markings],
    }


def from_dict(data: dict[str, Any]) -> Stage:
    if data.get("format") != STAGE_FORMAT:
        raise StageError(f"not a stage file: format={data.get('format')!r}")
    if data.get("version") != STAGE_VERSION:
        raise StageError(
            f"stage file is version {data.get('version')}, "
            f"this build reads {STAGE_VERSION}"
        )

    speeds = data.get("recon_speed_kmh") or []
    telemetry = data.get("telemetry") or {}
    steer = telemetry.get("steer") or []
    susp_max = telemetry.get("susp_max") or []
    wet_wheels = telemetry.get("wet_wheels") or []

    def at(values: list, i: int, default: float) -> float:
        # Older stage files carry none of these; they load as they always did.
        return values[i] if i < len(values) else default

    line = [
        LinePoint(
            x=xyz[0],
            y=xyz[1],
            z=xyz[2],
            speed=at(speeds, i, 0.0) / 3.6,
            steer=float(at(steer, i, 0.0)),
            susp_max=float(at(susp_max, i, 1.0)),
            wet_wheels=int(at(wet_wheels, i, 0)),
        )
        for i, xyz in enumerate(data.get("line", []))
    ]
    notes = [
        Note(
            at_m=n["at_m"],
            tokens=list(n["tokens"]),
            index=n.get("index", 0),
            kind=n.get("kind", "corner"),
            direction=n.get("direction"),
            severity=n.get("severity"),
            radius_m=n.get("radius_m"),
            parts=n.get("parts", []),
            length_m=n.get("length_m"),
            observed_kmh=n.get("observed_kmh"),
        )
        for n in data.get("notes", [])
    ]
    return Stage(
        name=data.get("name", "unnamed"),
        line=line,
        markings=[_marking_from_label(m) for m in data.get("markings", [])],
        notes=notes,
        spacing_m=data.get("spacing_m", 3.0),
        length_m=data.get("length_m", 0.0),
        source=data.get("source", {}),
        config=data.get("config", {}),
        generator=data.get("generator", {}),
    )


def save(stage: Stage, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(to_dict(stage), fh, indent=1)
        fh.write("\n")
    return path


def load(path: Path | str) -> Stage:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return from_dict(json.load(fh))
    except json.JSONDecodeError as exc:
        raise StageError(f"{path} is not valid JSON: {exc}") from exc


def render_notes(stage: Stage, width: int = 46) -> str:
    """The stage as a co-driver would read it. The acceptance test for a build.

    Reading this out loud against a replay of the recon lap tells you more
    about whether the thresholds are right than any amount of staring at the
    geometry.
    """
    lines = [
        f"{stage.name}, {stage.length_km:.2f} km, "
        f"{len(stage.notes)} notes, {len(stage.line)} points "
        f"at {stage.spacing_m:.1f} m",
        "",
    ]
    previous = 0.0
    for n in stage.notes:
        gap = n.at_m - previous
        previous = n.at_m
        seen = f"  ~{n.observed_kmh:3.0f} km/h" if n.observed_kmh is not None else ""
        lines.append(f"  {n.at_m / 1000:6.3f} km  +{gap:6.1f} m   {n.text:<{width}}{seen}")
    return "\n".join(lines)
