"""One scrcpy server run on one device, spoken to directly over adbutils.

No scrcpy client binary and no adb binary: the jar is pushed and started
through the same adbutils connection the bot already uses, and the video
socket is read here. Nothing is decoded on the host.

Wire format, scrcpy 4.1 with send_device_meta=false and send_dummy_byte=false
(verified against a BlueStacks Air instance):

    4 bytes   codec id, b"h264"
    then a stream of 12-byte headers:
      first byte has bit 7 set -> session packet: u32 flags, u32 width, u32 height (no payload)
      otherwise                -> media packet: u64 (bit 62 config, bit 61 keyframe,
                                  low 61 bits PTS in microseconds), u32 size, payload
"""

from __future__ import annotations

import logging
import random
import socket
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from adbutils import Network

logger = logging.getLogger(__name__)

SCRCPY_VERSION = "4.1"
JAR_PATH = Path(__file__).resolve().parent.parent / "vendor" / "scrcpy" / f"scrcpy-server-v{SCRCPY_VERSION}"
REMOTE_JAR = f"/data/local/tmp/scrcpy-server-v{SCRCPY_VERSION}.jar"

_SESSION_BIT = 0x80
_CONFIG_BIT = 1 << 62
_KEY_BIT = 1 << 61
_PTS_MASK = (1 << 61) - 1
# Read timeouts in a row, with part of a packet already read, before the
# stream counts as dead. With the default 1 s timeout that is about 10 s.
_MAX_STALLS = 10
# A desynced header could otherwise declare a packet of several GB and drive
# _read_exact into allocating a buffer that large. No real scrcpy frame at
# our max_size settings gets anywhere close to this.
_MAX_PACKET = 16 << 20
# Cap on a single recv() call, so one huge declared packet size does not turn
# into one huge read request either.
_MAX_RECV = 1 << 20


class StreamError(Exception):
    """The scrcpy stream could not start, or stopped."""


@dataclass(frozen=True)
class SessionInfo:
    width: int
    height: int


@dataclass(frozen=True)
class Packet:
    config: bool
    key: bool
    pts_us: int
    data: bytes


StreamEvent = SessionInfo | Packet | None  # None: a read timeout with nothing pending


class StreamSession(Protocol):
    def start(self) -> None: ...
    def events(self) -> Iterator[StreamEvent]: ...
    def close(self) -> None: ...


def server_command(scid: int, *, max_size: int, max_fps: int, i_frame_interval: int) -> str:
    return (
        f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
        f"scid={scid:08x} log_level=error tunnel_forward=true audio=false control=false "
        f"video_codec=h264 max_size={max_size} max_fps={max_fps} "
        "send_device_meta=false send_dummy_byte=false "
        f"video_codec_options=i-frame-interval:int={i_frame_interval} cleanup=true"
    )


class ScrcpySession:
    def __init__(self, device: Any, *, max_size: int, max_fps: int, i_frame_interval: int,
                 jar: Path = JAR_PATH, connect_timeout: float = 5.0, read_timeout: float = 1.0,
                 scid: int | None = None) -> None:
        self._device = device
        self._max_size = max_size
        self._max_fps = max_fps
        self._i_frame_interval = i_frame_interval
        self._jar = jar
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._scid = scid if scid is not None else random.randrange(1, 0x7FFFFFFF)
        self._shell: Any = None
        self._sock: socket.socket | None = None
        self._closed = False

    def start(self) -> None:
        self._push_jar()
        self._shell = self._device.shell(
            server_command(self._scid, max_size=self._max_size, max_fps=self._max_fps,
                           i_frame_interval=self._i_frame_interval),
            stream=True,
        )
        name = f"scrcpy_{self._scid:08x}"
        deadline = time.monotonic() + self._connect_timeout
        while True:
            try:
                self._sock = self._device.create_connection(Network.LOCAL_ABSTRACT, name)
                break
            except Exception as exc:  # noqa: BLE001 - adb reports "not listening yet" several ways
                if time.monotonic() >= deadline:
                    raise StreamError(f"scrcpy socket {name} never opened: {exc}") from exc
                time.sleep(0.1)
        self._sock.settimeout(self._connect_timeout)
        codec = self._read_exact(4, idle_ok=True)
        if codec != b"h264":
            raise StreamError(f"unexpected codec {codec!r} from scrcpy")
        self._sock.settimeout(self._read_timeout)

    def events(self) -> Iterator[StreamEvent]:
        while True:
            head = self._read_exact(12, idle_ok=True)
            if head is None:
                yield None
                continue
            if head[0] & _SESSION_BIT:
                _, width, height = struct.unpack(">III", head)
                yield SessionInfo(width, height)
                continue
            flags, size = struct.unpack(">QI", head)
            if size > _MAX_PACKET:
                raise StreamError("packet too large")
            data = self._read_exact(size)
            assert data is not None  # idle_ok=False never returns None
            yield Packet(config=bool(flags & _CONFIG_BIT), key=bool(flags & _KEY_BIT),
                         pts_us=flags & _PTS_MASK, data=data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in (self._sock, self._shell):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:  # noqa: BLE001 - closing is best effort
                    logger.debug("closing scrcpy stream: %s", exc)
        try:
            # cleanup=true already makes the server exit when its socket drops.
            # This is the backstop: a leaked server keeps a software encoder
            # running on the guest CPU. `pkill -f` matches every process's
            # full command line, including pkill's own - `[s]cid=` (a regex
            # matching the literal "scid=") is in the scrcpy server's argv
            # but not in this pkill invocation's, so pkill does not match
            # (and race to kill) itself.
            self._device.shell(f"pkill -f '[s]cid={self._scid:08x}'", timeout=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stopping scrcpy server %08x: %s", self._scid, exc)

    def _push_jar(self) -> None:
        local = self._jar.stat().st_size
        try:
            remote = self._device.sync.stat(REMOTE_JAR).size
        except Exception:  # noqa: BLE001 - a missing file is just "needs pushing"
            remote = -1
        if remote != local:
            self._device.sync.push(str(self._jar), REMOTE_JAR)

    def _read_exact(self, size: int, *, idle_ok: bool = False) -> bytes | None:
        """Read exactly `size` bytes. With idle_ok, a timeout before any byte returns None."""
        if self._sock is None:
            raise StreamError("scrcpy session not started")
        buffer = bytearray()
        stalls = 0
        while len(buffer) < size:
            try:
                chunk = self._sock.recv(min(size - len(buffer), _MAX_RECV))
            except TimeoutError:
                if idle_ok and not buffer:
                    return None
                stalls += 1
                if stalls > _MAX_STALLS:
                    raise StreamError("scrcpy stream stalled mid-packet") from None
                continue
            except OSError as exc:
                raise StreamError(f"scrcpy stream read failed: {exc}") from exc
            if not chunk:
                raise StreamError("scrcpy stream ended")
            buffer += chunk
            stalls = 0
        return bytes(buffer)
