"""StreamHub: one scrcpy session per worker, shared by every viewer."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

from stream.hub import StreamHub, Subscription
from stream.messages import ConfigMessage, End, FrameMessage, Message, ReplayDone
from stream.scrcpy_session import Packet, SessionInfo, StreamError, StreamEvent

CONFIG = bytes.fromhex("000000016742c0298d680900a1a420202020f08846a00000000168ce01a835c8")
IDR = b"\x00\x00\x00\x01\x65" + b"\x88" * 8
DELTA = b"\x00\x00\x00\x01\x41" + b"\x9a" * 4
CONFIG_MSG = ConfigMessage("avc1.42C029", 576, 1280)


def opening(*rest: StreamEvent) -> list[StreamEvent]:
    return [SessionInfo(576, 1280), Packet(True, False, 0, CONFIG), *rest]


def key(pts: int) -> Packet:
    return Packet(False, True, pts, IDR)


def delta(pts: int) -> Packet:
    return Packet(False, False, pts, DELTA)


def key_msg(pts: int) -> FrameMessage:
    return FrameMessage(True, pts, CONFIG + IDR)


def delta_msg(pts: int) -> FrameMessage:
    return FrameMessage(False, pts, DELTA)


class FakeSession:
    """Plays its events, then idles like a still screen until closed."""

    def __init__(self, events: list[StreamEvent] = (), *, fail_start: Exception | None = None,
                 fail_after: Exception | None = None, gate: threading.Event | None = None,
                 after_gate: list[StreamEvent] = ()) -> None:
        self._events = list(events)
        self._fail_start = fail_start
        self._fail_after = fail_after
        # If set, `events()` waits on it after `events` is exhausted, then
        # plays `after_gate` - lets a test subscribe a late joiner at an
        # exact point mid-stream.
        self._gate = gate
        self._after_gate = list(after_gate)
        self.closed = threading.Event()
        # Set once the hub has handled every scripted event: it only asks for
        # the next event after handling the previous one.
        self.played = threading.Event()

    def start(self) -> None:
        if self._fail_start is not None:
            raise self._fail_start

    def events(self) -> Iterator[StreamEvent]:
        yield from self._events
        self.played.set()
        if self._gate is not None:
            self._gate.wait()
            yield from self._after_gate
        if self._fail_after is not None:
            raise self._fail_after
        while not self.closed.is_set():
            time.sleep(0.005)
            yield None

    def close(self) -> None:
        self.closed.set()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.005)


def drain(sub: Subscription, count: int, timeout: float = 2.0) -> list[Message]:
    received: list[Message] = []
    deadline = time.monotonic() + timeout
    while len(received) < count and time.monotonic() < deadline:
        message = sub.get(0.02)
        if message is not None:
            received.append(message)
    return received


def hub_over(sessions: list[FakeSession], **kwargs: object) -> tuple[StreamHub, threading.Event, list[FakeSession]]:
    shutdown = threading.Event()
    opened: list[FakeSession] = []

    def factory() -> FakeSession:
        session = sessions.pop(0)
        opened.append(session)
        return session

    kwargs.setdefault("wait", lambda seconds: shutdown.wait(0.01))
    return StreamHub(factory, shutdown=shutdown, **kwargs), shutdown, opened  # type: ignore[arg-type]


def test_frame_message_wire_format() -> None:
    assert FrameMessage(True, 258, b"\xaa").to_bytes() == b"\x01" + (258).to_bytes(8, "big") + b"\xaa"
    assert FrameMessage(False, 0, b"").to_bytes() == b"\x00" + bytes(8)
    assert CONFIG_MSG.to_json() == '{"type": "config", "codec": "avc1.42C029", "width": 576, "height": 1280}'


def test_the_first_subscriber_starts_one_shared_session() -> None:
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1), delta(2)))])
    first = hub.subscribe()
    # `first` subscribes before the session has produced anything, so its
    # ReplayDone marker (nothing to replay) arrives before any live message.
    assert drain(first, 4) == [ReplayDone(), CONFIG_MSG, key_msg(1), delta_msg(2)]
    second = hub.subscribe()
    assert drain(second, 3) == [CONFIG_MSG, key_msg(1), delta_msg(2)]
    assert len(opened) == 1
    shutdown.set()


def test_a_keyframe_that_already_carries_its_sps_is_not_doubled() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(Packet(False, True, 1, CONFIG + IDR)))])
    assert drain(hub.subscribe(), 3) == [ReplayDone(), CONFIG_MSG, FrameMessage(True, 1, CONFIG + IDR)]
    shutdown.set()


def test_a_config_packet_before_any_session_info_is_rejected() -> None:
    # No SessionInfo ever arrived, so the hub has no real size to attach to a
    # ConfigMessage - sending a 0x0 one would be worse than refusing it.
    hub, shutdown, _ = hub_over([FakeSession([Packet(True, False, 0, CONFIG)])])
    sub = hub.subscribe()
    assert drain(sub, 2) == [ReplayDone(), End.UNAVAILABLE]
    shutdown.set()


def test_frames_before_the_first_keyframe_are_dropped() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(delta(1), key(2)))])
    assert drain(hub.subscribe(), 4, timeout=0.3) == [ReplayDone(), CONFIG_MSG, key_msg(2)]
    shutdown.set()


def test_a_late_joiner_gets_the_current_picture_on_a_still_screen() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(key(1), delta(2), delta(3)))])
    assert len(drain(hub.subscribe(), 4)) == 4
    # The session is idling now: no packet will ever arrive for the newcomer.
    assert drain(hub.subscribe(), 4) == [CONFIG_MSG, key_msg(1), delta_msg(2), delta_msg(3)]
    shutdown.set()


def test_a_subscriber_with_nothing_cached_still_gets_a_replay_done_marker() -> None:
    # No config, no GOP - but a subscriber must still get exactly one
    # ReplayDone, and nothing else, so it never mistakes "there was nothing to
    # replay" for "still replaying".
    hub, shutdown, _ = hub_over([FakeSession()])
    sub = hub.subscribe()
    assert drain(sub, 2, timeout=0.3) == [ReplayDone()]
    shutdown.set()


def test_a_late_joiner_receives_the_replay_done_marker_after_the_gop_replay() -> None:
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1), delta(2)))])
    hub.subscribe()
    wait_until(lambda: bool(opened) and opened[0].played.is_set())
    joiner = hub.subscribe()
    assert drain(joiner, 4) == [CONFIG_MSG, key_msg(1), delta_msg(2), ReplayDone()]
    shutdown.set()


def test_a_new_config_resets_the_cache_and_reaches_every_viewer() -> None:
    rotated = ConfigMessage("avc1.42C029", 720, 1280)
    events = opening(key(1), delta(2), SessionInfo(720, 1280), Packet(True, False, 0, CONFIG), key(3))
    hub, shutdown, _ = hub_over([FakeSession(events)])
    assert drain(hub.subscribe(), 6) == [ReplayDone(), CONFIG_MSG, key_msg(1), delta_msg(2), rotated, key_msg(3)]
    assert drain(hub.subscribe(), 2) == [rotated, key_msg(3)]
    shutdown.set()


def test_a_gop_too_long_to_replay_is_dropped_until_the_next_keyframe() -> None:
    events = opening(key(1), delta(2), delta(3), delta(4))
    hub, shutdown, opened = hub_over([FakeSession(events)], gop_limit=3)
    hub.subscribe()
    wait_until(lambda: bool(opened) and opened[0].played.is_set())
    assert drain(hub.subscribe(), 3, timeout=0.3) == [CONFIG_MSG, ReplayDone()]
    shutdown.set()


def test_a_late_joiner_after_a_gop_overflow_waits_for_the_next_keyframe() -> None:
    # gop_limit=3: key(1), delta(2), delta(3) fill the GOP; delta(4) overflows
    # it, so the hub drops the cached GOP but stays "keyed" (it keeps
    # broadcasting deltas live). A joiner arriving in that gap has nothing
    # decodable cached, so it must not be handed a bare delta - it has to
    # wait for the next keyframe.
    gate = threading.Event()
    events = opening(key(1), delta(2), delta(3), delta(4))
    session = FakeSession(events, gate=gate, after_gate=[delta(5), key(6)])
    hub, shutdown, opened = hub_over([session], gop_limit=3)
    hub.subscribe()
    wait_until(lambda: bool(opened) and opened[0].played.is_set())
    joiner = hub.subscribe()
    gate.set()
    assert drain(joiner, 3) == [CONFIG_MSG, ReplayDone(), key_msg(6)]
    shutdown.set()


def test_a_late_joiner_replay_exceeding_capacity_is_not_capped() -> None:
    # capacity=3 would normally skip a viewer to the next keyframe after 3
    # queued frames, but a replay longer than that (up to gop_limit=10) must
    # still be delivered whole - the lag cap is for live frames only.
    events = opening(key(1), delta(2), delta(3), delta(4), delta(5), delta(6), delta(7))
    hub, shutdown, opened = hub_over([FakeSession(events)], capacity=3, gop_limit=10)
    hub.subscribe()
    wait_until(lambda: bool(opened) and opened[0].played.is_set())
    joiner = hub.subscribe()
    assert drain(joiner, 8) == [CONFIG_MSG, key_msg(1), delta_msg(2), delta_msg(3), delta_msg(4), delta_msg(5),
                                 delta_msg(6), delta_msg(7)]
    shutdown.set()


def test_replayed_frames_do_not_count_toward_the_lag_cap() -> None:
    sub = Subscription(capacity=3, on_close=lambda _: None)
    sub.put(CONFIG_MSG)
    replay = [key_msg(1), delta_msg(2), delta_msg(3), delta_msg(4), delta_msg(5), delta_msg(6), delta_msg(7)]
    for message in replay:
        sub.put(message, counts=False)
    assert drain(sub, 8) == [CONFIG_MSG, *replay]
    assert sub._frames == 0  # noqa: SLF001 - the replay must not have touched the lag counter
    # Live frames after the replay still respect the cap (capacity=3): three
    # deltas fill it, and the fourth live frame - key_msg(11) - is what trips
    # the clear. Being a keyframe, it survives the clear itself and is what
    # the viewer actually gets.
    sub.put(delta_msg(8))
    sub.put(delta_msg(9))
    sub.put(delta_msg(10))
    sub.put(key_msg(11))
    assert drain(sub, 1) == [key_msg(11)]
    assert sub._frames == 0  # noqa: SLF001 - back to 0 after the clear, never negative


def test_a_live_overflow_clears_still_queued_replay_frames_too() -> None:
    # Nothing has been drained yet: the replay (put uncounted, as subscribe()
    # does) is still sitting in the queue when 4 live deltas overflow the
    # capacity=3 cap and a keyframe follows. The clear must strip the
    # replay's own frames too, not just the live ones, leaving just the
    # config and the new keyframe - with the counter reflecting only that one
    # counted (live) frame.
    sub = Subscription(capacity=3, on_close=lambda _: None)
    sub.put(CONFIG_MSG)
    for message in (key_msg(1), delta_msg(2), delta_msg(3)):
        sub.put(message, counts=False)
    sub.put(delta_msg(4))
    sub.put(delta_msg(5))
    sub.put(delta_msg(6))
    sub.put(delta_msg(7))  # the 4th live frame: trips the clear (frames was 3)
    sub.put(key_msg(8))
    assert sub._frames == 1  # noqa: SLF001 - only the surviving keyframe counts
    assert drain(sub, 5) == [CONFIG_MSG, key_msg(8)]


def test_a_quick_resubscribe_reuses_the_session_and_linger_then_stops_it() -> None:
    clock = Clock()
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1))), FakeSession(opening(key(2)))],
                                     linger=10.0, clock=clock)
    sub = hub.subscribe()
    # Drain all 3 (ReplayDone, config, key) so key(1) is fully processed
    # before closing - otherwise the resubscribe below could race it.
    drain(sub, 3)
    sub.close()
    clock.now = 5.0  # scrolled back within the linger
    again = hub.subscribe()
    assert drain(again, 2) == [CONFIG_MSG, key_msg(1)]
    assert len(opened) == 1 and not opened[0].closed.is_set()
    again.close()
    clock.now = 16.0
    wait_until(opened[0].closed.is_set)
    assert hub.subscriber_count == 0
    fresh = hub.subscribe()
    assert drain(fresh, 3) == [ReplayDone(), CONFIG_MSG, key_msg(2)]
    assert len(opened) == 2
    shutdown.set()


def test_a_slow_viewer_skips_to_the_next_keyframe() -> None:
    sub = Subscription(capacity=3, on_close=lambda _: None)
    for message in (CONFIG_MSG, key_msg(1), delta_msg(2), delta_msg(3), delta_msg(4),
                    delta_msg(5), key_msg(6), delta_msg(7)):
        sub.put(message)
    assert drain(sub, 8, timeout=0.2) == [CONFIG_MSG, key_msg(6), delta_msg(7)]


def test_failures_tell_viewers_and_back_off_up_to_thirty_seconds() -> None:
    shutdown = threading.Event()
    delays: list[float] = []

    def wait(seconds: float) -> bool:
        delays.append(seconds)
        if len(delays) == 7:
            shutdown.set()
        return shutdown.is_set()

    sessions = [FakeSession(fail_start=StreamError("boom")) for _ in range(7)]
    hub = StreamHub(lambda: sessions.pop(0), shutdown=shutdown, wait=wait)
    sub = hub.subscribe()
    assert drain(sub, 2) == [ReplayDone(), End.UNAVAILABLE]
    wait_until(lambda: len(delays) == 7)
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_a_session_that_delivered_resets_the_backoff() -> None:
    shutdown = threading.Event()
    delays: list[float] = []

    def wait(seconds: float) -> bool:
        delays.append(seconds)
        if len(delays) == 4:
            shutdown.set()
        return shutdown.is_set()

    sessions = [FakeSession(fail_start=StreamError("a")), FakeSession(fail_start=StreamError("b")),
                FakeSession(opening(key(1)), fail_after=StreamError("dropped")),
                FakeSession(fail_start=StreamError("c"))]
    hub = StreamHub(lambda: sessions.pop(0), shutdown=shutdown, wait=wait)
    hub.subscribe()
    wait_until(lambda: len(delays) == 4)
    assert delays == [1, 2, 1, 2]


def test_no_retry_once_the_last_viewer_closes_on_an_unavailable_stream() -> None:
    # The endpoint closes the subscription right after it sees
    # End.UNAVAILABLE. Simulate that from inside `wait()` itself so the close
    # is ordered deterministically relative to the hub's own retry loop,
    # rather than racing a second thread.
    shutdown = threading.Event()
    calls: list[int] = []
    state: dict[str, object] = {"sub": None, "closed": False}

    def factory() -> FakeSession:
        calls.append(1)
        return FakeSession(fail_start=StreamError("boom"))

    def wait(seconds: float) -> bool:
        if state["sub"] is not None and not state["closed"]:
            state["sub"].close()
            state["closed"] = True
        return shutdown.is_set()

    hub = StreamHub(factory, shutdown=shutdown, wait=wait)
    state["sub"] = hub.subscribe()
    assert drain(state["sub"], 2) == [ReplayDone(), End.UNAVAILABLE]
    wait_until(lambda: hub.subscriber_count == 0)
    calls_at_close = len(calls)
    time.sleep(0.1)
    assert len(calls) == calls_at_close  # no retry with nobody watching
    shutdown.set()


def test_a_resubscribe_after_an_unwatched_failure_waits_out_the_backoff() -> None:
    shutdown = threading.Event()
    clock = Clock()
    calls: list[int] = []
    pending_waits: list[float] = []
    closed_first = threading.Event()
    subs: list[Subscription] = []

    def factory() -> FakeSession:
        calls.append(1)
        return FakeSession(fail_start=StreamError("boom"))

    def wait(seconds: float) -> bool:
        pending_waits.append(seconds)
        if not closed_first.is_set():
            subs[0].close()
            closed_first.set()
        return shutdown.is_set()

    hub = StreamHub(factory, shutdown=shutdown, wait=wait, clock=clock)
    subs.append(hub.subscribe())
    assert drain(subs[0], 2) == [ReplayDone(), End.UNAVAILABLE]
    wait_until(lambda: hub.subscriber_count == 0)
    assert len(calls) == 1  # the solo failure did not retry unwatched

    # The clock never moved, so a resubscribe right away must still wait out
    # the 1 s backoff the unwatched failure left behind, not retry at once.
    subs.append(hub.subscribe())
    wait_until(lambda: len(calls) >= 2)
    assert pending_waits[1] == 1.0  # the new thread's own first-attempt wait
    shutdown.set()


def test_shutdown_sends_going_away_and_closes_the_session() -> None:
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1)))])
    sub = hub.subscribe()
    # Drain all 3 (ReplayDone, config, key) so nothing pre-shutdown is left
    # queued ahead of the going_away that follows.
    drain(sub, 3)
    shutdown.set()
    assert drain(sub, 1) == [End.GOING_AWAY]
    assert opened[0].closed.is_set()
