"""Find which UDP port the game is actually sending to.

An empty capture almost always means the port in the game does not match the
port in the config. Rather than guessing one at a time, bind a whole range at
once and report which ones receive anything.

**5200-5300 is excluded by default and you should leave it that way.** The
official docs say to avoid that range because the game binds its own
*outgoing* socket somewhere in it. Binding those ports here does not just
fail to help, it can take the port the game wanted and break Data Out
outright, turning "wrong port" into "no telemetry at all". The same warning
that makes 5300 a bad choice for Data Out makes it a bad choice to scan.
"""

from __future__ import annotations

import logging
import select
import socket
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

RESERVED_LO, RESERVED_HI = 5200, 5300

DEFAULT_SPEC = "5301-5500,4444,8000,8888,9999,10001,20777"
"""Our default, the rest of the usable 53xx/54xx band, and the port numbers
that turn up most often in SimHub guides and other games' telemetry docs."""


def parse_port_spec(spec: str) -> list[int]:
    """Parse ``"5301-5500,8000,9999"`` into a sorted list of ports."""
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                raise ValueError(f"bad range {part!r}: {lo} is above {hi}")
            ports.update(range(lo, hi + 1))
        else:
            ports.add(int(part))
    bad = [p for p in ports if not (1 <= p <= 65535)]
    if bad:
        raise ValueError(f"port(s) out of range: {sorted(bad)}")
    return sorted(ports)


@dataclass
class PortHit:
    port: int
    packets: int = 0
    sizes: dict[int, int] = field(default_factory=dict)
    senders: set[str] = field(default_factory=set)

    @property
    def looks_like_fh6(self) -> bool:
        return set(self.sizes) == {324}

    def describe(self) -> str:
        sizes = ", ".join(f"{n} bytes x{c}" for n, c in sorted(self.sizes.items()))
        who = ", ".join(sorted(self.senders))
        verdict = " <- FH6 Data Out" if self.looks_like_fh6 else ""
        return f"port {self.port}: {self.packets} packets from {who} ({sizes}){verdict}"


@dataclass
class ScanResult:
    hits: dict[int, PortHit] = field(default_factory=dict)
    bound: int = 0
    refused: dict[int, str] = field(default_factory=dict)
    skipped_reserved: list[int] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def found(self) -> list[PortHit]:
        return sorted(self.hits.values(), key=lambda h: -h.packets)


def scan(
    ports: list[int],
    duration_s: float = 20.0,
    allow_reserved: bool = False,
    on_first_hit=None,
) -> ScanResult:
    """Listen on many ports at once and report which receive UDP traffic."""
    result = ScanResult()

    if not allow_reserved:
        keep = []
        for p in ports:
            if RESERVED_LO <= p <= RESERVED_HI:
                result.skipped_reserved.append(p)
            else:
                keep.append(p)
        ports = keep

    socks: dict[int, socket.socket] = {}
    try:
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError as exc:
                # Something else already owns it. Worth reporting: if the port
                # you configured is in this list, that is your answer.
                result.refused[port] = str(exc)
                sock.close()
                continue
            sock.setblocking(False)
            socks[sock.fileno()] = sock
            result.bound += 1

        if not socks:
            return result

        by_fd = {fd: s.getsockname()[1] for fd, s in socks.items()}
        deadline = time.monotonic() + duration_s
        start = time.monotonic()
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select(
                list(socks.values()), [], [], min(0.5, max(0.0, remaining))
            )
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError, OSError):
                    continue
                port = by_fd[sock.fileno()]
                hit = result.hits.get(port)
                if hit is None:
                    hit = result.hits[port] = PortHit(port=port)
                    if on_first_hit is not None:
                        on_first_hit(hit)
                hit.packets += 1
                hit.sizes[len(data)] = hit.sizes.get(len(data), 0) + 1
                hit.senders.add(addr[0])
        result.duration_s = time.monotonic() - start
    finally:
        for sock in socks.values():
            sock.close()

    return result
