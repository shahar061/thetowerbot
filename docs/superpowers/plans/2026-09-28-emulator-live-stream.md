# Emulator Live Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show smooth live video of each emulator in the fleet grid, on the
device page and on the phone. It is an on-demand scrcpy H.264 stream that each
worker relays over a WebSocket, decoded in the browser. The existing MJPEG feed
stays as the fallback.

**Architecture:**

- Each worker has one `StreamHub`, which owns one `ScrcpySession`.
  - The session talks scrcpy 4.1's protocol directly through `adbutils`.
  - The hub starts the session when the first viewer subscribes and stops it
    10 s after the last one leaves. It keeps the current GOP so late joiners
    get a picture straight away, and relays access units unchanged.
- `WS /api/stream` on the worker's existing port sends those access units to
  the browser.
- In the browser, `LiveVideo` decodes them with WebCodecs onto a `<canvas>`.
  `FleetCapture`, `RemoteDeviceView` and `DeviceView` switch to MJPEG whenever
  live video is unsupported or fails.

**Tech stack:**

- Python 3.12, FastAPI/Starlette WebSockets, `adbutils` 2.12, `websockets`
  (the uvicorn WebSocket backend), and the scrcpy-server 4.1 jar.
- Next 15 / React 19 / TypeScript strict, WebCodecs `VideoDecoder`, vitest +
  Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-28-emulator-live-stream-design.md`

## Refinements to the spec

These were made while writing the plan. Each comes from something verified on
the device or read in the code:

1. **Protocol.** scrcpy 4.1 sends a 4-byte codec id, then 12-byte headers.
   Bit 7 of the first byte marks a *session packet* (flags, width, height).
   Media packets use bit 62 for config, bit 61 for keyframe, and the low 61
   bits for the PTS. This was verified on BlueStacks Air, and the spec has been
   updated to match.
2. **`ScrcpySession.close()`** also runs `pkill -f scid=<scid>` on the device.
   A leaked server would keep a software encoder burning guest CPU.
3. **No `paused` prop on `LiveVideo`.** Unmounting it closes the stream, which
   is how all three views pause. `useLiveSupported` became `liveSupported()`
   plus a `useLiveFallback()` hook that also owns the 30 s retry.
4. **Close code `4409`.** The browser falls back to MJPEG. `/api/frame` then
   returns its own 409, and the view shows the unavailable state it shows
   today. This keeps a single close handler.
5. **`STREAM_FIRST_FRAME_TIMEOUT_S`.** Only the browser uses it, so it lives in
   the UI as `FIRST_FRAME_TIMEOUT_MS`.
6. **New config values:**
   - `STREAM_SUBSCRIBER_FRAMES = 60`: the per-viewer queue, and also the
     longest GOP kept for late joiners, so a replay always fits in a viewer's
     queue.
   - `STREAM_ACCOUNT_RECHECK_SECONDS = 1.0`.
7. **Badge text** is **Live** / **Snapshots** rather than "0.5 fps". The scan
   rate varies (4 fps while a tap is being confirmed), so "0.5 fps" would often
   be wrong.

## Global Constraints

- scrcpy server: version **4.1**, committed at `vendor/scrcpy/scrcpy-server-v4.1`,
  733706 bytes, sha256 `deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae`,
  pushed to `/data/local/tmp/scrcpy-server-v4.1.jar`.
- Stream: H.264, `max_size=1280`, `max_fps=15`,
  `video_codec_options=i-frame-interval:int=2`, `audio=false`, `control=false`.
- Linger 10 s. Backoff 1, 2, 4, 8, 16, then 30 s.
- Close codes: `4403` origin rejected, `4409` account mismatch, `4503`
  unavailable or disabled, `1001` worker shutting down.
- Browser: 5 s first-frame timeout, retry live 30 s after a fallback, and skip
  deltas while `decodeQueueSize > 30`.
- The bot's scan loop, `GuardedDevice` and the `screencap -p` capture path must
  not change.
- Workers keep binding to loopback. Every WebSocket passes the Origin check.
- Python: type hints on all functions, and `from __future__ import annotations`
  as in the existing modules.
- **Tests:** run only the new or changed test files. Never run the whole suite
  (`pytest tests/`, `vitest run` with no file). This repo has no CI, so these
  local runs are the gate.
- **Commits:** the user approves every commit. At each "Commit" step, stage the
  files, show `git diff --cached --stat`, and wait for a yes. Commit messages
  end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These are the five inputs most likely to hurt a real user that the spec implies
but doesn't spell out. Each is pinned by a named test:

1. **A viewer leaves while the screen is still.** No frames are being sent, so
   a failed send can't reveal the disconnect. The server must still notice and
   release the stream, or scrcpy runs forever.
   → `test_a_viewer_leaving_a_still_screen_releases_its_subscription` (Task 4)
2. **Someone joins while the screen is still.** The encoder sends nothing new,
   so the viewer must get the current picture from the cache.
   → `test_a_late_joiner_gets_the_current_picture_on_a_still_screen` (Task 3)
3. **The socket stalls or times out mid-packet.** The bytes already read must
   be kept, or every later frame is garbage.
   → `test_a_timeout_mid_packet_keeps_the_bytes_already_read` and
   `test_a_packet_that_never_finishes_raises` (Task 2)
4. **The worker's account changes while a stream is open.** The stream must
   close with 4409 instead of showing the new account's screen under the old
   one's label.
   → `test_an_account_change_mid_stream_closes_with_4409` (Task 4)
5. **A card is scrolled away and back, or the tab is hidden and shown, within
   the linger window.** scrcpy must not restart, and a hidden tab must stop
   pulling video.
   → `test_a_quick_resubscribe_reuses_the_session_and_linger_then_stops_it`
   (Task 3) and `stops the stream while the tab is hidden` (Task 7)

---

### Task 1: Pinned scrcpy server and H.264 helpers

**Files:**
- Create: `vendor/scrcpy/scrcpy-server-v4.1` (binary copy)
- Create: `vendor/scrcpy/README.md`
- Create: `stream/__init__.py`
- Create: `stream/h264.py`
- Test: `tests/test_stream_h264.py`

**Interfaces:**
- Produces:
  - `stream.h264.nal_units(annexb: bytes) -> list[bytes]`
  - `nal_type(unit: bytes) -> int`
  - `find_sps(annexb: bytes) -> bytes | None`
  - `codec_string(sps: bytes) -> str`
  - `starts_with_sps(annexb: bytes) -> bool`
  - `NAL_SPS = 7`

- [ ] **Step 1: Vendor the jar**

```bash
mkdir -p vendor/scrcpy stream
cp /opt/homebrew/share/scrcpy/scrcpy-server vendor/scrcpy/scrcpy-server-v4.1
shasum -a 256 vendor/scrcpy/scrcpy-server-v4.1
```

Expected: `deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae`.
If Homebrew's scrcpy is no longer 4.1, get `scrcpy-server-v4.1` from
https://github.com/Genymobile/scrcpy/releases/tag/v4.1 and check the same
checksum.

- [ ] **Step 2: Write `vendor/scrcpy/README.md`**

```markdown
# scrcpy-server v4.1

