# Emulator Live Stream — Design

Date: 2026-09-28

## Goal

Show smooth live video of each emulator in the UI, in three places: the fleet
grid on the desk, the single-device page used for debugging the bot, and the
phone over Tailscale. Today those views show the bot's scan frames as MJPEG,
which is about 0.5 fps.

The stream is for viewing only. The bot keeps taking its `screencap -p` capture
once per scan for every decision. The stream must not slow the bot down.

Success means:

- Visible cards and the device page show 15 fps video.
- The worker's CPU and median capture time stay at the baseline measured below.
- Every failure falls back to the existing MJPEG feed with no user action.

## Measured basis (spike, 2026-09-28)

The spike streamed one BlueStacks Air instance (Android 13, 2 vCPUs) while its
bot was in a run, on an M4 Pro host. Each setting ran for 60 s.

| Setting | Guest CPU (2 vCPUs) | Host BlueStacks process (% of one core) | scrcpy client | Bitrate |
|---|---|---|---|---|
| No stream | 15.5–16.9% | 43–45% | — | — |
| 576×1280 @ 15 fps | 23.9% | 59% | 1.0% | 1.8 Mbps |
| 1080×2400 @ 30 fps | 47.3% | 115% | 1.1% | 4.0 Mbps |

- BlueStacks Air has only software H.264/HEVC encoders
  (`c2.android.avc.encoder`). Encoding runs in the guest's `mediaswcodec`
  process.
- The worker's own CPU and the median `screencap -p` time (185–240 ms) did not
  change while streaming.
- Worst-case capture time got longer while streaming: 678–1318 ms, against
  244–553 ms at baseline. Treat this as something to watch, not a conclusion.
- Relaying H.264 without decoding it costs about 1% of one core.

## Decisions

| Topic | Decision |
|---|---|
| Stream settings | scrcpy 4.1, H.264, 1280 px on the long side, 15 fps, no audio, no control. |
| When it runs | Only while at least one viewer is watching. It stops `STREAM_LINGER_SECONDS` (10 s) after the last viewer leaves. |
| Fleet grid | Every visible card streams live. Off-screen cards and hidden tabs pause, using the existing visibility logic. |
| Device page | A toggle between **Live** (the default) and **Bot's view**. Bot's view is the current `/api/frame` image with the match-box overlay. |
| Server approach | Each worker speaks the scrcpy protocol in Python through `adbutils`. It needs no scrcpy CLI, no adb binary path, and no decoding. |
| scrcpy server jar | Pinned and committed at `vendor/scrcpy/scrcpy-server-v4.1` (717 KB, Apache-2.0), taken from Homebrew's scrcpy 4.1. |
| Transport | A WebSocket on each worker's existing port. It reaches the phone through Tailscale Serve as `wss://`. |
| Browser decode | WebCodecs `VideoDecoder`, drawn to a `<canvas>`. |
| Fallback | The existing MJPEG `<img>`. |

## Architecture

```
emulator ──adb──▶ ScrcpySession ──packets──▶ StreamHub ──▶ WS /api/stream ──▶ LiveVideo (browser)
 (scrcpy-server,                              (viewer count, linger,          (VideoDecoder → canvas)
  H.264 1280@15)                               GOP cache, fan-out)
```

Each worker owns only its own emulator's stream. No component spans more than
one worker.

## Server components

### `stream/scrcpy_session.py` — `ScrcpySession`

Handles one scrcpy server run on one device. It contains no web or threading
logic.

- `start()` does four things:
  - Pushes the pinned jar to `/data/local/tmp/scrcpy-server.jar` with
    `adbutils` `sync.push`, but only when the size on the device differs.
  - Picks a random 31-bit `scid`.
  - Starts the server through an `adbutils` streaming shell with:
    `CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / com.genymobile.scrcpy.Server 4.1 scid=<hex> log_level=info tunnel_forward=true audio=false control=false video_codec=h264 max_size=1280 max_fps=15 send_device_meta=false send_dummy_byte=false video_codec_options=i-frame-interval:int=2 cleanup=true`
  - Connects to `localabstract:scrcpy_<scid hex, 8 chars>` with
    `create_connection`, retrying every 100 ms for up to 5 s while the server
    starts.
- After the socket connects, the server sends a 4-byte codec id. `start()`
  checks that it is `h264`.
- `events()` then parses a stream of 12-byte headers. This layout was verified
  against scrcpy 4.1 on a BlueStacks Air instance:
  - If the first byte has bit 7 set, it is a **session packet**: u32 flags, u32
    width, u32 height, and no payload. It yields `SessionInfo(width, height)`.
  - Otherwise it is a **media packet**: a u64 (bit 62 = config, bit 61 =
    keyframe, low 61 bits = PTS in µs), then a u32 payload size. It yields
    `Packet(config, key, pts_us, data)`.
  - A read timeout between packets yields `None`, so the caller can check its
    stop conditions on a still screen.
- `close()` closes the video socket and the shell stream, which ends the server
  on the device. It is idempotent and never blocks longer than 1 s.
- The exact server option names and header layout are checked against the
  scrcpy 4.1 source during implementation.

