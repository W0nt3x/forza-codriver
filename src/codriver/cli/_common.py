"""Helpers shared by the command modules. Nothing here parses arguments."""

from __future__ import annotations

import logging
import sys

from ..adapters.base import TelemetryFrame
from ..config import Config
from ..net.udp import UdpListener


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def err(message: str) -> None:
    """Status and progress go to stderr; stdout is reserved for results such
    as the path of a file just written, so commands compose in scripts."""
    print(message, file=sys.stderr)


def frame_line(f: TelemetryFrame) -> str:
    state = "RACE" if f.race_on else "menu"
    return (
        f"{state}  t={f.t:7.2f}s  {f.speed_kmh:6.1f} km/h  g{f.gear}  "
        f"{f.rpm:5.0f} rpm  "
        f"pos {f.x:9.1f} {f.y:7.1f} {f.z:9.1f}  "
        f"yaw {f.yaw:+.2f}  thr {f.accel:.2f} brk {f.brake:.2f} "
        # Raw most-loaded-wheel travel, not the derived score: this is the
        # number stage.hazards.jump_susp_max_stretch is compared against, so
        # what you read here is what you type into the config.
        f"str {f.steer:+.2f}  susp {max(f.susp):.2f}  "
        f"dist {f.distance_traveled:8.1f} m"
    )


def waiting_hint(cfg: Config) -> str:
    return (
        f"waiting for telemetry on {cfg.get('telemetry.bind_host')}:"
        f"{cfg.get('telemetry.port')} ...\n"
        f"  In game: SETTINGS -> HUD AND GAMEPLAY -> Data Out = On, "
        f"IP 127.0.0.1, Port {cfg.get('telemetry.port')}\n"
        f"  The game only sends while you are actually driving: "
        f"nothing in menus, pauses or replays."
    )


def open_listener(cfg: Config) -> UdpListener:
    return UdpListener(
        host=cfg.get("telemetry.bind_host"),
        port=cfg.get("telemetry.port"),
        rcvbuf=cfg.get("telemetry.socket_rcvbuf_bytes"),
        timeout_s=cfg.get("telemetry.socket_timeout_s"),
    )
