"""Packet layout tests.

The offset table in ``adapters/fh6.py`` is re-transcribed here, independently,
straight from the Data Out spec. That duplication is the point: these tests
compare two separate readings of the spec, so a typo in the adapter is caught
by a disagreement rather than by both copies being wrong together.

``test_every_field_reads_from_its_documented_offset`` writes a distinct
sentinel value at each documented byte offset and asserts the parser reads
each one back under the right name. A one-field shift anywhere makes it fail
and names the field that moved.
"""

from __future__ import annotations

import struct

import pytest

from codriver.adapters.base import PacketError
from codriver.adapters.fh6 import (
    FORMAT,
    LAYOUT,
    PACKET_SIZE,
    PAD_BYTES,
    PAYLOAD_BYTES,
    FH6Adapter,
    Field,
    _build,
    pack_fields,
)

# --------------------------------------------------------------------------
# Transcribed by hand from the Data Out spec. Do not generate this from
# LAYOUT, an independent copy is the whole value of the file.
#   (offset, struct code, element count, field name)
# --------------------------------------------------------------------------
DOC_LAYOUT: list[tuple[int, str, int, str]] = [
    (0, "i", 1, "IsRaceOn"),
    (4, "I", 1, "TimestampMS"),
    (8, "f", 1, "EngineMaxRpm"),
    (12, "f", 1, "EngineIdleRpm"),
    (16, "f", 1, "CurrentEngineRpm"),
    (20, "f", 3, "Acceleration"),
    (32, "f", 3, "Velocity"),
    (44, "f", 3, "AngularVelocity"),
    (56, "f", 1, "Yaw"),
    (60, "f", 1, "Pitch"),
    (64, "f", 1, "Roll"),
    (68, "f", 4, "NormalizedSuspensionTravel"),
    (84, "f", 4, "TireSlipRatio"),
    (100, "f", 4, "WheelRotationSpeed"),
    (116, "i", 4, "WheelOnRumbleStrip"),
    (132, "i", 4, "WheelInPuddle"),
    (148, "f", 4, "SurfaceRumble"),
    (164, "f", 4, "TireSlipAngle"),
    (180, "f", 4, "TireCombinedSlip"),
    (196, "f", 4, "SuspensionTravelMeters"),
    (212, "i", 1, "CarOrdinal"),
    (216, "i", 1, "CarClass"),
    (220, "i", 1, "CarPerformanceIndex"),
    (224, "i", 1, "DrivetrainType"),
    (228, "i", 1, "NumCylinders"),
    # The FH6-only 12-byte insert.
    (232, "I", 1, "CarGroup"),
    (236, "f", 1, "SmashableVelDiff"),
    (240, "f", 1, "SmashableMass"),
    (244, "f", 1, "PositionX"),
    (248, "f", 1, "PositionY"),
    (252, "f", 1, "PositionZ"),
    (256, "f", 1, "Speed"),
    (260, "f", 1, "Power"),
    (264, "f", 1, "Torque"),
    (268, "f", 4, "TireTemp"),
    (284, "f", 1, "Boost"),
    (288, "f", 1, "Fuel"),
    (292, "f", 1, "DistanceTraveled"),
    (296, "f", 1, "BestLap"),
    (300, "f", 1, "LastLap"),
    (304, "f", 1, "CurrentLap"),
    (308, "f", 1, "CurrentRaceTime"),
    (312, "H", 1, "LapNumber"),
    (314, "B", 1, "RacePosition"),
    (315, "B", 1, "Accel"),
    (316, "B", 1, "Brake"),
    (317, "B", 1, "Clutch"),
    (318, "B", 1, "HandBrake"),
    (319, "B", 1, "Gear"),
    (320, "b", 1, "Steer"),
    (321, "b", 1, "NormalizedDrivingLine"),
    (322, "b", 1, "NormalizedAIBrakeDifference"),
]

WHEELS = ("FL", "FR", "RL", "RR")


def _expanded_names(name: str, count: int) -> list[str]:
    if count == 4:
        return [name + w for w in WHEELS]
    if count == 3:
        return [name + a for a in "XYZ"]
    return [name]