`scrcpy-server-v4.1` is the unmodified device-side server from scrcpy 4.1
(https://github.com/Genymobile/scrcpy, Apache-2.0). `stream/scrcpy_session.py`
pushes it to the emulator and speaks its video protocol directly, so the
version is pinned: the protocol changes between scrcpy releases, and a
`brew upgrade scrcpy` must not break the live stream.

- Version: 4.1
- Size: 733706 bytes
- sha256: deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae

To upgrade: replace the file, update `SCRCPY_VERSION` in
`stream/scrcpy_session.py`, and re-verify the header layout documented there
against a real device.
```

- [ ] **Step 3: Write the failing test** in `tests/test_stream_h264.py`

```python
"""H.264 helpers for the live stream, checked against a real BlueStacks Air config packet."""

from __future__ import annotations

import pytest

from stream.h264 import codec_string, find_sps, nal_type, nal_units, starts_with_sps

# SPS + PPS exactly as scrcpy 4.1 sent them from a BlueStacks Air instance
# (576x1280, Baseline profile, level 4.1).
CONFIG = bytes.fromhex(
    "000000016742c0298d680900a1a420202020f08846a00000000168ce01a835c8"
)
IDR = b"\x00\x00\x00\x01\x65\x88\x84\x00"


def test_nal_units_split_on_four_byte_start_codes() -> None:
    assert [nal_type(unit) for unit in nal_units(CONFIG)] == [7, 8]


def test_nal_units_split_on_three_byte_start_codes() -> None:
    stream = b"\x00\x00\x01\x67\x42\xc0\x29\x00\x00\x01\x68\xce"
    assert nal_units(stream) == [b"\x67\x42\xc0\x29", b"\x68\xce"]


def test_codec_string_reads_profile_constraints_and_level() -> None:
    sps = find_sps(CONFIG)
    assert sps is not None
    assert codec_string(sps) == "avc1.42C029"


def test_codec_string_rejects_a_non_sps_unit() -> None:
    with pytest.raises(ValueError):
        codec_string(nal_units(CONFIG)[1])


def test_find_sps_is_none_without_one() -> None:
    assert find_sps(IDR) is None


def test_starts_with_sps() -> None:
    assert starts_with_sps(CONFIG + IDR)
    assert not starts_with_sps(IDR)
    assert not starts_with_sps(b"")
```

- [ ] **Step 4: Run it and check that it fails**

Run: `uv run pytest tests/test_stream_h264.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stream'`.

- [ ] **Step 5: Implement**

`stream/__init__.py`:

```python
"""On-demand live video from the emulator (scrcpy -> WebSocket -> browser WebCodecs)."""
```

`stream/h264.py`:

```python
"""Just enough H.264 Annex-B parsing to name the codec and spot an SPS.

The live stream never decodes video on the host; the browser does. The host
only needs the SPS (to tell the browser which decoder to build) and to know
whether a keyframe already carries SPS/PPS.
"""

from __future__ import annotations

NAL_SPS = 7
_START = b"\x00\x00\x01"


def nal_units(annexb: bytes) -> list[bytes]:
    """Split Annex-B on 3- or 4-byte start codes; each unit starts with its header byte."""
    units: list[bytes] = []
    index = annexb.find(_START)
    while index != -1:
        begin = index + len(_START)
        following = annexb.find(_START, begin)
        unit = annexb[begin:] if following == -1 else annexb[begin:following]
        if following != -1 and unit.endswith(b"\x00"):
            # The first zero of a 4-byte start code. A NAL unit never ends in a
            # zero byte (rbsp trailing bits), so this cannot eat real data.
            unit = unit[:-1]
        if unit:
            units.append(unit)
        index = following
    return units


def nal_type(unit: bytes) -> int:
    return unit[0] & 0x1F


def find_sps(annexb: bytes) -> bytes | None:
    return next((unit for unit in nal_units(annexb) if nal_type(unit) == NAL_SPS), None)


def codec_string(sps: bytes) -> str:
    """The RFC 6381 name WebCodecs wants: avc1.PPCCLL (profile, constraint flags, level)."""
    if len(sps) < 4 or nal_type(sps) != NAL_SPS:
        raise ValueError("not an SPS NAL unit")
    return f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


def starts_with_sps(annexb: bytes) -> bool:
    units = nal_units(annexb)
    return bool(units) and nal_type(units[0]) == NAL_SPS
```

- [ ] **Step 6: Run it and check that it passes**

Run: `uv run pytest tests/test_stream_h264.py -q`
Expected: 6 passed.

- [ ] **Step 7: Commit** (ask the user first)

```bash
git add vendor/scrcpy stream/__init__.py stream/h264.py tests/test_stream_h264.py
git commit -m "Pin scrcpy-server 4.1 and add H.264 helpers for the live stream"
```

---

### Task 2: `ScrcpySession` — scrcpy 4.1 over adbutils

**Files:**
- Create: `stream/scrcpy_session.py`
- Test: `tests/test_stream_session.py`

**Interfaces:**
- Consumes: the vendored jar from Task 1.
- Produces:
  - `SCRCPY_VERSION = "4.1"`, `JAR_PATH: Path`, and
    `REMOTE_JAR = "/data/local/tmp/scrcpy-server-v4.1.jar"`.
  - `class StreamError(Exception)`.
  - `@dataclass(frozen=True) SessionInfo(width: int, height: int)`.
  - `@dataclass(frozen=True) Packet(config: bool, key: bool, pts_us: int, data: bytes)`.
  - `StreamEvent = SessionInfo | Packet | None`. `None` is an idle tick.
  - `class StreamSession(Protocol)` with `start() -> None`,
    `events() -> Iterator[StreamEvent]` and `close() -> None`.
  - `server_command(scid: int, *, max_size: int, max_fps: int, i_frame_interval: int) -> str`.
  - `ScrcpySession(device, *, max_size, max_fps, i_frame_interval, jar=JAR_PATH, connect_timeout=5.0, read_timeout=1.0, scid=None)`,
    which implements `StreamSession`.

- [ ] **Step 1: Write the failing tests** in `tests/test_stream_session.py`

```python
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
```

- [ ] **Step 2: Run them and check that they fail**

Run: `uv run pytest tests/test_stream_session.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stream.scrcpy_session'`.

- [ ] **Step 3: Implement** `stream/scrcpy_session.py`

```python
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
        f"scid={scid:08x} log_level=warn tunnel_forward=true audio=false control=false "
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
            # running on the guest CPU.
            self._device.shell(f"pkill -f scid={self._scid:08x}", timeout=1)
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
                chunk = self._sock.recv(size - len(buffer))
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
```

- [ ] **Step 4: Run the tests and check that they pass**

Run: `uv run pytest tests/test_stream_session.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit** (ask the user first)

```bash
git add stream/scrcpy_session.py tests/test_stream_session.py
git commit -m "Add ScrcpySession: scrcpy 4.1 video protocol over adbutils"
```

---

### Task 3: Stream messages and `StreamHub`

**Files:**
- Create: `stream/messages.py`
- Create: `stream/hub.py`
- Modify: `config.py`, after `FRAME_POLL_SECONDS` (around line 618)
- Test: `tests/test_stream_hub.py`

**Interfaces:**
- Consumes:
  - From Task 1: `codec_string`, `find_sps` and `starts_with_sps`.
  - From Task 2: `SessionInfo`, `Packet`, `StreamError`, `StreamEvent` and
    `StreamSession`.
- Produces:
  - `ConfigMessage(codec: str, width: int, height: int)` with `.to_json() -> str`.
  - `FrameMessage(key: bool, pts_us: int, data: bytes)` with `.to_bytes() -> bytes`.
  - `End.UNAVAILABLE` and `End.GOING_AWAY`.
  - `Message = ConfigMessage | FrameMessage | End`.
  - `Subscription(*, capacity: int, on_close: Callable[[Subscription], None])`,
    with `.put(message: Message) -> None`,
    `.get(timeout: float) -> Message | None` and `.close() -> None`.
  - `StreamHub(session_factory: Callable[[], StreamSession], *, shutdown: threading.Event, linger=config.STREAM_LINGER_SECONDS, capacity=config.STREAM_SUBSCRIBER_FRAMES, clock=time.monotonic, wait=None, label="")`,
    with `.subscribe() -> Subscription` and `.subscriber_count -> int`.
  - New config values: `STREAM_ENABLED`, `STREAM_MAX_SIZE`, `STREAM_MAX_FPS`,
    `STREAM_I_FRAME_INTERVAL_S`, `STREAM_LINGER_SECONDS`,
    `STREAM_SUBSCRIBER_FRAMES` and `STREAM_ACCOUNT_RECHECK_SECONDS`.

- [ ] **Step 1: Add the config block** to `config.py`, directly after the
  `FRAME_POLL_SECONDS: float = 0.25` line

```python

# --- Live video stream ------------------------------------------------------
# An on-demand scrcpy H.264 stream per worker, for viewing only: the bot still
# decides from its own screencap. The measured cost is in
# docs/superpowers/specs/2026-09-28-emulator-live-stream-design.md. False makes
# /api/stream refuse with 4503, and every view falls back to MJPEG.
STREAM_ENABLED: bool = True
# 1280 on the long side at 15 fps cost about +7% guest CPU and +15% of one
# host core per watched emulator in the spike. Full size at 30 fps cost 4x that.
STREAM_MAX_SIZE: int = 1280
STREAM_MAX_FPS: int = 15
# Keyframe spacing while the screen moves. It bounds how much a viewer joining
# mid-stream has to replay before it sees the current picture.
STREAM_I_FRAME_INTERVAL_S: int = 2
# How long the stream outlives its last viewer, so a card scrolled away and
# back (or a tab flipped) does not restart the encoder.
STREAM_LINGER_SECONDS: float = 10.0
# How far a viewer may fall behind (4 s at 15 fps) before it skips to the next
# keyframe. Also the longest GOP kept for late joiners, so a replay always fits.
STREAM_SUBSCRIBER_FRAMES: int = 60
# How often an open stream re-checks that the selected account still runs here.
STREAM_ACCOUNT_RECHECK_SECONDS: float = 1.0
```

- [ ] **Step 2: Write the failing tests** in `tests/test_stream_hub.py`

```python
"""StreamHub: one scrcpy session per worker, shared by every viewer."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

from stream.hub import StreamHub, Subscription
from stream.messages import ConfigMessage, End, FrameMessage, Message
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
                 fail_after: Exception | None = None) -> None:
        self._events = list(events)
        self._fail_start = fail_start
        self._fail_after = fail_after
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
    assert drain(first, 3) == [CONFIG_MSG, key_msg(1), delta_msg(2)]
    second = hub.subscribe()
    assert drain(second, 3) == [CONFIG_MSG, key_msg(1), delta_msg(2)]
    assert len(opened) == 1
    shutdown.set()


def test_a_keyframe_that_already_carries_its_sps_is_not_doubled() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(Packet(False, True, 1, CONFIG + IDR)))])
    assert drain(hub.subscribe(), 2) == [CONFIG_MSG, FrameMessage(True, 1, CONFIG + IDR)]
    shutdown.set()


def test_frames_before_the_first_keyframe_are_dropped() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(delta(1), key(2)))])
    assert drain(hub.subscribe(), 3, timeout=0.3) == [CONFIG_MSG, key_msg(2)]
    shutdown.set()


def test_a_late_joiner_gets_the_current_picture_on_a_still_screen() -> None:
    hub, shutdown, _ = hub_over([FakeSession(opening(key(1), delta(2), delta(3)))])
    assert len(drain(hub.subscribe(), 4)) == 4
    # The session is idling now: no packet will ever arrive for the newcomer.
    assert drain(hub.subscribe(), 4) == [CONFIG_MSG, key_msg(1), delta_msg(2), delta_msg(3)]
    shutdown.set()


def test_a_new_config_resets_the_cache_and_reaches_every_viewer() -> None:
    rotated = ConfigMessage("avc1.42C029", 720, 1280)
    events = opening(key(1), delta(2), SessionInfo(720, 1280), Packet(True, False, 0, CONFIG), key(3))
    hub, shutdown, _ = hub_over([FakeSession(events)])
    assert drain(hub.subscribe(), 5) == [CONFIG_MSG, key_msg(1), delta_msg(2), rotated, key_msg(3)]
    assert drain(hub.subscribe(), 2) == [rotated, key_msg(3)]
    shutdown.set()


def test_a_gop_too_long_to_replay_is_dropped_until_the_next_keyframe() -> None:
    events = opening(key(1), delta(2), delta(3), delta(4))
    hub, shutdown, opened = hub_over([FakeSession(events)], capacity=3)
    hub.subscribe()
    wait_until(lambda: bool(opened) and opened[0].played.is_set())
    assert drain(hub.subscribe(), 2, timeout=0.3) == [CONFIG_MSG]
    shutdown.set()


def test_a_quick_resubscribe_reuses_the_session_and_linger_then_stops_it() -> None:
    clock = Clock()
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1))), FakeSession(opening(key(2)))],
                                     linger=10.0, clock=clock)
    sub = hub.subscribe()
    drain(sub, 2)
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
    assert drain(fresh, 2) == [CONFIG_MSG, key_msg(2)]
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
    assert drain(sub, 1) == [End.UNAVAILABLE]
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


def test_shutdown_sends_going_away_and_closes_the_session() -> None:
    hub, shutdown, opened = hub_over([FakeSession(opening(key(1)))])
    sub = hub.subscribe()
    drain(sub, 2)
    shutdown.set()
    assert drain(sub, 1) == [End.GOING_AWAY]
    assert opened[0].closed.is_set()
```

- [ ] **Step 3: Run them and check that they fail**

Run: `uv run pytest tests/test_stream_hub.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stream.hub'`.

- [ ] **Step 4: Implement** `stream/messages.py`

```python
"""What the live stream sends to viewers (see docs/superpowers/specs/2026-09-28-emulator-live-stream-design.md)."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigMessage:
    """Everything a browser VideoDecoder needs before the first keyframe."""

    codec: str
    width: int
    height: int

    def to_json(self) -> str:
        return json.dumps({"type": "config", "codec": self.codec, "width": self.width, "height": self.height})


@dataclass(frozen=True)
class FrameMessage:
    """One H.264 access unit. Keyframes carry SPS/PPS, so each decodes on its own."""

    key: bool
    pts_us: int
    data: bytes

    def to_bytes(self) -> bytes:
        return bytes([1 if self.key else 0]) + self.pts_us.to_bytes(8, "big") + self.data


class End(enum.Enum):
    UNAVAILABLE = "unavailable"  # the session failed; viewers fall back to MJPEG
    GOING_AWAY = "going_away"  # the worker process is shutting down


Message = ConfigMessage | FrameMessage | End
```

- [ ] **Step 5: Implement** `stream/hub.py`

```python
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
        self._skip_to_key = False
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
            if error is None:
                delay = _BACKOFF_START_S
                continue
            logger.warning("live stream %s unavailable: %s", self._label, error)
            with self._lock:
                self._reset_locked()
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
```

- [ ] **Step 6: Run the tests and check that they pass**

Run: `uv run pytest tests/test_stream_hub.py -q`
Expected: 12 passed.

- [ ] **Step 7: Commit** (ask the user first)

```bash
git add config.py stream/messages.py stream/hub.py tests/test_stream_hub.py
git commit -m "Add StreamHub: one shared on-demand stream per worker with late-joiner cache"
```

---

### Task 4: Origin check and `WS /api/stream`

**Files:**
- Create: `stream/origin.py`
- Modify: `web/app.py`
  - Imports (lines 34-35).
  - `create_app` signature (line 354).
  - The `_live_choice` annotation (line 448).
  - Add the new route directly after `frame_mjpeg` (after line 1069).
- Modify: `pyproject.toml` and `uv.lock`, via `uv add`.
- Test: `tests/test_stream_api.py`

**Interfaces:**
- Consumes:
  - From Task 3: `StreamHub.subscribe()`, `Subscription.get/close`,
    `ConfigMessage.to_json`, `FrameMessage.to_bytes`, `End`, and
    `config.STREAM_ENABLED` / `STREAM_ACCOUNT_RECHECK_SECONDS`.
- Produces:
  - `stream.origin.origin_allowed(origin: str | None, host: str | None) -> bool`.
  - A `create_app(..., stream_hub: StreamHub | None = None)` keyword.
  - `WS /api/stream`, which takes the optional query parameters `scope` and
    `expected_account_id`.

- [ ] **Step 1: Add the WebSocket backend dependency**

Run: `uv add "websockets>=15"`
Expected: `pyproject.toml` lists `websockets>=15`, and `uv.lock` is updated.
Without it, uvicorn refuses WebSocket upgrades. Starlette's `TestClient` works
either way, so this has to come first for real servers.

- [ ] **Step 2: Write the failing tests** in `tests/test_stream_api.py`

```python
"""WS /api/stream: the live video endpoint."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config
import db
from autopilot import AutopilotState
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from stream.messages import ConfigMessage, End, FrameMessage, Message
from stream.origin import origin_allowed
from web.app import create_app

CONFIG_MSG = ConfigMessage("avc1.42C029", 576, 1280)
KEY = FrameMessage(True, 1000, b"\x00\x00\x00\x01\x65\x88")


class FakeSubscription:
    def __init__(self, messages: list[Message], on_get: Any = None) -> None:
        self.messages = list(messages)
        self.on_get = on_get
        self.gets = 0
        self.closed = threading.Event()

    def get(self, timeout: float) -> Message | None:
        self.gets += 1
        if self.on_get is not None:
            self.on_get(self.gets)
        if self.messages:
            return self.messages.pop(0)
        time.sleep(min(timeout, 0.02))
        return None

    def close(self) -> None:
        self.closed.set()


class FakeHub:
    def __init__(self, messages: list[Message] = (), on_get: Any = None) -> None:
        self.messages = list(messages)
        self.on_get = on_get
        self.subscriptions: list[FakeSubscription] = []

    def subscribe(self) -> FakeSubscription:
        sub = FakeSubscription(self.messages, self.on_get)
        self.subscriptions.append(sub)
        return sub


def client_for(hub: Any, **kwargs: Any) -> TestClient:
    kwargs.setdefault("db_path", None)
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 unknown_dir=config.UNKNOWN_DIR, stream_hub=hub, **kwargs))


def close_code(client: TestClient, url: str = "/api/stream", headers: dict[str, str] | None = None) -> int:
    with client.websocket_connect(url, headers=headers or {}) as ws:
        while True:
            message = ws.receive()
            if message["type"] == "websocket.close":
                return int(message["code"])


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


@pytest.mark.parametrize("origin, host, allowed", [
    (None, "127.0.0.1:10059", True),
    ("http://127.0.0.1:8765", "127.0.0.1:10059", True),
    ("http://localhost:3000", "127.0.0.1:10059", True),
    ("http://[::1]:8765", "[::1]:10059", True),
    ("https://mac.tail.ts.net", "mac.tail.ts.net:10059", True),
    ("https://evil.example", "127.0.0.1:10059", False),
    ("https://evil.example", "mac.tail.ts.net:10059", False),
    ("null", "127.0.0.1:10059", False),
    ("https://mac.tail.ts.net", None, False),
])
def test_origin_allowed(origin: str | None, host: str | None, allowed: bool) -> None:
    assert origin_allowed(origin, host) is allowed


def test_streams_the_config_then_binary_frames() -> None:
    hub = FakeHub([CONFIG_MSG, KEY])
    with client_for(hub).websocket_connect("/api/stream") as ws:
        assert json.loads(ws.receive_text()) == {"type": "config", "codec": "avc1.42C029",
                                                 "width": 576, "height": 1280}
        data = ws.receive_bytes()
    assert data[0] == 1
    assert int.from_bytes(data[1:9], "big") == 1000
    assert data[9:] == KEY.data


def test_an_unavailable_stream_closes_with_4503() -> None:
    hub = FakeHub([End.UNAVAILABLE])
    assert close_code(client_for(hub)) == 4503
    wait_until(hub.subscriptions[0].closed.is_set)


def test_a_worker_without_a_hub_closes_with_4503() -> None:
    assert close_code(client_for(None)) == 4503


def test_a_disabled_stream_closes_with_4503_without_subscribing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STREAM_ENABLED", False)
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub)) == 4503
    assert hub.subscriptions == []


def test_shutdown_closes_with_1001() -> None:
    assert close_code(client_for(FakeHub([End.GOING_AWAY]))) == 1001


def test_a_foreign_origin_closes_with_4403() -> None:
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub), headers={"origin": "https://evil.example"}) == 4403
    assert hub.subscriptions == []


def test_the_tailscale_host_is_accepted_via_forwarded_host() -> None:
    hub = FakeHub([CONFIG_MSG])
    headers = {"origin": "https://mac.tail.ts.net", "x-forwarded-host": "mac.tail.ts.net:10059"}
    with client_for(hub).websocket_connect("/api/stream", headers=headers) as ws:
        assert json.loads(ws.receive_text())["type"] == "config"


def test_an_expected_account_that_is_not_here_closes_with_4409() -> None:
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub), "/api/stream?expected_account_id=ACCOUNT_A") == 4409
    assert hub.subscriptions == []


def test_a_viewer_leaving_a_still_screen_releases_its_subscription() -> None:
    hub = FakeHub([CONFIG_MSG])  # then nothing: a still screen sends no frames
    with client_for(hub).websocket_connect("/api/stream") as ws:
        ws.receive_text()
    wait_until(hub.subscriptions[0].closed.is_set)


def _worker(root: Path, name: str, account_id: str) -> Path:
    path = root / "workers" / name
    binding = path / "checkpoints" / ("a" * 32 + ".json")
    binding.parent.mkdir(parents=True)
    identity = {"worker_id": name, "account_id": account_id,
                "endpoint": "127.0.0.1:5555", "lease_id": "lease"}
    binding.write_text(json.dumps(identity))
    (path / "fleet-registration.json").write_text(json.dumps({
        **identity, "state": "registered", "instance": name, "binding": str(binding),
        "web_port": 10000 + int(name.rsplit("_", 1)[-1]),
    }))
    return path / "tower_bot.db"


def test_an_account_change_mid_stream_closes_with_4409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STREAM_ACCOUNT_RECHECK_SECONDS", 0.0)
    root = tmp_path / "fleet"
    path = _worker(root, "Tiramisu64_18", "ACCOUNT_A")
    db.bind_account(path, "ACCOUNT_A")
    runner = type("Runner", (), {"autopilot_state": AutopilotState(),
                                 "status": lambda self: {"running": True},
                                 "verified_account": lambda self: "ACCOUNT_A"})()

    def move_account(gets: int) -> None:
        if gets != 2:
            return
        # The same registration switch the MJPEG revalidation test makes.
        registration = path.parent / "fleet-registration.json"
        record = json.loads(registration.read_text())
        record["account_id"] = "ACCOUNT_B"
        registration.write_text(json.dumps(record))
        binding = Path(record["binding"])
        identity = json.loads(binding.read_text())
        identity["account_id"] = "ACCOUNT_B"
        binding.write_text(json.dumps(identity))

    hub = FakeHub([CONFIG_MSG], on_get=move_account)
    client = client_for(hub, db_path=path, runner=runner, fleet=type("Fleet", (), {"root": root})())
    url = "/api/stream?scope=worker:Tiramisu64_18&expected_account_id=ACCOUNT_A"
    assert close_code(client, url) == 4409
```

- [ ] **Step 3: Run them and check that they fail**

Run: `uv run pytest tests/test_stream_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stream.origin'`.

- [ ] **Step 4: Implement** `stream/origin.py`

```python
"""Which pages may open a worker's live stream.

Workers listen on loopback, but any website open in a browser on this Mac can
still dial ws://127.0.0.1:<port>. Unlike an MJPEG <img>, whose pixels a foreign
page cannot read, a WebSocket hands that page the video. So only our own pages
get it: loopback origins (the local dashboard and worker ports), or the page
host the request arrived on (Tailscale Serve's https://<mac>.ts.net). A
browser cannot forge Origin, or Host / X-Forwarded-Host, on a WebSocket.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str) -> str | None:
    return urlsplit(value if "//" in value else f"//{value}").hostname


def origin_allowed(origin: str | None, host: str | None) -> bool:
    if origin is None:
        return True  # not a browser page (curl, tools/stream_probe.py)
    name = _hostname(origin)
    if name is None:
        return False
    if name in _LOOPBACK:
        return True
    return host is not None and _hostname(host) == name
```

- [ ] **Step 5: Wire the endpoint into `web/app.py`**

Change the FastAPI imports at lines 34-35 to:

```python
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.requests import HTTPConnection
```

Add these after `from frames import FrameBuffer` (line 63):

```python
from stream.hub import StreamHub
from stream.messages import ConfigMessage, End, FrameMessage
from stream.origin import origin_allowed
```

Add the keyword to `create_app`'s signature, after `telegram_suppressed: bool = False,`:

```python
    stream_hub: StreamHub | None = None,
```

Widen `_live_choice` so it accepts a WebSocket too. Both are HTTPConnections
with `query_params`:

```python
    def _live_choice(request: HTTPConnection) -> AccountChoice | None:
```

Insert this route directly after the `frame_mjpeg` function, before the static
mount at the end:

```python
    @app.websocket("/api/stream")
    async def live_stream(websocket: WebSocket) -> None:
        # Accept before any close: a close sent during the handshake reaches
        # the browser as a bare 1006, and the page needs these codes to choose
        # its fallback.
        await websocket.accept()
        host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host")
        if not origin_allowed(websocket.headers.get("origin"), host):
            await websocket.close(code=4403)
            return
        try:
            choice = _live_choice(websocket)
        except HTTPException:
            await websocket.close(code=4409)
            return
        if stream_hub is None or not config.STREAM_ENABLED:
            await websocket.close(code=4503)
            return
        subscription = stream_hub.subscribe()
        # The client never sends anything, so this only completes when it goes
        # away. On a still screen there are no sends to fail, so this is the
        # only way to notice.
        gone = asyncio.ensure_future(websocket.receive())
        checked = time.monotonic()
        try:
            while not shutdown.is_set() and not gone.done():
                message = await asyncio.to_thread(subscription.get, 0.5)
                if isinstance(message, ConfigMessage):
                    await websocket.send_text(message.to_json())
                elif isinstance(message, FrameMessage):
                    await websocket.send_bytes(message.to_bytes())
                elif message is End.UNAVAILABLE:
                    await websocket.close(code=4503)
                    return
                elif message is End.GOING_AWAY:
                    break
                if choice is not None and time.monotonic() - checked >= config.STREAM_ACCOUNT_RECHECK_SECONDS:
                    checked = time.monotonic()
                    try:
                        current = _live_choice(websocket)
                    except HTTPException:
                        current = None
                    if current is None or current.account_id != choice.account_id:
                        await websocket.close(code=4409)
                        return
            if not gone.done():
                await websocket.close(code=1001)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass  # the viewer left mid-send
        finally:
            gone.cancel()
            subscription.close()
```

- [ ] **Step 6: Run the new tests, plus the existing frame tests that share `_live_choice`**

Run: `uv run pytest tests/test_stream_api.py tests/test_frame_api.py tests/test_account_catalog_api.py -q`
Expected: all pass (19 new).

- [ ] **Step 7: Commit** (ask the user first)

```bash
git add stream/origin.py web/app.py pyproject.toml uv.lock tests/test_stream_api.py
git commit -m "Serve the live stream over WS /api/stream with origin and account checks"
```

---

### Task 5: Wire the hub into the worker, and a probe tool

**Files:**
- Modify: `tower_bot.py`, in the `elif args.web:` block (around lines 3840-3890)
- Create: `tools/stream_probe.py`

**Interfaces:**
- Consumes:
  - From Task 2: `ScrcpySession(device, max_size=, max_fps=, i_frame_interval=)`.
  - From Task 3: `StreamHub(factory, shutdown=, label=)`.
  - From Task 4: `create_app(stream_hub=)`.
  - `BotRunner.identity() -> {"serial": str | None, ...}` (runner.py:222).
- Produces: nothing new for code. This step is what makes the stream reachable
  on a real worker.

- [ ] **Step 1: Build the hub next to the runner.** In `tower_bot.py`, directly
  after the `runner = BotRunner(...)` call and before `app = create_app(`,
  insert:

```python
            stream_hub = None
            if config.STREAM_ENABLED:
                from adbutils import AdbClient
                from stream.hub import StreamHub
                from stream.scrcpy_session import ScrcpySession

                def open_stream() -> ScrcpySession:
                    # The runner's serial is the transport the bot actually
                    # connected to (it may be the emulator-N alias). The CLI
                    # endpoint covers the time before the first connect.
                    serial = runner.identity().get("serial") or f"{args.host}:{args.port}"
                    device = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT).device(serial)
                    return ScrcpySession(device, max_size=config.STREAM_MAX_SIZE,
                                         max_fps=config.STREAM_MAX_FPS,
                                         i_frame_interval=config.STREAM_I_FRAME_INTERVAL_S)

                stream_hub = StreamHub(open_stream, shutdown=shutdown,
                                       label=args.worker_id or f"{args.host}:{args.port}")
```

Then add `stream_hub=stream_hub,` to the `create_app(...)` call, after
`telegram_suppressed=...`.

- [ ] **Step 2: Write `tools/stream_probe.py`**

```python
"""Watch a worker's live stream from the terminal and report frames, fps and bitrate.

    uv run tools/stream_probe.py 10199 --seconds 20
"""

from __future__ import annotations

import argparse
import json
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Report a worker's live stream frame rate and bitrate.")
    parser.add_argument("port", type=int, help="the worker's web port")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    frames = keys = size = 0
    first: float | None = None
    started = time.monotonic()
    try:
        with connect(f"ws://{args.host}:{args.port}/api/stream", open_timeout=5) as ws:
            while time.monotonic() - started < args.seconds:
                try:
                    message = ws.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(message, str):
                    print("config", json.loads(message))
                    continue
                if first is None:
                    first = time.monotonic()
                    print(f"first frame after {first - started:.2f}s")
                frames += 1
                keys += message[0] & 1
                size += len(message)
    except ConnectionClosed as closed:
        print(f"closed by the worker: code={closed.rcvd.code if closed.rcvd else None}")
        return 1
    elapsed = max(time.monotonic() - (first or started), 1e-6)
    print(f"{frames} frames ({keys} keyframes) in {elapsed:.1f}s = "
          f"{frames / elapsed:.1f} fps, {size * 8 / elapsed / 1e6:.2f} Mbps")
    return 0 if frames else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Smoke test against a real emulator.** Ask the user first: this
  starts a second, idle web process pointed at an emulator that a production
  worker is already driving.

The `--idle` flag starts the web server without the bot, so nothing taps the
emulator. The `--no-store` flag avoids the worktree database. Run this in the
background:

```bash
uv run tower_bot.py --web --idle --no-store --no-telegram --host 127.0.0.1 --port 6145 --web-port 10199
```

Then:

```bash
uv run tools/stream_probe.py 10199 --seconds 20
```

Expected:
- `config {'type': 'config', 'codec': 'avc1.42C029', 'width': 576, 'height': 1280}`
- "first frame after" under about 2 s
- about 15 fps while the game animates, and at least one keyframe every 2 s
  while frames flow
- about 1.8 Mbps

Then run it again within 10 s and check that the first frame arrives in under
0.3 s (the hub is lingering and replays its cache).

Finally, stop the idle process with Ctrl+C and confirm nothing is left behind:

```bash
~/Library/Android/sdk/platform-tools/adb -s 127.0.0.1:6145 shell 'ps -A -o ARGS | grep -c genymobile.scrcpy.Server'
```

Expected: `0`.

- [ ] **Step 4: Commit** (ask the user first)

```bash
git add tower_bot.py tools/stream_probe.py
git commit -m "Start the live stream hub in web workers and add a stream probe tool"
```

---

### Task 6: Browser stream helpers and `LiveVideo`

**Files:**
- Create: `web/ui/lib/liveStream.ts`
- Create: `web/ui/lib/liveStreamTesting.ts` (test doubles shared by Tasks 6-8)
- Create: `web/ui/components/LiveVideo.tsx`
- Test: `web/ui/lib/liveStream.test.ts`
- Test: `web/ui/components/LiveVideo.test.tsx`

**Interfaces:**
- Consumes:
  - From Task 4: the wire protocol. A text config message
    `{"type":"config","codec","width","height"}`, then binary frames of
    `[flags u8][pts u64 BE][Annex-B]`.
  - From `lib/api.ts`: `reachableDashboardUrl` (the callers already pass
    reachable URLs).
- Produces:
  - `FIRST_FRAME_TIMEOUT_MS = 5000`, `LIVE_RETRY_MS = 30000`,
    `MAX_DECODE_QUEUE = 30`.
  - `type StreamConfig` and `type StreamFrame`.
  - `streamUrl(dashboardUrl: string, scope?: string | null, expectedAccountId?: string | null): string`.
  - `parseFrame(buffer: ArrayBuffer): StreamFrame`.
  - `liveSupported(): boolean`.
  - `<LiveVideo dashboardUrl? scope? expectedAccountId? label className? onUnavailable onFrame? />`.
  - Test doubles: `MockSocket`, `MockDecoder`, `MockChunk`, `CONFIG_TEXT`,
    `frameBytes`, `fakeVideoFrame`, `installLiveStreamMocks()` and
    `uninstallLiveStreamMocks()`.

- [ ] **Step 1: Write the test doubles** in `web/ui/lib/liveStreamTesting.ts`

```ts
import { vi } from "vitest";

/** Stand-ins for WebSocket / VideoDecoder / EncodedVideoChunk, which jsdom lacks. */
export class MockSocket {
  static instances: MockSocket[] = [];
  binaryType = "blob";
  readyState = 0;
  closed = false;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor(public url: string) { MockSocket.instances.push(this); }
  close(): void { this.closed = true; this.readyState = 3; }
  receive(data: unknown): void { this.onmessage?.({ data }); }
  serverClose(code: number): void { this.readyState = 3; this.onclose?.({ code }); }
  static latest(): MockSocket {
    const socket = MockSocket.instances.at(-1);
    if (!socket) throw new Error("no socket was opened");
    return socket;
  }
}

export class MockChunk {
  constructor(public init: { type: "key" | "delta"; timestamp: number; data: Uint8Array }) {}
}

export class MockDecoder {
  static instances: MockDecoder[] = [];
  state: "unconfigured" | "configured" | "closed" = "unconfigured";
  decodeQueueSize = 0;
  config: unknown = null;
  chunks: MockChunk[] = [];
  constructor(public init: { output: (frame: unknown) => void; error: (error: unknown) => void }) {
    MockDecoder.instances.push(this);
  }
  configure(config: unknown): void { this.config = config; this.state = "configured"; }
  decode(chunk: MockChunk): void { this.chunks.push(chunk); }
  close(): void { this.state = "closed"; }
  static latest(): MockDecoder {
    const decoder = MockDecoder.instances.at(-1);
    if (!decoder) throw new Error("no decoder was created");
    return decoder;
  }
}

export const CONFIG_TEXT = JSON.stringify({ type: "config", codec: "avc1.42C029", width: 576, height: 1280 });

export function frameBytes(key: boolean, timestamp: number, payload: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(9 + payload.length);
  const view = new DataView(buffer);
  view.setUint8(0, key ? 1 : 0);
  view.setBigUint64(1, BigInt(timestamp));
  new Uint8Array(buffer, 9).set(payload);
  return buffer;
}

export function fakeVideoFrame(): { displayWidth: number; displayHeight: number; close: ReturnType<typeof vi.fn> } {
  return { displayWidth: 576, displayHeight: 1280, close: vi.fn() };
}

const secureContext = Object.getOwnPropertyDescriptor(window, "isSecureContext");

export function installLiveStreamMocks(): { drawImage: ReturnType<typeof vi.fn> } {
  MockSocket.instances = [];
  MockDecoder.instances = [];
  vi.stubGlobal("WebSocket", MockSocket);
  vi.stubGlobal("VideoDecoder", MockDecoder);
  vi.stubGlobal("EncodedVideoChunk", MockChunk);
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  const drawImage = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as never);
  return { drawImage };
}

export function uninstallLiveStreamMocks(): void {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (secureContext) Object.defineProperty(window, "isSecureContext", secureContext);
  else delete (window as { isSecureContext?: boolean }).isSecureContext;
}
```

- [ ] **Step 2: Write the failing helper tests** in `web/ui/lib/liveStream.test.ts`

```ts
import { afterEach, describe as group, expect, it } from "vitest";
import { liveSupported, parseFrame, streamUrl } from "./liveStream";
import { frameBytes, installLiveStreamMocks, uninstallLiveStreamMocks } from "./liveStreamTesting";

afterEach(() => uninstallLiveStreamMocks());

group("streamUrl", () => {
  it("points at the worker's api/stream over ws with the scope and account", () => {
    expect(streamUrl("http://127.0.0.1:10059/", "worker:T_59", "ACC"))
      .toBe("ws://127.0.0.1:10059/api/stream?scope=worker%3AT_59&expected_account_id=ACC");
  });

  it("uses wss behind Tailscale's https and leaves out empty parameters", () => {
    expect(streamUrl("https://mac.tail.ts.net:10059/", null, null)).toBe("wss://mac.tail.ts.net:10059/api/stream");
  });
});

group("parseFrame", () => {
  it("reads the keyframe flag, the timestamp and the payload", () => {
    const frame = parseFrame(frameBytes(true, 123456, [0, 0, 0, 1, 0x65]));
    expect(frame.key).toBe(true);
    expect(frame.timestamp).toBe(123456);
    expect(Array.from(frame.data)).toEqual([0, 0, 0, 1, 0x65]);
    expect(parseFrame(frameBytes(false, 0, [])).key).toBe(false);
  });
});

group("liveSupported", () => {
  it("is false without WebCodecs", () => {
    expect(liveSupported()).toBe(false);
  });

  it("is true with WebCodecs in a secure context", () => {
    installLiveStreamMocks();
    expect(liveSupported()).toBe(true);
  });
});
```

- [ ] **Step 3: Run it and check that it fails**

Run: `cd web/ui && npx vitest run lib/liveStream.test.ts`
Expected: FAIL, because `./liveStream` can't be resolved.

- [ ] **Step 4: Implement** `web/ui/lib/liveStream.ts`

```ts
/** The browser half of a worker's live stream (WS /api/stream); see the live stream design spec. */

export const FIRST_FRAME_TIMEOUT_MS = 5_000;
export const LIVE_RETRY_MS = 30_000;
/** Deltas queued in the decoder beyond this are skipped until the next keyframe. */
export const MAX_DECODE_QUEUE = 30;

export type StreamConfig = { type: "config"; codec: string; width: number; height: number };
export type StreamFrame = { key: boolean; timestamp: number; data: Uint8Array };

export function streamUrl(dashboardUrl: string, scope?: string | null, expectedAccountId?: string | null): string {
  const url = new URL("api/stream", dashboardUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (scope) url.searchParams.set("scope", scope);
  if (expectedAccountId) url.searchParams.set("expected_account_id", expectedAccountId);
  return url.href;
}

/** A binary stream message: [flags u8 (bit 0 = keyframe)][pts u64 big-endian, µs][Annex-B access unit]. */
export function parseFrame(buffer: ArrayBuffer): StreamFrame {
  const view = new DataView(buffer);
  return { key: (view.getUint8(0) & 1) === 1, timestamp: Number(view.getBigUint64(1)), data: new Uint8Array(buffer, 9) };
}

/** WebCodecs exists only in secure contexts: localhost, or https such as Tailscale Serve. */
export function liveSupported(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && typeof VideoDecoder !== "undefined";
}
```

- [ ] **Step 5: Run it and check that it passes**

Run: `cd web/ui && npx vitest run lib/liveStream.test.ts`
Expected: 5 passed.

- [ ] **Step 6: Write the failing component tests** in `web/ui/components/LiveVideo.test.tsx`

```tsx
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe as group, expect, it, vi } from "vitest";
import { LiveVideo } from "./LiveVideo";
import {
  CONFIG_TEXT, MockDecoder, MockSocket, fakeVideoFrame, frameBytes, installLiveStreamMocks, uninstallLiveStreamMocks,
} from "@/lib/liveStreamTesting";

let drawImage: ReturnType<typeof vi.fn>;
beforeEach(() => { ({ drawImage } = installLiveStreamMocks()); });
afterEach(() => { uninstallLiveStreamMocks(); vi.useRealTimers(); });

function mount() {
  const onUnavailable = vi.fn();
  const onFrame = vi.fn();
  const view = render(<LiveVideo dashboardUrl="http://127.0.0.1:10059/" scope="worker:T_59" expectedAccountId="ACC"
    label="Live screen of T_59" onUnavailable={onUnavailable} onFrame={onFrame} />);
  return { ...view, onUnavailable, onFrame };
}

group("LiveVideo", () => {
  it("opens the worker's stream socket for the scoped account", () => {
    mount();
    expect(MockSocket.latest().url).toBe("ws://127.0.0.1:10059/api/stream?scope=worker%3AT_59&expected_account_id=ACC");
    expect(MockSocket.latest().binaryType).toBe("arraybuffer");
  });

  it("configures the decoder from the config message and decodes from the first keyframe on", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => socket.receive(CONFIG_TEXT));
    const decoder = MockDecoder.latest();
    expect(decoder.config).toMatchObject({ codec: "avc1.42C029", codedWidth: 576, codedHeight: 1280 });
    act(() => {
      socket.receive(frameBytes(false, 1, [0, 0, 0, 1, 0x41])); // no keyframe yet: undecodable
      socket.receive(frameBytes(true, 2, [0, 0, 0, 1, 0x65]));
      socket.receive(frameBytes(false, 3, [0, 0, 0, 1, 0x41]));
    });
    expect(decoder.chunks.map(chunk => [chunk.init.type, chunk.init.timestamp])).toEqual([["key", 2], ["delta", 3]]);
  });

  it("draws each decoded frame, releases it, and reports only the first", () => {
    const { onFrame } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    const frame = fakeVideoFrame();
    act(() => { MockDecoder.latest().init.output(frame); MockDecoder.latest().init.output(fakeVideoFrame()); });
    expect(drawImage).toHaveBeenCalledWith(frame, 0, 0);
    expect(frame.close).toHaveBeenCalled();
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "Live screen of T_59" })).toHaveAttribute("width", "576");
  });

  it.each([4503, 1001, 4409, 4403, 1006])("falls back when the stream closes with %i", code => {
    const { onUnavailable } = mount();
    act(() => MockSocket.latest().serverClose(code));
    expect(onUnavailable).toHaveBeenCalledTimes(1);
  });

  it("falls back on a decoder error and closes the socket", () => {
    const { onUnavailable } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    act(() => MockDecoder.latest().init.error(new Error("bad bitstream")));
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(MockSocket.latest().closed).toBe(true);
  });

  it("falls back when no frame is drawn within five seconds", () => {
    vi.useFakeTimers();
    const { onUnavailable } = mount();
    act(() => { vi.advanceTimersByTime(4_999); });
    expect(onUnavailable).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    expect(onUnavailable).toHaveBeenCalledTimes(1);
  });

  it("skips to the next keyframe when the decoder falls behind", () => {
    mount();
    const socket = MockSocket.latest();
    act(() => { socket.receive(CONFIG_TEXT); socket.receive(frameBytes(true, 1, [0x65])); });
    const decoder = MockDecoder.latest();
    decoder.decodeQueueSize = 31;
    act(() => socket.receive(frameBytes(false, 2, [0x41])));
    decoder.decodeQueueSize = 0;
    act(() => { socket.receive(frameBytes(false, 3, [0x41])); socket.receive(frameBytes(true, 4, [0x65])); });
    expect(decoder.chunks.map(chunk => chunk.init.timestamp)).toEqual([1, 4]);
  });

  it("closes quietly on unmount", () => {
    const { unmount, onUnavailable } = mount();
    act(() => MockSocket.latest().receive(CONFIG_TEXT));
    unmount();
    expect(MockSocket.latest().closed).toBe(true);
    expect(MockDecoder.latest().state).toBe("closed");
    expect(onUnavailable).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 7: Run them and check that they fail**

Run: `cd web/ui && npx vitest run components/LiveVideo.test.tsx`
Expected: FAIL, because `./LiveVideo` can't be resolved.

- [ ] **Step 8: Implement** `web/ui/components/LiveVideo.tsx`

```tsx
"use client";

import { useEffect, useRef } from "react";
import { FIRST_FRAME_TIMEOUT_MS, MAX_DECODE_QUEUE, parseFrame, streamUrl, type StreamConfig } from "@/lib/liveStream";

/**
 * A worker's live H.264 stream, decoded in the browser (WebCodecs) and drawn
 * to a canvas. The host never decodes anything. Unmounting closes the stream.
 * Any failure calls onUnavailable once, so the parent can show the MJPEG
 * snapshots instead.
 */
export function LiveVideo({ dashboardUrl, scope, expectedAccountId, label, className, onUnavailable, onFrame }: {
  /** The worker to stream from. Defaults to this page's own origin (the device page). */
  dashboardUrl?: string;
  scope?: string | null;
  expectedAccountId?: string | null;
  label: string;
  className?: string;
  onUnavailable: () => void;
  onFrame?: () => void;
}): React.JSX.Element {
  const canvas = useRef<HTMLCanvasElement>(null);
  const callbacks = useRef({ onUnavailable, onFrame });
  useEffect(() => { callbacks.current = { onUnavailable, onFrame }; });

  useEffect(() => {
    const socket = new WebSocket(streamUrl(dashboardUrl ?? `${window.location.origin}/`, scope, expectedAccountId));
    socket.binaryType = "arraybuffer";
    let decoder: VideoDecoder | null = null;
    let waitingForKey = true;
    let drawn = false;
    let finished = false;
    let timer = 0;

    const stop = (): void => {
      finished = true;
      window.clearTimeout(timer);
      socket.onmessage = null;
      socket.onclose = null;
      if (socket.readyState < 2) socket.close();
      if (decoder && decoder.state !== "closed") decoder.close();
    };
    const fail = (): void => {
      if (finished) return;
      stop();
      callbacks.current.onUnavailable();
    };
    const draw = (frame: VideoFrame): void => {
      const target = canvas.current;
      const context = target?.getContext("2d");
      if (target && context) {
        if (target.width !== frame.displayWidth) target.width = frame.displayWidth;
        if (target.height !== frame.displayHeight) target.height = frame.displayHeight;
        context.drawImage(frame, 0, 0);
        if (!drawn) {
          drawn = true;
          callbacks.current.onFrame?.();
        }
      }
      frame.close();
    };

    timer = window.setTimeout(() => { if (!drawn) fail(); }, FIRST_FRAME_TIMEOUT_MS);
    socket.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
      try {
        if (typeof event.data === "string") {
          const config = JSON.parse(event.data) as StreamConfig;
          if (decoder && decoder.state !== "closed") decoder.close();
          decoder = new VideoDecoder({ output: draw, error: fail });
          decoder.configure({ codec: config.codec, codedWidth: config.width, codedHeight: config.height,
            optimizeForLatency: true });
          waitingForKey = true;
          return;
        }
        if (!decoder || decoder.state !== "configured") return;
        const frame = parseFrame(event.data);
        if (!frame.key && (waitingForKey || decoder.decodeQueueSize > MAX_DECODE_QUEUE)) {
          // A delta is useless without everything since its keyframe: before
          // the first one, or once decoding has fallen behind, wait for the next.
          waitingForKey = true;
          return;
        }
        waitingForKey = false;
        decoder.decode(new EncodedVideoChunk({ type: frame.key ? "key" : "delta", timestamp: frame.timestamp,
          data: frame.data }));
      } catch {
        fail();
      }
    };
    socket.onclose = fail;
    return stop;
  }, [dashboardUrl, scope, expectedAccountId]);

  return <canvas ref={canvas} role="img" aria-label={label} className={className} />;
}
```

- [ ] **Step 9: Run the tests and check that they pass**

Run: `cd web/ui && npx vitest run lib/liveStream.test.ts components/LiveVideo.test.tsx`
Expected: 17 passed (5 + 12).

- [ ] **Step 10: Commit** (ask the user first)

```bash
git add web/ui/lib/liveStream.ts web/ui/lib/liveStreamTesting.ts web/ui/lib/liveStream.test.ts web/ui/components/LiveVideo.tsx web/ui/components/LiveVideo.test.tsx
git commit -m "Add LiveVideo: decode the worker's H.264 stream with WebCodecs"
```

---

### Task 7: Fallback hooks, badge, and the grid and remote views

**Files:**
- Create: `web/ui/lib/useLiveFallback.ts`
- Create: `web/ui/lib/usePageVisible.ts`
- Create: `web/ui/components/FeedBadge.tsx`
- Modify: `web/ui/app/fleet/reroll/FleetCapture.tsx` (the whole component)
- Modify: `web/ui/components/RemoteDeviceView.tsx` (the whole component)
- Test: `web/ui/app/fleet/reroll/FleetCapture.test.tsx`
- Test: `web/ui/components/RemoteDeviceView.test.tsx`

**Interfaces:**
- Consumes: from Task 6, `LiveVideo`, `LIVE_RETRY_MS`, `liveSupported` and the
  test doubles.
- Produces:
  - `useLiveFallback(): { supported: boolean; live: boolean; markUnavailable: () => void }`.
  - `usePageVisible(): boolean`.
  - `<FeedBadge live={boolean} />`, which renders `data-feed="live" | "snapshots"`.

- [ ] **Step 1: Write the failing tests**

`web/ui/app/fleet/reroll/FleetCapture.test.tsx`:

```tsx
import { act, render } from "@testing-library/react";
import { afterEach, describe as group, expect, it, vi } from "vitest";
import { FleetCapture } from "./FleetCapture";
import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

