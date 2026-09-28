"""ScrcpySession speaks scrcpy 4.1's video protocol; these drive it with a scripted socket."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stream.scrcpy_session import REMOTE_JAR, Packet, ScrcpySession, SessionInfo, StreamError

CONFIG = bytes.fromhex("000000016742c0298d680900a1a420202020f08846a00000000168ce01a835c8")
IDR = b"\x00\x00\x00\x01\x65" + b"\x88" * 20
DELTA = b"\x00\x00\x00\x01\x41" + b"\x9a" * 10


def session_header(width: int, height: int) -> bytes:
    return struct.pack(">III", 0x80000000, width, height)


def media(data: bytes, *, config: bool = False, key: bool = False, pts: int = 0) -> bytes:
    flags = (1 << 62 if config else 0) | (1 << 61 if key else 0) | pts
    return struct.pack(">QI", flags, len(data)) + data


class FakeSocket:
    """recv() plays a script: bytes are returned (split to the requested size), exception types are raised."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.timeouts: list[float] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, size: int) -> bytes:
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, type):
            raise item()
        if len(item) > size:
            self.script.insert(0, item[size:])
            return item[:size]
        return item

    def close(self) -> None:
        self.closed = True


class FakeShellStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeDevice:
    def __init__(self, sockets: list[Any], *, remote_size: int = 0) -> None:
        self.sockets = list(sockets)
        self.commands: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.stream = FakeShellStream()
        self.connected_to: str | None = None
        self.sync = SimpleNamespace(
            stat=lambda path: SimpleNamespace(size=remote_size),
            push=lambda src, dst: self.pushed.append((str(src), dst)),
        )

    def shell(self, cmd: str, stream: bool = False, timeout: float | None = None) -> Any:
        self.commands.append(cmd)
        return self.stream if stream else ""

    def create_connection(self, network: Any, name: str) -> Any:
        self.connected_to = name
        item = self.sockets.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def jar(tmp_path: Path) -> Path:
    path = tmp_path / "scrcpy-server"
    path.write_bytes(b"x" * 10)
    return path


def make(device: FakeDevice, jar: Path) -> ScrcpySession:
    return ScrcpySession(device, max_size=1280, max_fps=15, i_frame_interval=2,
                         jar=jar, scid=0xABCD, connect_timeout=0.3)


def test_start_pushes_the_jar_only_when_the_device_copy_differs(jar: Path) -> None:
    same = FakeDevice([FakeSocket([b"h264"])], remote_size=10)
    make(same, jar).start()
    assert same.pushed == []
    stale = FakeDevice([FakeSocket([b"h264"])], remote_size=3)
    make(stale, jar).start()
    assert stale.pushed == [(str(jar), REMOTE_JAR)]


def test_start_runs_the_pinned_server_with_the_stream_options(jar: Path) -> None:
    device = FakeDevice([FakeSocket([b"h264"])])
    make(device, jar).start()
    command = device.commands[0]
    for part in ("com.genymobile.scrcpy.Server 4.1", "scid=0000abcd", "tunnel_forward=true",
                 "audio=false", "control=false", "video_codec=h264", "max_size=1280",
                 "max_fps=15", "send_device_meta=false", "send_dummy_byte=false",
                 "video_codec_options=i-frame-interval:int=2", "cleanup=true"):
        assert part in command
    assert device.connected_to == "scrcpy_0000abcd"


def test_start_retries_until_the_server_socket_opens(jar: Path) -> None:
    device = FakeDevice([OSError("not yet"), OSError("not yet"), FakeSocket([b"h264"])])
    make(device, jar).start()
    assert device.sockets == []


def test_start_gives_up_when_the_socket_never_opens(jar: Path) -> None:
    device = FakeDevice([OSError("nope")] * 100)
    with pytest.raises(StreamError, match="never opened"):
        make(device, jar).start()


def test_start_rejects_a_codec_other_than_h264(jar: Path) -> None:
    with pytest.raises(StreamError, match="codec"):
        make(FakeDevice([FakeSocket([b"h265"])]), jar).start()


def test_events_parse_session_config_keyframe_and_delta(jar: Path) -> None:
    sock = FakeSocket([b"h264", session_header(576, 1280), media(CONFIG, config=True),
                       media(IDR, key=True, pts=1000), media(DELTA, pts=67000)])
    session = make(FakeDevice([sock]), jar)
    session.start()
    events = session.events()
    assert next(events) == SessionInfo(576, 1280)
    assert next(events) == Packet(config=True, key=False, pts_us=0, data=CONFIG)
    assert next(events) == Packet(config=False, key=True, pts_us=1000, data=IDR)
    assert next(events) == Packet(config=False, key=False, pts_us=67000, data=DELTA)


def test_an_idle_second_yields_none_so_the_caller_can_check_in(jar: Path) -> None:
    sock = FakeSocket([b"h264", TimeoutError, media(DELTA, pts=5)])
    session = make(FakeDevice([sock]), jar)
    session.start()
    events = session.events()
    assert next(events) is None
    assert next(events) == Packet(config=False, key=False, pts_us=5, data=DELTA)


def test_a_timeout_mid_packet_keeps_the_bytes_already_read(jar: Path) -> None:
    whole = media(IDR, key=True, pts=9)
    sock = FakeSocket([b"h264", whole[:7], TimeoutError, whole[7:20], TimeoutError, whole[20:]])
    session = make(FakeDevice([sock]), jar)
    session.start()
    assert next(session.events()) == Packet(config=False, key=True, pts_us=9, data=IDR)


def test_a_packet_that_never_finishes_raises(jar: Path) -> None:
    sock = FakeSocket([b"h264", media(IDR)[:5], *([TimeoutError] * 11)])
    session = make(FakeDevice([sock]), jar)
    session.start()
    with pytest.raises(StreamError, match="stalled"):
        next(session.events())


def test_the_stream_ending_raises_stream_error(jar: Path) -> None:
    sock = FakeSocket([b"h264", media(DELTA)[:5]])
    session = make(FakeDevice([sock]), jar)
    session.start()
    with pytest.raises(StreamError, match="ended"):
        next(session.events())


def test_close_is_idempotent_and_stops_the_server_by_scid(jar: Path) -> None:
    sock = FakeSocket([b"h264"])
    device = FakeDevice([sock])
    session = make(device, jar)
    session.start()
    session.close()
    session.close()
    assert sock.closed and device.stream.closed
    assert [cmd for cmd in device.commands if "pkill" in cmd] == ["pkill -f scid=0000abcd"]
