"""The overlay, stage 2: the runtime's events become a View, the View becomes
a picture, the scheduler says what is upcoming, and the UI can start and
stop the overlay in-process on the same event stream the web HUD reads."""

from __future__ import annotations

import pytest

from codriver.overlay.render import Style, render_frame, shorthand
from codriver.overlay.state import NoteBrief, OverlayState, View


def _status(along, speed_kmh=90.0, state="tracking", upcoming=None):
    return {"kind": "status", "job": "run", "state": state, "along_m": along, "speed_kmh": speed_kmh,
            "off_m": 0.5, "upcoming": upcoming if upcoming is not None else [
                {"text": "3 right", "tokens": ["3", "right"], "severity": 3, "direction": "right",
                 "kind": "corner", "at_m": along + 120.0},
                {"text": "jump", "tokens": ["jump"], "severity": None, "direction": None,
                 "kind": "jump", "at_m": along + 300.0},
            ]}


# --------------------------------------------------------------------------
# events -> view
# --------------------------------------------------------------------------


def test_status_events_become_next_after_and_a_moving_distance():
    s = OverlayState()
    s.set_connected(True)
    s.handle_event(_status(1000.0, speed_kmh=72.0), now=10.0)
    v = s.view(now=10.0)
    assert v.mode == "tracking"
    assert v.next.text == "3 right" and v.next.direction == "right" and v.next.severity == 3
    assert v.after.kind == "jump"
    assert v.distance_m == pytest.approx(120.0)
    # half a second later, no new status yet: 72 km/h is 20 m/s, so 10 m closer
    assert s.view(now=10.5).distance_m == pytest.approx(110.0)
    assert s.view(now=10.5).speed_kmh == pytest.approx(72.0)


def test_stale_pause_and_done_are_not_a_frozen_arrow():
    s = OverlayState()
    s.set_connected(True)
    s.handle_event(_status(500.0), now=0.0)
    assert s.view(now=1.0).mode == "tracking"
    assert s.view(now=3.5).mode == "stale", "no status for over two seconds dims the picture"
    assert s.view(now=3.5).distance_m == pytest.approx(120.0), "and stops moving it"
    s.handle_event({"kind": "suspended", "job": "run"}, now=4.0)
    assert s.view(now=4.0).mode == "suspended"
    s.handle_event(_status(520.0), now=5.0)
    assert s.view(now=5.0).mode == "tracking"
    s.handle_event({"kind": "done", "job": "run", "summary": "x"}, now=6.0)
    v = s.view(now=6.0)
    assert v.mode == "idle" and v.next is None


def test_other_jobs_and_garbage_are_ignored():
    s = OverlayState()
    s.set_connected(True)
    s.handle_event({"kind": "status", "job": "capture", "packets": 5}, now=0.0)
    assert s.view(now=0.0).mode == "idle"
    s.handle_event("not a dict", now=0.0)
    s.handle_event({"kind": "status", "job": "run", "state": "tracking", "along_m": "far", "upcoming": "no"}, now=0.0)
    v = s.view(now=0.0)
    assert v.mode == "tracking" and v.next is None
    s.handle_event(_status(10.0, upcoming=[{"tokens": 5}, None, {"text": "ok", "tokens": ["1", "left"], "at_m": 30}]), now=1.0)
    assert s.view(now=1.0).next.text == "ok", "bad entries are skipped, good ones kept"


def test_waiting_and_disconnected_states():
    s = OverlayState()
    assert s.view().connected is False
    s.set_connected(True)
    s.handle_event({"kind": "waiting", "job": "run", "port": 5400}, now=0.0)
    assert s.view(now=0.0).mode == "waiting"
    s.set_connected(False)
    assert s.view().mode == "idle" and s.view().connected is False


# --------------------------------------------------------------------------
# words and pictures
# --------------------------------------------------------------------------


def test_shorthand_reads_like_a_crew_note():
    assert shorthand(("3", "right")) == "3 R"
    assert shorthand(("100", "left", "tightens", "2")) == "L tightens 2"
    assert shorthand(("3", "right", "into", "2", "left")) == "3 R into 2 L"
    assert shorthand(("6", "left", "and", "jump")) == "6 L + JUMP"
    assert shorthand(("water",)) == "WATER"
    assert shorthand(("4", "right", "long")) == "4 R long"


def _corner(direction, at=120.0, sev=3):
    return NoteBrief(f"{sev} {direction}", (str(sev), direction), sev, direction, "corner", at)


