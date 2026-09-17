"""Contracts shared by the macOS manager-input helper and its callers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol
from pathlib import Path


class HostCapabilityError(ValueError):
    """The local host cannot provide a valid input-agent capability."""


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_AGENT_STATES = frozenset({"ready", "blocked", "awaiting_source", "creating", "created", "failed"})
BLUESTACKS_MIM_BUNDLE_ID = "com.now.gg.BlueStacksAirMIM"
_ALLOWED_ACTIONS = frozenset({"health", "capture_manager", "open_new_instance",
                              "choose_clone_instance", "choose_source", "create_clone"})
_DEFAULT_RUNTIME_DIR = Path.home() / "Library/Application Support/TheTowerBot/runtime"


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_finite_float(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class AgentFrame:
    """A captured manager window and its geometry in display points."""

    window_id: int
    owner_bundle_id: str
    x: float
    y: float
    width: float
    height: float
    pixel_width: int
    pixel_height: int
    digest: str

    def __post_init__(self) -> None:
        if not _is_positive_int(self.window_id):
            raise HostCapabilityError("window id must be a positive integer")
        if not isinstance(self.owner_bundle_id, str) or not self.owner_bundle_id.strip():
            raise HostCapabilityError("window owner bundle id is required")
        if self.owner_bundle_id != BLUESTACKS_MIM_BUNDLE_ID:
            raise HostCapabilityError("window is not owned by BlueStacks MIM")
        if not all(_is_finite_float(value) for value in (self.x, self.y, self.width, self.height)):
            raise HostCapabilityError("frame coordinates must be finite display points")
        if self.width <= 0 or self.height <= 0:
            raise HostCapabilityError("frame dimensions must be positive")
        if not _is_positive_int(self.pixel_width) or not _is_positive_int(self.pixel_height):
            raise HostCapabilityError("pixel dimensions must be positive integers")
        if not isinstance(self.digest, str) or _SHA256_HEX.fullmatch(self.digest) is None:
            raise HostCapabilityError("frame digest must be a lowercase SHA-256 hex digest")

    @classmethod
    def digest_for(cls, raw_bytes: bytes) -> str:
        """Return the stable SHA-256 digest of raw frame bytes."""
        if not isinstance(raw_bytes, bytes):
            raise HostCapabilityError("raw frame data must be bytes")
        return hashlib.sha256(raw_bytes).hexdigest()

    @classmethod
    def digest_from_bytes(cls, raw_bytes: bytes) -> str:
        """Compatibility spelling for callers that name the raw-byte source."""
        return cls.digest_for(raw_bytes)

    @classmethod
    def from_wire(cls, raw: object) -> AgentFrame:
        """Decode one untrusted helper response into validated frame evidence."""
        if not isinstance(raw, dict):
            raise HostCapabilityError("input agent evidence is invalid")
        try:
            return cls(**{
                key: raw[key] for key in ("window_id", "owner_bundle_id", "x", "y", "width",
                                           "height", "pixel_width", "pixel_height", "digest")
            })
        except (KeyError, TypeError) as exc:
            raise HostCapabilityError("input agent evidence is invalid") from exc


@dataclass(frozen=True)
class AgentStatus:
    """The input helper's current state, optionally evidenced by a capture."""

    state: str
    detail: str
    evidence: AgentFrame | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in _AGENT_STATES:
            raise HostCapabilityError("input agent state is invalid")
        if not isinstance(self.detail, str):
            raise HostCapabilityError("input agent detail must be a string")
        if self.evidence is not None and not isinstance(self.evidence, AgentFrame):
            raise HostCapabilityError("input agent evidence must be an agent frame")


class InputAgent(Protocol):
    """Operations the local input helper must expose to the fleet backend."""

    def health(self) -> AgentStatus: ...

    def capture_manager(self) -> AgentFrame: ...

    def request(self, action: str, evidence: AgentFrame) -> AgentStatus: ...


