"""Independent progress detection and fenced process recovery."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fleet.identity import Attempt
from fleet.input_lease import InputLease, InputLeaseExpired
from fleet.worker_monitor import WorkerMonitor
from runtime_identity import PROCESS_IDENTITY
from runtime_records import RuntimeRecords
from tests.test_reroll_supervisor import _harness


NAME = "Tiramisu64_20"


def _worker(root: Path, supervisor: object, *, now: float = 1000.0) -> tuple[InputLease, Attempt]:
    row = supervisor._read(NAME)
    assert row is not None
    attempt = Attempt(NAME, row["endpoint"], row["lease_id"], row["attempt_id"],
                      row["input_generation"], 100.0)
    path = root / "workers" / NAME
    records = RuntimeRecords(path / "runtime-records.json")
    records.start(attempt, PROCESS_IDENTITY, boot_id="boot-a", pid=row["pid"])
    (path / "worker-heartbeat.json").write_text(json.dumps({
        "worker_id": NAME, "account_id": NAME, "lease_id": row["lease_id"],
        "attempt_id": row["attempt_id"], "generation": attempt.generation,
        "boot_id": "boot-a", "pid": row["pid"], "scan_sequence": 3,
        "phase": "ocr_inference", "phase_started_utc": 900.0,
        "phase_deadline_utc": 950.0, "last_completed_scan_utc": 900.0,
        "observation_required": False, "wait_reason": None, "next_wake_utc": None,
        "capabilities": {},
    }))
    lease = InputLease(path / "input-lease.json")
    lease.assert_current(attempt.generation)
    return lease, attempt


def test_input_lease_rejects_revoked_generation(tmp_path: Path) -> None:
    lease = InputLease(tmp_path / "lease.json")
    lease.grant("one")
    lease.assert_current("one")
    lease.revoke("one", "blocked scan")
    with pytest.raises(InputLeaseExpired):
        lease.assert_current("one")
    with pytest.raises(InputLeaseExpired):
        lease.rotate("one", "two")


def test_new_process_without_child_evidence_is_starting(tmp_path: Path) -> None:
    make, _, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    assert WorkerMonitor(tmp_path, supervisor).check_once()[NAME]["state"] == "starting"


def test_live_unresponsive_pid_prevents_replacement(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    lease, attempt = _worker(tmp_path, supervisor)
    supervisor.terminate = lambda pid: killed.append(pid)
    supervisor.force_kill = lambda pid: killed.append(pid)
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                            sleep=lambda _: None, stop_grace=0)
    result = monitor.check_once()
    assert result[NAME]["state"] == "quarantined"
    assert len(spawned) == 1
    assert killed == [4000, 4000]
    assert 4000 in live
    held = monitor.check_once()[NAME]
    assert (held["state"], held["reason"]) == ("quarantined", "old_process_alive")
    assert killed == [4000, 4000]
    with pytest.raises(InputLeaseExpired):
        lease.assert_current(attempt.generation)


def test_revoke_precedes_signal_and_new_pid_waits_for_child_record(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    lease, attempt = _worker(tmp_path, supervisor)

    def terminate(pid: int) -> None:
        with pytest.raises(InputLeaseExpired):
            lease.assert_current(attempt.generation)
        killed.append(pid)
        live.pop(pid)

    supervisor.terminate = terminate
    monitor = WorkerMonitor(tmp_path, supervisor, clock=time.time,
                            endpoint_available=lambda _: True)
    assert monitor.check_once()[NAME]["state"] == "starting"
    assert len(spawned) == 2
    assert killed == [4000]
    archived = json.loads((tmp_path / "reroll-processes" /
                           f"{NAME}.monitor-incidents.json").read_text())
    assert archived[-1]["outcome"] == "fresh_scan_pending"
    assert archived[-1]["boot_id"] == "boot-a"
    assert monitor.check_once()[NAME]["state"] == "starting"
    new_generation = supervisor._read(NAME)["input_generation"]
    assert new_generation != attempt.generation
    with pytest.raises(InputLeaseExpired):
        lease.assert_current(attempt.generation)
    lease.assert_current(new_generation)
    registration = json.loads((tmp_path / "workers" / NAME /
                               "fleet-registration.json").read_text())
    assert json.loads(Path(registration["binding"]).read_text())["generation"] == new_generation


def test_recovery_refuses_replaced_record_before_first_signal(tmp_path: Path) -> None:
    make, spawned, _, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    original = supervisor.pause

    def replace_before_pause(name: str, **kwargs: object) -> dict:
        record = supervisor._read(name)
        supervisor._save(name, {**record, "pid": 4999})
        return original(name, **kwargs)

    supervisor.pause = replace_before_pause
    row = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0).check_once()[NAME]
    assert row["state"] == "identity_conflict"
    assert killed == [] and len(spawned) == 1


def test_recovery_refuses_replaced_record_before_force_signal(tmp_path: Path) -> None:
    make, spawned, _, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    supervisor.terminate = lambda pid: killed.append(pid)
    supervisor.force_kill = lambda pid: killed.append(pid)
    original = supervisor.kill

    def replace_before_kill(name: str, **kwargs: object) -> dict:
        record = supervisor._read(name)
        supervisor._save(name, {**record, "pid": 4999})
        return original(name, **kwargs)

    supervisor.kill = replace_before_kill
    row = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                        stop_grace=0).check_once()[NAME]
    assert row["state"] == "identity_conflict"
    assert killed == [4000] and len(spawned) == 1


def test_recovery_refuses_replaced_record_at_handoff(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    lease, _ = _worker(tmp_path, supervisor)

    def replacement(_endpoint: str) -> bool:
        record = supervisor._read(NAME)
        supervisor._save(NAME, {**record, "pid": 4999})
        lease.grant("operator-generation")
        return True

    row = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                        endpoint_available=replacement).check_once()[NAME]
    assert row["state"] == "identity_conflict"
    assert len(spawned) == 1
    lease.assert_current("operator-generation")


def test_shutdown_during_grace_never_spawns_replacement(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    lease, attempt = _worker(tmp_path, supervisor)
    supervisor.terminate = lambda pid: killed.append(pid)
    supervisor.force_kill = lambda pid: killed.append(pid)
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                            stop_grace=1, sleep=lambda _: monitor.stop())
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("quarantined", "recovery_cancelled")
    assert killed == [4000] and 4000 in live and len(spawned) == 1
    with pytest.raises(InputLeaseExpired):
        lease.assert_current(attempt.generation)


def test_monitor_stop_surfaces_incomplete_shutdown(tmp_path: Path) -> None:
    make, _, _, _ = _harness(tmp_path)
    monitor = WorkerMonitor(tmp_path, make())

    class StuckThread:
        def join(self, timeout: float) -> None:
            assert timeout > 0

        def is_alive(self) -> bool:
            return True

    monitor._thread = StuckThread()
    with pytest.raises(RuntimeError, match="did not stop"):
        monitor.stop()


def test_endpoint_lock_must_release_before_replacement(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                            endpoint_available=lambda _: False)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("quarantined", "endpoint_lock_held")
    assert len(spawned) == 1


def test_operator_pause_during_recovery_prevents_replacement(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)

    def endpoint_available(_endpoint: str) -> bool:
        assert supervisor.pause(NAME)["state"] == "paused"
        return True

    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                            endpoint_available=endpoint_available)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("paused", "operator_pause")
    assert len(spawned) == 1


def test_restart_budget_survives_monitor_recreation(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    first = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                          max_restarts=1, endpoint_available=lambda _: True)
    assert first.check_once()[NAME]["state"] == "starting"
    _worker(tmp_path, supervisor)
    second = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                           max_restarts=1, endpoint_available=lambda _: True)
    row = second.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("quarantined", "restart_budget")
    assert len(spawned) == 2


def test_changed_pid_is_never_signalled(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    live[4000] = ("another", "process")
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0)
    assert monitor.check_once()[NAME]["state"] == "identity_conflict"
    assert killed == []
    assert len(spawned) == 1


def test_quarantined_session_conflict_is_not_restarted(tmp_path: Path) -> None:
    make, spawned, _, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    checkpoint = tmp_path / "workers" / NAME / "checkpoints" / "supervisor.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(json.dumps({"endpoint": "127.0.0.1:5755",
                                     "expected_account": NAME,
                                     "state": "quarantined", "reason": "session_conflict"}))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("identity_conflict", "session_conflict")
    assert len(spawned) == 1
    assert killed == []


def test_manual_pause_and_stopped_worker_never_restart(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    supervisor.pause(NAME)
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0)
    assert monitor.check_once()[NAME]["state"] == "paused"
    assert len(spawned) == 1


def test_in_process_operator_pause_survives_stale_scan(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    path = tmp_path / "workers" / NAME / "worker-heartbeat.json"
    beat = json.loads(path.read_text())
    beat.update(phase="idle", phase_deadline_utc=None,
                wait_reason="operator_pause", next_wake_utc=None)
    path.write_text(json.dumps(beat))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0)
    assert monitor.check_once()[NAME]["state"] == "paused"
    assert len(spawned) == 1


def test_stale_completed_scan_is_independent_of_fresh_phase(tmp_path: Path) -> None:
    make, _, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    path = tmp_path / "workers" / NAME / "worker-heartbeat.json"
    beat = json.loads(path.read_text())
    beat["phase_deadline_utc"] = 1100.0
    path.write_text(json.dumps(beat))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                            recover=False, scan_stale_seconds=60)
    row = monitor.check_once()[NAME]
    assert row["state"] == "blocked_scan"
    assert {key: row[key] for key in ("worker_id", "lease_id", "attempt_id",
                                      "generation", "boot_id", "pid")} == {
        "worker_id": NAME, "lease_id": "a", "attempt_id": beat["attempt_id"],
            "generation": beat["generation"], "boot_id": "boot-a", "pid": 4000,
    }
    assert row["observed_at_utc"] == 1000.0


def test_clock_jump_requires_new_observation_before_recovery(tmp_path: Path) -> None:
    make, spawned, _, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    path = tmp_path / "workers" / NAME / "worker-heartbeat.json"
    beat = json.loads(path.read_text())
    beat.update(phase_deadline_utc=1100.0, last_completed_scan_utc=950.0)
    path.write_text(json.dumps(beat))
    wall = iter((1000.0, 2000.0))
    mono = iter((100.0, 101.0))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: next(wall),
                            monotonic=lambda: next(mono), scan_stale_seconds=200)
    assert monitor.check_once()[NAME]["state"] == "healthy"
    assert monitor.check_once()[NAME]["state"] == "observation_required"
    assert len(spawned) == 1
    assert killed == []


def test_blocked_child_ocr_phase_is_detected_without_child_cooperation(tmp_path: Path) -> None:
    root = tmp_path / "workers" / NAME
    child_code = """
