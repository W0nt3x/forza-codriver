"""GPX export, for eyeballing a stage in gpsvisualizer.com.

The coordinate notes is emphatic that no geo library belongs in this project: the game
gives world-space metres, and every pace-note calculation stays in metres.
This module is the one exception it explicitly allows, an *export* to GPX
for debugging is fine and useful.

So the projection here is a debug fiction. The stage is dropped near (0, 0)
with a single metres-per-degree constant on both axes, which keeps the shape
undistorted and puts the track in the Gulf of Guinea. Nothing reads this file
back in. Do not be tempted to make it round-trip.

One track per corner class, so gpsvisualizer colours them separately: you can
see at a glance whether your severity bands are calling half the stage a 4.
Notes come out as waypoints labelled with the text that would be spoken.
"""

from __future__ import annotations

from typing import Sequence
from xml.sax.saxutils import escape

from .curvature import Marking
from .line import LinePoint
from .schema import Stage

METRES_PER_DEGREE = 111320.0
"""Same constant on both axes. Not accurate; undistorted, which is what
matters when the only question is whether the line looks like the road."""


def to_latlon(
    point: LinePoint,
    origin_x: float,
    origin_z: float,
    lat0: float = 0.0,
    lon0: float = 0.0,
) -> tuple[float, float]:
    return (
        lat0 + (point.z - origin_z) / METRES_PER_DEGREE,
        lon0 + (point.x - origin_x) / METRES_PER_DEGREE,
    )


def _runs(markings: Sequence[Marking]) -> dict[str, list[tuple[int, int]]]:
    """Contiguous index ranges per marking label."""
    out: dict[str, list[tuple[int, int]]] = {}
    if not markings:
        return out
    start = 0
    for i in range(1, len(markings) + 1):
        ended = i == len(markings) or markings[i].label != markings[start].label
        if ended:
            out.setdefault(markings[start].label, []).append((start, i - 1))
            start = i
    return out


def to_gpx(
    stage: Stage,
    lat0: float = 0.0,
    lon0: float = 0.0,
    by_class: bool = True,
) -> str:
    """Render a stage as GPX. ``by_class`` splits the line into one track per
    corner class so they come out in different colours."""
    if not stage.line:
        raise ValueError("stage has no line to export")

    origin_x = stage.line[0].x
    origin_z = stage.line[0].z

    def pt(i: int, tag: str) -> str:
        lat, lon = to_latlon(stage.line[i], origin_x, origin_z, lat0, lon0)
        return (
            f'    <{tag} lat="{lat:.7f}" lon="{lon:.7f}">'
            f"<ele>{stage.line[i].y:.1f}</ele></{tag}>"
        )

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="codriver" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{escape(stage.name)}</name>",
        f"    <desc>{stage.length_km:.2f} km, {len(stage.notes)} notes. "
        f"Projection is a debug fiction: local metres dropped near "
        f"(0,0).</desc>",
        "  </metadata>",
    ]

    for note in stage.notes:
        i = min(note.index, len(stage.line) - 1)
        lat, lon = to_latlon(stage.line[i], origin_x, origin_z, lat0, lon0)
        out.append(f'  <wpt lat="{lat:.7f}" lon="{lon:.7f}">')
        out.append(f"    <ele>{stage.line[i].y:.1f}</ele>")
        out.append(f"    <name>{escape(note.text)}</name>")
        out.append(
            f"    <desc>{note.at_m / 1000:.3f} km, {escape(note.kind)}"
            + (f", r={note.radius_m:.0f} m" if note.radius_m else "")
            + "</desc>"
        )
        out.append("  </wpt>")

    if by_class and stage.markings:
        for label, runs in sorted(_runs(stage.markings).items()):
            out.append("  <trk>")
            out.append(f"    <name>{escape(label)}</name>")
            for lo, hi in runs:
                out.append("    <trkseg>")
                # Overlap by one point so the coloured segments visually join
                # instead of showing a gap at every class change.
                for i in range(lo, min(hi + 2, len(stage.line))):
                    out.append(pt(i, "trkpt"))
                out.append("    </trkseg>")
            out.append("  </trk>")
    else:
        out.append("  <trk>")
        out.append(f"    <name>{escape(stage.name)}</name>")
        out.append("    <trkseg>")
        for i in range(len(stage.line)):
            out.append(pt(i, "trkpt"))
        out.append("    </trkseg>")
        out.append("  </trk>")

    out.append("</gpx>")
    return "\n".join(out) + "\n"
