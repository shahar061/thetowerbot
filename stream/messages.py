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


@dataclass(frozen=True)
class ReplayDone:
    """Marks the end of a new subscription's replay (config + any cached GOP).

    Sent exactly once per subscription, right after that replay, even when
    there was nothing to replay. It tells the client where its own catch-up
    burst ends and genuinely live frames begin - see
    web/ui/components/LiveVideo.tsx.
    """

    def to_json(self) -> str:
        return json.dumps({"type": "live"})


class End(enum.Enum):
    UNAVAILABLE = "unavailable"  # the session failed; viewers fall back to MJPEG
    GOING_AWAY = "going_away"  # the worker process is shutting down


Message = ConfigMessage | FrameMessage | ReplayDone | End