const props = { dashboardUrl: "http://127.0.0.1:10059/", scope: "worker:T_59", instance: "T_59", accountId: "ACC" };

afterEach(() => { uninstallLiveStreamMocks(); vi.useRealTimers(); });

group("FleetCapture", () => {
  it("shows the MJPEG snapshots where live video is not supported", () => {
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("img")?.getAttribute("src"))
      .toBe("http://127.0.0.1:10059/api/frame?scope=worker%3AT_59&expected_account_id=ACC");
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("snapshots");
  });

  it("streams live, falls back to MJPEG when the stream fails, and retries after 30 s", () => {
    vi.useFakeTimers();
    installLiveStreamMocks();
    const { container } = render(<FleetCapture {...props} />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(container.querySelector("[data-feed]")?.getAttribute("data-feed")).toBe("live");
    expect(MockSocket.instances).toHaveLength(1);

    act(() => MockSocket.latest().serverClose(4503));
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("img")).not.toBeNull();

    act(() => { vi.advanceTimersByTime(30_000); });
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(MockSocket.instances).toHaveLength(2);
  });
});
```

`web/ui/components/RemoteDeviceView.test.tsx`:

```tsx
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe as group, expect, it } from "vitest";
import { RemoteDeviceView } from "./RemoteDeviceView";
import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => { uninstallLiveStreamMocks(); setHidden(false); });