### `stream/h264.py`

Pure functions:

- `codec_string(sps: bytes) -> str` returns `avc1.PPCCLL`, built from the SPS
  NAL's profile_idc, constraint flags and level_idc.
- `find_sps(annexb: bytes) -> bytes | None`.

### `stream/hub.py` — `StreamHub`

One per worker. The session factory, clock and sleep are injected so tests can
control them.

- `subscribe() -> Subscription` and `Subscription.close()`:
  - The first subscriber starts a reader thread, which opens a session.
  - When the last subscriber closes, the hub arms a linger timer. If nobody has
    subscribed again when it fires, the hub closes the session and the thread
    exits.
- The reader thread sets up each packet as follows:
  - **Config packets** are stored, not sent. From the stored SPS the hub builds
    a `config` message (`codec`, `width`, `height`). If it differs from the
    previous one, the hub broadcasts it and resets the GOP cache.
  - **Keyframes** have the stored config bytes put in front of them, so each
    keyframe decodes without the others.
  - **GOP cache:** it holds the current config message, the latest keyframe and
    every frame since. A new keyframe replaces the cache. It's capped at
    `STREAM_GOP_FRAMES` frames - sized for scrcpy's observed ~10 s keyframe
    spacing (BlueStacks Air ignores the 2 s interval request) - and a late
    joiner's replay of it is exempt from the per-viewer lag cap below.
- A new subscriber first receives the current config message and the whole GOP
  cache, then live frames. A viewer who joins mid-stream sees the current
  picture within about 100 ms, even on a still screen that sends no new frames.
- Each subscriber has a bounded queue of about 2 s of frames (30 messages).
  - When it overflows, that queue is cleared and the subscriber skips ahead to
    the next keyframe.
  - Other subscribers are not affected.
- If the session fails, all subscribers get an `unavailable` signal.
  - While any subscriber remains, the hub retries with backoff: 1, 2, 4, 8, 16,
    then 30 s.
  - The error is logged once per attempt, with `worker_id` and serial.
- The hub watches the worker's existing `shutdown` event. On shutdown it closes
  the session and ends every subscription with `going_away`.

### `web/app.py` — `WS /api/stream`

- It is registered before the static mount.
- It accepts the same `scope` and `expected_account_id` query parameters as
  `/api/frame` and reuses `_live_choice`'s account check.
- **Origin check:** the connection is accepted only when the `Origin` header is
  missing, points to a loopback host, or matches the hostname in the request's
  `Host` header. Anything else closes with `4403`.
  - Without this check, any website open on the Mac could read the emulator's
    screen through the WebSocket.
  - Behind Tailscale Serve, the page (`https://<mac>.ts.net`) and the worker
    (`https://<mac>.ts.net:100NN`) share a hostname. Implementation must confirm
    that Serve forwards the original `Host`. If it doesn't, compare against
    `X-Forwarded-Host` instead.
- It subscribes to the hub and forwards messages until the client disconnects
  or the subscription ends.

### Config and dependencies

- New settings in `config.py`:
  - `STREAM_ENABLED = True`
  - `STREAM_MAX_SIZE = 1280`
  - `STREAM_MAX_FPS = 15`
  - `STREAM_I_FRAME_INTERVAL_S = 2`
  - `STREAM_LINGER_SECONDS = 10`
  - `STREAM_FIRST_FRAME_TIMEOUT_S = 5`
- Add `websockets` to `pyproject.toml`. Without it, uvicorn can't accept
  WebSocket connections.
- Add `vendor/scrcpy/scrcpy-server-v4.1` with a `vendor/scrcpy/README.md`
  giving its source, version, checksum and licence.

## Wire protocol

| Direction | Frame | Content |
|---|---|---|
| server → client | text | `{"type":"config","codec":"avc1.42C028","width":576,"height":1280}`. Sent first, and again whenever the encoder config changes. |
| server → client | binary | 1 flag byte (bit 0 = keyframe), an 8-byte big-endian PTS in µs, then one Annex-B access unit. |
| client → server | none | The client sends no messages. |

Close codes:

| Code | Meaning | Client action |
|---|---|---|
| `4403` | Origin rejected | Fall back to MJPEG |
| `4409` | This account isn't running on the worker (same rule as `/api/frame`'s 409) | Stop live; show the existing mismatch state |
| `4503` | Stream unavailable: scrcpy failed, or `STREAM_ENABLED` is false | Fall back to MJPEG; retry live after 30 s |
| `1001` | Worker shutting down | Fall back to MJPEG; retry live after 30 s |

## Frontend

### `web/ui/components/LiveVideo.tsx`

The component knows nothing about where it's shown.

- **Props:** `dashboardUrl`, `scope`, `expectedAccountId?`, `paused`,
  `onUnavailable()`, `className`.
- **Connecting:** the WebSocket URL is
  `new URL("api/stream", reachableDashboardUrl(dashboardUrl))`, with `http:`
  turned into `ws:` and `https:` into `wss:`, plus the query parameters.