def test_left_and_right_arrows_point_their_way():
    style = Style()
    right = render_frame(View("tracking", _corner("right"), None, 120.0, 90.0, True), 360, 300, style)
    left = render_frame(View("tracking", _corner("left"), None, 120.0, 90.0, True), 360, 300, style)
    # the arrow head sits in the upper band, right of centre for a right-hander
    # and left of centre for a left-hander
    def opaque_x_centroid(img, y_frac):
        y = int(img.height * y_frac)
        xs = [x for x in range(img.width) if img.getpixel((x, y))[3] > 200]
        return sum(xs) / len(xs) if xs else None
    assert opaque_x_centroid(right, 0.22) > right.width * 0.5
    assert opaque_x_centroid(left, 0.22) < left.width * 0.5
    for img in (right, left):
        for corner in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
            assert img.getpixel(corner)[3] == 0, "the background stays transparent"


def test_hazards_are_a_word_not_an_arrow_and_after_next_is_drawn():
    style = Style()
    jump = NoteBrief("jump", ("jump",), None, None, "jump", 80.0)
    img = render_frame(View("tracking", jump, _corner("left", 300.0, 2), 80.0, 100.0, True), 360, 300, style)
    band = [img.getpixel((x, int(300 * 0.40)))[3] for x in range(360)]
    assert max(band) == 255, "the hazard word sits where the arrow would be"
    lower = [img.getpixel((x, int(300 * 0.93)))[3] for x in range(360)]
    assert max(lower) > 0, "the call after next is drawn small below"


def test_idle_is_invisible_and_not_live_is_dimmed():
    style = Style()
    idle = render_frame(View("idle"), 200, 160, style)
    assert idle.getchannel("A").getextrema() == (0, 0), "no run: nothing on screen"
    live = render_frame(View("tracking", _corner("right"), None, 50.0, 80.0, True), 200, 160, style)
    paused = render_frame(View("suspended", _corner("right"), None, 50.0, 0.0, True), 200, 160, style)
    assert live.getchannel("A").getextrema()[1] == 255
    assert paused.getchannel("A").getextrema()[1] < 120, "paused is faded, not frozen at full strength"
    waiting = render_frame(View("waiting", None, None, None, 0.0, True), 200, 160, style)
    assert waiting.getchannel("A").getextrema()[1] > 0, "waiting says so in small words"


# --------------------------------------------------------------------------
# the scheduler tells what is coming
# --------------------------------------------------------------------------


def test_scheduler_upcoming_lists_the_next_calls_in_order():
    from codriver.runtime.scheduler import Scheduler
    from codriver.stage.notes import Note

    notes = [Note(at_m=100.0, tokens=["3", "right"], severity=3, direction="right"),
             Note(at_m=220.0, tokens=["jump"], kind="jump"),
             Note(at_m=400.0, tokens=["2", "left"], severity=2, direction="left")]
    s = Scheduler(notes=notes, duration_fn=lambda tokens: 1.0)
    assert [n.at_m for n in s.upcoming(2)] == [100.0, 220.0]
    s.relocate(150.0)
    assert [n.at_m for n in s.upcoming(2)] == [220.0, 400.0]
    assert [n.at_m for n in s.upcoming(5)] == [220.0, 400.0]
    s.relocate(1000.0)
    assert s.upcoming(2) == []


def test_status_event_carries_the_upcoming_notes():
    from codriver.runtime.run import note_brief
    from codriver.stage.notes import Note

    n = Note(at_m=100.0, tokens=["3", "right", "into", "2", "left"], severity=3, direction="right")
    assert note_brief(n) == {"text": "3 right into 2 left", "tokens": ["3", "right", "into", "2", "left"],
                             "severity": 3, "direction": "right", "kind": "corner", "at_m": 100.0}


def test_long_calls_shrink_to_fit_the_box():
    """"L tightens 2 into 3 R long" in a narrow box must shrink, not run off
    the edges: no opaque pixel may touch the left or right border."""
    style = Style()
    long_note = NoteBrief("left tightens 2 into 3 right long",
                          ("100", "left", "tightens", "2", "into", "3", "right", "long"), 2, "left", "corner", 120.0)
    after = NoteBrief("4 right long", ("4", "right", "long"), 4, "right", "corner", 300.0)
    for width, height in ((300, 300), (200, 260), (420, 200)):
        img = render_frame(View("tracking", long_note, after, 120.0, 90.0, True), width, height, style)
        left_edge = max(img.getpixel((0, y))[3] for y in range(height))
        right_edge = max(img.getpixel((width - 1, y))[3] for y in range(height))
        assert left_edge == 0 and right_edge == 0, f"text ran off the box at {width}x{height}"
        # and it is still there, readable: opaque pixels in the call line
        assert max(img.getpixel((x, int(height * 0.79)))[3] for x in range(width)) == 255