class InputAgentClient:
    """Current-user Unix-socket client for the persistent macOS helper."""

    def __init__(self, socket_path: Path, *, runtime_dir: Path = _DEFAULT_RUNTIME_DIR,
                 timeout: float = 3.) -> None:
        runtime = runtime_dir.expanduser().resolve()
        path = socket_path.expanduser().resolve()
        if path.parent != runtime or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise HostCapabilityError("input agent socket must be in its runtime directory")
        self._socket_path = path
        self._timeout = float(timeout)
        self._request_id = 0

    def _exchange(self, payload: dict[str, object]) -> dict[str, object]:
        self._request_id += 1
        payload["request_id"] = self._request_id
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout)
                connection.connect(str(self._socket_path))
                connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
                response = connection.recv(4097)
        except OSError as exc:
            raise HostCapabilityError("input agent is unavailable") from exc
        if not response or len(response) > 4096 or not response.endswith(b"\n"):
            raise HostCapabilityError("input agent response is invalid")
        try:
            parsed = json.loads(response)
        except ValueError as exc:
            raise HostCapabilityError("input agent response is invalid") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("state"), str) \
                or not isinstance(parsed.get("detail"), str):
            raise HostCapabilityError("input agent response is invalid")
        return parsed

    def health(self) -> AgentStatus:
        """Return the helper's permission and connectivity state."""
        parsed = self._exchange({"action": "health"})
        try:
            return AgentStatus(parsed["state"], parsed["detail"], None)
        except (KeyError, TypeError, ValueError) as exc:
            raise HostCapabilityError("input agent response is invalid") from exc

    def capture_manager(self) -> AgentFrame:
        """Return one validated capture of the exact BlueStacks Manager window."""
        parsed = self._exchange({"action": "capture_manager"})
        if parsed.get("state") != "ready":
            raise HostCapabilityError(str(parsed["detail"]))
        return AgentFrame.from_wire(parsed.get("evidence"))

    def request(self, action: str, evidence: AgentFrame) -> AgentStatus:
        if action not in _ALLOWED_ACTIONS:
            raise HostCapabilityError("input agent action is unavailable")
        if not isinstance(evidence, AgentFrame):
            raise HostCapabilityError("request evidence must be an agent frame")
        parsed = self._exchange({"action": action, "window_id": evidence.window_id,
                                 "digest": evidence.digest})
        try:
            return AgentStatus(parsed["state"], parsed["detail"], evidence)
        except (KeyError, TypeError, ValueError) as exc:
            raise HostCapabilityError("input agent response is invalid") from exc


class EvidenceBoundInputAgent(ABC):
    """Base agent that accepts requests only for its latest manager capture."""

    def __init__(self) -> None:
        self._latest_manager_frame: AgentFrame | None = None

    @abstractmethod
    def health(self) -> AgentStatus:
        """Return the helper's current capability status."""

    @abstractmethod
    def _capture_manager(self) -> AgentFrame:
        """Capture the manager window from the local host."""

    @abstractmethod
    def _request(self, action: str, evidence: AgentFrame) -> AgentStatus:
        """Perform a request whose evidence has already been verified."""

    def capture_manager(self) -> AgentFrame:
        frame = self._capture_manager()
        if not isinstance(frame, AgentFrame):
            raise HostCapabilityError("manager capture must return an agent frame")
        self._latest_manager_frame = frame
        return frame

    def request(self, action: str, evidence: AgentFrame) -> AgentStatus:
        if not isinstance(action, str) or not action.strip():
            raise HostCapabilityError("input agent action is required")
        current = self._latest_manager_frame
        if current is None:
            raise HostCapabilityError("capture manager before requesting input")
        if not isinstance(evidence, AgentFrame):
            raise HostCapabilityError("request evidence must be an agent frame")
        if (evidence.window_id, evidence.digest) != (current.window_id, current.digest):
            raise HostCapabilityError("request evidence is not the current manager capture")
        return self._request(action, evidence)


def pixel_to_global_point(frame: AgentFrame, pixel: tuple[int, int]) -> tuple[float, float]:
    """Convert a capture pixel coordinate to its global display-point position."""
    if not isinstance(frame, AgentFrame):
        raise HostCapabilityError("frame must be an agent frame")
    if not isinstance(pixel, tuple) or len(pixel) != 2:
        raise HostCapabilityError("pixel coordinate must be a two-item tuple")
    pixel_x, pixel_y = pixel
    if any(not isinstance(value, int) or isinstance(value, bool) for value in pixel):
        raise HostCapabilityError("pixel coordinates must be integers")
    if not 0 <= pixel_x <= frame.pixel_width or not 0 <= pixel_y <= frame.pixel_height:
        raise HostCapabilityError("pixel coordinate is outside the captured frame")
    return (
        frame.x + frame.width * pixel_x / frame.pixel_width,
        frame.y + frame.height * pixel_y / frame.pixel_height,
    )