def _sentinel(offset: int, code: str) -> int | float:
    """A value unique to this byte offset, exactly representable in its type."""
    if code == "f":
        return offset + 0.5  # exact in float32 for offsets under 2**23
    if code == "b":
        return (offset % 101) - 50
    if code == "B":
        return offset % 251
    return offset + 7


# --------------------------------------------------------------------------
# size and shape
# --------------------------------------------------------------------------


def test_packet_is_324_bytes_with_one_pad_byte():
    assert PACKET_SIZE == 324
    assert PAYLOAD_BYTES == 323
    assert PAD_BYTES == 1
    assert struct.calcsize(FORMAT) == 323


def test_format_is_explicitly_little_endian_and_unaligned():
    # Without the leading "<", struct inserts native alignment padding and
    # every offset past the first mixed-width field silently shifts.
    assert FORMAT.startswith("<")


def test_adapter_layout_matches_the_document():
    ours = [(f.offset, f.code, f.count, f.name) for f in LAYOUT]
    assert ours == DOC_LAYOUT


def test_doc_layout_is_itself_contiguous_and_totals_323():
    cursor = 0
    for offset, code, count, name in DOC_LAYOUT:
        assert offset == cursor, f"{name} starts at {offset}, expected {cursor}"
        cursor += count * struct.calcsize("<" + code)
    assert cursor == 323


# --------------------------------------------------------------------------
# the offsets themselves
# --------------------------------------------------------------------------


def test_every_field_reads_from_its_documented_offset():
    buf = bytearray(PACKET_SIZE)
    expected: dict[str, int | float] = {}
    for offset, code, count, name in DOC_LAYOUT:
        width = struct.calcsize("<" + code)
        for i, element in enumerate(_expanded_names(name, count)):
            at = offset + i * width
            value = _sentinel(at, code)
            struct.pack_into("<" + code, buf, at, value)
            expected[element] = value

    got = FH6Adapter().describe(bytes(buf))

    mismatched = {
        name: (want, got[name])
        for name, want in expected.items()
        if got[name] != pytest.approx(want)
    }
    assert not mismatched, f"fields read from the wrong offset: {mismatched}"


def test_fh6_insert_sits_between_numcylinders_and_positionx():
    """The single most likely parser bug, as its own test.

    A layout copied from FM7 or FH5 has PositionX at 232. If it ever lands
    there again, this fails before any coordinate does.
    """
    by_name = {f.name: f for f in LAYOUT}
    assert by_name["NumCylinders"].offset == 228
    assert by_name["CarGroup"].offset == 232
    assert by_name["SmashableVelDiff"].offset == 236
    assert by_name["SmashableMass"].offset == 240
    assert by_name["PositionX"].offset == 244, "PositionX must NOT be at 232"


def test_sled_block_ends_at_232():
    # Bytes 0..231 are byte-identical to FM7 / FH4 / FH5.
    sled = [f for f in LAYOUT if f.offset < 232]
    assert sled[-1].offset + sled[-1].size == 232


def test_fields_absent_from_fh6_are_absent_here():
    # These exist in Forza Motorsport's Dash format, not in FH6.
    names = {f.name for f in LAYOUT}
    assert "TireWear" not in names
    assert "TrackOrdinal" not in names


def test_noncontiguous_layout_is_rejected_at_build_time():
    broken = (
        Field("A", 0, "i"),
        Field("B", 8, "i"),  # 4-byte hole
    )
    with pytest.raises(RuntimeError, match="not contiguous"):
        _build(broken)


# --------------------------------------------------------------------------
# parse semantics
# --------------------------------------------------------------------------


