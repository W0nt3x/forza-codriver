"""Hot-reloadable configuration.

Every tunable value in the project lives in ``config/defaults.yaml``.
``config/local.yaml``, if present, is deep-merged on top and is the file you
edit while driving. Both are re-read whenever their mtime changes, so tuning
never requires a restart.

Usage::

    cfg = Config.load()
    buffer = cfg.get("runtime.trigger.reaction_buffer_s")
    ...
    if cfg.poll():           # cheap: an mtime stat, throttled
        rebuild_whatever_depends_on_config()

Nothing in this module knows about telemetry, geometry or audio.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

log = logging.getLogger(__name__)

DEFAULTS_NAME = "defaults.yaml"
LOCAL_NAME = "local.yaml"
ENV_CONFIG_DIR = "CODRIVER_CONFIG_DIR"

_MISSING = object()

# Don't stat the files more than this often; poll() is called every frame.
_STAT_THROTTLE_S = 0.25


def find_config_dir(start: Path | None = None) -> Path:
    """Locate the ``config/`` directory holding defaults.yaml.

    Order: $CODRIVER_CONFIG_DIR, then the nearest ``config/defaults.yaml``
    walking up from this file, then from the current working directory.
    """
    env = os.environ.get(ENV_CONFIG_DIR)
    if env:
        p = Path(env).expanduser().resolve()
        if not (p / DEFAULTS_NAME).is_file():
            raise FileNotFoundError(f"{ENV_CONFIG_DIR}={p} has no {DEFAULTS_NAME}")
        return p

    for origin in (start or Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (origin, *origin.parents):
            candidate = parent / "config" / DEFAULTS_NAME
            if candidate.is_file():
                return candidate.parent
    raise FileNotFoundError(
        "could not locate config/defaults.yaml; set $" + ENV_CONFIG_DIR
    )


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base``, returning a new dict.

    Lists are replaced wholesale, not concatenated, overriding
    ``class_speed_bands_kmh`` should mean "these bands", not "these as well".
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _walk_keys(node: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path
            yield from _walk_keys(value, path)


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


@dataclass
class Config:
    """A hot-reloadable view of defaults.yaml + local.yaml."""

    config_dir: Path
    data: dict = field(default_factory=dict)
    _stamps: dict[Path, float] = field(default_factory=dict, repr=False)
    _callbacks: list[Callable[["Config"], None]] = field(
        default_factory=list, repr=False
    )
    _last_stat: float = field(default=0.0, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    reload_count: int = 0

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, config_dir: Path | str | None = None) -> "Config":
        directory = (
            Path(config_dir).expanduser().resolve()
            if config_dir is not None
            else find_config_dir()
        )
        cfg = cls(config_dir=directory)
        cfg._read()
        return cfg

    @property
    def defaults_path(self) -> Path:
        return self.config_dir / DEFAULTS_NAME

    @property
    def local_path(self) -> Path:
        return self.config_dir / LOCAL_NAME

    # -- reading -----------------------------------------------------------

    def _read(self) -> None:
        defaults = _load_yaml(self.defaults_path)
        local = _load_yaml(self.local_path)

        if local:
            known = set(_walk_keys(defaults))
            unknown = sorted(set(_walk_keys(local)) - known)
            # Report only the outermost unknown key of each branch, so one
            # typo'd section does not produce twenty warnings.
            roots = [
                key
                for key in unknown
                if not any(key.startswith(other + ".") for other in unknown)
            ]
            for key in roots:
                log.warning(
                    "%s sets '%s', which does not exist in %s, typo? "
                    "It will be loaded but nothing reads it.",
                    LOCAL_NAME,
                    key,
                    DEFAULTS_NAME,
                )

        self.data = deep_merge(defaults, local)
        self._stamps = {
            path: (path.stat().st_mtime_ns if path.is_file() else -1)
            for path in (self.defaults_path, self.local_path)
        }

    def poll(self, immediate: bool = False) -> bool:
        """Reload if either file changed on disk. Returns True if reloaded.

        Cheap enough to call every frame: it stats at most every 250 ms.
        ``immediate`` skips that throttle, it does not force a reload, so a
        poll on an unchanged file still returns False. Use ``reload()`` to
        re-read unconditionally.
        """
        now = time.monotonic()
        with self._lock:
            if not immediate and now - self._last_stat < _STAT_THROTTLE_S:
                return False
            self._last_stat = now
            changed = any(
                (path.stat().st_mtime_ns if path.is_file() else -1) != stamp
                for path, stamp in self._stamps.items()
            )
            if not changed:
                return False
        return self.reload()

    def reload(self) -> bool:
        """Re-read both files unconditionally. Returns True if it succeeded.

        A failed read keeps the previous values and leaves the mtime stamps
        alone, so the next poll tries again, you will save a half-written
        YAML file while driving, and that must not take the co-driver down.
        """
        with self._lock:
            try:
                self._read()
            except Exception as exc:
                log.error("config reload failed, keeping previous values: %s", exc)
                return False
            self.reload_count += 1

        log.info("config reloaded from %s", self.config_dir)
        for callback in list(self._callbacks):
            try:
                callback(self)
            except Exception:
                log.exception("config reload callback failed")
        return True

    def on_reload(self, callback: Callable[["Config"], None]) -> None:
        self._callbacks.append(callback)

    # -- access ------------------------------------------------------------

    def get(self, path: str, default: Any = _MISSING) -> Any:
        """Fetch a dotted key, e.g. ``"runtime.trigger.reaction_buffer_s"``.

        Raises KeyError when the key is absent and no default is given --
        a silently-defaulted threshold is a threshold you will spend an
        evening failing to tune.
        """
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise KeyError(f"missing config key: {path}")
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict:
        node = self.get(path)
        if not isinstance(node, dict):
            raise TypeError(f"config key {path} is not a section")
        return node

    def __getitem__(self, path: str) -> Any:
        return self.get(path)

    def describe(self) -> str:
        return yaml.safe_dump(self.data, sort_keys=False, default_flow_style=False)
