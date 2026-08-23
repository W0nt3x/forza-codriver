"""Forza Horizon 6 "Data Out" adapter.

**This is the only module in the project that knows what a 324-byte Forza
datagram looks like.** Everything downstream works on ``TelemetryFrame``.

In-game setup: SETTINGS -> HUD AND GAMEPLAY::

    Data Out            : On
    Data Out IP Address : 127.0.0.1
    Data Out IP Port    : 5400   <- NOT 5300, see below

The official docs say to avoid ports 5200-5300 because the game binds its own
outgoing socket in that range. Nearly every tutorial, SimHub guide and GitHub
README online says to use 5300. Do not.

Layout notes that matter
------------------------
* Bytes 0..231 are the classic Forza "Sled" block, byte-identical to FM7 /
  FH4 / FH5.
* FH6 then inserts **12 bytes**, ``CarGroup``, ``SmashableVelDiff``,
  ``SmashableMass``, after ``NumCylinders`` and *before* ``PositionX``.
  Any parser copied from an FM7 or Forza Motorsport layout reads garbage from
  ``PositionX`` onward. This is the single most likely cause of "why are my
  coordinates insane".
* FH6 has no ``TireWear`` and no ``TrackOrdinal``. Do not look for them.
* The fields end at byte 323 and the datagram is 324 bytes: there is one
  trailing pad byte. ``unpack_from`` ignores it, which is why this module
  never slices the buffer.

The offset table below is the single source of truth. The struct format is
*generated* from it, and import fails loudly if the fields are not contiguous
or do not total exactly 323 bytes, so a typo here is an error at startup,
not plausible-looking garbage at 140 km/h.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .base import PacketError, Quad, TelemetryFrame

NAME = "fh6"

PACKET_SIZE = 324
"""Length of the datagram the game sends, including the trailing pad byte."""

WHEELS = ("FL", "FR", "RL", "RR")


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    offset: int
    code: str
    count: int = 1
    unit: str = ""
    note: str = ""

    @property
    def size(self) -> int:
        return self.count * struct.calcsize("<" + self.code)

    @property
    def names(self) -> tuple[str, ...]:
        """Expanded per-element names: quads get FL/FR/RL/RR, triples get XYZ."""
        if self.count == 4:
            return tuple(self.name + w for w in WHEELS)
        if self.count == 3:
            return tuple(self.name + axis for axis in "XYZ")
        if self.count == 1:
            return (self.name,)
        return tuple(f"{self.name}{i}" for i in range(self.count))


F = Field

# --------------------------------------------------------------------------
# The layout, transcribed from the Data Out spec / the official FH6 docs.
# Order matters: the format string is built by walking this in sequence, and
# each entry's declared offset is checked against the running cursor.
# --------------------------------------------------------------------------
LAYOUT: tuple[Field, ...] = (
    # --- classic Forza "Sled" block, bytes 0..231 -------------------------
    F("IsRaceOn", 0, "i", 1, "", "1 while racing, 0 in menus/stopped"),
    F("TimestampMS", 4, "I", 1, "ms", "can overflow to 0"),
    F("EngineMaxRpm", 8, "f", 1, "rpm"),
    F("EngineIdleRpm", 12, "f", 1, "rpm"),
    F("CurrentEngineRpm", 16, "f", 1, "rpm"),
    F("Acceleration", 20, "f", 3, "m/s^2", "car local space; X=right Y=up Z=fwd"),
    F("Velocity", 32, "f", 3, "m/s", "car local space"),
    F("AngularVelocity", 44, "f", 3, "rad/s", "X=pitch Y=yaw Z=roll"),
    F("Yaw", 56, "f", 1, "rad"),
    F("Pitch", 60, "f", 1, "rad"),
    F("Roll", 64, "f", 1, "rad"),
    F("NormalizedSuspensionTravel", 68, "f", 4, "", "0=max stretch 1=max compression"),
    F("TireSlipRatio", 84, "f", 4, "", "0=grip, abs>1 = loss of grip"),
    F("WheelRotationSpeed", 100, "f", 4, "rad/s"),
    F("WheelOnRumbleStrip", 116, "i", 4),
    # The original field table called this S32. The official Forza sled spec calls the same 16
    # bytes f32 ("WheelInPuddleDepth"). Offset and width are identical either
    # way, so nothing downstream shifts, only the interpretation of these
    # bytes. Both readings are decoded; see PUDDLE_F32 below, and the
    # "codriver verify" command, which prints both from a real capture.
    F("WheelInPuddle", 132, "i", 4, "", "see PUDDLE_F32: may really be f32 depth"),
    F("SurfaceRumble", 148, "f", 4),
    F("TireSlipAngle", 164, "f", 4),
    F("TireCombinedSlip", 180, "f", 4),
    F("SuspensionTravelMeters", 196, "f", 4, "m"),
    F("CarOrdinal", 212, "i"),
    F("CarClass", 216, "i", 1, "", "0 (D) .. 7 (X)"),
    F("CarPerformanceIndex", 220, "i", 1, "", "100..999"),
    F("DrivetrainType", 224, "i", 1, "", "0=FWD 1=RWD 2=AWD"),
    F("NumCylinders", 228, "i"),
    # --- FH6-only 12-byte insert. Everything after this is where a parser
    #     copied from FM7 / FH5 goes wrong. ---------------------------------
    F("CarGroup", 232, "I", 1, "", "FH6-only"),
    F("SmashableVelDiff", 236, "f", 1, "m/s", "FH6-only: speed lost in collision"),
    F("SmashableMass", 240, "f", 1, "kg", "FH6-only: mass of object hit"),
    # --- dash block -------------------------------------------------------
    F("PositionX", 244, "f", 1, "m", "world space"),
    F("PositionY", 248, "f", 1, "m", "world space, altitude"),
    F("PositionZ", 252, "f", 1, "m", "world space"),
    F("Speed", 256, "f", 1, "m/s"),
    F("Power", 260, "f", 1, "W"),
    F("Torque", 264, "f", 1, "Nm"),
    F("TireTemp", 268, "f", 4),
    F("Boost", 284, "f", 1, "psi", "above atmospheric"),
    F("Fuel", 288, "f", 1, "", "0=empty 1=full"),
    F("DistanceTraveled", 292, "f", 1, "m"),
    F("BestLap", 296, "f", 1, "s"),
    F("LastLap", 300, "f", 1, "s"),
    F("CurrentLap", 304, "f", 1, "s"),
    F("CurrentRaceTime", 308, "f", 1, "s", "since driving started"),
    F("LapNumber", 312, "H"),
    F("RacePosition", 314, "B"),
    F("Accel", 315, "B", 1, "", "0..255"),
    F("Brake", 316, "B", 1, "", "0..255"),
    F("Clutch", 317, "B", 1, "", "0..255"),
    F("HandBrake", 318, "B", 1, "", "0..255"),
    F("Gear", 319, "B", 1, "", "0=reverse"),
    F("Steer", 320, "b", 1, "", "-127 full left .. 127 full right"),
    F("NormalizedDrivingLine", 321, "b"),
    F("NormalizedAIBrakeDifference", 322, "b"),
)


def _build(layout: Iterable[Field]) -> tuple[str, dict[str, int], int, int]:
    """Generate the struct format from the layout, asserting it is contiguous."""
    parts = ["<"]
    cursor = 0
    index: dict[str, int] = {}
    value_i = 0
    for f in layout:
        if f.offset != cursor:
            raise RuntimeError(
                f"fh6 layout is not contiguous: {f.name} declares offset "
                f"{f.offset} but the previous field ends at {cursor}"
            )
        index[f.name] = value_i
        parts.append(f"{f.count}{f.code}" if f.count > 1 else f.code)
        value_i += f.count
        cursor += f.size
    return "".join(parts), index, cursor, value_i


FORMAT, _IX, PAYLOAD_BYTES, VALUE_COUNT = _build(LAYOUT)
_STRUCT = struct.Struct(FORMAT)
PAD_BYTES = PACKET_SIZE - PAYLOAD_BYTES

# These are import-time errors on purpose. A wrong offset table must fail at
# startup, not produce coordinates that look almost right.
if PAYLOAD_BYTES != 323:
    raise RuntimeError(
        f"fh6 layout totals {PAYLOAD_BYTES} bytes, expected 323 "
        f"(a 324-byte datagram with one trailing pad byte)"
    )
if _STRUCT.size != PAYLOAD_BYTES:
    raise RuntimeError(
        f"generated format {FORMAT!r} is {_STRUCT.size} bytes, "
        f"but the layout says {PAYLOAD_BYTES}"
    )
if PAD_BYTES != 1:
    raise RuntimeError(f"expected exactly 1 trailing pad byte, got {PAD_BYTES}")

PUDDLE_F32 = struct.Struct("<4f")
PUDDLE_OFFSET = 132
"""The same 16 bytes as WheelInPuddle, read as float. See the note in LAYOUT."""

# Field name -> position in the unpacked tuple. Resolved once, at import.
I_RACE_ON = _IX["IsRaceOn"]
I_TIMESTAMP = _IX["TimestampMS"]
I_RPM_MAX = _IX["EngineMaxRpm"]
I_RPM_IDLE = _IX["EngineIdleRpm"]
I_RPM = _IX["CurrentEngineRpm"]
I_VELOCITY = _IX["Velocity"]
I_YAW = _IX["Yaw"]
I_PITCH = _IX["Pitch"]
I_ROLL = _IX["Roll"]
I_SUSP = _IX["NormalizedSuspensionTravel"]
I_SLIP_RATIO = _IX["TireSlipRatio"]
I_ON_RUMBLE = _IX["WheelOnRumbleStrip"]
I_IN_PUDDLE = _IX["WheelInPuddle"]
I_SURFACE_RUMBLE = _IX["SurfaceRumble"]
I_SLIP_ANGLE = _IX["TireSlipAngle"]
I_POS_X = _IX["PositionX"]
I_POS_Y = _IX["PositionY"]
I_POS_Z = _IX["PositionZ"]
I_SPEED = _IX["Speed"]
I_DISTANCE = _IX["DistanceTraveled"]
I_RACE_TIME = _IX["CurrentRaceTime"]
I_LAP = _IX["LapNumber"]
I_RACE_POSITION = _IX["RacePosition"]
I_DRIVING_LINE = _IX["NormalizedDrivingLine"]
I_ACCEL = _IX["Accel"]
I_BRAKE = _IX["Brake"]
I_CLUTCH = _IX["Clutch"]
I_HANDBRAKE = _IX["HandBrake"]
I_GEAR = _IX["Gear"]
I_STEER = _IX["Steer"]


def _quad(values: tuple, start: int) -> Quad:
    return (values[start], values[start + 1], values[start + 2], values[start + 3])


class FH6Adapter:
    """Stateless decoder for FH6 Data Out datagrams.

    Being stateless is what lets one instance serve a live socket, a replay
    and an offline capture decode without knowing which is which.
    """

    name = NAME
    packet_size = PACKET_SIZE

    __slots__ = ("strict", "keep_raw")

    def __init__(self, *, strict: bool = False, keep_raw: bool = False) -> None:
        # strict=True rejects anything that is not exactly PACKET_SIZE bytes.
        # The default accepts any datagram long enough to hold every field, so
        # a capture from a future patch that appends fields still decodes.
        self.strict = strict
        self.keep_raw = keep_raw

    def parse(self, data: bytes, t: float = 0.0) -> TelemetryFrame:
        n = len(data)
        if self.strict and n != PACKET_SIZE:
            raise PacketError(f"expected {PACKET_SIZE} bytes, got {n}")
        if n < PAYLOAD_BYTES:
            raise PacketError(
                f"datagram too short: {n} bytes, need at least {PAYLOAD_BYTES}"
            )

        v = _STRUCT.unpack_from(data, 0)

        return TelemetryFrame(
            t=t,
            t_src=v[I_TIMESTAMP] / 1000.0,
            race_time=v[I_RACE_TIME],
            race_on=bool(v[I_RACE_ON]),
            x=v[I_POS_X],
            y=v[I_POS_Y],
            z=v[I_POS_Z],
            speed=v[I_SPEED],
            yaw=v[I_YAW],
            pitch=v[I_PITCH],
            roll=v[I_ROLL],
            susp=_quad(v, I_SUSP),
            slip_ratio=_quad(v, I_SLIP_RATIO),
            slip_angle=_quad(v, I_SLIP_ANGLE),
            on_rumble=tuple(float(x) for x in _quad(v, I_ON_RUMBLE)),
            in_puddle=PUDDLE_F32.unpack_from(data, PUDDLE_OFFSET),
            surface_rumble=_quad(v, I_SURFACE_RUMBLE),
            rpm=v[I_RPM],
            rpm_idle=v[I_RPM_IDLE],
            rpm_max=v[I_RPM_MAX],
            gear=v[I_GEAR],
            accel=v[I_ACCEL] / 255.0,
            brake=v[I_BRAKE] / 255.0,
            clutch=v[I_CLUTCH] / 255.0,
            handbrake=v[I_HANDBRAKE] / 255.0,
            steer=max(-1.0, min(1.0, v[I_STEER] / 127.0)),
            distance_traveled=v[I_DISTANCE],
            lap=v[I_LAP],
            race_position=int(v[I_RACE_POSITION]),
            driving_line=int(v[I_DRIVING_LINE]),
            raw=self.describe(data) if self.keep_raw else None,
        )

    def describe(self, data: bytes) -> dict[str, Any]:
        """Every native field under its native name. Debugging and NDJSON export."""
        if len(data) < PAYLOAD_BYTES:
            raise PacketError(f"datagram too short: {len(data)} bytes")
        v = _STRUCT.unpack_from(data, 0)
        out: dict[str, Any] = {}
        for f in LAYOUT:
            start = _IX[f.name]
            for i, name in enumerate(f.names):
                out[name] = v[start + i]
        # The disputed bytes, also read as float. See the note in LAYOUT.
        depth = PUDDLE_F32.unpack_from(data, PUDDLE_OFFSET)
        for i, w in enumerate(WHEELS):
            out["WheelInPuddleDepth_f32" + w] = depth[i]
        return out


ALL_FIELD_NAMES: frozenset[str] = frozenset(n for f in LAYOUT for n in f.names)
BASE_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in LAYOUT)


def pack_fields(values: Mapping[str, Any]) -> bytes:
    """Build a valid 324-byte datagram. For tests and synthetic stage generation.

    Unspecified fields are zero. Accepts either the expanded per-element names
    (``PositionX``, ``TireTempFL``) or a base name with a sequence
    (``TireTemp=[1, 2, 3, 4]``).
    """
    unknown = set(values) - ALL_FIELD_NAMES - BASE_FIELD_NAMES
    if unknown:
        raise ValueError(f"unknown fh6 field(s): {sorted(unknown)}")

    flat: list[Any] = []
    for f in LAYOUT:
        if f.count > 1 and f.name in values:
            seq = list(values[f.name])
            if len(seq) != f.count:
                raise ValueError(f"{f.name} needs {f.count} values, got {len(seq)}")
            flat.extend(seq)
            continue
        for name in f.names:
            flat.append(values.get(name, 0.0 if f.code == "f" else 0))
    if len(flat) != VALUE_COUNT:
        raise RuntimeError(f"packed {len(flat)} values, expected {VALUE_COUNT}")
    return _STRUCT.pack(*flat) + b"\x00" * PAD_BYTES


_TYPE_NAMES = {"i": "s32", "I": "u32", "f": "f32", "H": "u16", "B": "u8", "b": "s8"}


def layout_table() -> str:
    """Human-readable rendering of the layout, for the "fields" command."""
    head = f"  {'off':>4}  {'end':>4}  {'type':<7}  {'field':<30}  note"
    rule = f"  {'-' * 4}  {'-' * 4}  {'-' * 7}  {'-' * 30}  {'-' * 42}"
    lines = [
        f"FH6 Data Out, {PACKET_SIZE} byte datagram, little-endian",
        f"  {PAYLOAD_BYTES} bytes of fields + {PAD_BYTES} trailing pad byte",
        f"  struct format: {FORMAT}",
        "",
        head,
        rule,
    ]
    for f in LAYOUT:
        kind = _TYPE_NAMES[f.code]
        type_s = f"{kind}x{f.count}" if f.count > 1 else kind
        note = " ".join(x for x in (f.unit, f.note) if x)
        lines.append(
            f"  {f.offset:>4}  {f.offset + f.size:>4}  {type_s:<7}  "
            f"{f.name:<30}  {note}"
        )
    return "\n".join(lines)
