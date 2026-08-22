"""Empirical verification of the packet layout against a real capture.

Rule one: do not trust the offset table blindly and do not copy an
FH5 parser. These are those checks, run against a recording rather than
asserted in the abstract.

The load-bearing one is ``speed_vs_position``. FH6 inserts 12 bytes
(``CarGroup``, ``SmashableVelDiff``, ``SmashableMass``) between
``NumCylinders`` and ``PositionX``. If that insert is missing or misplaced,
every field from ``PositionX`` onward is read from the wrong bytes, and the
symptom is not a crash, it is coordinates that look like plausible floats.
Comparing the distance the car moved against the speed it reported catches
that immediately: two independently-decoded regions of the packet have to
agree about physics, and they only do if both are read correctly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..adapters.base import PacketError, TelemetryFrame
from ..adapters.fh6 import PACKET_SIZE, FH6Adapter
from .capture import CaptureReader

PASS, FAIL, WARN, SKIP, INFO = "PASS", "FAIL", "WARN", "SKIP", "INFO"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in (PASS, SKIP, INFO)


@dataclass
class VerifyReport:
    path: Path
    checks: list[Check] = field(default_factory=list)
    frames: int = 0
    packets: int = 0
    """Datagrams in the file, decodable or not. Zero means the capture is
    empty, which is a completely different problem from a wrong layout."""

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = [f"layout verification, {self.path}", ""]
        for c in self.checks:
            lines.append(f"  [{c.status:<4}] {c.name:<{width}}  {c.detail}")
        lines.append("")
        if self.packets == 0:
            lines.append(
                "  This capture is EMPTY, it contains no datagrams at all, "
                "so nothing here\n  is evidence about the packet layout. Find "
                "the port first:\n\n      python -m codriver scan"
            )
        elif self.failed:
            lines.append(
                f"  {len(self.failed)} FAILED. The offset table in "
                f"adapters/fh6.py does not match this capture."
            )
        elif self.warned:
            lines.append(f"  no failures, {len(self.warned)} warning(s).")
        else:
            lines.append("  all checks passed.")
        return "\n".join(lines)


@dataclass
class VerifyContext:
    """Everything a check may look at. One object instead of seven positional
    arguments, so adding a check is adding a function, not editing a loop."""

    report: VerifyReport
    adapter: FH6Adapter
    frames: list[TelemetryFrame]
    payloads: list[bytes]
    sizes: dict[int, int]
    parse_errors: int
    speed_tolerance: float
    distance_tolerance: float


# --------------------------------------------------------------------------


def _moving_segments(
    frames: Sequence[TelemetryFrame],
    window_s: float,
    min_speed: float,
) -> list[tuple[int, int]]:
    """Index ranges of consecutive race-on driving, each spanning ~window_s."""
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for i, f in enumerate(frames):
        driving = f.race_on and f.speed >= min_speed
        if driving and start is None:
            start = i
        elif not driving and start is not None:
            start = None
            continue
        if start is not None and frames[i].t - frames[start].t >= window_s:
            segments.append((start, i))
            start = i
    return segments


def _longest_run(frames: Sequence[TelemetryFrame], predicate) -> tuple[int, int]:
    best = (0, 0)
    start: int | None = None
    for i, f in enumerate(frames):
        if predicate(f):
            if start is None:
                start = i
        else:
            if start is not None and i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if start is not None and len(frames) - start > best[1] - best[0]:
        best = (start, len(frames))
    return best


def _median(values: list[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


# --------------------------------------------------------------------------


def verify_capture(
    path: Path | str,
    adapter: FH6Adapter | None = None,
    speed_tolerance: float = 0.05,
    distance_tolerance: float = 0.05,
) -> VerifyReport:
    """Run every layout check against a ``.fzr`` capture."""
    path = Path(path)
    adapter = adapter or FH6Adapter()
    report = VerifyReport(path=path)

    sizes: dict[int, int] = {}
    frames: list[TelemetryFrame] = []
    payloads: list[bytes] = []
    parse_errors = 0

    with CaptureReader(path) as reader:
        for t_ns, payload in reader:
            sizes[len(payload)] = sizes.get(len(payload), 0) + 1
            try:
                frames.append(adapter.parse(payload, t_ns / 1e9))
                payloads.append(payload)
            except PacketError:
                parse_errors += 1

    report.frames = len(frames)
    report.packets = len(frames) + parse_errors
    ctx = VerifyContext(
        report=report,
        adapter=adapter,
        frames=frames,
        payloads=payloads,
        sizes=sizes,
        parse_errors=parse_errors,
        speed_tolerance=speed_tolerance,
        distance_tolerance=distance_tolerance,
    )

    # These two run even when nothing decoded, that is precisely the case
    # where the packet-size histogram is the whole diagnosis.
    _check_packet_size(ctx)
    _check_parse(ctx)

    if not frames:
        report.add(
            "physics",
            SKIP,
            "no datagram decoded, so nothing downstream could be checked",
        )
        return report

    for check in (
        _check_positions_finite,
        _check_stationary,
        _check_rpm_band,
        _check_speed_vs_velocity,
        _check_speed_vs_position,
        _check_distance_traveled,
        _check_controls,
        _check_timestamp,
        _check_race_on,
        _check_puddle_interpretation,
    ):
        check(ctx)

    return report


# -- individual checks ------------------------------------------------------
# All take the context, so the runner above stays a plain loop.

def _check_packet_size(ctx: VerifyContext) -> None:
    hist = ", ".join(f"{n} bytes x{c}" for n, c in sorted(ctx.sizes.items()))
    if not ctx.sizes:
        # An empty capture says nothing whatsoever about the packet layout.
        # Blaming the offset table here would send you off fixing the one
        # thing that is not broken.
        ctx.report.add(
            "packet_size",
            FAIL,
            "capture is empty, not one datagram arrived, so the layout was "
            "never tested. Run `codriver scan` to find the port the game is "
            "actually sending to.",
        )
    elif set(ctx.sizes) == {PACKET_SIZE}:
        ctx.report.add("packet_size", PASS, f"every datagram is {PACKET_SIZE} bytes")
    elif PACKET_SIZE in ctx.sizes:
        ctx.report.add(
            "packet_size",
            WARN,
            f"mixed sizes ({hist}), expected only {PACKET_SIZE}",
        )
    else:
        ctx.report.add(
            "packet_size",
            FAIL,
            f"no datagram is {PACKET_SIZE} bytes ({hist}). "
            f"The layout in adapters/fh6.py is for a different game or patch.",
        )


def _check_parse(ctx: VerifyContext) -> None:
    total = len(ctx.frames) + ctx.parse_errors
    if total == 0:
        ctx.report.add("parse", SKIP, "nothing to decode")
    elif ctx.parse_errors == 0:
        ctx.report.add("parse", PASS, f"{len(ctx.frames)} datagrams decoded, 0 rejected")
    else:
        ctx.report.add(
            "parse",
            FAIL if ctx.parse_errors > total * 0.01 else WARN,
            f"{ctx.parse_errors}/{total} datagrams rejected by the adapter",
        )


def _check_positions_finite(ctx: VerifyContext) -> None:
    bad = [
        f
        for f in ctx.frames
        if not all(math.isfinite(v) for v in (f.x, f.y, f.z))
        or max(abs(f.x), abs(f.z)) > 1e6
        or abs(f.y) > 1e5
    ]
    if not bad:
        xs = [f.x for f in ctx.frames]
        ys = [f.y for f in ctx.frames]
        zs = [f.z for f in ctx.frames]
        ctx.report.add(
            "position_range",
            PASS,
            f"X {min(xs):.0f}..{max(xs):.0f}  "
            f"Y {min(ys):.0f}..{max(ys):.0f}  "
            f"Z {min(zs):.0f}..{max(zs):.0f} m",
        )
    else:
        ctx.report.add(
            "position_range",
            FAIL,
            f"{len(bad)} frames have non-finite or absurd coordinates "
            f"(first: {bad[0].pos}). Suspect the 12-byte FH6 insert at 232.",
        )


def _check_stationary(ctx: VerifyContext) -> None:
    lo, hi = _longest_run(ctx.frames, lambda f: f.race_on and f.speed < 0.1)
    n = hi - lo
    if n < 30:
        ctx.report.add(
            "stationary",
            SKIP,
            "no run of 30+ stationary race-on frames in this capture "
            "(sit still for a second before driving off next time)",
        )
        return
    run = ctx.frames[lo:hi]
    spread = max(
        _stdev([f.x for f in run]),
        _stdev([f.y for f in run]),
        _stdev([f.z for f in run]),
    )
    if spread < 0.5:
        ctx.report.add(
            "stationary",
            PASS,
            f"{n} frames at rest, position spread {spread * 100:.1f} cm",
        )
    else:
        ctx.report.add(
            "stationary",
            FAIL,
            f"{n} frames report speed ~0 but position moves "
            f"(spread {spread:.2f} m), Speed and Position disagree",
        )


def _check_rpm_band(ctx: VerifyContext) -> None:
    live = [f for f in ctx.frames if f.race_on and f.rpm_max > 0]
    if not live:
        ctx.report.add("rpm_band", SKIP, "no race-on frames with a nonzero EngineMaxRpm")
        return
    bad = [f for f in live if not (0 <= f.rpm <= f.rpm_max * 1.02)]
    idle = _median([f.rpm_idle for f in live])
    top = _median([f.rpm_max for f in live])
    if not bad and 100 < idle < top < 20000:
        ctx.report.add(
            "rpm_band",
            PASS,
            f"idle {idle:.0f}, max {top:.0f}, current always within range",
        )
    else:
        ctx.report.add(
            "rpm_band",
            FAIL if bad else WARN,
            f"idle {idle:.0f}, max {top:.0f}, {len(bad)} frames out of range",
        )


def _check_speed_vs_velocity(ctx: VerifyContext) -> None:
    """Speed (offset 256) against the local velocity vector (offset 32).

    Two separately-decoded regions of the packet describing the same physical
    quantity. They agree only if both are read from the right bytes.
    """
    adapter = ctx.adapter
    ratios = []
    for f, payload in zip(ctx.frames, ctx.payloads):
        if not (f.race_on and f.speed > 5.0):
            continue
        native = adapter.describe(payload)
        mag = math.sqrt(
            native["VelocityX"] ** 2
            + native["VelocityY"] ** 2
            + native["VelocityZ"] ** 2
        )
        ratios.append(mag / f.speed)
    if len(ratios) < 30:
        ctx.report.add("speed_vs_velocity", SKIP, "not enough frames above 5 m/s")
        return
    ratio = _median(ratios)
    if abs(ratio - 1.0) <= ctx.speed_tolerance:
        ctx.report.add(
            "speed_vs_velocity",
            PASS,
            f"|Velocity| / Speed = {ratio:.4f} over {len(ratios)} frames",
        )
    else:
        ctx.report.add(
            "speed_vs_velocity",
            FAIL,
            f"|Velocity| / Speed = {ratio:.4f}, expected 1.0, "
            f"offset 32 and/or offset 256 are wrong",
        )


def _check_speed_vs_position(ctx: VerifyContext) -> None:
    """The load-bearing check: does the car move as fast as it says it does?

    Path length from PositionX/Y/Z (offsets 244/248/252) against Speed
    (offset 256), over half-second windows to shake off timestamp
    quantisation. This is what catches a missing or misplaced FH6 12-byte
    insert, which no amount of "the floats look plausible" will.
    """
    segments = _moving_segments(ctx.frames, window_s=0.5, min_speed=5.0)
    ratios = []
    for lo, hi in segments:
        dist = 0.0
        for i in range(lo + 1, hi + 1):
            a, b = ctx.frames[i - 1], ctx.frames[i]
            dist += math.sqrt(
                (b.x - a.x) ** 2 + (b.y - a.y) ** 2 + (b.z - a.z) ** 2
            )
        elapsed = ctx.frames[hi].t - ctx.frames[lo].t
        mean_speed = sum(f.speed for f in ctx.frames[lo : hi + 1]) / (hi - lo + 1)
        if elapsed <= 0 or mean_speed <= 0:
            continue
        ratios.append((dist / elapsed) / mean_speed)

    if len(ratios) < 5:
        ctx.report.add(
            "speed_vs_position",
            SKIP,
            "not enough sustained driving above 5 m/s to measure",
        )
        return
    ratio = _median(ratios)
    if abs(ratio - 1.0) <= ctx.speed_tolerance:
        ctx.report.add(
            "speed_vs_position",
            PASS,
            f"d(position)/dt / Speed = {ratio:.4f} over "
            f"{len(ratios)} half-second windows",
        )
    else:
        ctx.report.add(
            "speed_vs_position",
            FAIL,
            f"d(position)/dt / Speed = {ratio:.4f}, expected 1.0. "
            f"PositionX/Y/Z are almost certainly not at 244/248/252, "
            f"check the FH6 12-byte insert at offset 232.",
        )


def _check_distance_traveled(ctx: VerifyContext) -> None:
    """Is DistanceTraveled *proportional* to the distance actually covered?

    Proportionality is the question, not equality. A misaligned field produces
    a ratio that scatters wildly frame to frame; a correctly located field
    that simply counts in its own units produces a tight one. Only the first
    is a layout bug.

    Measured on a real FH6 capture: the ratio is a tight ~0.79, so the field
    is exactly where the table says it is and is not a metric odometer.
    The runtime already avoids it as a primary signal --
    treat it as not being a distance at all.
    """
    driving = [f for f in ctx.frames if f.race_on]
    if len(driving) < 60:
        ctx.report.add("distance_traveled", SKIP, "fewer than 60 race-on frames")
        return

    ratios: list[float] = []
    reported = 0.0
    measured = 0.0
    for a, b in zip(driving, driving[1:]):
        delta = b.distance_traveled - a.distance_traveled
        moved = math.sqrt((b.x - a.x) ** 2 + (b.y - a.y) ** 2 + (b.z - a.z) ** 2)
        gap = b.t - a.t
        # Skip stream gaps, session resets, and frames too slow to measure.
        if gap <= 0 or gap > 0.5 or delta < 0 or delta > 200 or moved < 0.05:
            continue
        reported += delta
        measured += moved
        if delta > 1e-4:
            ratios.append(delta / moved)

    if reported < 50 or len(ratios) < 30:
        ctx.report.add(
            "distance_traveled",
            SKIP,
            f"only {reported:.0f} m of usable driving in this capture",
        )
        return

    ratios.sort()
    n = len(ratios)
    median = _median(ratios)
    p25, p75 = ratios[n // 4], ratios[(3 * n) // 4]
    spread = (p75 - p25) / median if median > 0 else float("inf")
    common = (
        f"reported {reported:.0f} vs measured {measured:.0f} m, "
        f"ratio {median:.3f} (spread {spread * 100:.1f}%)"
    )

    if spread > 0.25:
        ctx.report.add(
            "distance_traveled",
            FAIL,
            f"{common}, the ratio scatters instead of holding steady, "
            f"which is what a misaligned field looks like. Suspect offset 292.",
        )
    elif abs(median - 1.0) <= ctx.distance_tolerance:
        ctx.report.add("distance_traveled", PASS, f"{common}, agrees with position")
    else:
        ctx.report.add(
            "distance_traveled",
            INFO,
            f"{common}, holds a constant ratio, so offset 292 is right, but "
            f"the field is not metres: it counts {median:.3f} per metre "
            f"actually driven. Do not use it as a distance.",
        )


def _check_controls(ctx: VerifyContext) -> None:
    driving = [f for f in ctx.frames if f.race_on]
    if not driving:
        ctx.report.add("controls", SKIP, "no race-on frames")
        return
    gears = {f.gear for f in driving}
    max_accel = max(f.accel for f in driving)
    max_brake = max(f.brake for f in driving)
    steer_lo = min(f.steer for f in driving)
    steer_hi = max(f.steer for f in driving)

    problems = []
    if not gears <= set(range(0, 12)):
        problems.append(f"implausible gears {sorted(gears)}")
    if max_accel < 0.5:
        problems.append(f"throttle never exceeded {max_accel:.2f}")
    if not (-1.0 <= steer_lo <= steer_hi <= 1.0):
        problems.append(f"steer out of range {steer_lo:.2f}..{steer_hi:.2f}")

    detail = (
        f"gears {sorted(gears)}, throttle max {max_accel:.2f}, "
        f"brake max {max_brake:.2f}, steer {steer_lo:+.2f}..{steer_hi:+.2f}"
    )
    if problems:
        ctx.report.add("controls", WARN, detail + ", " + "; ".join(problems))
    else:
        ctx.report.add("controls", PASS, detail)


def _check_timestamp(ctx: VerifyContext) -> None:
    backwards = sum(1 for a, b in zip(ctx.frames, ctx.frames[1:]) if b.t_src < a.t_src)
    span = ctx.frames[-1].t_src - ctx.frames[0].t_src
    wall = ctx.frames[-1].t - ctx.frames[0].t
    if backwards == 0 and wall > 0 and abs(span / wall - 1.0) < 0.05:
        ctx.report.add(
            "timestamp",
            PASS,
            f"TimestampMS advanced {span:.1f} s over {wall:.1f} s of capture",
        )
    elif backwards:
        ctx.report.add(
            "timestamp",
            INFO,
            f"TimestampMS went backwards {backwards}x "
            f"(overflow or session restart, expected; nothing paces from it)",
        )
    else:
        ctx.report.add(
            "timestamp",
            WARN,
            f"TimestampMS advanced {span:.1f} s over {wall:.1f} s of capture",
        )


def _check_race_on(ctx: VerifyContext) -> None:
    on = sum(1 for f in ctx.frames if f.race_on)
    transitions = sum(
        1 for a, b in zip(ctx.frames, ctx.frames[1:]) if a.race_on != b.race_on
    )
    if on == 0:
        ctx.report.add(
            "race_on",
            WARN,
            "IsRaceOn was never 1, was the capture taken in a menu?",
        )
    else:
        ctx.report.add(
            "race_on",
            PASS,
            f"{on}/{len(ctx.frames)} frames race-on, {transitions} transition(s)",
        )


def _check_puddle_interpretation(ctx: VerifyContext) -> None:
    """Settle whether bytes 132..147 are s32 flags or f32 depths.

    The original field table called them S32; the official Forza sled spec calls them f32
    ("WheelInPuddleDepth"). Same offset, same width, so nothing downstream
    shifts either way, but one reading is nonsense, and driving through
    water once makes it obvious which.
    """
    adapter = ctx.adapter
    int_values: set[int] = set()
    float_max = 0.0
    float_weird = 0
    for payload in ctx.payloads:
        native = adapter.describe(payload)
        for w in ("FL", "FR", "RL", "RR"):
            iv = native["WheelInPuddle" + w]
            fv = native["WheelInPuddleDepth_f32" + w]
            int_values.add(iv)
            if math.isfinite(fv):
                float_max = max(float_max, fv)
                if fv != 0.0 and not (1e-6 < abs(fv) < 100.0):
                    float_weird += 1

    if int_values <= {0, 1}:
        verdict = (
            "s32 reading is 0/1 only, consistent with the original S32 flag "
            "reading, but also with f32 zeros. Drive through water to settle it."
        )
    elif float_weird == 0 and 0.0 < float_max <= 10.0:
        verdict = (
            f"f32 reading is a sane depth (max {float_max:.3f}); the s32 "
            f"reading gives large integers. Bytes 132..147 are f32 depth, "
            f"the original S32 type was wrong (offset unaffected)."
        )
    else:
        verdict = (
            f"inconclusive: s32 values {sorted(int_values)[:6]}, "
            f"f32 max {float_max:.4g}"
        )
    ctx.report.add("puddle_bytes", INFO, verdict)
