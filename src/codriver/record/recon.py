"""Recording a recon lap: the capture loop, shared by the CLI and the UI.

Listens on the telemetry port and writes every datagram to a ``.fzr`` file,
decoding a frame alongside only to report progress. Events go to a callback
so the terminal and the browser can both watch the same loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..adapters import get_adapter
from ..adapters.base import PacketError
from ..config import Config
from ..net.udp import UdpListener
from .capture import CaptureWriter

EventFn = Callable[[dict[str, Any]], None]


@dataclass
class CaptureResult:
    path: Path
    packets: int = 0
    race_frames: int = 0
    bytes_written: int = 0
    seconds: float = 0.0
    fixture_path: Path | None = None


def capture_stream(
    cfg: Config,
    path: Path,
    *,
    note: str = "",
    duration_s: float = 0.0,
    fixture_path: Path | None = None,
    on_event: EventFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    status_interval_s: float = 0.25,
) -> CaptureResult:
    """Record until ``should_stop()``, ``duration_s`` elapses, or Ctrl-C.

    Events: ``{"kind": "waiting"}`` once at start, ``{"kind": "started",
    "packet_size": n}`` on the first datagram, ``{"kind": "status", ...}``
    throttled while recording (packets, elapsed, race_on, speed_kmh,
    distance_m, idle), ``{"kind": "fixture", "path": ...}`` when the fixture
    datagram is written, ``{"kind": "done", ...}`` at the end.
    """
    adapter = get_adapter(cfg.get("telemetry.adapter"))
    emit = on_event or (lambda e: None)
    header = {
        "adapter": adapter.name,
        "expected_packet_size": adapter.packet_size,
        "port": cfg.get("telemetry.port"),
        "note": note,
        "codriver_version": __import__("codriver").__version__,
    }
    result = CaptureResult(path=Path(path))
    start_ns: int | None = None
    last_ns: int | None = None
    last_status = 0.0
    fixture_written = False

    emit({"kind": "waiting", "port": cfg.get("telemetry.port")})
    with UdpListener(
        host=cfg.get("telemetry.bind_host"),
        port=cfg.get("telemetry.port"),
        rcvbuf=cfg.get("telemetry.socket_rcvbuf_bytes"),
        timeout_s=cfg.get("telemetry.socket_timeout_s"),
    ) as listener, CaptureWriter(
        path, header=header, flush_interval_s=cfg.get("capture.flush_interval_s")
    ) as writer:
        try:
            while True:
                if should_stop is not None and should_stop():
                    break
                got = listener.recv()
                now = time.monotonic()
                if got is None:
                    if start_ns is not None and now - last_status >= status_interval_s:
                        last_status = now
                        emit({"kind": "status", "packets": writer.count, "idle": True,
                              "elapsed_s": (last_ns - start_ns) / 1e9 if last_ns else 0.0})
                    continue
                data, t_ns = got
                if start_ns is None:
                    start_ns = t_ns
                    emit({"kind": "started", "packet_size": len(data), "path": str(path)})
                last_ns = t_ns
                writer.add(data, t_ns)

                frame = None
                try:
                    frame = adapter.parse(data, (t_ns - start_ns) / 1e9)
                except PacketError:
                    pass
                if frame is not None and frame.race_on:
                    result.race_frames += 1
                    if fixture_path is not None and not fixture_written:
                        fixture_path.parent.mkdir(parents=True, exist_ok=True)
                        fixture_path.write_bytes(data)
                        fixture_written = True
                        result.fixture_path = fixture_path
                        emit({"kind": "fixture", "path": str(fixture_path)})

                if now - last_status >= status_interval_s:
                    last_status = now
                    emit({
                        "kind": "status",
                        "packets": writer.count,
                        "idle": False,
                        "elapsed_s": (t_ns - start_ns) / 1e9,
                        "race_on": bool(frame and frame.race_on),
                        "speed_kmh": frame.speed_kmh if frame else 0.0,
                        "distance_m": frame.distance_traveled if frame else 0.0,
                    })

                if duration_s and (t_ns - start_ns) / 1e9 >= duration_s:
                    break
        except KeyboardInterrupt:
            pass

        result.packets = writer.count
        result.bytes_written = writer.bytes_written
        result.seconds = (last_ns - start_ns) / 1e9 if start_ns and last_ns else 0.0

    if result.packets == 0:
        result.path.unlink(missing_ok=True)
    emit({
        "kind": "done",
        "packets": result.packets,
        "race_frames": result.race_frames,
        "seconds": result.seconds,
        "path": str(result.path) if result.packets else None,
    })
    return result
