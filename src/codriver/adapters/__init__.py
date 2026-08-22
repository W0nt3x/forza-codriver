"""Telemetry adapters. Only these modules know about specific games."""

from .base import PacketError, Quad, TelemetryAdapter, TelemetryFrame

__all__ = ["PacketError", "Quad", "TelemetryAdapter", "TelemetryFrame", "get_adapter"]


def get_adapter(name: str, **kwargs):
    """Instantiate an adapter by the name used in config (telemetry.adapter)."""
    if name == "fh6":
        from .fh6 import FH6Adapter

        return FH6Adapter(**kwargs)
    raise ValueError(f"unknown telemetry adapter: {name!r}")
