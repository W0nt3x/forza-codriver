"""Game-agnostic telemetry types.

Nothing in this module knows about Forza. A second adapter (Assetto Corsa
Rally, DiRT Rally 2.0, EA WRC) must be able to produce ``TelemetryFrame``
without a single change anywhere else in the codebase.

Units are fixed and non-negotiable across adapters:

    position    metres, world space, right-handed, **Y is up**
    speed       metres per second
    angles      radians
    time        seconds
    pedals      0.0 .. 1.0
    steer       -1.0 (full left) .. +1.0 (full right)

An adapter that natively reports km/h, feet or degrees converts on the way
out. Downstream code never asks what game it is talking to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

Quad = tuple[float, float, float, float]
"""Per-wheel values, always ordered front-left, front-right, rear-left, rear-right."""

FL, FR, RL, RR = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class TelemetryFrame:
    """One instant of car state, in canonical units.

    ``t`` is the arrival clock and is the only field guaranteed monotonic.
    ``t_src`` comes from the game and may wrap or reset; ``race_time`` is the
    game's own per-session clock. Gap detection uses ``t``, because a paused
    game emits no packet from which to read either of the other two.
    """

    # -- clocks ------------------------------------------------------------
    t: float
    """Seconds since the start of this stream, from packet arrival. Monotonic."""
    t_src: float = 0.0
    """The game's own timestamp, in seconds. May overflow to zero or reset."""
    race_time: float = 0.0
    """Game's seconds-since-driving-started. Zero outside a session."""

    # -- state -------------------------------------------------------------
    race_on: bool = False
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    speed: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    # -- per-wheel ---------------------------------------------------------
    susp: Quad = (0.0, 0.0, 0.0, 0.0)
    """NormalizedSuspensionTravel: 0.0 = max stretch (airborne), 1.0 = max compression."""
    slip_ratio: Quad = (0.0, 0.0, 0.0, 0.0)
    slip_angle: Quad = (0.0, 0.0, 0.0, 0.0)
    on_rumble: Quad = (0.0, 0.0, 0.0, 0.0)
    in_puddle: Quad = (0.0, 0.0, 0.0, 0.0)
    surface_rumble: Quad = (0.0, 0.0, 0.0, 0.0)

    # -- driver / drivetrain ----------------------------------------------
    rpm: float = 0.0
    rpm_idle: float = 0.0
    rpm_max: float = 0.0
    gear: int = 0
    accel: float = 0.0
    brake: float = 0.0
    clutch: float = 0.0
    handbrake: float = 0.0
    steer: float = 0.0

    # -- session -----------------------------------------------------------
    distance_traveled: float = 0.0
    lap: int = 0
    race_position: int = 0
    """The game's race position, 1 and up. Only ever nonzero inside an
    event: free roam and menus report 0. What tells a race from a drive."""
    driving_line: int = 0
    """The racing-line assist's offset, -127..127. Zero outside events."""

    raw: Mapping[str, Any] | None = field(default=None, repr=False)
    """Every field the adapter decoded, under its native name. Debugging only --
    no module outside ``adapters/`` may read this."""

    # -- convenience -------------------------------------------------------

    @property
    def pos(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    @property
    def ground(self) -> tuple[float, float]:
        """X/Z only. The plane every pace-note calculation happens in."""
        return (self.x, self.z)

    @property
    def speed_kmh(self) -> float:
        return self.speed * 3.6

    @property
    def airborne_score(self) -> float:
        """0.0 when any wheel is loaded, 1.0 when all four are fully extended.

        All four at max stretch *simultaneously* means the car is in the
        air. Far more reliable than inferring a jump from altitude, but it
        has to be all four. This keys off the most-loaded wheel (``max``), so
        a single wheel dropping into a rut or cresting a kerb does not read as
        flight. Using ``min`` here would make every pothole a jump.

        Note that a stationary car does not score 0.0: it sits at its static
        ride height, typically 0.3-0.6, so this reads ~0.4-0.7 at rest. Only
        values near 1.0 mean anything. Compare against
        ``stage.hazards.jump_susp_max_stretch``, which is expressed in raw
        suspension travel rather than this score.
        """
        return 1.0 - max(self.susp)

    def ground_distance_to(self, other: "TelemetryFrame") -> float:
        """Plain hypot in the X/Z plane.

        Positions are already world-space metres in a Euclidean frame:
        no projection, no haversine, no geo library. Ever.
        """
        return math.hypot(other.x - self.x, other.z - self.z)


@runtime_checkable
class TelemetryAdapter(Protocol):
    """Decodes one game's datagrams into ``TelemetryFrame``.

    Implementations must be stateless: ``parse`` is a pure function of the
    datagram and the arrival time it is handed. That is what lets the same
    adapter serve a live socket, a replay and an offline capture decode
    without knowing which is which.
    """

    name: str
    packet_size: int
    """Expected datagram length in bytes."""

    def parse(self, data: bytes, t: float) -> TelemetryFrame:
        """Decode one datagram. ``t`` is arrival time in seconds, already
        relative to the start of the stream. Raises ``PacketError`` on a
        datagram this adapter cannot decode."""
        ...

    def describe(self, data: bytes) -> dict[str, Any]:
        """Decode every native field under its native name, for debugging."""
        ...


class PacketError(ValueError):
    """A datagram could not be decoded by the adapter it was handed to."""