group("RemoteDeviceView", () => {
  it("streams the remote worker live over wss behind Tailscale", () => {
    installLiveStreamMocks();
    const { container } = render(<RemoteDeviceView dashboardUrl="https://mac.tail.ts.net:10059/" scope="worker:T_59" instance="T_59" />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(MockSocket.latest().url).toBe("wss://mac.tail.ts.net:10059/api/stream?scope=worker%3AT_59");
  });

  it("stops the stream while the tab is hidden", () => {
    installLiveStreamMocks();
    const { container } = render(<RemoteDeviceView dashboardUrl="https://mac.tail.ts.net:10059/" scope="worker:T_59" instance="T_59" />);
    const socket = MockSocket.latest();
    act(() => setHidden(true));
    expect(socket.closed).toBe(true);
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("paused");
  });
});
```

- [ ] **Step 2: Run them and check that they fail**

Run: `cd web/ui && npx vitest run app/fleet/reroll/FleetCapture.test.tsx components/RemoteDeviceView.test.tsx`
Expected: FAIL. With the mocks installed there is still only an `<img>`, so
the `canvas` assertions fail.

- [ ] **Step 3: Implement the hooks and the badge**

`web/ui/lib/useLiveFallback.ts`:

```ts
"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { LIVE_RETRY_MS, liveSupported } from "./liveStream";

const neverChanges = (): (() => void) => () => {};

/** Show live video now? Only if this browser can decode it, and it hasn't failed in the last LIVE_RETRY_MS. */
export function useLiveFallback(): { supported: boolean; live: boolean; markUnavailable: () => void } {
  // The static export's server render has no window, so it (and hydration)
  // answers "not supported" and the client corrects it straight after.
  const supported = useSyncExternalStore(neverChanges, liveSupported, () => false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!failed) return;
    const timer = window.setTimeout(() => setFailed(false), LIVE_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [failed]);
  const markUnavailable = useCallback(() => setFailed(true), []);
  return { supported, live: supported && !failed, markUnavailable };
}
```

`web/ui/lib/usePageVisible.ts`:

```ts
"use client";

import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void): () => void {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
}

