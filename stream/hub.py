"""One live stream per worker, shared by every viewer.

A reader thread owns the scrcpy session. It starts when the first viewer
subscribes and stops STREAM_LINGER_SECONDS after the last one leaves, so a
card scrolled away and back does not restart the encoder. It keeps the
current GOP (latest keyframe and everything since), so a viewer joining
mid-stream, or joining a still screen that sends nothing new, sees the
current picture at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import config
from stream.h264 import codec_string, find_sps, starts_with_sps
from stream.messages import ConfigMessage, End, FrameMessage, Message
from stream.scrcpy_session import Packet, SessionInfo, StreamError, StreamSession

logger = logging.getLogger(__name__)

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0


class Subscription:
    """One viewer's queue. A viewer more than `capacity` frames behind resumes at the next keyframe."""

    def __init__(self, *, capacity: int, on_close: Callable[[Subscription], None]) -> None:
        self._capacity = capacity
        self._on_close = on_close
        self._items: deque[Message] = deque()
        self._frames = 0
        # A viewer never has a decodable delta until it has seen a keyframe.
        # The replayed GOP (if any) always starts with one, so this only ever
        # matters for a joiner whose cache had no keyframe to replay - e.g.
        # right after a GOP overflow reset it to [] but kept `_keyed`.
        self._skip_to_key = True
        self._cond = threading.Condition()
        self._closed = False

    def put(self, message: Message) -> None:
        with self._cond:
            if isinstance(message, FrameMessage):
                if message.key:
                    self._skip_to_key = False
                elif self._skip_to_key:
                    return
                if self._frames >= self._capacity:
                    # Too far behind. The queued frames are worthless without
                    # the ones about to be dropped, so throw them all away
                    # (config messages stay) and resume at a keyframe.
                    self._items = deque(item for item in self._items if not isinstance(item, FrameMessage))
                    self._frames = 0
                    if not message.key:
                        self._skip_to_key = True
                        return
                self._frames += 1
            self._items.append(message)
            self._cond.notify()

    def get(self, timeout: float) -> Message | None:
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            if not self._items:
                return None
            message = self._items.popleft()
            if isinstance(message, FrameMessage):
                self._frames -= 1
            return message

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
        self._on_close(self)


class StreamHub:
    def __init__(self, session_factory: Callable[[], StreamSession], *, shutdown: threading.Event,
                 linger: float = config.STREAM_LINGER_SECONDS,
                 capacity: int = config.STREAM_SUBSCRIBER_FRAMES,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], bool] | None = None, label: str = "") -> None:
        self._factory = session_factory
        self._shutdown = shutdown
        self._linger = linger
        self._capacity = capacity
        self._clock = clock
        # Returns True when shutdown was requested during the wait.
        self._wait = wait if wait is not None else shutdown.wait
        self._label = label
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._linger_until: float | None = None
        self._thread: threading.Thread | None = None
        # Only the reader thread touches these two.
        self._size = (0, 0)
        self._config_bytes = b""
        # Shared with subscribe(); guarded by _lock.
        self._config_msg: ConfigMessage | None = None
        self._gop: list[FrameMessage] = []
        self._keyed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self) -> Subscription:
        sub = Subscription(capacity=self._capacity, on_close=self._unsubscribe)
        with self._lock:
            self._subs.append(sub)
            self._linger_until = None
            if self._config_msg is not None:
                sub.put(self._config_msg)
            for frame in self._gop:
                sub.put(frame)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"live-stream {self._label}".strip(),
                                                daemon=True)
                self._thread.start()
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            if not self._subs:
                self._linger_until = self._clock() + self._linger

    def _stop_locked(self) -> bool:
        if self._shutdown.is_set():
            return True
        return not self._subs and self._linger_until is not None and self._clock() >= self._linger_until

    def _should_stop(self) -> bool:
        with self._lock:
            return self._stop_locked()

    def _reset_locked(self) -> None:
        self._config_bytes = b""
        self._config_msg = None
        self._gop = []
        self._keyed = False

    def _run(self) -> None:
        delay = _BACKOFF_START_S
        while True:
            with self._lock:
                # Decided under the lock that subscribe() takes, so a viewer
                # arriving now either sees _thread still set (and this loop
                # keeps serving it) or sees None (and starts a new thread).
                if self._stop_locked():
                    self._thread = None
                    self._reset_locked()
                    if self._shutdown.is_set():
                        for sub in self._subs:
                            sub.put(End.GOING_AWAY)
                    return
            session: StreamSession | None = None
            delivered = False
            error: Exception | None = None
            try:
                session = self._factory()
                session.start()
                for event in session.events():
                    if event is not None:
                        self._handle(event)
                        delivered = True
                    if self._should_stop():
                        break
            except Exception as exc:  # noqa: BLE001 - any failure means "no stream right now"
                error = exc
            finally:
                if session is not None:
                    session.close()
            # The cache belongs to the session that just ended, not to
            # whichever one (if any) replaces it - drop it every time a
            # session ends, not only when stopping or failing, so a
            # subscribe() that lands right after this can never be replayed
            # a torn-down session's config or GOP.
            with self._lock:
                self._reset_locked()
            if error is None:
                delay = _BACKOFF_START_S
                continue
            logger.warning("live stream %s unavailable: %s", self._label, error)
            with self._lock:
                for sub in self._subs:
                    sub.put(End.UNAVAILABLE)
            if delivered:
                delay = _BACKOFF_START_S
            if not self._wait(delay):
                delay = min(delay * 2, _BACKOFF_MAX_S)

    def _handle(self, event: SessionInfo | Packet) -> None:
        if isinstance(event, SessionInfo):
            self._size = (event.width, event.height)
            return
        if event.config:
            sps = find_sps(event.data)
            if sps is None:
                raise StreamError("config packet carried no SPS")
            self._config_bytes = event.data
            message = ConfigMessage(codec_string(sps), *self._size)
            with self._lock:
                if message != self._config_msg:
                    self._config_msg = message
                    self._gop = []
                    self._keyed = False
                    for sub in self._subs:
                        sub.put(message)
            return
        data = event.data
        if event.key and self._config_bytes and not starts_with_sps(data):
            # scrcpy sends SPS/PPS once, ahead of the stream. A keyframe that
            # carries them decodes on its own, which is what a late joiner needs.
            data = self._config_bytes + data
        frame = FrameMessage(key=event.key, pts_us=event.pts_us, data=data)
        with self._lock:
            if frame.key:
                self._gop = [frame]
                self._keyed = True
            elif not self._keyed:
                return  # nothing decodable has been sent yet
            elif self._gop:
                if len(self._gop) < self._capacity:
                    self._gop.append(frame)
                else:
                    # Longer than a viewer's queue: replaying it would overflow.
                    # Late joiners wait for the next keyframe instead.
                    self._gop = []
            for sub in self._subs:
                sub.put(frame)
