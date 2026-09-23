"""An emulator never leaves the pool while its worker may still play."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.reroll_journal import RerollJournal
from fleet.reroll_retirement import RetireError, remove_from_run, stop_bot

MEMBER = {"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "a"}


class FakeSupervisor:
    def __init__(self, states: list[str], *, after_kill: list[str] | None = None) -> None:
        self.states = list(states)          # returned by pause() then each reconcile()
        self.after_kill = after_kill
        self.calls: list[str] = []

    def _next(self) -> dict:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"name": MEMBER["name"], "state": state}

    def pause(self, name: str) -> dict:
        self.calls.append("pause")
        return self._next()

    def reconcile(self) -> dict:
        self.calls.append("reconcile")
        return {MEMBER["name"]: self._next()}

    def kill(self, name: str) -> dict:
        self.calls.append("kill")
        if self.after_kill is not None:
            self.states = list(self.after_kill)
        return self._next()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _stop(tmp_path: Path, supervisor: FakeSupervisor, *, host: str | None = "running"):
    clock = Clock()
    return stop_bot(MEMBER, supervisor=supervisor, instance_state=lambda name: host,
                    journal=RerollJournal(tmp_path), clock=clock, sleep=clock.sleep,
                    timeout=5, kill_grace=2, poll=1)


def _kinds(tmp_path: Path) -> list[str]:
    return [entry["kind"] for entry in RerollJournal(tmp_path).list_entries()]


def test_stop_bot_waits_for_exit_and_leaves_the_emulator_alone(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(["stopping", "stopping", "paused"])
    assert _stop(tmp_path, supervisor) == {"name": "Tiramisu64_20", "worker": "stopped"}
    assert supervisor.calls[:2] == ["pause", "reconcile"]
    assert _kinds(tmp_path) == ["worker_stopped"]


def test_sigterm_ignored_escalates_to_sigkill_then_gives_up(tmp_path: Path) -> None:
    killed = FakeSupervisor(["stopping"], after_kill=["paused"])
    assert _stop(tmp_path, killed)["worker"] == "killed" and "kill" in killed.calls

    survivor = FakeSupervisor(["stopping"], after_kill=["stopping"])
    with pytest.raises(RetireError, match="worker_did_not_exit"):
        _stop(tmp_path, survivor)
    assert survivor.calls.count("kill") == 1


def test_a_worker_that_failed_to_start_counts_as_stopped(tmp_path: Path) -> None:
    # Only the start path writes "failed", and only when no process was launched.
    assert _stop(tmp_path, FakeSupervisor(["failed"])) == {"name": "Tiramisu64_20",
                                                           "worker": "stopped"}


@pytest.mark.parametrize("state", ["identity_changed", "starting", "unverified"])
def test_unprovable_worker_state_raises_while_the_emulator_exists(tmp_path: Path, state: str) -> None:
    with pytest.raises(RetireError, match=state):
        _stop(tmp_path, FakeSupervisor([state]))


def test_a_deleted_emulator_is_dropped_even_when_its_worker_is_unprovable(tmp_path: Path) -> None:
    result = _stop(tmp_path, FakeSupervisor(["identity_changed"]), host=None)
    assert result == {"name": "Tiramisu64_20", "worker": "gone"}
    assert _kinds(tmp_path) == ["instance_missing"]


def _remove(tmp_path: Path, supervisor: FakeSupervisor, *, host: str | None = "running",
            stop_error: Exception | None = None):
    calls: list[str] = []
    clock = Clock()

    def stop_instance(name: str, endpoint: str, lease_id: str) -> None:
        calls.append(f"stop:{name}")
        if stop_error:
            raise stop_error

    result = remove_from_run(MEMBER, supervisor=supervisor, stop_instance=stop_instance,
                             instance_state=lambda name: host,
                             journal=RerollJournal(tmp_path), clock=clock, sleep=clock.sleep,
                             timeout=5, kill_grace=2, poll=1)
    return result, calls


def test_remove_stops_worker_and_host_but_leaves_pool_to_the_caller(tmp_path: Path) -> None:
    result, calls = _remove(tmp_path, FakeSupervisor(["stopping", "paused"]))
    assert result == {"name": "Tiramisu64_20", "worker": "stopped", "instance": "stopped"}
    assert calls == ["stop:Tiramisu64_20"]
    assert _kinds(tmp_path) == ["worker_removed", "instance_stopped"]


def test_remove_skips_an_already_stopped_host(tmp_path: Path) -> None:
    result, calls = _remove(tmp_path, FakeSupervisor(["paused"]), host="stopped")
    assert result["instance"] == "already_stopped"
    assert calls == []


def test_remove_fails_when_the_host_will_not_stop(tmp_path: Path) -> None:
    # A running emulator with Tower opened can't be re-added, so the removal must not complete.
    with pytest.raises(RetireError, match="instance_stop_failed"):
        _remove(tmp_path, FakeSupervisor(["paused"]), stop_error=RuntimeError("window stuck"))
    assert _kinds(tmp_path) == ["worker_removed", "instance_stop_failed"]


def test_remove_refuses_an_unprovable_worker_exit(tmp_path: Path) -> None:
    with pytest.raises(RetireError, match="identity_changed"):
        _remove(tmp_path, FakeSupervisor(["identity_changed"]))


def test_remove_drops_a_deleted_emulator_without_stopping_it(tmp_path: Path) -> None:
    result, calls = _remove(tmp_path, FakeSupervisor(["paused"]), host=None)
    assert result == {"name": "Tiramisu64_20", "worker": "stopped", "instance": "missing"}
    assert calls == []

    result, calls = _remove(tmp_path, FakeSupervisor(["identity_changed"]), host=None)
    assert result == {"name": "Tiramisu64_20", "worker": "gone", "instance": "missing"}
    assert calls == []
