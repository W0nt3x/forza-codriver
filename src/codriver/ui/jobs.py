"""Background jobs for the UI: one at a time, events fan out to listeners.

Capture, run and scan all need the telemetry port, so only one of them may
be alive, the manager enforces that. Everything a job has to say goes
through ``emit`` as a plain dict; the server turns those into WebSocket
messages, and keeps the last few so a browser that connects late sees where
things stand.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

Event = dict[str, Any]
Listener = Callable[[Event], None]
JobFn = Callable[[Callable[[Event], None], Callable[[], bool]], Any]
"""A job receives (emit, should_stop) and returns a result."""


class JobBusy(RuntimeError):
    pass


@dataclass
class JobManager:
    history: deque = field(default_factory=lambda: deque(maxlen=300))
    kind: str | None = None
    label: str = ""
    result: Any = None
    error: str | None = None
    started_at: float = 0.0
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _listeners: list[Listener] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- observers -----------------------------------------------------------

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def emit(self, event: Event) -> None:
        event = {"t": time.time(), "job": self.kind, **event}
        self.history.append(event)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                log.exception("job listener failed")

    # -- lifecycle -----------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "kind": self.kind if self.busy else None,
            "label": self.label if self.busy else "",
            "since": self.started_at if self.busy else None,
            "last_kind": self.kind,
            "last_error": self.error,
        }

    def start(self, kind: str, fn: JobFn, label: str = "") -> None:
        with self._lock:
            if self.busy:
                raise JobBusy(f"a {self.kind} job is already running")
            self.kind = kind
            self.label = label
            self.result = None
            self.error = None
            self.started_at = time.time()
            self._stop.clear()

            def wrapper() -> None:
                try:
                    self.result = fn(self.emit, self._stop.is_set)
                except Exception as exc:
                    self.error = str(exc)
                    log.error("job %s failed: %s", kind, traceback.format_exc())
                    self.emit({"kind": "error", "message": str(exc)})
                finally:
                    self.emit({"kind": "finished"})

            self._thread = threading.Thread(target=wrapper, daemon=True, name=f"job-{kind}")
            self._thread.start()
            self.emit({"kind": "started_job", "label": label})

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Ask the job to stop and wait for it. Returns True if it ended."""
        if not self.busy:
            return True
        self._stop.set()
        assert self._thread is not None
        self._thread.join(timeout_s)
        return not self.busy
