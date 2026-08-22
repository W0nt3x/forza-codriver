"""Checks that run against a REAL capture, when one has been committed.

A real captured packet belongs in the fixtures as a test fixture. Until
one exists these skip, loudly enough to be noticed in the test output,
because everything else in this suite only proves the code is self-consistent.
Nothing but a recording from the actual game can tell you the offset table is
right.

To produce them::

    python -m codriver capture --name real --fixture tests/fixtures/packet_real.bin
    copy recordings\\real.fzr tests\\fixtures\\real_capture.fzr

Drive for a minute or so, including a few seconds stationary at the start,
some sustained speed, and ideally a puddle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codriver.adapters.fh6 import PACKET_SIZE, FH6Adapter
from codriver.record.verify import verify_capture

FIXTURES = Path(__file__).parent / "fixtures"
PACKET = FIXTURES / "packet_real.bin"
CAPTURE = FIXTURES / "real_capture.fzr"

_HOWTO = (
    "no real capture committed yet, run "
    "`python -m codriver capture --name real --fixture "
    "tests/fixtures/packet_real.bin`"
)


@pytest.mark.skipif(not PACKET.is_file(), reason=_HOWTO)
def test_real_packet_is_the_expected_size():
    assert PACKET.stat().st_size == PACKET_SIZE


@pytest.mark.skipif(not PACKET.is_file(), reason=_HOWTO)
def test_real_packet_decodes_to_plausible_values():
    frame = FH6Adapter(strict=True).parse(PACKET.read_bytes())
    assert frame.race_on, "fixture should be captured while actually driving"
    assert 0 < frame.rpm_idle < frame.rpm_max < 20000
    assert frame.rpm_idle * 0.5 <= frame.rpm <= frame.rpm_max * 1.02
    assert 0.0 <= frame.speed < 200.0
    assert all(abs(v) < 1e6 for v in frame.pos)
    assert all(-0.1 <= s <= 1.1 for s in frame.susp)
    assert -1.0 <= frame.steer <= 1.0
    assert 0.0 <= frame.accel <= 1.0


@pytest.mark.skipif(not CAPTURE.is_file(), reason=_HOWTO)
def test_real_capture_passes_the_layout_checks():
    report = verify_capture(CAPTURE)
    assert report.ok, "\n" + report.render()