import os, sys, time
from pathlib import Path
from fleet.identity import Attempt
from runtime_identity import PROCESS_IDENTITY
from runtime_records import RuntimeRecords
from runtime_progress import ProgressRecorder
root = Path(sys.argv[1])
attempt = Attempt('Tiramisu64_20', '127.0.0.1:5755', 'a', 'attempt-a', 'generation-a', 100.0)
RuntimeRecords(root / 'runtime-records.json').start(attempt, PROCESS_IDENTITY,
                                                      boot_id='boot-child', pid=os.getpid())
progress = ProgressRecorder(root / 'worker-heartbeat.json', attempt=attempt,
                            account_id=None, boot_id='boot-child', pid=os.getpid())
with progress.phase('ocr_inference', .2):
    time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", child_code, str(root)])
    try:
        deadline = time.monotonic() + 3
        while not (root / "worker-heartbeat.json").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root / "worker-heartbeat.json").exists(), "child did not publish OCR phase"

        class Supervisor:
            def reconcile(self) -> dict:
                return {NAME: {"state": "running", "pid": child.pid,
                               "attempt_id": "attempt-a"}}

            def _read(self, name: str) -> dict:
                assert name == NAME
                return {"name": NAME, "endpoint": "127.0.0.1:5755",
                        "lease_id": "a", "attempt_id": "attempt-a", "pid": child.pid,
                        "args": ("blocked-child",)}

        deadline = time.monotonic() + 2
        result = None
        monitor = WorkerMonitor(tmp_path, Supervisor(), recover=False)
        while time.monotonic() < deadline:
            result = monitor.check_once()[NAME]
            if result["state"] == "blocked_scan":
                break
            time.sleep(.02)
        assert result is not None
        assert (result["state"], result["reason"], result["phase"]) == (
            "blocked_scan", "phase_deadline", "ocr_inference")
        assert child.poll() is None
    finally:
        child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)