/** False while the tab is hidden, so screen views stop pulling video nobody sees. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribe, () => document.visibilityState !== "hidden", () => true);
}
```

`web/ui/components/FeedBadge.tsx`:

```tsx
/** Which feed a screen view is showing: live video, or the bot's per-scan snapshots. */
export function FeedBadge({ live }: { live: boolean }): React.JSX.Element {
  return <span data-feed={live ? "live" : "snapshots"}
    className="pointer-events-none absolute left-2 top-2 rounded bg-background/90 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
    {live ? "Live" : "Snapshots"}
  </span>;
}
```

- [ ] **Step 4: Replace `web/ui/app/fleet/reroll/FleetCapture.tsx`**

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { Maximize2, MonitorOff } from "lucide-react";
import { FeedBadge } from "@/components/FeedBadge";
import { LiveVideo } from "@/components/LiveVideo";
import { useLiveFallback } from "@/lib/useLiveFallback";
import { usePageVisible } from "@/lib/usePageVisible";

/** The worker validates scope on each frame; replacing the identity remounts this view. */
export function FleetCapture({ dashboardUrl, scope, instance, accountId }: {
  dashboardUrl: string; scope: string; instance: string; accountId: string;
}): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(true);
  const foreground = usePageVisible();
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { live, markUnavailable } = useLiveFallback();
  useEffect(() => {
    const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(
      entries => setVisible(entries[0]?.isIntersecting ?? false), { rootMargin: "120px" },
    );
    if (container.current) observer?.observe(container.current);
    return () => observer?.disconnect();
  }, []);
  useEffect(() => setLoaded(false), [live]);
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);
  url.searchParams.set("expected_account_id", accountId);
  // Off-screen or hidden cards unmount their feed, which closes the live
  // stream. The worker keeps it for 10 s, so scrolling back is instant.
  const active = visible && foreground;
  return <div ref={container} className="relative flex h-[min(30vh,18rem)] min-h-48 items-center justify-center overflow-hidden rounded-lg border bg-well">
    {active && !failed ? <>
      {live
        ? <LiveVideo key={`${scope}:${attempt}`} dashboardUrl={dashboardUrl} scope={scope} expectedAccountId={accountId}
            label={`Live screen of ${instance}`} className="size-full object-contain"
            onFrame={() => setLoaded(true)} onUnavailable={markUnavailable} />
        : <>
          {/* Native MJPEG works across worker origins without a fetch/CORS proxy. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img key={`${scope}:${attempt}`} src={url.href} alt={`Live screen of ${instance}`}
            onLoad={() => setLoaded(true)} onError={() => { setFailed(true); setLoaded(false); }}
            className="size-full object-contain" />
        </>}
      <FeedBadge live={live} />
      {!loaded && <span className="pointer-events-none absolute bottom-2 rounded bg-background/90 px-2 py-1 text-[10px] text-muted-foreground">Connecting screen…</span>}
      <button type="button" aria-label={`Enlarge screen of ${instance}`} title="Open full screen"
        className="absolute right-2 top-2 rounded-md border bg-background/90 p-1.5 text-muted-foreground hover:text-foreground"
        onClick={() => { void container.current?.requestFullscreen?.().catch(() => {}); }}>
        <Maximize2 className="size-3.5" aria-hidden="true" />
      </button>
    </> : <div role="status" className="flex flex-col items-center gap-3 px-5 text-center text-xs text-muted-foreground">
      <MonitorOff className="size-6" aria-hidden="true" />
      <span>{failed ? "Screen unavailable. This worker may be reconnecting." : "Screen paused while out of view."}</span>
      {failed && <button className="rounded-md border px-3 py-1.5 text-foreground" onClick={() => { setFailed(false); setLoaded(false); setAttempt(value => value + 1); }}>Retry screen</button>}
    </div>}
  </div>;
}
```

