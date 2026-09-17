from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import threading
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fleet.input_agent import (
    AgentFrame,
    AgentStatus,
    BLUESTACKS_MIM_BUNDLE_ID,
    EvidenceBoundInputAgent,
    HostCapabilityError,
    InputAgent,
    InputAgentClient,
    pixel_to_global_point,
)


def _frame(**changes: object) -> AgentFrame:
    values: dict[str, object] = {
        "window_id": 42,
        "owner_bundle_id": BLUESTACKS_MIM_BUNDLE_ID,
        "x": 100.0,
        "y": 200.0,
        "width": 500.0,
        "height": 300.0,
        "pixel_width": 1_000,
        "pixel_height": 1_200,
        "digest": AgentFrame.digest_for(b"manager-frame"),
    }
    values.update(changes)
    return AgentFrame(**values)  # type: ignore[arg-type]


def _agent_server(tmp_path: Path, response: dict[str, object]) -> tuple[Path, list[dict[str, object]], threading.Thread]:
    """Serve one local helper response and retain its single request for assertions."""
    path = tmp_path / "agent.sock"
    requests: list[dict[str, object]] = []

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            client, _ = listener.accept()
            with client:
                requests.append(json.loads(client.recv(4096)))
                client.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    for _ in range(100):
        if path.exists():
            return path, requests, thread
        threading.Event().wait(.001)
    raise AssertionError("local input-agent socket did not start")


def test_frame_digest_is_stable_sha256_for_raw_bytes() -> None:
    digest = AgentFrame.digest_for(b"manager-frame")

    assert digest == hashlib.sha256(b"manager-frame").hexdigest()
    assert AgentFrame.digest_for(b"manager-frame") == digest


def test_agent_frame_is_immutable_and_accepts_valid_capture_metadata() -> None:
    frame = _frame()

    assert frame.window_id == 42
    with pytest.raises(FrozenInstanceError):
        frame.width = 600.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_id", 0),
        ("window_id", False),
        ("window_id", 1.5),
        ("owner_bundle_id", "  "),
        ("owner_bundle_id", "com.bluestacks.BlueStacksManager"),
        ("x", float("nan")),
        ("y", float("inf")),
        ("width", 0.0),
        ("height", -1.0),
        ("width", True),
        ("pixel_width", 0),
        ("pixel_width", False),
        ("pixel_width", 1.5),
        ("pixel_height", -1),
        ("pixel_height", "1200"),
        ("digest", "ABCDEF" * 10 + "abcd"),
        ("digest", "a" * 63),
        ("digest", "g" * 64),
        ("digest", None),
    ],
)
def test_agent_frame_rejects_invalid_capture_metadata(field: str, value: object) -> None:
    with pytest.raises(HostCapabilityError):
        _frame(**{field: value})


def test_agent_status_is_immutable_and_carries_optional_frame_evidence() -> None:
    status = AgentStatus(state="ready", detail="screen recording granted", evidence=_frame())

    assert status.evidence is not None
    with pytest.raises(FrozenInstanceError):
        status.state = "failed"  # type: ignore[misc]


@pytest.mark.parametrize("state", ["unknown", "READY", ""])
def test_agent_status_rejects_unknown_state(state: str) -> None:
    with pytest.raises(HostCapabilityError):
        AgentStatus(state=state, detail="nope", evidence=None)


@pytest.mark.parametrize(
    ("detail", "evidence"),
    [(None, None), (1, None), ("ok", object())],
)
def test_agent_status_rejects_invalid_detail_or_evidence(
    detail: object, evidence: object
) -> None:
    with pytest.raises(HostCapabilityError):
        AgentStatus(state="ready", detail=detail, evidence=evidence)  # type: ignore[arg-type]


def test_input_agent_protocol_describes_the_agent_contract() -> None:
    class StubAgent:
        def health(self) -> AgentStatus:
            return AgentStatus("ready", "ok", None)

        def capture_manager(self) -> AgentFrame:
            return _frame()

        def request(self, action: str, evidence: AgentFrame) -> AgentStatus:
            return AgentStatus("awaiting_source", action, evidence)

    agent: InputAgent = StubAgent()

    assert agent.request("clone", agent.capture_manager()).state == "awaiting_source"


