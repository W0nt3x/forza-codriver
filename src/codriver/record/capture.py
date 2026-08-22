"""Raw datagram capture, the ``.fzr`` file format.

What gets stored is **the bytes off the wire**, plus the arrival time of each
datagram. Not parsed rows.

The original design specified NDJSON/Parquet with all fields for the raw
recon file. This deviates deliberately, because the development rules says in the same
breath that the offset table must be verified empirically and may be wrong.
If the first recording of a stage is stored already-parsed and offset 244
later turns out to be 240, every recording made before the fix is landfill.
A byte log re-parses forever, survives a parser bug, and is what the replayer
needs anyway. NDJSON is still available, as an *export* ("codriver decode"),
derived on demand, which is what the stage builder consumes.

Layout::

    magic       6 bytes   b"FZRAW\\x00"
    version     u16
    header_len  u32
    header      header_len bytes of UTF-8 JSON
    record*     u64 t_ns (since first datagram) | u16 length | payload

Records are length-prefixed and append-only, so a capture killed mid-drive
(alt-F4, crash, unplugged) still reads back cleanly up to the last complete
record. A truncated tail is reported, not raised.
"""

from __future__ import annotations

import json
import logging
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator

log = logging.getLogger(__name__)

MAGIC = b"FZRAW\x00"
VERSION = 1
SUFFIX = ".fzr"

_PREAMBLE = struct.Struct("<HI")  # version, header_len
_RECORD = struct.Struct("<QH")  # t_ns, length
MAX_PAYLOAD = 0xFFFF


class CaptureError(Exception):
    pass


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


