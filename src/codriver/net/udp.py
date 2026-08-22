"""UDP transport.

This module moves bytes. It has no idea what is inside them, that is the
adapter's job, and keeping the two apart is what lets a second game plug in
without touching anything here.

Two Windows-specific details are handled, because both bite in practice:

* ``SIO_UDP_CONNRESET``. On Windows, a UDP socket that has sent to a port
  nobody is listening on gets an ICMP port-unreachable back, and the *next*
  ``recvfrom`` raises ``ConnectionResetError``, on a socket that did nothing
  wrong. Replaying into a dead port would otherwise kill the listener.
* Receive buffer. The default is small enough that a stalled consumer drops
  60 Hz telemetry silently. It is raised, and the achieved size is reported
  so a refused request is visible rather than assumed.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from types import TracebackType

log = logging.getLogger(__name__)

DEFAULT_RCVBUF = 1 << 20


def _suppress_windows_conn_reset(sock: socket.socket) -> None:
    """Stop ICMP port-unreachable from poisoning subsequent recv calls."""
    if not hasattr(socket, "SIO_UDP_CONNRESET"):
        return
    try:
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
    except OSError as exc:  # not fatal; we also catch the error at recv time
        log.debug("SIO_UDP_CONNRESET not accepted: %s", exc)


@dataclass
class ListenerStats:
    packets: int = 0
    bytes: int = 0
    timeouts: int = 0
    errors: int = 0
    first_ns: int | None = None
    last_ns: int | None = None

    @property
    def duration_s(self) -> float:
        if self.first_ns is None or self.last_ns is None:
            return 0.0
        return (self.last_ns - self.first_ns) / 1e9

    @property
    def rate_hz(self) -> float:
        d = self.duration_s
        return (self.packets - 1) / d if d > 0 and self.packets > 1 else 0.0


@dataclass
class UdpListener:
    """Blocking-with-timeout UDP receiver.

    ``recv`` returns ``(payload, t_ns)`` where ``t_ns`` is a
    ``perf_counter_ns`` reading taken immediately after the syscall returned,
    or ``None`` on timeout. Arrival time is captured here, at the closest
    point to the wire we control, and is the clock everything downstream
    paces from, the game's own timestamp does not advance across the gaps
    that matter.
    """

    host: str = "0.0.0.0"
    port: int = 5400
    rcvbuf: int = DEFAULT_RCVBUF
    timeout_s: float = 0.5
    bufsize: int = 2048

    sock: socket.socket | None = field(default=None, init=False, repr=False)
    stats: ListenerStats = field(default_factory=ListenerStats)
    actual_rcvbuf: int = field(default=0, init=False)

    def open(self) -> "UdpListener":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately NOT SO_REUSEADDR. On Windows it lets a second socket
        # bind this same UDP port, and delivery between the two is
        # indeterminate, so a `capture` started while a `listen` is running
        # would silently record a fraction of the stream. A refused bind with
        # a clear message is far better than a half-recorded recon lap.
        _suppress_windows_conn_reset(sock)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        except OSError as exc:
            log.warning("could not set SO_RCVBUF=%d: %s", self.rcvbuf, exc)
        self.actual_rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        sock.settimeout(self.timeout_s)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise OSError(
                f"cannot bind UDP {self.host}:{self.port}, {exc}. "
                f"Another listener (SimHub? a previous run?) may already have it."
            ) from exc
        self.sock = sock
        log.info(
            "listening on %s:%d (rcvbuf %d bytes)",
            self.host,
            self.port,
            self.actual_rcvbuf,
        )
        return self

    def recv(self) -> tuple[bytes, int] | None:
        if self.sock is None:
            raise RuntimeError("listener is not open")
        try:
            data = self.sock.recv(self.bufsize)
        except socket.timeout:
            self.stats.timeouts += 1
            return None
        except ConnectionResetError:
            # Windows ICMP port-unreachable from an unrelated send. Not our
            # problem, and not fatal: the socket stays usable.
            self.stats.errors += 1
            return None
        except OSError as exc:
            self.stats.errors += 1
            log.debug("recv error: %s", exc)
            return None

        t_ns = time.perf_counter_ns()
        self.stats.packets += 1
        self.stats.bytes += len(data)
        if self.stats.first_ns is None:
            self.stats.first_ns = t_ns
        self.stats.last_ns = t_ns
        return data, t_ns

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "UdpListener":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class UdpSender:
    """Fire-and-forget UDP sender, used by the replayer."""

    host: str = "127.0.0.1"
    port: int = 5400

    sock: socket.socket | None = field(default=None, init=False, repr=False)
    sent: int = 0

    def open(self) -> "UdpSender":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _suppress_windows_conn_reset(sock)
        self.sock = sock
        return self

    def send(self, data: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("sender is not open")
        try:
            self.sock.sendto(data, (self.host, self.port))
            self.sent += 1
        except ConnectionResetError:
            # Nothing is listening yet. Keep going: a replay is often started
            # before the consumer is.
            pass

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "UdpSender":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
