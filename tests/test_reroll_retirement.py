"""Retiring an emulator never leaves the pool while its worker may still play."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.reroll_journal import RerollJournal
from fleet.reroll_retirement import RetireError, retire

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


def _run(tmp_path: Path, supervisor: FakeSupervisor, *, host: str | None = "running",
         stop_error: Exception | None = None):
    calls: list[str] = []
    clock = Clock()

    def stop_instance(name: str, endpoint: str, lease_id: str) -> None:
        calls.append(f"stop:{name}:{endpoint}:{lease_id}")
        if stop_error:
            raise stop_error

    result = retire(MEMBER, supervisor=supervisor,
                    remove_from_pool=lambda name: calls.append(f"remove:{name}"),
                    stop_instance=stop_instance, instance_state=lambda name: host,
                    journal=RerollJournal(tmp_path), clock=clock, sleep=clock.sleep,
                    timeout=5, kill_grace=2, poll=1)
    return result, calls


def _kinds(tmp_path: Path) -> list[str]:
    return [entry["kind"] for entry in RerollJournal(tmp_path).list_entries()]


def test_happy_path_waits_for_exit_then_stops_host_then_leaves_pool(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(["stopping", "stopping", "paused"])
    result, calls = _run(tmp_path, supervisor)
    assert result == {"name": "Tiramisu64_20", "worker": "stopped", "instance": "stopped"}
    assert calls == ["stop:Tiramisu64_20:127.0.0.1:5755:a", "remove:Tiramisu64_20"]
    assert supervisor.calls[:2] == ["pause", "reconcile"]
    assert _kinds(tmp_path) == ["worker_retired", "instance_stopped"]


def test_sigterm_ignored_escalates_to_sigkill_then_gives_up(tmp_path: Path) -> None:
    killed = FakeSupervisor(["stopping"], after_kill=["paused"])
    result, _ = _run(tmp_path, killed)
    assert result["worker"] == "killed" and "kill" in killed.calls

    survivor = FakeSupervisor(["stopping"], after_kill=["stopping"])
    with pytest.raises(RetireError, match="worker_did_not_exit"):
        _run(tmp_path, survivor)
    assert survivor.calls.count("kill") == 1


@pytest.mark.parametrize("state", ["identity_changed", "failed", "starting", "unverified"])
def test_unprovable_worker_state_never_stops_host_or_leaves_pool(tmp_path: Path, state: str) -> None:
    calls: list[str] = []
    with pytest.raises(RetireError, match=state):
        retire(MEMBER, supervisor=FakeSupervisor([state]),
               remove_from_pool=lambda name: calls.append("remove"),
               stop_instance=lambda *args: calls.append("stop"),
               instance_state=lambda name: "running", journal=RerollJournal(tmp_path),
               clock=Clock(), sleep=lambda s: None)
    assert calls == []


def test_host_stop_failure_still_leaves_pool_and_is_reported(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, FakeSupervisor(["paused"]), stop_error=RuntimeError("window stuck"))
    assert result["instance"] == "stop_failed" and result["error"] == "window stuck"
    assert calls[-1] == "remove:Tiramisu64_20"
    assert _kinds(tmp_path) == ["worker_retired", "instance_stop_failed"]


def test_already_stopped_host_is_not_stopped_again(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, FakeSupervisor(["stopped"]), host="stopped")
    assert result["instance"] == "already_stopped"
    assert calls == ["remove:Tiramisu64_20"]
