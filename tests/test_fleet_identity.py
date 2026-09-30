from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.identity import Attempt, IdentityEvidence
from fleet.runtime import WorkerRuntime, validate_isolation
from fleet import runtime as fleet_runtime
from tower_bot import parse_args, resolve_worker_runtime


@pytest.fixture(autouse=True)
def isolated_runtime_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests must not acquire the real fleet's ADB/port locks on a running host.
    monkeypatch.setattr(fleet_runtime.tempfile, "tempdir", str(tmp_path))


def test_worker_dashboard_ports_preserve_safe_assignments_without_collisions() -> None:
    ports = [fleet_runtime.worker_dashboard_port(number) for number in range(55_536)]
    assert ports[80] == 9999
    assert ports[79] == 10079
    assert ports[81] == 10081
    assert ports[10_080] == 20080
    assert len(ports) == len(set(ports))
    assert 10080 not in ports
    assert min(ports) >= 1 and max(ports) == 65535


@pytest.mark.parametrize("number", [-1, 55_536, True, 1.5])
def test_worker_dashboard_port_rejects_invalid_numbers(number: object) -> None:
    with pytest.raises(ValueError, match="worker number"):
        fleet_runtime.worker_dashboard_port(number)


@pytest.mark.parametrize("port", [21, 22, 5060, 5061, 6000, *range(6665, 6670), 10080])
def test_worker_runtime_rejects_browser_blocked_dashboard_ports(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="browser"):
        WorkerRuntime.for_worker(tmp_path, "worker-a", port)


def test_attempt_binding_requires_observed_identity_evidence(tmp_path: Path) -> None:
    binding = tmp_path / "binding.json"
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    assert not binding.exists()

    with pytest.raises(ValueError, match="evidence"):
        attempt.persist(binding, None)
    assert not binding.exists()

    observed_at = attempt.created_at + 1
    attempt.persist(binding, IdentityEvidence("account-a", observed_at, "frame://one"))
    saved = json.loads(binding.read_text())
    assert saved["account_id"] == "account-a"
    assert saved["observed_at"] == observed_at
    assert saved["evidence_ref"] == "frame://one"
    assert saved["endpoint"] == "127.0.0.1:5555"


def test_reusing_emulator_creates_new_immutable_generation() -> None:
    first = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    second = Attempt.new("worker-a", "127.0.0.1:5555", "lease-b", "attempt-b")
    assert first.generation != second.generation
    with pytest.raises(AttributeError):
        first.generation = second.generation


def test_attempt_rejects_evidence_from_before_its_generation(tmp_path: Path) -> None:
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    binding = tmp_path / "binding.json"
    with pytest.raises(ValueError, match="before"):
        attempt.persist(binding, IdentityEvidence("account-a", attempt.created_at - 1, "frame://old"))
    assert not binding.exists()


def test_two_workers_cannot_share_writable_paths_or_web_port(tmp_path: Path) -> None:
    first = WorkerRuntime.for_worker(tmp_path, "worker-a", 8765)
    second = WorkerRuntime.for_worker(tmp_path, "worker-b", 8766)
    validate_isolation([first, second])
    assert first.writable_paths().isdisjoint(second.writable_paths())

    with pytest.raises(ValueError, match="web port"):
        validate_isolation([first, WorkerRuntime.for_worker(tmp_path, "worker-b", 8765)])
    with pytest.raises(ValueError, match="runtime path"):
        validate_isolation([first, WorkerRuntime.for_worker(tmp_path, "worker-a", 8766)])


def test_fleet_cli_resolves_all_writable_roots(tmp_path: Path) -> None:
    args = parse_args([
        "--worker-id", "worker-a", "--lease-id", "lease-a", "--attempt-id", "attempt-a",
        "--runtime-root", str(tmp_path), "--web-port", "8765",
    ])
    runtime = resolve_worker_runtime(args)
    assert runtime is not None
    assert runtime.db_path == tmp_path / "worker-a" / "tower_bot.db"
    assert runtime.strategy_root == tmp_path / "worker-a" / "strategies"
    assert runtime.evidence_root == tmp_path / "worker-a" / "evidence"
    assert runtime.checkpoint_root == tmp_path / "worker-a" / "checkpoints"


def test_fleet_cli_refuses_partial_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="worker identity"):
        resolve_worker_runtime(parse_args(["--worker-id", "worker-a", "--runtime-root", str(tmp_path)]))


def test_live_runtime_reservation_rejects_shared_root_or_port(tmp_path: Path) -> None:
    first = WorkerRuntime.for_worker(tmp_path, "worker-a", 8765)
    same_root = WorkerRuntime.for_worker(tmp_path, "worker-a", 8766)
    same_port = WorkerRuntime.for_worker(tmp_path, "worker-b", 8765)
    independent = WorkerRuntime.for_worker(tmp_path, "worker-b", 8766)
    with first.reserve():
        with pytest.raises(ValueError, match="runtime path"):
            with same_root.reserve():
                pass
        with pytest.raises(ValueError, match="web port"):
            with same_port.reserve():
                pass
        with independent.reserve():
            pass


def test_live_runtime_reservation_rejects_same_adb_endpoint(tmp_path: Path) -> None:
    first = WorkerRuntime.for_worker(tmp_path, "worker-a", 8765)
    second = WorkerRuntime.for_worker(tmp_path, "worker-b", 8766)
    with first.reserve("127.0.0.1:5555"):
        with pytest.raises(ValueError, match="ADB endpoint"):
            with second.reserve("127.0.0.1:5555"):
                pass


def test_web_port_collision_across_fleet_roots_is_rejected(tmp_path: Path) -> None:
    first = WorkerRuntime.for_worker(tmp_path / "fleet-a", "worker-a", 8765)
    second = WorkerRuntime.for_worker(tmp_path / "fleet-b", "worker-b", 8765)
    with first.reserve():
        with pytest.raises(ValueError, match="web port"):
            with second.reserve():
                pass