- [ ] **Step 5: Replace `web/ui/components/RemoteDeviceView.tsx`**

```tsx
"use client";

import { useState } from "react";
import { FeedBadge } from "@/components/FeedBadge";
import { LiveVideo } from "@/components/LiveVideo";
import { useLiveFallback } from "@/lib/useLiveFallback";
import { usePageVisible } from "@/lib/usePageVisible";

export function RemoteDeviceView({ dashboardUrl, scope, instance }: {
  dashboardUrl: string; scope: string; instance: string | null;
}) {
  const [failed, setFailed] = useState(false);
  const foreground = usePageVisible();
  const { live, markUnavailable } = useLiveFallback();
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);
  const label = `Live screen of ${instance ?? "selected emulator"}`;

  return <div className="relative w-full max-w-[400px] overflow-hidden rounded-xl border bg-well">
    {failed ? <div role="status" className="flex aspect-[9/16] flex-col items-start gap-3 p-4 text-sm text-muted-foreground">
      <p>Live screen unavailable. The worker may be starting or reconnecting.</p>
      <button className="rounded border px-3 py-2 text-foreground" onClick={() => setFailed(false)}>Retry screen</button>
    </div> : !foreground ? <p role="status" className="flex aspect-[9/16] items-center justify-center p-4 text-sm text-muted-foreground">
      Screen paused while this tab is hidden.
    </p> : live ? <LiveVideo dashboardUrl={dashboardUrl} scope={scope} label={label}
      onUnavailable={markUnavailable} className="block aspect-[9/16] w-full object-contain" />
      : /* eslint-disable-next-line @next/next/no-img-element */
      <img key={url.href} src={url.href} alt={label}
        onError={() => setFailed(true)} className="block aspect-[9/16] w-full object-contain" />}
    {!failed && foreground && <FeedBadge live={live} />}
  </div>;
}
```