def test_graceful_exit_zombie_of_owned_child_is_exited_not_pid_change(tmp_path: Path) -> None:
    """After SIGTERM the owned child is a zombie ('<defunct>' in macOS ps)
    until polled; death is confirmed through the owned Popen, not argv."""
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    exiting: set[int] = set()
    zombies: set[int] = set()
    real_probe = supervisor.process_identity

    def probe(pid: int):
        if pid in zombies:
            return ("<defunct>",)
        return real_probe(pid)

    def terminate(pid: int) -> None:
        killed.append(pid)
        exiting.add(pid)

    def sleep(_: float) -> None:
        for pid in list(exiting):
            exiting.discard(pid)
            live.pop(pid, None)
            zombies.add(pid)

    owned = supervisor._owned[NAME].process
    original_poll = owned.poll

    def poll():
        code = original_poll()
        if code is not None:
            zombies.discard(owned.pid)  # poll reaps: ps no longer lists it
        return code

    owned.poll = poll
    supervisor.process_identity = probe
    supervisor.terminate = terminate
    monitor = WorkerMonitor(tmp_path, supervisor, clock=time.time, sleep=sleep,
                            endpoint_available=lambda _: True)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("starting", "fresh_scan_pending"), row
    assert len(spawned) == 2 and killed == [4000]


