"""The runtime design, deciding when each note is spoken.

The contract: a note must **finish** roughly ``reaction_buffer_s`` before its
corner, not start then. So the firing distance is

    lead_m = speed * (phrase_duration + reaction_buffer) * curve(speed)

scaled by *current* speed, clamped to [min_lead, max_lead]. The curve is the
open question from the runtime design (linear vs slightly super-linear at high speed)
made tunable instead of answered.

Queue discipline, in order of importance:

1. Two phrases never play on top of each other. A co-driver that talks over
   itself is worse than no co-driver.
2. A note that can no longer finish usefully before its corner is dropped,
   not played late. Hearing "3 right" while exiting the 3 right is noise.
3. When two notes compete for the same mouth, the less severe one loses.

The scheduler is deliberately player-agnostic and clock-agnostic: it is fed
``(along_m, speed, now)`` and returns what to say. That is what makes it
testable against a fake clock, and what keeps audio latency out of the
decision logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..stage.notes import Note

log = logging.getLogger(__name__)

DurationFn = Callable[[Sequence[str]], float]
"""tokens -> seconds the assembled phrase will take to say."""


def interp_curve(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """Piecewise-linear interpolation, flat outside the range."""
    if not xs:
        return 1.0
    if x <= xs[0]:
        return ys[0]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x <= x1:
            span = x1 - x0
            return y0 if span <= 0 else y0 + (y1 - y0) * (x - x0) / span
    return ys[-1]


@dataclass(frozen=True, slots=True)
class PlayEvent:
    """One phrase the runner should start saying right now."""

    note: Note
    duration_s: float
    lead_m: float
    """The firing distance in force when it fired, kept for the HUD, so
    tuning sessions can see the number they are tuning."""


@dataclass(slots=True)
class _Pending:
    note: Note
    fired_at_m: float


def _rank_one(kind: str, severity: int | None) -> int:
    """Lower = more important. Corners by class; hazards sit between 2 and 3 --
    a jump call matters more than most corners."""
    if severity is not None:
        return severity
    return 2 if kind == "jump" else 3


def _severity_rank(note: Note) -> int:
    """A linked phrase is as important as its most important part.

    "6 left and jump into 4 left" led with a 6, but it carries a jump and a
    4, ranking it by its head alone got exactly that phrase dropped in
    favour of a bare jump call on a real stage, and the driver went over a
    jump into a 4 with no warning.
    """
    best = _rank_one(note.kind, note.severity)
    for part in note.parts:
        best = min(best, _rank_one(part.get("kind", "corner"), part.get("severity")))
    return best


@dataclass
class Scheduler:
    """Feed fixes in, get phrases out.

    Config attributes are read every tick, so a hot-reload takes effect by
    assigning to them.
    """

    notes: list[Note]
    duration_fn: DurationFn

    reaction_buffer_s: float = 1.8
    speed_curve_kmh: Sequence[float] = (0, 60, 120, 200)
    speed_curve_mult: Sequence[float] = (1.0, 1.0, 1.1, 1.25)
    min_lead_m: float = 15.0
    max_lead_m: float = 400.0
    drop_if_later_than_s: float = 0.3

    _next: int = field(default=0, repr=False)
    _queue: list[_Pending] = field(default_factory=list, repr=False)
    _speaking_until: float = field(default=-1e9, repr=False)
    dropped: int = 0
    spoken: int = 0

    def __post_init__(self) -> None:
        self.notes = sorted(self.notes, key=lambda n: n.at_m)

    # -- lifecycle ---------------------------------------------------------

    def relocate(self, along_m: float) -> None:
        """Point the scheduler at a new position: after a rewind, a restart,
        or a cold acquire mid-stage. Everything queued is stale by definition."""
        if self._queue:
            log.info("relocate: flushed %d queued note(s)", len(self._queue))
        self._queue.clear()
        self._next = 0
        while self._next < len(self.notes) and self.notes[self._next].at_m <= along_m:
            self._next += 1

    def flush(self) -> None:
        """Stream suspended (pause/finish): whatever is queued must not play
        when it resumes."""
        self._queue.clear()

    # -- the tick ----------------------------------------------------------

    def lead_m(self, speed_mps: float, phrase_s: float) -> float:
        mult = interp_curve(
            speed_mps * 3.6, list(self.speed_curve_kmh), list(self.speed_curve_mult)
        )
        lead = speed_mps * (phrase_s + self.reaction_buffer_s) * mult
        return min(self.max_lead_m, max(self.min_lead_m, lead))

    def tick(self, along_m: float, speed_mps: float, now: float) -> list[PlayEvent]:
        """Advance to ``along_m`` at ``speed_mps``; return phrases to start now."""
        # 1. Fire: move notes whose lead distance has been reached into the
        #    queue. The lead uses each note's own phrase duration, a long
        #    linked phrase fires earlier than a bare "3 left".
        while self._next < len(self.notes):
            note = self.notes[self._next]
            phrase_s = self.duration_fn(note.tokens)
            if note.at_m - along_m > self.lead_m(speed_mps, phrase_s):
                break
            self._queue.append(_Pending(note, along_m))
            self._next += 1

        # 2. Contend: if more than one note is waiting for the mouth, the less
        #    severe loses. This is the two-notes-overlap rule from the runtime design;
        #    build-time linking handles the common case, this handles the rest.
        while len(self._queue) > 1:
            a, b = self._queue[0], self._queue[1]
            victim = 0 if _severity_rank(a.note) > _severity_rank(b.note) else 1
            dropped = self._queue.pop(victim)
            self.dropped += 1
            log.info("dropped '%s': queue contention", dropped.note.text)

        # 3. Speak: at most one phrase, only if the mouth is free, only if it
        #    can still finish before the corner is upon the driver, and only
        #    if saying it would not make a *more severe* note behind it late.
        events: list[PlayEvent] = []
        if now >= self._speaking_until:
            while self._queue:
                pending = self._queue[0]
                phrase_s = self.duration_fn(pending.note.tokens)
                eta_s = (
                    (pending.note.at_m - along_m) / speed_mps
                    if speed_mps > 0.5
                    else float("inf")
                )
                if phrase_s - eta_s > self.drop_if_later_than_s:
                    # It would still be talking when the corner arrives.
                    self._queue.pop(0)
                    self.dropped += 1
                    log.info("dropped '%s': would finish %.1fs late",
                             pending.note.text, phrase_s - eta_s)
                    continue
                blocker = self._would_delay_a_tighter_corner(
                    pending.note, phrase_s, along_m, speed_mps, now
                )
                if blocker is not None:
                    self._queue.pop(0)
                    self.dropped += 1
                    log.info("dropped '%s': the mouth is needed for '%s'",
                             pending.note.text, blocker.text)
                    continue
                self._queue.pop(0)
                self._speaking_until = now + phrase_s
                self.spoken += 1
                events.append(
                    PlayEvent(
                        note=pending.note,
                        duration_s=phrase_s,
                        lead_m=self.lead_m(speed_mps, phrase_s),
                    )
                )
                break
        return events

    def _would_delay_a_tighter_corner(
        self,
        pending_note: Note,
        phrase_s: float,
        along_m: float,
        speed_mps: float,
        now: float,
    ) -> Note | None:
        """The upcoming note it would sabotage, or None.

        The queue-contention rule only sees notes that have already fired.
        A phrase started now occupies the mouth into the future, and if a
        more severe note falls due in that window, *its* call goes stale --
        the driver would be told about a 5 and surprised by the 1 behind it.
        Sacrifice the milder note instead.
        """
        if speed_mps <= 0.5 or self._next >= len(self.notes):
            return None
        upcoming = self.notes[self._next]
        if _severity_rank(upcoming) >= _severity_rank(pending_note):
            return None
        upcoming_s = self.duration_fn(upcoming.tokens)
        upcoming_corner_t = now + (upcoming.at_m - along_m) / speed_mps
        earliest_finish = now + phrase_s + upcoming_s
        if earliest_finish > upcoming_corner_t + self.drop_if_later_than_s:
            return upcoming
        return None

    # -- introspection -----------------------------------------------------

    @property
    def next_note(self) -> Note | None:
        if self._queue:
            return self._queue[0].note
        if self._next < len(self.notes):
            return self.notes[self._next]
        return None

    def speaking(self, now: float) -> bool:
        return now < self._speaking_until
