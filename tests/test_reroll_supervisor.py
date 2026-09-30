"""Coordinator lifecycle boundaries without launching emulators or workers."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import time
from threading import Barrier, Event, Lock, Thread

import pytest

from fleet.reroll_supervisor import RerollSupervisor, _process_identity
from fleet.runtime import reserve_endpoint
from fleet.identity import IdentityEvidence


class FakeChild:
    def __init__(self, pid: int, live: dict[int, tuple]) -> None:
        self.pid = pid
        self.live = live
        self.args = live[pid]

    def poll(self) -> int | None:
        return None if self.live.get(self.pid) == self.args else 0


def _harness(root: Path, *, fail: str | None = None,
             births: dict[int, str] | None = None):
    members = [{"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "a", "state": "ready"},
               {"name": "Tiramisu64_21", "endpoint": "127.0.0.1:5765", "lease_id": "b", "state": "ready"}]
    spawned = []
    live = {}
    killed = []
    lock = Lock()

    def enroll(member, runtime, attempt):
        runtime.checkpoint_root.mkdir(parents=True, exist_ok=True)
        binding = runtime.checkpoint_root / f"{attempt.generation}.json"
        attempt.persist(binding, IdentityEvidence(member["name"], attempt.created_at + 1,
                                                   "fake-proof"))
        registration = {"state": "registered", "instance": member["name"],
                "endpoint": member["endpoint"], "lease_id": member["lease_id"],
                "account_id": member["name"], "job_id": attempt.attempt_id, "binding": str(binding),
                "web_port": runtime.web_port}
        (runtime.root / "fleet-registration.json").write_text(json.dumps(registration))
        return registration

    def spawn(args):
        name = args[args.index("--worker-id") + 1]
        if name == fail:
            raise RuntimeError("spawn refused")
        with lock:
            pid = 4000 + len(spawned)
            spawned.append(tuple(args))
            live[pid] = tuple(args)
            return FakeChild(pid, live)

    def probe(pid):
        return live.get(pid)

    def terminate(pid):
        killed.append(pid)
        live.pop(pid, None)

    def make():
        return RerollSupervisor(root, pool_snapshot=lambda: {"members": members},
                                enroll=enroll, spawn=spawn, process_identity=probe,
                                process_birth=(births.get if births is not None else None),
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
    assert {row["state"] for row in make().reconcile().values()} == {"identity_changed"}
    assert len(spawned) == 2


def test_worker_launch_suppresses_telegram(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    make().start("Tiramisu64_20")
    assert "--no-telegram" in spawned[0]


def test_worker_80_launches_on_a_browser_safe_port(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["name"] = "Tiramisu64_80"
    assert supervisor.start(member["name"])["state"] == "running"
    assert spawned[0][spawned[0].index("--web-port") + 1] == "9999"


@pytest.mark.parametrize("corrupt", [False, True])
def test_stopped_legacy_port_migration_keeps_identity_checks(tmp_path: Path, corrupt: bool) -> None:
    from fleet.identity import Attempt

    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["name"] = "Tiramisu64_80"
    member["state"] = "tower_already_opened"
    root = tmp_path / "workers" / member["name"]
    checkpoint = root / "checkpoints"
    checkpoint.mkdir(parents=True)
    attempt = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "saved-job")
    binding = checkpoint / f"{attempt.generation}.json"
    attempt.persist(binding, IdentityEvidence("account80", attempt.created_at + 1, "saved-proof"))
    registration = {"state": "registered", "instance": member["name"],
                    "endpoint": member["endpoint"], "lease_id": member["lease_id"],
                    "account_id": "wrong-account" if corrupt else "account80", "job_id": "saved-job",
                    "binding": str(binding), "web_port": 10080}
    path = root / "fleet-registration.json"
    path.write_text(json.dumps(registration))
    supervisor.enroll = lambda *_: pytest.fail("must not enroll an existing account")
    supervisor.start_instance = lambda *_: pytest.fail("must not restart the emulator")
    result = supervisor.start(member["name"])
    saved = json.loads(path.read_text())
    if corrupt:
        assert result["state"] == "failed"
        assert saved == registration
        assert spawned == []
    else:
        assert result["state"] == "running"
        assert saved == {**registration, "web_port": 9999}
        assert spawned[0][spawned[0].index("--web-port") + 1] == "9999"


def test_start_requires_released_endpoint_lock(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    with reserve_endpoint("127.0.0.1:5755"):
        status = make().start("Tiramisu64_20")
    assert status["state"] == "failed"
    assert status["error"] == "endpoint_lock_held"
    assert spawned == []


def test_process_identity_probe_has_explicit_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def timed_out(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired("ps", 2)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(TimeoutError, match="process_identity_timeout"):
        _process_identity(4000)
    assert seen["timeout"] == 2


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
    assert supervisor.pause_all()["Tiramisu64_20"]["state"] == "paused"
    assert killed == [4000]


def test_pause_waits_for_actual_exit_before_allowing_restart(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    supervisor = make()
    assert supervisor.start("Tiramisu64_20")["state"] == "running"
    supervisor.terminate = lambda pid: killed.append(pid)
    assert supervisor.pause("Tiramisu64_20")["state"] == "stopping"
    assert make().start("Tiramisu64_20")["state"] == "identity_changed"
    assert len(spawned) == 1
    del live[4000]
    assert make().reconcile()["Tiramisu64_20"]["state"] == "stopped"


def test_each_owned_process_restart_gets_a_fresh_input_generation(tmp_path: Path) -> None:
    from fleet.input_lease import InputLease, InputLeaseExpired

    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    name = "Tiramisu64_20"
    assert supervisor.start(name)["state"] == "running"
    lease = InputLease(tmp_path / "workers" / name / "input-lease.json")
    generations = [supervisor._read(name)["input_generation"]]
    for _ in range(2):
        assert supervisor.pause(name)["state"] == "paused"
        assert supervisor.start(name)["state"] == "running"
        generations.append(supervisor._read(name)["input_generation"])
    assert len(spawned) == 3 and len(set(generations)) == 3
    lease.assert_current(generations[-1])
    for generation in generations[:-1]:
        with pytest.raises(InputLeaseExpired):
            lease.assert_current(generation)


def test_stale_record_never_duplicates_or_kills_unproven_pid(tmp_path: Path) -> None:
    make, spawned, live, killed = _harness(tmp_path)
    make().start("Tiramisu64_20")
    live[4000] = ("reused-pid",)
    assert make().start("Tiramisu64_20")["state"] == "identity_changed"
    assert len(spawned) == 1
    assert make().pause("Tiramisu64_20")["state"] == "identity_changed"
    assert killed == []


def test_reused_pid_with_same_argv_but_new_birth_is_never_killed(tmp_path: Path) -> None:
    births = {4000: "first birth"}
    make, spawned, live, killed = _harness(tmp_path, births=births)
    supervisor = make()
    assert supervisor.start("Tiramisu64_20")["state"] == "running"
    births[4000] = "second birth"
    assert live[4000] == spawned[0]
    assert supervisor.pause("Tiramisu64_20")["state"] == "identity_changed"
    assert supervisor.kill("Tiramisu64_20")["state"] == "identity_changed"
    assert killed == []


def test_restarted_supervisor_adopts_a_surviving_worker_it_can_prove(tmp_path: Path) -> None:
    births = {4000: "first birth"}
    make, spawned, live, killed = _harness(tmp_path, births=births)
    assert make().start("Tiramisu64_20")["state"] == "running"
    restarted = make()
    assert restarted.reconcile()["Tiramisu64_20"]["state"] == "running"
    assert restarted.start("Tiramisu64_20")["state"] == "running"
    assert len(spawned) == 1
    assert restarted.pause("Tiramisu64_20")["state"] == "paused"
    assert killed == [4000] and 4000 not in live


def test_restarted_supervisor_never_adopts_a_reused_pid(tmp_path: Path) -> None:
    births = {4000: "first birth"}
    make, spawned, live, killed = _harness(tmp_path, births=births)
    assert make().start("Tiramisu64_20")["state"] == "running"
    restarted = make()
    assert restarted.reconcile()["Tiramisu64_20"]["state"] == "running"
    births[4000] = "second birth"
    assert restarted.reconcile()["Tiramisu64_20"]["state"] == "identity_changed"
    assert restarted.pause("Tiramisu64_20")["state"] == "identity_changed"
    assert restarted.kill("Tiramisu64_20")["state"] == "identity_changed"
    assert make().start("Tiramisu64_20")["state"] == "identity_changed"
    assert killed == [] and len(spawned) == 1


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
    make, spawned, live, _ = _harness(tmp_path)
    supervisor = make()
    supervisor.start("Tiramisu64_20")
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "tower_already_opened"
    live.clear()
    supervisor.enroll = lambda *_: (_ for _ in ()).throw(AssertionError("must not enroll twice"))
    assert supervisor.start(member["name"])["state"] == "running"
    assert spawned[-1][spawned[-1].index("--attempt-id") + 1] == spawned[0][
        spawned[0].index("--attempt-id") + 1]


def test_opened_tower_without_registration_asks_enroll_to_resume(tmp_path: Path) -> None:
    make, spawned, live, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "tower_already_opened"
    enrolled = []
    original = supervisor.enroll
    supervisor.enroll = lambda *args: enrolled.append(args[0]["name"]) or original(*args)
    assert supervisor.start(member["name"])["state"] == "running"
    assert enrolled == [member["name"]]

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
    runtime.checkpoint_root.mkdir(parents=True, exist_ok=True)
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


def _stubborn(root: Path):
    """A worker that ignores SIGTERM until it is force-killed."""
    members = [{"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "a", "state": "ready"}]
    live: dict[int, tuple] = {}
    forced: list[int] = []

    def enroll(member, runtime, attempt):
        return {"state": "registered", "instance": member["name"], "endpoint": member["endpoint"],
                "lease_id": member["lease_id"], "account_id": member["name"],
                "job_id": attempt.attempt_id, "binding": str(runtime.root / "binding.json"),
                "web_port": runtime.web_port}

    def spawn(args):
        live[5000] = tuple(args)
        return FakeChild(5000, live)

    def force_kill(pid):
        forced.append(pid)
        live.pop(pid, None)

    supervisor = RerollSupervisor(root, pool_snapshot=lambda: {"members": members}, enroll=enroll,
                                  spawn=spawn, process_identity=live.get, terminate=lambda pid: None,
                                  force_kill=force_kill, start_stagger_seconds=0)
    return supervisor, live, forced


def test_kill_force_stops_a_worker_that_ignored_sigterm(tmp_path: Path) -> None:
    supervisor, _, forced = _stubborn(tmp_path)
    supervisor.start("Tiramisu64_20")
    assert supervisor.pause("Tiramisu64_20")["state"] == "stopping"

    assert supervisor.kill("Tiramisu64_20")["state"] == "paused"
    assert forced == [5000]


def test_kill_refuses_a_pid_whose_identity_changed(tmp_path: Path) -> None:
    supervisor, live, forced = _stubborn(tmp_path)
    supervisor.start("Tiramisu64_20")
    live[5000] = ("someone", "else")

    assert supervisor.kill("Tiramisu64_20")["state"] == "identity_changed"
    assert forced == []


def test_kill_is_a_no_op_for_a_paused_worker(tmp_path: Path) -> None:
    supervisor, _, forced = _stubborn(tmp_path)
    assert supervisor.kill("Tiramisu64_20")["state"] == "paused"
    assert forced == []


def test_first_launches_of_different_members_overlap(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    inner = supervisor.enroll
    both_enrolling = Barrier(2, timeout=5)

    def enroll(member, runtime, attempt):
        both_enrolling.wait()  # Broken (and the start failed) if enrollment were serialized.
        return inner(member, runtime, attempt)

    supervisor.enroll = enroll
    status = supervisor.start_all()
    assert {row["state"] for row in status.values()} == {"running"}
    assert len(spawned) == 2


def test_boot_wait_runs_after_the_instance_start(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    supervisor = make()
    member = supervisor.pool_snapshot()["members"][0]
    member["state"] = "start_required"
    events = []
    def start_instance(selected):
        events.append("start")
    def wait_booted(selected):
        events.append("booted")
        member["state"] = "ready"
    supervisor.start_instance = start_instance
    supervisor.wait_booted = wait_booted
    assert supervisor.start(member["name"])["state"] == "running"
    assert events == ["start", "booted"]
