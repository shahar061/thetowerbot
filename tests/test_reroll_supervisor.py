"""Coordinator lifecycle boundaries without launching emulators or workers."""

from __future__ import annotations

from pathlib import Path
import time
from threading import Event, Lock, Thread

from fleet.reroll_supervisor import RerollSupervisor


def _harness(root: Path, *, fail: str | None = None):
    members = [{"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "a", "state": "ready"},
               {"name": "Tiramisu64_21", "endpoint": "127.0.0.1:5765", "lease_id": "b", "state": "ready"}]
    spawned = []
    live = {}
    killed = []
    lock = Lock()

    def enroll(member, runtime, attempt):
        return {"state": "registered", "instance": member["name"],
                "endpoint": member["endpoint"], "lease_id": member["lease_id"],
                "account_id": member["name"], "job_id": attempt.attempt_id, "binding": str(runtime.root / "binding.json"),
                "web_port": runtime.web_port}

    def spawn(args):
        name = args[args.index("--worker-id") + 1]
        if name == fail:
            raise RuntimeError("spawn refused")
        with lock:
            pid = 4000 + len(spawned)
            spawned.append(tuple(args))
            live[pid] = tuple(args)
            return pid

    def probe(pid):
        return live.get(pid)

    def terminate(pid):
        killed.append(pid)
        live.pop(pid, None)

    def make():
        return RerollSupervisor(root, pool_snapshot=lambda: {"members": members},
                                enroll=enroll, spawn=spawn, process_identity=probe,
                                terminate=terminate, start_stagger_seconds=0)

    return make, spawned, live, killed


def test_start_all_isolates_workers_and_persists_distinct_identities(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    status = make().start_all()
    assert {name: row["state"] for name, row in status.items()} == {
        "Tiramisu64_20": "running", "Tiramisu64_21": "running"}
    assert len(spawned) == 2
    for flag in ("--bluestacks-instance", "--lease-id", "--attempt-id",
                 "--web-port"):
        assert len({args[args.index(flag) + 1] for args in spawned}) == 2
    assert {args[args.index("--reroll-pool") + 1] for args in spawned} == {str(tmp_path / "reroll-pool.json")}
    assert len({Path(args[args.index("--runtime-root") + 1]) / args[args.index("--worker-id") + 1] for args in spawned}) == 2
    assert all("--web" in args and "--game-package" in args for args in spawned)
    assert {args[args.index("--port") + 1] for args in spawned} == {"5755", "5765"}
    assert {row["state"] for row in make().reconcile().values()} == {"running"}
    assert len(spawned) == 2


def test_failure_isolated_and_pause_requires_matching_process_identity(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path, fail="Tiramisu64_21")
    supervisor = make()
    status = supervisor.start_all()
    assert status["Tiramisu64_20"]["state"] == "running"
    assert status["Tiramisu64_21"]["state"] == "failed"
    assert len(spawned) == 1
    live[4000] = ("unrelated",)
    assert make().pause("Tiramisu64_20")["state"] == "identity_changed"
    assert killed == []
    live[4000] = spawned[0]
    assert make().pause_all()["Tiramisu64_20"]["state"] == "paused"
    assert killed == [4000]


def test_pause_waits_for_actual_exit_before_allowing_restart(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    assert supervisor.start("Tiramisu64_20")["state"] == "running"
    supervisor.terminate = lambda pid: killed.append(pid)
    assert supervisor.pause("Tiramisu64_20")["state"] == "stopping"
    assert make().start("Tiramisu64_20")["state"] == "stopping"
    assert len(spawned) == 1
    del live[4000]
    assert make().reconcile()["Tiramisu64_20"]["state"] == "paused"


def test_stale_record_never_duplicates_or_kills_unproven_pid(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    make().start("Tiramisu64_20")
    live[4000] = ("reused-pid",)
    assert make().start("Tiramisu64_20")["state"] == "identity_changed"
    assert len(spawned) == 1
    assert make().pause("Tiramisu64_20")["state"] == "identity_changed"
    assert killed == []


def test_capacity_reports_pressure_and_defers_extra_member(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    original = make()
    members = original.pool_snapshot()["members"]
    members.append({"name": "Tiramisu64_22", "endpoint": "127.0.0.1:5775",
                    "lease_id": "c", "state": "ready"})
    status = original.start_all()
    assert [row["state"] for row in status.values()].count("running") == 2
    assert status["Tiramisu64_22"]["state"] == "capacity_wait"
    assert original.pressure() == {"running": 2, "starting": 0, "limit": 2, "available": 0}
    assert len(spawned) == 2


def test_unverified_spawn_retains_pid_for_review(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    def unverified(pid):
        return ("unrelated",) if pid == 4000 else live.get(pid)
    supervisor.process_identity = unverified
    assert supervisor.start("Tiramisu64_20")["state"] == "identity_changed"
    assert supervisor.start("Tiramisu64_20")["state"] == "identity_changed"
    assert len(spawned) == 1
    assert killed == []


def test_existing_registration_restarts_opened_tower_without_enrollment(tmp_path: Path) -> None:
    import json
    from fleet.identity import Attempt, IdentityEvidence

    make, spawned, live, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start("Tiramisu64_20")
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "tower_already_opened"
    live.clear()
    from fleet.runtime import WorkerRuntime
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10020)
    runtime.checkpoint_root.mkdir(parents=True)
    attempt = Attempt.new(member["name"], member["endpoint"], member["lease_id"],
                          "saved-attempt")
    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
    attempt.persist(binding, IdentityEvidence(member["name"], attempt.created_at + 1,
                                              "saved-proof"))
    (runtime.root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": member["name"], "endpoint": member["endpoint"],
        "lease_id": member["lease_id"], "account_id": member["name"],
        "job_id": "saved-attempt", "web_port": 10020, "binding": str(binding)}))
    supervisor.enroll = lambda *_: (_ for _ in ()).throw(AssertionError("must not enroll twice"))
    assert supervisor.start(member["name"])["state"] == "running"
    assert spawned[-1][spawned[-1].index("--attempt-id") + 1] == "saved-attempt"


def test_opened_tower_rejects_registration_with_changed_attempt(tmp_path: Path) -> None:
    import json
    from fleet.identity import Attempt, IdentityEvidence
    from fleet.runtime import WorkerRuntime

    make, spawned, live, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start("Tiramisu64_20")
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "tower_already_opened"
    live.clear()
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10020)
    runtime.checkpoint_root.mkdir(parents=True)
    attempt = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "original")
    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
    attempt.persist(binding, IdentityEvidence(member["name"], attempt.created_at + 1,
                                              "saved-proof"))
    (runtime.root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": member["name"], "endpoint": member["endpoint"],
        "lease_id": member["lease_id"], "account_id": member["name"],
        "job_id": "changed", "web_port": 10020, "binding": str(binding)}))
    assert supervisor.start(member["name"])["state"] == "failed"
    assert len(spawned) == 1


