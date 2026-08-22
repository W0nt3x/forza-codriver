"""The note algorithm, steps 2-6, turning a classified line into pace notes.

The reduction, in order:

2. Emit a candidate wherever the classification changes.
3. Drop a note whose predecessor is more severe and turns the same way.
   Coming out of a corner you have already slowed and can see it opening, so
   being told about it is noise.
4. Collapse a note that is more severe than its predecessor, same direction
   and close behind it, into that predecessor. When a corner starts turning,
   what matters is how tight it *ends up*, and calling it early beats
   calling it late. The distance guard stays: an R2 after a long R4 really is
   a separate call.
5. Drop the straights. They were only a robustness device for steps 3 and 4;
   what the driver wants instead is the distance to the next corner.
6. Link what is close together, and call the distance when it is not.

Also here: the hazards the note algorithm lists as detectable from telemetry rather
than geometry. A jump is not inferred from altitude, it is read off four
suspension sensors that all went slack at once.

The thresholds in every step are driver-specific and unknown. They are
config, and they are expected to move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Sequence

from .curvature import Direction, Marking
from .line import LinePoint

CORNER = "corner"
CREST = "crest"
DIP = "dip"
JUMP = "jump"


@dataclass(slots=True)
class Candidate:
    """A note under construction, still tied to a resampled point index."""

    index: int
    marking: Marking
    apex_index: int | None = None
    """When step 4 collapsed a tightening corner into this candidate, the
    original index of the marking that now names it. The distance between
    ``index`` and ``apex_index`` is how far the corner develops before it is
    as tight as the call says, the difference between an immediate hairpin
    and a long corner that keeps coming."""
    end_index: int | None = None
    """Where the next candidate starts: the extent of this corner. Set once
    the reduction is done, used for "long" and for observed-speed stats."""


@dataclass(slots=True)
class Note:
    """A finished pace note.

    Positioned by distance along the stage, carrying its token list, so a
    stage file stays hand-editable, generation gets you most of the way and
    a human fixes the three corners it got wrong.
    """

    at_m: float
    tokens: list[str]
    index: int = 0
    kind: str = CORNER
    direction: str | None = None
    severity: int | None = None
    radius_m: float | None = None
    parts: list[dict] = field(default_factory=list)
    """The individual notes making up a linked phrase, when there is more
    than one. Empty for a single note."""
    length_m: float | None = None
    """How far the corner extends past its call point."""
    observed_kmh: float | None = None
    """Slowest speed actually driven through this corner, median over the
    runs `codriver learn` has seen. Information, not a target."""

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


# -- steps 2 to 5 -----------------------------------------------------------


def candidates(markings: Sequence[Marking]) -> list[Candidate]:
    """Step 2: a candidate wherever the classification changes."""
    out: list[Candidate] = []
    previous: Marking | None = None
    for i, marking in enumerate(markings):
        if previous is None or marking.label != previous.label:
            out.append(Candidate(index=i, marking=marking))
        previous = marking
    return out


def filter_descending(cands: Sequence[Candidate]) -> list[Candidate]:
    """Step 3: remove a note whose predecessor is more severe, same direction."""
    out = list(cands)
    for i in range(len(out) - 1, 0, -1):
        prev, cur = out[i - 1], out[i]
        if prev.marking.same_direction_as(cur.marking) and prev.marking.more_severe_than(
            cur.marking
        ):
            del out[i]
    return out


def collapse_ascending(
    cands: Sequence[Candidate],
    collapse_window_points: int = 20,
) -> list[Candidate]:
    """Step 4: fold a tightening corner back into where it started.

    While folding, remember where the apex marking originally sat. How far it
    was pulled back is what separates a real tightening corner from an
    artifact: the sliding classification window ramps through the milder
    classes over roughly ``window_points`` on the way into *every* corner,
    but only a corner that genuinely develops over distance collapses across
    much more than that.
    """
    out = list(cands)
    for i in range(len(out) - 1, 0, -1):
        prev, cur = out[i - 1], out[i]
        if (
            cur.marking.same_direction_as(prev.marking)
            and cur.marking.more_severe_than(prev.marking)
            and cur.index - prev.index <= collapse_window_points
        ):
            out[i - 1] = Candidate(
                index=prev.index,
                marking=cur.marking,
                apex_index=cur.apex_index if cur.apex_index is not None else cur.index,
            )
            del out[i]
    return out


def merge_same_label(cands: Sequence[Candidate]) -> list[Candidate]:
    """Step 4b: adjacent candidates with the *same* marking are one corner.

    Steps 3 and 4 handle a corner getting tighter and a corner opening out.
    What they leave behind is a corner that flickers: R4, R5, R4 loses its
    R5 to step 3 and becomes R4, R4, two notes for one long right-hander,
    spoken 100 m apart in quick succession. Seen live, three times in a row.
    Between two equal labels there can be no straight (it would sit between
    them as its own candidate), so merging is always safe: the survivor keeps
    the earliest index and the earliest apex.
    """
    out: list[Candidate] = []
    for c in cands:
        if out and out[-1].marking.label == c.marking.label:
            prev = out[-1]
            out[-1] = Candidate(
                index=prev.index,
                marking=prev.marking,
                apex_index=prev.apex_index if prev.apex_index is not None else c.apex_index,
            )
            continue
        out.append(c)
    return out


def drop_straights(cands: Sequence[Candidate]) -> list[Candidate]:
    """Step 5."""
    return [c for c in cands if c.marking.is_corner]


def reduce_candidates(
    markings: Sequence[Marking],
    collapse_window_points: int = 20,
) -> list[Candidate]:
    """Steps 2 through 5, in order. Survivors know where they end."""
    cands = candidates(markings)
    cands = filter_descending(cands)
    cands = collapse_ascending(cands, collapse_window_points)
    cands = merge_same_label(cands)
    for this, following in zip(cands, cands[1:]):
        this.end_index = following.index
    if cands:
        cands[-1].end_index = len(markings)
    return drop_straights(cands)


# -- hazards from telemetry, not geometry ------------------------------------


def detect_jumps(
    line: Sequence[LinePoint],
    cumulative: Sequence[float],
    susp_max_stretch: float = 0.05,
    min_duration_s: float = 0.15,
) -> list[Note]:
    """All four wheels at max stretch at once means the car left the ground.

    Read off the recon lap rather than guessed from the altitude profile,
    which is what the note algorithm recommends and is far more reliable.
    """
    notes: list[Note] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i].susp_max > susp_max_stretch:
            i += 1
            continue
        start = i
        while i < n and line[i].susp_max <= susp_max_stretch:
            i += 1
        end = i - 1
        span_m = cumulative[end] - cumulative[start]
        speed = max(line[start].speed, 1.0)
        if span_m >= min_duration_s * speed:
            notes.append(
                Note(
                    at_m=cumulative[start],
                    tokens=[JUMP],
                    index=start,
                    kind=JUMP,
                )
            )
    return notes


def _gradients(
    line: Sequence[LinePoint],
    cumulative: Sequence[float],
    window_points: int,
) -> list[float]:
    """dy/ds at each point, over the same window the curvature fit uses."""
    n = len(line)
    out = [0.0] * n
    for i in range(n):
        lo, hi = max(0, i - window_points), min(n - 1, i + window_points)
        run = cumulative[hi] - cumulative[lo]
        if run > 1e-6:
            out[i] = (line[hi].y - line[lo].y) / run
    return out


def detect_crests_and_dips(
    line: Sequence[LinePoint],
    cumulative: Sequence[float],
    window_points: int = 7,
    crest_gradient: float = 0.06,
    dip_gradient: float = -0.06,
) -> list[Note]:
    """Where the road stops climbing and starts falling, and the reverse.

    A crest is not merely a steep bit: it is a brow, rising into it and
    falling out. Testing the gradient on both sides is what separates the two,
    and stops a long climb from being called as a crest every 20 metres.
    """
    grads = _gradients(line, cumulative, window_points)
    notes: list[Note] = []
    span = window_points
    last_index = -(span * 4)
    for i in range(span, len(line) - span):
        before, after = grads[i - span], grads[i + span]
        is_crest = before >= crest_gradient and after <= -crest_gradient
        is_dip = before <= dip_gradient and after >= -dip_gradient
        if not (is_crest or is_dip):
            continue
        if i - last_index < span * 2:  # one call per feature, not per point
            continue
        last_index = i
        kind = CREST if is_crest else DIP
        notes.append(Note(at_m=cumulative[i], tokens=[kind], index=i, kind=kind))
    return notes


# -- step 6: distances and linking ------------------------------------------


def distance_token(gap_m: float, buckets_m: Sequence[float]) -> str:
    """Round a gap down to a callable distance.

    Down, not to nearest: hearing "one hundred" and finding 140 m is a
    pleasant surprise, the other way round is not.
    """
    usable = [b for b in buckets_m if b <= gap_m]
    return str(int(usable[-1])) if usable else str(int(buckets_m[0]))


def _as_part(note: Note) -> dict:
    return {
        "tokens": list(note.tokens),
        "kind": note.kind,
        "at_m": round(note.at_m, 2),
        **({"direction": note.direction} if note.direction else {}),
        **({"severity": note.severity} if note.severity else {}),
    }


def link_notes(
    notes: Sequence[Note],
    link_into_max_m: float = 20.0,
    link_and_max_m: float = 50.0,
    max_linked_notes: int = 3,
) -> list[Note]:
    """Join notes that arrive too close together to be spoken separately.

    A phrase takes the position of its *first* note: it has to be finished
    before the driver reaches the first corner, not the last.
    """
    if not notes:
        return []

    out: list[Note] = []
    i = 0
    while i < len(notes):
        head = notes[i]
        tokens = list(head.tokens)
        parts = [_as_part(head)]
        j = i + 1
        while j < len(notes) and len(parts) < max_linked_notes:
            gap = notes[j].at_m - notes[j - 1].at_m
            if gap <= link_into_max_m:
                connector = "into"
            elif gap <= link_and_max_m:
                connector = "and"
            else:
                break
            tokens.append(connector)
            tokens.extend(notes[j].tokens)
            parts.append(_as_part(notes[j]))
            j += 1

        out.append(replace(head, tokens=tokens, parts=parts if len(parts) > 1 else []))
        i = j
    return out


def add_distance_calls(
    notes: Sequence[Note],
    distance_call_min_m: float = 60.0,
    distance_buckets_m: Sequence[float] = (30, 50, 70, 100, 150, 200, 250, 300, 400, 500),
    stage_start_m: float = 0.0,
) -> list[Note]:
    """Prepend a distance to any note far enough from the one before it.

    The distance leads the phrase, "one hundred, three right", so that one
    corner is one phrase and the runtime scheduler has a single thing to time.
    Whether a real co-driver would instead call it trailing the previous note
    is a question for the ear, not the geometry.
    """
    out: list[Note] = []
    previous_end = stage_start_m
    for note in notes:
        gap = note.at_m - previous_end
        tokens = list(note.tokens)
        if gap >= distance_call_min_m:
            tokens.insert(0, distance_token(gap, distance_buckets_m))
        out.append(replace(note, tokens=tokens))
        previous_end = note.at_m
    return out


# -- the whole thing --------------------------------------------------------


def generate(
    line: Sequence[LinePoint],
    markings: Sequence[Marking],
    cumulative: Sequence[float],
    *,
    collapse_window_points: int = 20,
    tightens_min_run_points: int = 12,
    tightens_max_severity: int = 3,
    link_into_max_m: float = 20.0,
    link_and_max_m: float = 50.0,
    max_linked_notes: int = 3,
    distance_call_min_m: float = 60.0,
    distance_buckets_m: Sequence[float] = (30, 50, 70, 100, 150, 200, 250, 300, 400, 500),
    long_min_m: float = 120.0,
    hazards: bool = True,
    window_points: int = 7,
    jump_susp_max_stretch: float = 0.05,
    jump_min_duration_s: float = 0.15,
    crest_gradient: float = 0.06,
    dip_gradient: float = -0.06,
) -> list[Note]:
    """The note algorithm steps 2-6, plus telemetry hazards, in one call."""
    corners = []
    last = len(cumulative) - 1
    for c in reduce_candidates(markings, collapse_window_points):
        end = min(c.end_index if c.end_index is not None else c.index, last)
        length_m = max(0.0, cumulative[end] - cumulative[c.index])
        develops = (
            c.apex_index is not None
            and c.apex_index - c.index >= tightens_min_run_points
        )
        if develops and c.marking.severity <= tightens_max_severity:
            # The apex severity was pulled back well past the classification
            # window's own ramp-in, so the corner genuinely keeps coming after
            # the driver has committed to it. "left tightens 1" beats a bare
            # "1 left" spoken 70 m before the hairpin.
            tokens = [
                c.marking.direction.value,
                "tightens",
                str(c.marking.severity),
            ]
        else:
            tokens = [str(c.marking.severity), c.marking.direction.value]
            if length_m >= long_min_m:
                # One corner, not two: the merged long right-hander that used
                # to be called twice is called once, with its length.
                tokens.append("long")
        corners.append(
            Note(
                at_m=cumulative[c.index],
                tokens=tokens,
                index=c.index,
                kind=CORNER,
                direction=c.marking.direction.value,
                severity=c.marking.severity,
                radius_m=(
                    round(c.marking.radius_m, 1)
                    if math.isfinite(c.marking.radius_m)
                    else None
                ),
                length_m=round(length_m, 1),
            )
        )

    everything = list(corners)
    if hazards:
        everything += detect_jumps(
            line, cumulative, jump_susp_max_stretch, jump_min_duration_s
        )
        everything += detect_crests_and_dips(
            line, cumulative, window_points, crest_gradient, dip_gradient
        )
    everything.sort(key=lambda n: (n.at_m, n.kind))

    linked = link_notes(
        everything, link_into_max_m, link_and_max_m, max_linked_notes
    )
    return add_distance_calls(linked, distance_call_min_m, distance_buckets_m)


def required_tokens(notes: Sequence[Note]) -> set[str]:
    """Every token a voice pack would need to speak this stage.

    The voice tooling uses this to report what a pack is missing before you find out at
    140 km/h.
    """
    return {token for note in notes for token in note.tokens}
