"""Port discovery, for when a capture comes back empty."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from codriver.adapters.fh6 import pack_fields
from codriver.net.scan import (
    DEFAULT_SPEC,
    RESERVED_HI,
    RESERVED_LO,
    parse_port_spec,
    scan,
)


def test_parse_port_spec_handles_ranges_and_singles():
    assert parse_port_spec("5400") == [5400]
    assert parse_port_spec("5400-5402") == [5400, 5401, 5402]
    assert parse_port_spec("8000,5400-5401") == [5400, 5401, 8000]
    assert parse_port_spec(" 5400 , 8000 ") == [5400, 8000]


def test_parse_port_spec_rejects_nonsense():
    with pytest.raises(ValueError, match="is above"):
        parse_port_spec("5500-5400")
    with pytest.raises(ValueError, match="out of range"):
        parse_port_spec("70000")


def test_the_default_sweep_avoids_the_range_the_game_binds():
    """Binding 5200-5300 could take the port FH6 wants for its own outgoing
    socket, turning a wrong-port problem into no-telemetry-at-all."""
    ports = parse_port_spec(DEFAULT_SPEC)
    assert not [p for p in ports if RESERVED_LO <= p <= RESERVED_HI]


def test_reserved_ports_are_skipped_and_reported():
    result = scan([5250, 5251], duration_s=0.1)
    assert result.skipped_reserved == [5250, 5251]
    assert result.bound == 0


def test_reserved_ports_can_be_forced():
    result = scan([5250], duration_s=0.1, allow_reserved=True)
    assert result.skipped_reserved == []
    assert result.bound == 1


def test_scan_finds_the_port_traffic_arrives_on():
    ports = [5471, 5472, 5473]
    target = 5472
    found: list[int] = []

    def send() -> None:
        time.sleep(0.3)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(5):
            s.sendto(pack_fields({"Speed": 25.0}), ("127.0.0.1", target))
            time.sleep(0.05)
        s.close()

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    result = scan(ports, duration_s=2.0, on_first_hit=lambda h: found.append(h.port))
    thread.join(timeout=2.0)

    assert [h.port for h in result.found] == [target]
    assert found == [target]
    hit = result.hits[target]
    assert hit.packets == 5
    assert hit.sizes == {324: 5}
    assert hit.senders == {"127.0.0.1"}
    assert hit.looks_like_fh6 is True


def test_a_port_already_in_use_is_reported_not_silently_dropped():
    """If the configured port is held by SimHub or a stray listener, that is
    the answer, it must not look like 'no traffic'."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", 5474))
    try:
        result = scan([5474], duration_s=0.1)
        assert 5474 in result.refused
        assert result.bound == 0
    finally:
        holder.close()


def test_traffic_of_the_wrong_size_is_not_called_fh6():
    port = 5475

    def send() -> None:
        time.sleep(0.2)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"x" * 100, ("127.0.0.1", port))
        s.close()

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    result = scan([port], duration_s=1.5)
    thread.join(timeout=2.0)

    assert result.hits[port].looks_like_fh6 is False