def test_stopped_member_is_started_then_rechecked_before_enrollment(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "start_required"
    started = []
    def start_instance(selected):
        started.append(selected["name"])
        member["state"] = "ready"
    supervisor.start_instance = start_instance
    assert supervisor.start(member["name"])["state"] == "running"
    assert started == [member["name"]]
    assert len(spawned) == 1


def test_capacity_wait_does_not_start_stopped_emulator(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    assert supervisor.start("Tiramisu64_20")["state"] == "running"
    assert supervisor.start("Tiramisu64_21")["state"] == "running"
    members = supervisor.pool_snapshot()["members"]
    members.append({"name": "Tiramisu64_22", "endpoint": "127.0.0.1:5775",
                    "lease_id": "c", "state": "start_required"})
    started = []
    supervisor.start_instance = lambda member: started.append(member["name"])
    assert supervisor.start("Tiramisu64_22")["state"] == "capacity_wait"
    assert started == []
    assert len(spawned) == 2


def test_unavailable_manager_start_records_failure(tmp_path: Path) -> None:
    make, _, _, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "start_required"
    assert supervisor.start(member["name"]) == {
        "name": member["name"], "state": "failed", "error": "start_required"}
    assert supervisor.reconcile()[member["name"]]["state"] == "failed"


def test_pause_all_stops_running_peer_while_another_enrolls(tmp_path: Path) -> None:
    make, _, _, killed = _harness(tmp_path)
    supervisor = make()
    assert supervisor.start("Tiramisu64_21")["state"] == "running"
    entered = Event()
    release = Event()
    original_enroll = supervisor.enroll

    def slow_enroll(member, runtime, attempt):
        if member["name"] == "Tiramisu64_20":
            entered.set()
            assert release.wait(2)
        return original_enroll(member, runtime, attempt)

    supervisor.enroll = slow_enroll
    starting = Thread(target=lambda: supervisor.start("Tiramisu64_20"))
    starting.start()
    assert entered.wait(2)
    pausing = Thread(target=supervisor.pause_all)
    pausing.start()
    try:
        deadline = time.monotonic() + 2
        while not killed and time.monotonic() < deadline:
            time.sleep(.01)
        assert killed, "running peer was not paused until enrollment finished"
    finally:
        release.set()
        starting.join(2)
        pausing.join(2)