- **Decoding:**
  - A config message (re)creates a `VideoDecoder` with its `codec` string. No
    `description` is passed, because the stream is Annex-B.
  - Each binary message becomes an `EncodedVideoChunk` (`type` taken from the
    flag byte, `timestamp` from the PTS).
  - The decoder is fed only after the first keyframe.
- **Drawing:** each decoded `VideoFrame` is drawn to the canvas, which is sized
  from the config, and then closed at once.
- **Pausing:** when `paused` becomes true, it closes the WebSocket and the
  decoder.
- **`onUnavailable()` is called when:**
  - the live-support check fails
  - the connection closes with `4403`, `4503` or `1001`
  - the connection closes unexpectedly
  - the decoder reports an error
  - no frame is drawn within `5 s` of connecting

### `web/ui/lib/useLiveSupported.ts`

Returns `typeof VideoDecoder !== "undefined" && window.isSecureContext`.

### Integration

- **`app/fleet/reroll/FleetCapture.tsx`:**
  - It renders `LiveVideo` when live is supported and hasn't failed in the last
    30 s; otherwise the current MJPEG `<img>`.
  - The existing IntersectionObserver and hidden-tab state drive `paused`.
  - The fullscreen button targets the element that holds both views.
- **`components/RemoteDeviceView.tsx`:**
  - The same live/MJPEG swap.
  - It also gains the hidden-tab pause it lacks today.
- **`components/DeviceView.tsx`:**
  - A **Live | Bot's view** toggle. Live renders `LiveVideo`; Bot's view is the
    current image with the match-box overlay.
  - The choice is kept in `localStorage` under a per-browser key. Reads and
    writes are wrapped in try/catch, and the default is Live.
  - If `onUnavailable` fires, the page shows Bot's view until the 30 s retry.
- **Badge:** each of the three views shows a small **LIVE** or **0.5 fps** badge
  in the corner, so it's clear which feed is showing.

## Error handling

| Failure | Behaviour |
|---|---|
| Emulator or adb drops mid-stream | The session's read raises and the hub signals `unavailable`. Viewers get `4503` and fall back; the hub retries with backoff while viewers remain. |
| Jar push or server start fails | Logged once per backoff attempt; viewers get `4503`. |
| Worker shutdown | The session closes within 1 s and viewers get `1001`. This fits the existing 2 s graceful-shutdown budget. |
| Worker crashes without cleanup | The server on the device exits when its adb socket drops (confirmed in the spike). The next session starts cleanly with a new `scid`. |
| Old browser, or a page that isn't secure | Live is never attempted; MJPEG is shown. |
| Slow viewer | Its queue is cleared and it skips to the next keyframe. Other viewers and the bot are unaffected. |

The bot's scan loop, `GuardedDevice` and the capture path are unchanged. The
stream uses its own adb connection.

## Testing

This repo has no CI, so the local run is the gate. Only the new and changed
test files are run.

**Python:**

- `tests/test_stream_session.py`, with a fake socket and a fake shell:
  - codec header parsing
  - frame header flags and PTS
  - partial reads split across `recv` calls
  - `close()` is idempotent and bounded in time
- `tests/test_stream_h264.py`: the codec string built from a real SPS captured
  during the spike.
- `tests/test_stream_hub.py`, with a fake session, clock and sleep:
  - first-subscriber start and linger stop
  - resubscribing during the linger keeps the session
  - config merged into keyframes
  - GOP cache replayed to a late joiner
  - queue overflow skips to a keyframe
  - backoff sequence
  - shutdown sends `going_away`
- `tests/test_stream_api.py`, using `TestClient.websocket_connect` with a fake
  hub:
  - the config message, then binary frames
  - `4409` account mismatch
  - `4503` when disabled or unavailable
  - `4403` for a foreign Origin; loopback and same-host Origins are accepted

**UI (vitest):**

- `components/LiveVideo.test.tsx`, with a mocked `WebSocket` and
  `VideoDecoder`:
  - it configures the decoder from the config message
  - it decodes chunks and draws them
  - each fallback path calls `onUnavailable`
  - `paused` closes the connection
- `app/fleet/reroll/FleetCapture.test.tsx`: the live/MJPEG swap and the 30 s
  retry.
- `components/DeviceView.test.tsx`: the toggle, its persistence and the
  automatic fallback.

**Manual check on the real fleet:**

- Grid with 3 live cards:
  - Per stream, guest and host CPU should rise by about the spike's numbers.
  - The worker's CPU and median capture time should stay at baseline.
- Phone over Tailscale: `wss://` plays.
- Killing the scrcpy server on a device leads to MJPEG fallback, then live
  recovery after the retry.
- Device page: the toggle shows the match boxes in Bot's view.

## Rollout

- `STREAM_ENABLED` defaults to on. Setting it to false makes the endpoint
  return `4503`, so every view reverts to MJPEG with no UI change.
- The README's frame-feed section gets a short paragraph about the live stream
  and the flag.

## Out of scope

- Audio, input control through scrcpy, and recording.
- A host-wide cap on simultaneous streams.
- Adaptive quality or a second, smaller quality tier.
- Replacing the bot's per-scan capture with stream frames.
