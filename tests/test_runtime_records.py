"""Durable startup and incident records without a live device or account."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.identity import Attempt
from runtime_identity import BackendIdentity
from runtime_records import RuntimeRecords, RuntimeRecordsError


def _attempt() -> Attempt:
    return Attempt("worker-1", "127.0.0.1:5555", "lease-1", "attempt-1",
                   generation="generation-1", created_at=100.0)


def _identity() -> BackendIdentity:
    return BackendIdentity("revision-1", "hash-1", 99.0)


def test_missing_record_is_unknown_and_corrupt_record_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    assert records.read() is None

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeRecordsError, match="invalid"):
        records.read()
    with pytest.raises(RuntimeRecordsError, match="invalid"):
        records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_valid_json_missing_source_identity_cannot_make_start_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    incomplete = json.loads(path.read_text(encoding="utf-8"))
    del incomplete["start"]["source_hash"]
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    previous = path.read_bytes()

    with pytest.raises(RuntimeRecordsError, match="invalid"):
        records.read()
    with pytest.raises(RuntimeRecordsError, match="invalid"):
        records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    assert path.read_bytes() == previous


def test_valid_json_malformed_incident_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    records.incident(generation="generation-1", fingerprint="stalled",
                     phase="scan", reason="no_progress", outcome="failed")
    malformed = json.loads(path.read_text(encoding="utf-8"))
    malformed["incidents"][0]["count"] = "two"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(RuntimeRecordsError, match="invalid"):
        records.read()


def test_provisional_start_keeps_source_and_failure_without_account_claim(tmp_path: Path) -> None:
    records = RuntimeRecords(tmp_path / "runtime.json")
    records.begin(worker_id="worker-1", endpoint="127.0.0.1:5555",
                  lease_id="lease-1", attempt_id="attempt-1", identity=_identity(),
                  boot_id="boot-1", pid=123)
    records.fail_startup(boot_id="boot-1", pid=123,
                         reason="reroll worker registration unavailable")

    saved = records.read()
    assert saved["start"]["source_hash"] == "hash-1"
    assert saved["start"]["pid"] == 123
    assert saved["start"]["generation"] is None
    assert saved["start"]["attempt_verified"] is False
    assert saved["start"]["verified"] is False
    assert saved["failure"]["reason"] == "reroll worker registration unavailable"
    assert "account_id" not in json.dumps(saved)


def test_verified_attempt_completes_provisional_start(tmp_path: Path) -> None:
    records = RuntimeRecords(tmp_path / "runtime.json")
    records.begin(worker_id="worker-1", endpoint="127.0.0.1:5555",
                  lease_id="lease-1", attempt_id="attempt-1", identity=_identity(),
                  boot_id="boot-1", pid=123)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    saved = records.read()
    assert saved["start"]["generation"] == "generation-1"
    assert saved["start"]["attempt_verified"] is True
    assert saved["start"]["verified"] is False
    assert saved["failure"] is None


def test_start_persists_unverified_worker_and_source_before_failure(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["start"] == {
        "worker_id": "worker-1", "endpoint": "127.0.0.1:5555",
        "lease_id": "lease-1", "attempt_id": "attempt-1",
        "generation": "generation-1", "created_at": 100.0,
        "boot_id": "boot-1", "pid": 123,
        "revision": "revision-1", "source_hash": "hash-1",
        "started_at": 99.0, "verified": False, "attempt_verified": True,
    }
    assert saved["incidents"] == []
    assert "account_id" not in path.read_text(encoding="utf-8")
    assert not (tmp_path / "generation-1.json").exists()


def test_same_start_is_idempotent_and_new_generation_replaces_current(tmp_path: Path) -> None:
    records = RuntimeRecords(tmp_path / "runtime.json")
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    records.incident(generation="generation-1", fingerprint="connect-error",
                     phase="connect", reason="device_missing", outcome="failed")
    records.start(_attempt(), BackendIdentity("revision-2", "hash-2", 200.0),
                  boot_id="boot-1", pid=123)
    assert records.read()["start"]["source_hash"] == "hash-1"
    assert records.read()["incidents"][0]["count"] == 1

    next_attempt = Attempt("worker-1", "127.0.0.1:5555", "lease-1", "attempt-1",
                           generation="generation-2", created_at=201.0)
    records.start(next_attempt, BackendIdentity("revision-2", "hash-2", 200.0),
                  boot_id="boot-1", pid=123)
    assert records.read()["start"]["generation"] == "generation-2"
    assert records.read()["start"]["source_hash"] == "hash-2"
    assert records.read()["incidents"] == []


def test_incidents_group_by_fingerprint_and_bound_evidence(tmp_path: Path) -> None:
    records = RuntimeRecords(tmp_path / "runtime.json")
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    for outcome in ("failed", "recovered"):
        records.incident(generation="generation-1", fingerprint="same-stall",
                         phase="scan", reason="no_progress", outcome=outcome,
                         evidence_refs=[f"frame-{index}" for index in range(10)],
                         recent_outcomes=[f"action-{index}" for index in range(20)])
    saved = records.read()
    assert len(saved["incidents"]) == 1
    incident = saved["incidents"][0]
    assert incident["generation"] == "generation-1"
    assert incident["revision"] == "revision-1"
    assert incident["source_hash"] == "hash-1"
    assert incident["count"] == 2
    assert incident["outcome"] == "recovered"
    assert 0 < len(incident["evidence_refs"]) <= 4
    assert 0 < len(incident["recent_outcomes"]) <= 8
    records.incident(generation="generation-1", fingerprint="long-detail",
                     phase="scan", reason="no_progress", outcome="failed",
                     evidence_refs=["e" * 3000], recent_outcomes=["o" * 3000])
    long_detail = records.read()["incidents"][-1]
    assert len(long_detail["evidence_refs"][0]) <= 1024
    assert len(long_detail["recent_outcomes"][0]) <= 1024
    with pytest.raises(RuntimeRecordsError, match="generation"):
        records.incident(generation="wrong", fingerprint="same-stall",
                         phase="scan", reason="no_progress", outcome="failed")


@pytest.mark.parametrize("details", [
    {"evidence_refs": [""]},
    {"recent_outcomes": ["   "]},
    {"evidence_refs": [" " * 1024 + "valid"]},
])
def test_blank_incident_details_cannot_corrupt_future_records(
    tmp_path: Path, details: dict[str, list[str]]
) -> None:
    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    previous = path.read_bytes()

    with pytest.raises(ValueError, match="incident detail"):
        records.incident(generation="generation-1", fingerprint="bad-detail",
                         phase="scan", reason="no_progress", outcome="failed",
                         **details)
    assert path.read_bytes() == previous
    assert records.read()["incidents"] == []
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    records.incident(generation="generation-1", fingerprint="valid-detail",
                     phase="scan", reason="no_progress", outcome="recovered",
                     evidence_refs=["frame-1"], recent_outcomes=["capture-ok"])
    assert records.read()["incidents"][0]["fingerprint"] == "valid-detail"


def test_failed_atomic_replace_preserves_previous_record(tmp_path: Path, monkeypatch) -> None:
    import runtime_records

    path = tmp_path / "runtime.json"
    records = RuntimeRecords(path)
    records.start(_attempt(), _identity(), boot_id="boot-1", pid=123)
    previous = path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(runtime_records.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        records.incident(generation="generation-1", fingerprint="failure",
                         phase="scan", reason="stalled", outcome="failed")
    assert path.read_bytes() == previous
    assert list(tmp_path.glob(".*.tmp")) == []


def test_runner_records_failed_start_before_device_connection(tmp_path: Path) -> None:
    from control import Controls
    from device import IdentityError
    from events import EventBus
    from runner import BotRunner, RunnerError
    from sinks.state import BotState
    from strategy import Strategy

    path = tmp_path / "runtime.json"
    attempt = _attempt()

    def fail_connect() -> None:
        start = RuntimeRecords(path).read()["start"]
        assert start["generation"] == attempt.generation
        assert start["verified"] is False
        raise IdentityError("device unavailable")

    runner = BotRunner(
        bus=EventBus(), controls=Controls(strategy=Strategy.from_config()),
        state=BotState(), templates=object(), device_factory=fail_connect,
        checks={}, attempt=attempt, binding_path=tmp_path / "binding.json",
        runtime_records_path=path,
    )
    with pytest.raises(RunnerError, match="device unavailable"):
        runner.start()
    saved = RuntimeRecords(path).read()
    assert saved["start"]["pid"] > 0
    assert saved["incidents"][0]["phase"] == "startup"
    assert saved["incidents"][0]["reason"] == "device unavailable"
    assert not (tmp_path / "binding.json").exists()


def test_worker_main_records_source_before_ocr_setup(tmp_path: Path, monkeypatch) -> None:
    import tower_bot
    from fleet.runtime import WorkerRuntime

    class BeforeOcr(Exception):
        pass

    args = tower_bot.parse_args([
        "--worker-id", "worker-1", "--lease-id", "lease-1",
        "--attempt-id", "attempt-1", "--runtime-root", str(tmp_path),
        "--web-port", "10020", "--web", "--idle", "--no-store",
        "--no-telegram", "--web-host", "0.0.0.0",
    ])
    runtime = WorkerRuntime.for_worker(tmp_path, "worker-1", 10020)
    runtime.ensure_directories()

    def before_ocr(_loaded) -> None:
        saved = RuntimeRecords(runtime.root / "runtime-records.json").read()
        assert saved["start"]["pid"] > 0
        assert saved["start"]["source_hash"]
        assert saved["start"]["verified"] is False
        assert "account_id" not in saved["start"]
        raise BeforeOcr

    monkeypatch.setattr(tower_bot, "build_checks_and_controls", before_ocr)
    with pytest.raises(BeforeOcr):
        tower_bot._main(args, runtime)


def test_reroll_registration_failure_retains_child_start_evidence(tmp_path: Path) -> None:
    import tower_bot
    from fleet.runtime import WorkerRuntime

    args = tower_bot.parse_args([
        "--worker-id", "Tiramisu64_20", "--lease-id", "lease-1",
        "--attempt-id", "attempt-1", "--runtime-root", str(tmp_path),
        "--web-port", "10020", "--web", "--no-telegram",
        "--reroll-pool", str(tmp_path / "pool.json"),
        "--bluestacks-instance", "Tiramisu64_20",
    ])
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    runtime.ensure_directories()

    assert tower_bot._main(args, runtime) == 1
    saved = RuntimeRecords(runtime.root / "runtime-records.json").read()
    assert saved["start"]["pid"] > 0
    assert saved["start"]["source_hash"]
    assert saved["start"]["attempt_verified"] is False
    assert saved["start"]["generation"] is None
    assert saved["failure"]["reason"] == "reroll worker registration unavailable"
    assert "account_id" not in json.dumps(saved)


def test_coordinator_persists_pending_launch_without_claiming_child_identity(tmp_path: Path) -> None:
    from fleet.reroll_supervisor import RerollSupervisor

    member = {"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755",
              "lease_id": "lease-1", "state": "ready"}
    spawned: list[tuple[str, ...]] = []
    state_path = tmp_path / "reroll-processes" / "Tiramisu64_20.json"

    def enroll(_member, runtime, attempt):
        return {
            "state": "registered", "instance": member["name"],
            "endpoint": member["endpoint"], "lease_id": member["lease_id"],
            "account_id": "registered-account", "job_id": attempt.attempt_id,
            "binding": str(runtime.root / "binding.json"), "web_port": runtime.web_port,
        }

    def spawn(args):
        pending = json.loads(state_path.read_text(encoding="utf-8"))
        assert pending["pid"] is None
        assert pending["launch_identity"]["source_hash"]
        assert pending["launch_identity"]["coordinator_pid"] > 0
        assert not (tmp_path / "workers" / member["name"] / "runtime-records.json").exists()
        spawned.append(tuple(args))
        return type("FakeChild", (), {"pid": 4242, "poll": lambda self: None})()

    supervisor = RerollSupervisor(
        tmp_path, pool_snapshot=lambda: {"members": [member]}, enroll=enroll,
        spawn=spawn, process_identity=lambda _pid: spawned[0] if spawned else None,
        start_stagger_seconds=0,
    )
    assert supervisor.start(member["name"])["state"] == "running"
