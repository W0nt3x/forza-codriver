"""Synthetic captures.

A generated ``.fzr`` that looks like a real drive: correct packet layout,
plausible physics, a stationary start, a mid-stage pause where the stream
stops entirely, and a jump where all four wheels unload.

Two uses. It lets the whole capture/replay toolchain be exercised before the game is
ever launched, and it gives the layout checks in ``verify.py`` something with
a known-correct answer, including a deliberately *wrong* variant, so we can
prove the checks would actually catch a bad offset table rather than assuming
they would.

This is not a substitute for a real capture. Nothing here can tell you what
FH6 actually puts on the wire; only a recording can.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterator

from ..adapters.fh6 import pack_fields

Point = tuple[float, float]


def _circle(size: float) -> tuple[Callable[[float], Point], Callable[[float], Point]]:
    return (
        lambda u: (size * math.cos(u), size * math.sin(u)),
        lambda u: (-size * math.sin(u), size * math.cos(u)),
    )


def _figure8(size: float) -> tuple[Callable[[float], Point], Callable[[float], Point]]:
    # Lemniscate of Gerono: a genuine self-crossing, which is exactly the
    # case that breaks an unconstrained nearest-neighbour search at runtime.
    return (
        lambda u: (size * math.sin(u), 0.5 * size * math.sin(2 * u)),
        lambda u: (size * math.cos(u), size * math.cos(2 * u)),
    )


def _slalom(size: float) -> tuple[Callable[[float], Point], Callable[[float], Point]]:
    return (
        lambda u: (0.35 * size * math.sin(3 * u), size * u),
        lambda u: (1.05 * size * math.cos(3 * u), size),
    )


SHAPES = {"circle": _circle, "figure8": _figure8, "slalom": _slalom}


@dataclass
class SynthSpec:
    shape: str = "figure8"
    duration_s: float = 90.0
    rate_hz: float = 60.0
    speed_mps: float = 25.0
    size_m: float = 250.0
    origin: Point = (-4210.0, 6180.0)
    """Somewhere plausibly far from zero, so a parser bug cannot hide behind
    small numbers."""
    base_altitude_m: float = 120.0
    relief_m: float = 9.0
    stationary_s: float = 3.0
    pause_at_s: float | None = 40.0
    pause_len_s: float = 2.5
    jump_at_s: float | None = 25.0
    jump_len_s: float = 0.5


def synth_records(spec: SynthSpec | None = None) -> list[tuple[int, bytes]]:
    """Generate ``(t_ns, payload)`` records exactly as a capture would hold them."""
    spec = spec or SynthSpec()
    if spec.shape not in SHAPES:
        raise ValueError(f"unknown shape {spec.shape!r}; try {sorted(SHAPES)}")
    pos_fn, tangent_fn = SHAPES[spec.shape](spec.size_m)

    dt = 1.0 / spec.rate_hz
    step_m = spec.speed_mps * dt
    ox, oz = spec.origin

    records: list[tuple[int, bytes]] = []
    u = 0.0
    distance = 0.0
    paused_s = 0.0
    t = 0.0
    prev_yaw: float | None = None
    prev: tuple[float, float, float] | None = None

    while t < spec.duration_s:
        in_pause = (
            spec.pause_at_s is not None
            and spec.pause_at_s <= t < spec.pause_at_s + spec.pause_len_s
        )
        if in_pause:
            # The game sends nothing at all while paused. Wall clock keeps
            # running; the game's own clocks do not.
            paused_s += dt
            t += dt
            continue

        moving = t >= spec.stationary_s
        speed = spec.speed_mps if moving else 0.0

        if moving:
            tx, tz = tangent_fn(u)
            tangent_len = math.hypot(tx, tz) or 1.0
            u += step_m / tangent_len

        px, pz = pos_fn(u)
        x = ox + px
        z = oz + pz
        y = spec.base_altitude_m + spec.relief_m * math.sin(0.7 * u)

        if prev is not None:
            distance += math.dist((x, y, z), prev)
        prev = (x, y, z)

        tx, tz = tangent_fn(u)
        yaw = math.atan2(tx, tz)

        # Steering has to agree with which way the car is actually turning,
        # or the orientation check in stage/build.py has nothing real to test
        # itself against. Yaw rate is the honest source: at constant speed,
        # steering angle tracks it.
        d_yaw = 0.0 if prev_yaw is None else (yaw - prev_yaw + math.pi) % (
            2 * math.pi
        ) - math.pi
        prev_yaw = yaw
        steer = max(-127, min(127, int(d_yaw * 12000))) if moving else 0

        airborne = (
            spec.jump_at_s is not None
            and spec.jump_at_s <= t < spec.jump_at_s + spec.jump_len_s
        )
        susp = 0.03 if airborne else 0.48

        game_t = t - paused_s
        payload = pack_fields(
            {
                "IsRaceOn": 1,
                "TimestampMS": int(game_t * 1000),
                "EngineMaxRpm": 7200.0,
                "EngineIdleRpm": 850.0,
                "CurrentEngineRpm": 850.0 + (speed / 60.0) * 5500.0,
                # Car-local: X right, Y up, Z forward. Straight ahead at speed.
                "Velocity": [0.0, 0.0, speed],
                "Acceleration": [0.0, -9.81 if airborne else 0.0, 0.0],
                "Yaw": yaw,
                "Pitch": 0.0,
                "Roll": 0.0,
                "NormalizedSuspensionTravel": [susp] * 4,
                "SuspensionTravelMeters": [susp * 0.2] * 4,
                "CarOrdinal": 2942,
                "CarClass": 4,
                "CarPerformanceIndex": 750,
                "DrivetrainType": 2,
                "NumCylinders": 4,
                "CarGroup": 11,
                "PositionX": x,
                "PositionY": y,
                "PositionZ": z,
                "Speed": speed,
                "Power": 180000.0 if moving else 0.0,
                "Torque": 420.0 if moving else 0.0,
                "TireTemp": [78.0] * 4,
                "Fuel": 0.8,
                "DistanceTraveled": distance,
                "CurrentLap": game_t,
                "CurrentRaceTime": game_t,
                "LapNumber": 1,
                "RacePosition": 1,
                "Accel": 210 if moving else 0,
                "Brake": 0,
                "Gear": 4 if moving else 1,
                "Steer": steer,
            }
        )
        records.append((int(t * 1e9), payload))
        t += dt

    return records


def as_fh5_layout(payload: bytes) -> bytes:
    """Re-cut a correct FH6 datagram as if the 12-byte insert did not exist.

    This is what a parser copied from FM7 or FH5 effectively sees: everything
    from ``PositionX`` on, read 12 bytes early. Used to prove the layout
    checks actually catch the failure they were written for.
    """
    return payload[:232] + payload[244:] + b"\x00" * 12


def write_synth(
    path,
    spec: SynthSpec | None = None,
    note: str = "synthetic, not a real capture",
) -> int:
    """Write a synthetic capture to ``path``. Returns the packet count."""
    from .capture import CaptureWriter

    spec = spec or SynthSpec()
    records = synth_records(spec)
    header = {
        "adapter": "fh6",
        "expected_packet_size": 324,
        "synthetic": True,
        "shape": spec.shape,
        "note": note,
    }
    with CaptureWriter(path, header=header) as writer:
        for t_ns, payload in records:
            writer.add(payload, t_ns)
    return len(records)


def iter_shapes() -> Iterator[str]:
    return iter(sorted(SHAPES))