@dataclass
class CaptureWriter:
    """Append raw datagrams to a ``.fzr`` file.

    Timestamps are stored relative to the first datagram, in nanoseconds, from
    the monotonic clock the listener sampled at arrival.
    """

    path: Path
    header: dict[str, Any] = field(default_factory=dict)
    flush_interval_s: float = 2.0

    _fh: Any = field(default=None, init=False, repr=False)
    _start_ns: int | None = field(default=None, init=False)
    _last_flush: float = field(default=0.0, init=False)
    count: int = field(default=0, init=False)
    bytes_written: int = field(default=0, init=False)

    def open(self) -> "CaptureWriter":
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = dict(self.header)
        header.setdefault("format", "fzr")
        header.setdefault("version", VERSION)
        header.setdefault(
            "created_utc",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        header.setdefault("clock", "time.perf_counter_ns")
        blob = json.dumps(header, sort_keys=True).encode("utf-8")

        self._fh = self.path.open("wb")
        self._fh.write(MAGIC)
        self._fh.write(_PREAMBLE.pack(VERSION, len(blob)))
        self._fh.write(blob)
        self.header = header
        self._last_flush = time.monotonic()
        log.info("capturing to %s", self.path)
        return self

    def add(self, data: bytes, t_ns: int) -> None:
        """Append one datagram. ``t_ns`` is an absolute perf_counter_ns reading."""
        if self._fh is None:
            raise CaptureError("writer is not open")
        if len(data) > MAX_PAYLOAD:
            raise CaptureError(f"datagram too large for the format: {len(data)} bytes")
        if self._start_ns is None:
            self._start_ns = t_ns
        rel = t_ns - self._start_ns
        if rel < 0:  # non-monotonic clock; should not happen with perf_counter
            rel = 0
        self._fh.write(_RECORD.pack(rel, len(data)))
        self._fh.write(data)
        self.count += 1
        self.bytes_written += _RECORD.size + len(data)

        now = time.monotonic()
        if now - self._last_flush >= self.flush_interval_s:
            self._fh.flush()
            self._last_flush = now

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "CaptureWriter":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


@dataclass
class CaptureReader:
    """Iterate ``(t_ns, payload)`` from a ``.fzr`` file."""

    path: Path
    header: dict[str, Any] = field(default_factory=dict, init=False)
    truncated: bool = field(default=False, init=False)

    _fh: Any = field(default=None, init=False, repr=False)

    def open(self) -> "CaptureReader":
        self.path = Path(self.path)
        self._fh = self.path.open("rb")
        magic = self._fh.read(len(MAGIC))
        if magic != MAGIC:
            self._fh.close()
            self._fh = None
            raise CaptureError(f"{self.path} is not a capture file (bad magic)")
        version, header_len = _PREAMBLE.unpack(self._fh.read(_PREAMBLE.size))
        if version != VERSION:
            self._fh.close()
            self._fh = None
            raise CaptureError(
                f"{self.path} is format version {version}, this build reads {VERSION}"
            )
        self.header = json.loads(self._fh.read(header_len).decode("utf-8"))
        return self

    def __iter__(self) -> Iterator[tuple[int, bytes]]:
        if self._fh is None:
            raise CaptureError("reader is not open")
        while True:
            head = self._fh.read(_RECORD.size)
            if not head:
                return
            if len(head) < _RECORD.size:
                self.truncated = True
                log.warning("%s: truncated record header at end of file", self.path)
                return
            t_ns, length = _RECORD.unpack(head)
            payload = self._fh.read(length)
            if len(payload) < length:
                self.truncated = True
                log.warning(
                    "%s: truncated payload at end of file (wanted %d, got %d)",
                    self.path,
                    length,
                    len(payload),
                )
                return
            yield t_ns, payload

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "CaptureReader":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_all(path: Path | str) -> tuple[dict[str, Any], list[tuple[int, bytes]]]:
    """Load a whole capture into memory. Fine at 60 Hz, an hour is ~70 MB."""
    with CaptureReader(Path(path)) as reader:
        records = list(reader)
        return reader.header, records


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


@dataclass
class GapInfo:
    index: int
    at_s: float
    duration_s: float


@dataclass
class CaptureSummary:
    """What "codriver info" prints. Deliberately parser-independent.

    Everything here is derived from record headers alone, lengths and
    arrival times, so it stays truthful even if the packet layout is wrong.
    """

    path: Path
    header: dict[str, Any]
    packets: int = 0
    duration_s: float = 0.0
    size_histogram: dict[int, int] = field(default_factory=dict)
    gaps: list[GapInfo] = field(default_factory=list)
    truncated: bool = False

    @property
    def rate_hz(self) -> float:
        return (self.packets - 1) / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def gap_total_s(self) -> float:
        return sum(g.duration_s for g in self.gaps)


def summarize(path: Path | str, gap_threshold_s: float = 0.5) -> CaptureSummary:
    """Header-level summary: sizes, rate, and stream gaps.

    A gap is not an error. The game stops sending during menus, pauses,
    rewinds and after the finish line, so gaps are where the interesting
    session boundaries are.
    """
    path = Path(path)
    with CaptureReader(path) as reader:
        summary = CaptureSummary(path=path, header=reader.header)
        prev_ns: int | None = None
        last_ns = 0
        threshold_ns = int(gap_threshold_s * 1e9)
        for i, (t_ns, payload) in enumerate(reader):
            summary.packets += 1
            size = len(payload)
            summary.size_histogram[size] = summary.size_histogram.get(size, 0) + 1
            if prev_ns is not None and t_ns - prev_ns > threshold_ns:
                summary.gaps.append(
                    GapInfo(
                        index=i,
                        at_s=prev_ns / 1e9,
                        duration_s=(t_ns - prev_ns) / 1e9,
                    )
                )
            prev_ns = t_ns
            last_ns = t_ns
        summary.duration_s = last_ns / 1e9
        summary.truncated = reader.truncated
    return summary


def default_capture_path(directory: Path | str, name: str | None = None) -> Path:
    """recordings/2026-08-22_170455.fzr, or recordings/<name>.fzr."""
    directory = Path(directory)
    if name:
        stem = name[: -len(SUFFIX)] if name.endswith(SUFFIX) else name
    else:
        stem = time.strftime("%Y-%m-%d_%H%M%S")
    return directory / (stem + SUFFIX)