class _BoundStubAgent(EvidenceBoundInputAgent):
    def __init__(self, frame: AgentFrame) -> None:
        super().__init__()
        self.frame = frame
        self.requests: list[tuple[str, AgentFrame]] = []

    def health(self) -> AgentStatus:
        return AgentStatus("ready", "ok", None)

    def _capture_manager(self) -> AgentFrame:
        return self.frame

    def _request(self, action: str, evidence: AgentFrame) -> AgentStatus:
        self.requests.append((action, evidence))
        return AgentStatus("created", action, evidence)


def test_evidence_bound_agent_requires_a_current_manager_capture() -> None:
    frame = _frame()
    agent = _BoundStubAgent(frame)

    with pytest.raises(HostCapabilityError, match="capture"):
        agent.request("clone", frame)

    current = agent.capture_manager()
    assert agent.request("clone", current).state == "created"
    assert agent.requests == [("clone", current)]


@pytest.mark.parametrize(
    "evidence",
    [
        _frame(window_id=43),
        _frame(digest=AgentFrame.digest_for(b"stale-manager-frame")),
    ],
)
def test_evidence_bound_agent_rejects_wrong_or_stale_evidence(evidence: AgentFrame) -> None:
    agent = _BoundStubAgent(_frame())
    agent.capture_manager()

    with pytest.raises(HostCapabilityError, match="current"):
        agent.request("clone", evidence)
    assert agent.requests == []


def test_input_agent_client_rejects_an_unsafe_socket_or_action(tmp_path: Path) -> None:
    client = InputAgentClient(tmp_path / "agent.sock", runtime_dir=tmp_path)

    with pytest.raises(HostCapabilityError, match="action"):
        client.request("click", _frame())

    with pytest.raises(HostCapabilityError, match="runtime"):
        InputAgentClient(Path("/tmp") / "agent.sock")


def test_input_agent_client_decodes_a_captured_manager_and_binds_request_ids(tmp_path: Path) -> None:
    expected = _frame()
    # macOS limits Unix-domain socket names to 104 bytes.  pytest's per-test
    # directory can exceed that, so use a unique short root for this transport
    # boundary test while retaining the same runtime-parent validation.
    runtime_dir = Path("/tmp") / f"tia-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    runtime_dir.mkdir()
    path, requests, thread = _agent_server(runtime_dir, {
        "state": "ready", "detail": "exact manager captured", "evidence": {
            "window_id": expected.window_id, "owner_bundle_id": expected.owner_bundle_id,
            "x": expected.x, "y": expected.y, "width": expected.width, "height": expected.height,
            "pixel_width": expected.pixel_width, "pixel_height": expected.pixel_height,
            "digest": expected.digest,
        },
    })

    try:
        frame = InputAgentClient(path, runtime_dir=runtime_dir).capture_manager()
        thread.join(timeout=1)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)

    assert frame == expected
    assert requests == [{"action": "capture_manager", "request_id": 1}]


@pytest.mark.parametrize("evidence", [{}, {"owner_bundle_id": BLUESTACKS_MIM_BUNDLE_ID}])
def test_agent_frame_from_wire_rejects_incomplete_or_forged_evidence(evidence: object) -> None:
    with pytest.raises(HostCapabilityError, match="evidence|owner"):
        AgentFrame.from_wire(evidence)


def test_pixel_to_global_point_uses_each_axis_scale_independently() -> None:
    point = pixel_to_global_point(_frame(), (250, 600))

    assert point == (225.0, 350.0)


@pytest.mark.parametrize(
    ("pixel", "expected"),
    [
        ((0, 0), (100.0, 200.0)),
        ((1_000, 1_200), (600.0, 500.0)),
    ],
)
def test_pixel_to_global_point_includes_frame_edges(
    pixel: tuple[int, int], expected: tuple[float, float]
) -> None:
    assert pixel_to_global_point(_frame(), pixel) == expected


@pytest.mark.parametrize(
    "pixel",
    [(-1, 0), (0, -1), (1_001, 0), (0, 1_201), (True, 0), (0, 1.5)],
)
def test_pixel_to_global_point_rejects_invalid_pixel_coordinates(pixel: tuple[object, object]) -> None:
    with pytest.raises(HostCapabilityError):
        pixel_to_global_point(_frame(), pixel)  # type: ignore[arg-type]