def test_parse_maps_fields_into_canonical_units():
    payload = pack_fields(
        {
            "IsRaceOn": 1,
            "TimestampMS": 1_234_567,
            "CurrentEngineRpm": 4200.0,
            "EngineIdleRpm": 800.0,
            "EngineMaxRpm": 7000.0,
            "PositionX": -4210.25,
            "PositionY": 118.5,
            "PositionZ": 6180.75,
            "Speed": 32.5,
            "Yaw": 1.25,
            "NormalizedSuspensionTravel": [0.1, 0.2, 0.3, 0.4],
            "DistanceTraveled": 1234.5,
            "CurrentRaceTime": 61.25,
            "Accel": 255,
            "Brake": 0,
            "Gear": 4,
            "Steer": -127,
            "LapNumber": 3,
        }
    )
    frame = FH6Adapter().parse(payload, t=9.0)

    assert frame.t == 9.0
    assert frame.race_on is True
    assert frame.t_src == pytest.approx(1234.567)
    assert frame.race_time == pytest.approx(61.25)
    assert frame.pos == pytest.approx((-4210.25, 118.5, 6180.75))
    assert frame.ground == pytest.approx((-4210.25, 6180.75))
    assert frame.speed == pytest.approx(32.5)
    assert frame.speed_kmh == pytest.approx(117.0)
    assert frame.yaw == pytest.approx(1.25)
    assert frame.susp == pytest.approx((0.1, 0.2, 0.3, 0.4))
    # Keyed off the most-loaded wheel (0.4), not the least: one unloaded
    # corner is a kerb, not a jump.
    assert frame.airborne_score == pytest.approx(0.6)
    assert frame.accel == pytest.approx(1.0)
    assert frame.brake == pytest.approx(0.0)
    assert frame.steer == pytest.approx(-1.0)
    assert frame.gear == 4
    assert frame.lap == 3
    assert frame.distance_traveled == pytest.approx(1234.5)
    assert frame.raw is None


def test_pack_parse_round_trip_preserves_position():
    values = {"PositionX": 1e5, "PositionY": -250.5, "PositionZ": -3.25, "Speed": 88.0}
    frame = FH6Adapter().parse(pack_fields(values))
    assert frame.x == pytest.approx(1e5)
    assert frame.y == pytest.approx(-250.5)
    assert frame.z == pytest.approx(-3.25)
    assert frame.speed == pytest.approx(88.0)


def test_pack_fields_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown fh6 field"):
        pack_fields({"TireWearFL": 1.0})


def test_pack_fields_rejects_wrong_quad_length():
    with pytest.raises(ValueError, match="needs 4 values"):
        pack_fields({"TireTemp": [1.0, 2.0]})


def test_trailing_pad_byte_does_not_break_parsing():
    payload = pack_fields({"Speed": 10.0})
    assert len(payload) == 324
    # The parser must not care what the pad byte holds.
    poisoned = payload[:323] + b"\xff"
    assert FH6Adapter().parse(poisoned).speed == pytest.approx(10.0)


def test_short_datagram_is_rejected():
    with pytest.raises(PacketError, match="too short"):
        FH6Adapter().parse(b"\x00" * 200)


def test_strict_mode_rejects_any_size_but_324():
    payload = pack_fields({"Speed": 1.0})
    FH6Adapter(strict=True).parse(payload)  # exact size is fine
    with pytest.raises(PacketError, match="expected 324"):
        FH6Adapter(strict=True).parse(payload + b"\x00")
    # The default tolerates a longer datagram, so a future patch that appends
    # fields still decodes everything we know about.
    assert FH6Adapter().parse(payload + b"\x00" * 8).speed == pytest.approx(1.0)


def test_keep_raw_exposes_native_field_names():
    frame = FH6Adapter(keep_raw=True).parse(pack_fields({"CarGroup": 12}))
    assert frame.raw is not None
    assert frame.raw["CarGroup"] == 12
    assert "SmashableMass" in frame.raw


def test_puddle_bytes_are_decoded_both_ways():
    """The original field table said s32; the official sled spec says f32. Same 16 bytes."""
    buf = bytearray(pack_fields({}))
    struct.pack_into("<4f", buf, 132, 0.25, 0.5, 0.0, 0.0)
    native = FH6Adapter().describe(bytes(buf))
    assert native["WheelInPuddleDepth_f32FL"] == pytest.approx(0.25)
    assert native["WheelInPuddleDepth_f32FR"] == pytest.approx(0.5)
    # The s32 reading of the same bytes is a large meaningless integer, which
    # is exactly what makes the two distinguishable from a real capture.
    assert native["WheelInPuddleFL"] == struct.unpack("<i", struct.pack("<f", 0.25))[0]