- [ ] **Step 6: Run the tests and check that they pass**

Run: `cd web/ui && npx vitest run app/fleet/reroll/FleetCapture.test.tsx components/RemoteDeviceView.test.tsx app/fleet/reroll/FleetLiveCard.test.tsx components/AccountShell.test.tsx`
Expected: all pass. The last two files already render these components.

- [ ] **Step 7: Commit** (ask the user first)

```bash
git add web/ui/lib/useLiveFallback.ts web/ui/lib/usePageVisible.ts web/ui/components/FeedBadge.tsx web/ui/app/fleet/reroll/FleetCapture.tsx web/ui/app/fleet/reroll/FleetCapture.test.tsx web/ui/components/RemoteDeviceView.tsx web/ui/components/RemoteDeviceView.test.tsx
git commit -m "Show live video in fleet cards and the remote device view, with MJPEG fallback"
```

---

### Task 8: Device page — Live | Bot's view toggle

**Files:**
- Modify: `web/ui/components/DeviceView.tsx`
- Test: `web/ui/components/DeviceView.test.tsx` (add a group; the existing tests
  stay unchanged)

**Interfaces:**
- Consumes: from Task 6, `LiveVideo` and the test doubles; from Task 7,
  `useLiveFallback` and `FeedBadge`.
- Produces: the `localStorage` key `towerbot.deviceView.mode`, holding
  `"live" | "bot"`.