def test_hang_that_starts_while_paused_is_a_blocked_scan(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    path = tmp_path / "workers" / NAME / "worker-heartbeat.json"
    beat = json.loads(path.read_text())
    beat.update(phase="ocr_inference", phase_deadline_utc=950.0,
                last_completed_scan_utc=900.0, wait_reason="operator_pause",
                next_wake_utc=None)
    path.write_text(json.dumps(beat))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0, recover=False)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"], row["phase"]) == (
        "blocked_scan", "phase_deadline", "ocr_inference")
    # A stale completed scan alone stays exempt while paused.
    beat.update(phase="idle", phase_deadline_utc=None)
    path.write_text(json.dumps(beat))
    assert monitor.check_once()[NAME]["state"] == "paused"


def test_permanently_unverified_account_is_not_healthy_and_restarts(tmp_path: Path) -> None:
    """A mid-run reconnect leaves account_unverified while scans complete."""
    make, spawned, _, killed = _harness(tmp_path)
    supervisor = make()
    supervisor.start(NAME)
    _worker(tmp_path, supervisor)
    path = tmp_path / "workers" / NAME
    beat = json.loads((path / "worker-heartbeat.json").read_text())
    beat.update(phase="idle", phase_deadline_utc=None, last_completed_scan_utc=995.0)
    (path / "worker-heartbeat.json").write_text(json.dumps(beat))
    (path / "checkpoints").mkdir(exist_ok=True)
    checkpoint = {"endpoint": "127.0.0.1:5755", "expected_account": NAME, "state": "blocked",
                  "reason": "account_unverified", "account_unverified_since": 990.0}
    (path / "checkpoints" / "supervisor.json").write_text(json.dumps(checkpoint))
    monitor = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0, recover=False)
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("expected_wait", "account_reverification")
    checkpoint["account_unverified_since"] = 50.0
    (path / "checkpoints" / "supervisor.json").write_text(json.dumps(checkpoint))
    row = monitor.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("blocked_scan", "account_unverified")
    recovering = WorkerMonitor(tmp_path, supervisor, clock=lambda: 1000.0,
                               endpoint_available=lambda _: True)
    row = recovering.check_once()[NAME]
    assert (row["state"], row["reason"]) == ("starting", "fresh_scan_pending")
    assert len(spawned) == 2 and killed == [4000]


def test_supervisor_checkpoint_records_when_account_became_unverified(tmp_path: Path) -> None:
    from supervisor import DeviceSupervisor

    class Device:
        serial = "127.0.0.1:5755"

    now = [100.0]
    device_supervisor = DeviceSupervisor(path=tmp_path / "supervisor.json", endpoint="127.0.0.1:5755",
                                         connect=Device, expected_account="acct",
                                         clock=lambda: now[0])
    device_supervisor.recover()
    saved = json.loads((tmp_path / "supervisor.json").read_text())
    assert saved["account_unverified_since"] == 100.0
    now[0] = 101.0
    device_supervisor.verify_account("acct", observed_at=100.5)
    saved = json.loads((tmp_path / "supervisor.json").read_text())
    assert saved["account_unverified_since"] is None