- [ ] **Step 1: Write the failing tests.** Append this to
  `web/ui/components/DeviceView.test.tsx`, and extend its imports to
  `import { act, fireEvent, render, screen } from "@testing-library/react";`,
  `import { afterEach, describe as group, expect, it } from "vitest";` and
  `import { MockSocket, installLiveStreamMocks, uninstallLiveStreamMocks } from "@/lib/liveStreamTesting";`.

```tsx
group("DeviceView screen source", () => {
  afterEach(() => { uninstallLiveStreamMocks(); window.localStorage.clear(); });

  it("defaults to live video when supported, without the match overlay", () => {
    installLiveStreamMocks();
    const { container } = render(<DeviceView boxes={[matched]} size={size} />);
    expect(container.querySelector("canvas")).not.toBeNull();
    expect(screen.queryByTitle(/Damage 0/)).toBeNull();
    expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switches to the bot's view with its match boxes and remembers the choice", () => {
    installLiveStreamMocks();
    const first = render(<DeviceView boxes={[matched]} size={size} />);
    fireEvent.click(screen.getByRole("button", { name: "Bot's view" }));
    expect(screen.getByTitle(/Damage 0/)).toBeInTheDocument();
    expect(first.container.querySelector("canvas")).toBeNull();
    first.unmount();
    render(<DeviceView boxes={[matched]} size={size} />);
    expect(screen.getByRole("button", { name: "Bot's view" })).toHaveAttribute("aria-pressed", "true");
  });

  it("falls back to the bot's view when the live stream is unavailable", () => {
    installLiveStreamMocks();
    const { container } = render(<DeviceView boxes={[matched]} size={size} />);
    act(() => MockSocket.latest().serverClose(4503));
    expect(container.querySelector("canvas")).toBeNull();
    expect(screen.getByTitle(/Damage 0/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Bot's view" })).toHaveAttribute("aria-pressed", "true");
  });

  it("has no toggle where live video is not supported", () => {
    render(<DeviceView boxes={[matched]} size={size} />);
    expect(screen.queryByRole("group", { name: "Screen source" })).toBeNull();
  });
});
```

- [ ] **Step 2: Run them and check that they fail**

Run: `cd web/ui && npx vitest run components/DeviceView.test.tsx`
Expected: the 4 new tests fail (no canvas, no toggle). The existing tests still
pass.

- [ ] **Step 3: Implement.** In `web/ui/components/DeviceView.tsx`:

Replace the imports and the top of the component:

```tsx
"use client";

import { useEffect, useState } from "react";
import type { MatchBox } from "@/lib/types";
import { accountScope } from "@/lib/accountScope";
import { FeedBadge } from "@/components/FeedBadge";
import { LiveVideo } from "@/components/LiveVideo";
import { useLiveFallback } from "@/lib/useLiveFallback";

type Source = "live" | "bot";
const SOURCE_KEY = "towerbot.deviceView.mode";

function rememberedSource(): Source {
  try {
    return window.localStorage.getItem(SOURCE_KEY) === "bot" ? "bot" : "live";
  } catch {
    return "live"; // storage blocked (private window): just don't remember
  }
}

function SourceToggle({ shown, onChoose }: { shown: Source; onChoose: (source: Source) => void }): React.JSX.Element {
  return <div role="group" aria-label="Screen source"
    className="absolute right-2 top-2 z-10 flex overflow-hidden rounded-md border bg-background/90 text-[11px]">
    {(["live", "bot"] as const).map(value => (
      <button key={value} type="button" aria-pressed={shown === value} onClick={() => onChoose(value)}
        className={`px-2 py-1 ${shown === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}>
        {value === "live" ? "Live" : "Bot's view"}
      </button>
    ))}
  </div>;
}

export function DeviceView({
  boxes,
  size,
}: {
  boxes: MatchBox[];
  size: { width: number; height: number } | null;
}) {
  const [overlay, setOverlay] = useState(true);
  const [source, setSource] = useState<Source>("live");
  const { supported, live, markUnavailable } = useLiveFallback();
  useEffect(() => setSource(rememberedSource()), []);
  const choose = (next: Source): void => {
    setSource(next);
    try {
      window.localStorage.setItem(SOURCE_KEY, next);
    } catch {
      // storage blocked: the choice lasts until reload
    }
  };
  // Live is the default, but the bot's view is what shows whenever live
  // can't: unsupported browser, or a stream that just failed.
  const shown: Source = source === "live" && live ? "live" : "bot";
  const best = boxes.length ? boxes.reduce((a, b) => (b.score > a.score ? b : a)) : null;
  const scope = accountScope();
```

In the frame container (`<div className="relative w-full overflow-hidden rounded-t-xl bg-well">`),
change the opening of the conditional from `{size ? (` to:

```tsx
        {shown === "live" ? (
          // Same origin: this page is served by the worker it shows.
          <LiveVideo scope={scope} label="device screen, live" onUnavailable={markUnavailable} className="block w-full" />
        ) : size ? (
```

Leave the existing `size` branch (img, overlay boxes, "matches" checkbox) and
the "Waiting for the first frame…" branch unchanged. Directly before the
container's closing `</div>`, after the conditional, add:

```tsx
        {(shown === "live" || size) && <FeedBadge live={shown === "live"} />}
        {supported && <SourceToggle shown={shown} onChoose={choose} />}
```

- [ ] **Step 4: Run the tests and check that they pass**

Run: `cd web/ui && npx vitest run components/DeviceView.test.tsx`
Expected: all pass, the existing tests plus 4 new ones.

- [ ] **Step 5: Lint and type-check the UI**

Run: `cd web/ui && npx tsc --noEmit && npx eslint components lib app/fleet/reroll`
Expected: no errors.

- [ ] **Step 6: Commit** (ask the user first)

```bash
git add web/ui/components/DeviceView.tsx web/ui/components/DeviceView.test.tsx
git commit -m "Add a Live / Bot's view toggle to the device page"
```

---

### Task 9: README, build, and a fleet check

**Files:**
- Modify: `README.md`, after the paragraph that starts "The live device screen
  is `GET /api/frame`" (around lines 901-907), and in the "no authentication"
  warning (around line 983)

- [ ] **Step 1: Document the stream.** Insert this paragraph after the
  `/api/frame` paragraph:

```markdown
Where the browser supports WebCodecs (any current browser on `localhost` or
over Tailscale's HTTPS), the same views instead show **live video**: `WS
/api/stream` on each worker relays an on-demand scrcpy H.264 stream (1280 px,
15 fps) that the browser decodes itself, so the Mac never decodes or
re-encodes it. It starts when a view opens, stops 10 s after the last viewer
leaves, and costs about +7% of the emulator's CPU and +15% of one Mac core per
watched emulator. Any failure falls back to the MJPEG snapshots and retries
after 30 s; the device page has a **Live | Bot's view** toggle, where Bot's
view is the exact scan frame the bot decided on, with its match boxes. The
bot itself never reads the stream. Set `STREAM_ENABLED = False` in `config.py`
to turn it off. `uv run tools/stream_probe.py <worker port>` reports a
worker's stream fps and bitrate.
```

In the "no authentication" warning, change "a continuous MJPEG video stream of
the device (`/api/frame`)" to "a continuous video stream of the device
(`/api/frame` MJPEG and `/api/stream` H.264)".

- [ ] **Step 2: Build the UI bundle**

Run: `cd web/ui && npm run build`
Expected: the build succeeds and `web/static/` is refreshed.

- [ ] **Step 3: Check on the real fleet.** Ask the user first: this means
  restarting workers on this branch.

With the user's go-ahead, and once the production workers run this branch's
code:

1. Open `/fleet/reroll` with all cards visible.
   - Each card shows **LIVE** and moves smoothly.
   - Sample CPU for about 60 s, in the same way as the spike: `ps -o time=`
     deltas for each BlueStacks PID and worker PID, and guest `/proc/stat`
     through adb.
   - Per watched emulator, expect roughly +7% guest CPU and +15% of one host
     core over baseline.
   - The worker's own CPU and the median `screencap -p` time must stay at
     baseline, about 185–240 ms.
2. Scroll a card away and back within 10 s. The picture returns at once, and
   no new `genymobile.scrcpy.Server` appears on that device.
3. Open the dashboard on the phone over Tailscale. The cards play over `wss://`.
4. On one device, run
   `adb -s <serial> shell pkill -f genymobile.scrcpy.Server`. The card drops to
   **SNAPSHOTS** and comes back to **LIVE** within about 30 s.
5. On the device page, **Bot's view** shows the match boxes, and the choice
   survives a reload.
6. Close every view, wait 15 s, and check that no scrcpy server remains on any
   device.

- [ ] **Step 4: Commit** (ask the user first)

```bash
git add README.md
git commit -m "Document the live stream"
```
