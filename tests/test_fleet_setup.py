"""UI setup persists only explicit, host-bound Fleet choices."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fleet.setup import FleetSetupStore, FleetSetupError, FleetSetupService, wait_for_android_boot
from fleet.dashboard import FleetRequestError
from web.app import create_app
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState


def qualification(root: Path, *, source: str = "Tiramisu64_6") -> None:
    directory = root / "m05-air6"
    directory.mkdir()
    (directory / "m05.json").write_text(json.dumps({
        "schema": 2, "state": "passed", "live": True, "source_instance": source,
        "source_evidence": {"tower_unopened": True, "lease_id": "source-lease"},
        "scope": {"host_id": "local-air", "bluestacks_version": "Air",
                  "source_lineage": "source", "source_version": "image",
                  "game_version": "29.0.3", "instance_config": {
                      "source_instance": source, "source_endpoint": "127.0.0.1:5615",
                      "source_lease": "source-lease"}},
    }))


def test_setup_discovers_existing_proof_and_persists_host_policy(tmp_path: Path) -> None:
    qualification(tmp_path)
    store = FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path)

    assert store.snapshot()["configured"] is False
    assert store.snapshot()["qualifications"][0]["id"] == "m05-air6"
    saved = store.configure(capacity=7, name_prefix="Tiramisu64_",
                            qualification_id="m05-air6", installed_prefix="Tiramisu64_",
                            host_count=5)

    assert saved["configured"] is True
    assert saved["settings"] == {"capacity": 7, "name_prefix": "Tiramisu64_",
                                  "qualification_id": "m05-air6"}
    assert FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path).settings() \
        == saved["settings"]
    assert (tmp_path / "fleet" / "settings.json").stat().st_mode & 0o777 == 0o600


def test_setup_rejects_arbitrary_source_and_prefix(tmp_path: Path) -> None:
    qualification(tmp_path)
    store = FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path)
    for values in (
        {"capacity": 5, "name_prefix": "Tiramisu64_", "qualification_id": "m05-air6"},
        {"capacity": 7, "name_prefix": "other_", "qualification_id": "m05-air6"},
        {"capacity": 7, "name_prefix": "Tiramisu64_", "qualification_id": "../other"},
    ):
        with pytest.raises(FleetSetupError):
            store.configure(**values, installed_prefix="Tiramisu64_", host_count=5)
    assert store.settings() is None


def test_start_instance_uses_exact_installed_row_and_never_opens_tower(tmp_path: Path) -> None:
    service = FleetSetupService(tmp_path / "fleet", qualification_root=tmp_path)
    started: list[str] = []
    service.controller = SimpleNamespace(adapter=SimpleNamespace(
        driver=SimpleNamespace(start=lambda name: started.append(name))))
    service.instances_snapshot = lambda: {"instances": [
        {"name": "Tiramisu64_6", "endpoint": "127.0.0.1:5615", "state": "running", "template": True},
        {"name": "Tiramisu64_18", "endpoint": "127.0.0.1:5735", "state": "stopped", "template": False},
    ], "can_start": True}

    service.start_instance("Tiramisu64_18")
    assert started == ["Tiramisu64_18"]
    with pytest.raises(FleetRequestError, match="instance_not_installed"):
        service.start_instance("Tiramisu64_99")
    assert started == ["Tiramisu64_18"]


def test_pause_dispatches_while_start_is_enrolling(tmp_path: Path) -> None:
    service = FleetSetupService(tmp_path / "fleet", qualification_root=tmp_path)
    entered = Event()
    release = Event()
    paused = Event()

    def start_all() -> dict:
        entered.set()
        assert release.wait(2)
        return {"Tiramisu64_20": {"state": "running"}}

    def pause_all() -> dict:
        paused.set()
        return {"Tiramisu64_21": {"state": "paused"}}

    service._manual_supervisor = lambda: SimpleNamespace(start_all=start_all, pause_all=pause_all)
    service.reroll_snapshot = lambda: {"members": []}
    try:
        service.reroll_start()
        assert entered.wait(2)
        service.reroll_pause()
        assert paused.wait(2)
    finally:
        release.set()


def test_setup_api_routes_save_start_and_resume_before_generic_action() -> None:
    class Setup:
        def __init__(self) -> None:
            self.actions: list[str] = []

        def setup_snapshot(self) -> dict:
            return {"configured": False, "settings": None, "qualifications": [], "host": {}}

        def configure(self, *, capacity: int, name_prefix: str, qualification_id: str) -> dict:
            self.actions.append(f"save:{capacity}:{name_prefix}:{qualification_id}")
            return self.setup_snapshot()

        def start_source(self) -> dict:
            self.actions.append("start")
            return {"sources": [], "jobs": []}

        def resume_unopened_clone(self, job_id: str, index: int) -> dict:
            self.actions.append(f"resume:{job_id}:{index}")
            return {}

    setup = Setup()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=setup))
    assert client.get("/api/fleet/setup").status_code == 200
    assert client.post("/api/fleet/setup", json={"capacity": 7, "name_prefix": "Tiramisu64_",
                                                  "qualification_id": "m05-air6"}).status_code == 200
    assert client.post("/api/fleet/setup/start-source").status_code == 200
    assert client.post("/api/fleet/requests/job/targets/0/resume-first-launch").status_code == 200
    assert setup.actions == ["save:7:Tiramisu64_:m05-air6", "start", "resume:job:0"]


def test_manual_reroll_pool_api_routes_are_reachable() -> None:
    class Pool:
        def reroll_snapshot(self) -> dict:
            return {"candidates": [], "members": []}

        def reroll_add(self, names: list[str]) -> dict:
            return {"candidates": [], "members": [{"name": name} for name in names]}

        def reroll_remove(self, name: str) -> dict:
            return {"candidates": [{"name": name}], "members": []}

    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=Pool()))
    assert client.get("/api/fleet/reroll").json() == {"candidates": [], "members": []}
    assert client.post("/api/fleet/reroll/members", json={"names": ["Tiramisu64_20"]}).json()[
        "members"] == [{"name": "Tiramisu64_20"}]
    assert client.delete("/api/fleet/reroll/members/Tiramisu64_20").json()[
        "candidates"] == [{"name": "Tiramisu64_20"}]


def test_reroll_hide_routes_hide_and_restore_and_map_refusals_to_409() -> None:
    class Pool:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def reroll_hide(self, names: list[str], hidden: bool) -> dict:
            if names == ["bad"]:
                raise ValueError("instance_not_in_active_run")
            self.calls.append((names, hidden))
            return {"candidates": [], "members": []}

    pool = Pool()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=pool))
    assert client.post("/api/fleet/reroll/hidden", json={"names": ["A", "B"]}).status_code == 200
    assert client.request("DELETE", "/api/fleet/reroll/hidden", json={"names": ["A"]}).status_code == 200
    assert pool.calls == [(["A", "B"], True), (["A"], False)]
    refused = client.post("/api/fleet/reroll/hidden", json={"names": ["bad"]})
    assert refused.status_code == 409 and refused.json()["detail"] == "instance_not_in_active_run"


def test_manual_reroll_actions_and_journal_routes() -> None:
    class Pool:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reroll_start(self, name: str | None = None) -> dict:
            self.calls.append(f"start:{name}")
            return {"members": []}

        def reroll_pause(self, name: str | None = None) -> dict:
            self.calls.append(f"pause:{name}")
            return {"members": []}

        def reroll_set_concurrency(self, limit: int) -> dict:
            self.calls.append(f"limit:{limit}")
            return {"concurrency_limit": limit}

        def reroll_journal(self, *, cursor: int | None = None) -> dict:
            return {"entries": [], "next_cursor": cursor or 0}

    pool = Pool()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=pool))
    assert client.post("/api/fleet/reroll/start").status_code == 200
    assert client.post("/api/fleet/reroll/members/Tiramisu64_20/start").status_code == 200
    assert client.post("/api/fleet/reroll/pause").status_code == 200
    assert client.post("/api/fleet/reroll/members/Tiramisu64_20/pause").status_code == 200
    assert client.patch("/api/fleet/reroll/concurrency", json={"limit": 4}).json() == {
        "concurrency_limit": 4}
    assert client.get("/api/fleet/reroll/journal?cursor=5").json() == {
        "entries": [], "next_cursor": 5}
    assert pool.calls == ["start:None", "start:Tiramisu64_20", "pause:None",
                          "pause:Tiramisu64_20", "limit:4"]


class _Runs:
    """RerollRuns stand-in: records calls, lets a test hold start_new open."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.release = Event()
        self.release.set()
        self.busy = False
        self.hidden: set[str] = set()
        self.run = {"number": 2, "name": "Reroll #2", "started_at": "2026-09-23T09:00:00Z",
                    "status": "active", "members": ["Tiramisu64_20"]}

    def active(self):
        return self.run

    def leave_failures(self):
        return {"Tiramisu64_20": "identity_changed"}

    def hidden_names(self):
        return set(self.hidden)

    def set_hidden(self, names, hidden):
        self.calls.append(("set_hidden", names, hidden))
        self.hidden = self.hidden | set(names) if hidden else self.hidden - set(names)

    def summaries(self):
        return [{"number": 2}, {"number": 1}]

    def validate_new(self, keep, add):
        self.calls.append(("validate_new", keep, add))
        if keep == ["bad"]:
            raise ValueError("keep_not_in_active_run")

    def start_new(self, keep, add, name=None):
        self.release.wait(5)
        self.calls.append(("start_new", keep, add, name))
        return {"number": 3, "results": [{"name": "Tiramisu64_19", "worker": "stopped"}]}

    def add_members(self, names):
        self.calls.append(("add_members", names))

    def validate_remove(self, name):
        self.calls.append(("validate_remove", name))

    def remove_member(self, name):
        self.calls.append(("remove_member", name))
        return {"name": name, "worker": "stopped", "instance": "stopped"}


def _service_with_runs(tmp_path: Path, runs: _Runs) -> FleetSetupService:
    service = FleetSetupService(tmp_path / "fleet", qualification_root=tmp_path)
    service._reroll_runs = runs
    service._reroll_pool = SimpleNamespace(validate_add=lambda names: None, snapshot=lambda: {
        "candidates": [{"name": "Tiramisu64_18", "endpoint": "e", "state": "start_required"},
                       {"name": "Tiramisu64_22", "endpoint": "f", "state": "ready"}],
        "members": [{"name": "Tiramisu64_20", "endpoint": "g", "lease_id": "a", "state": "ready"}]})
    service._reroll_supervisor = SimpleNamespace(
        reconcile=lambda snapshot=None: {"Tiramisu64_20": {"name": "Tiramisu64_20", "state": "paused"}},
        pressure=lambda statuses=None: {"running": 0, "starting": 0, "limit": 2, "available": 2},
        max_concurrent_workers=2)
    return service


def _wait_for(predicate, timeout: float = 5.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


def test_snapshot_flags_bots_that_would_not_stop_and_blocks_no_candidate(tmp_path: Path) -> None:
    snapshot = _service_with_runs(tmp_path, _Runs()).reroll_snapshot()
    assert {row["name"]: row["state"] for row in snapshot["candidates"]} == {
        "Tiramisu64_18": "start_required", "Tiramisu64_22": "ready"}
    assert snapshot["run"] == {"number": 2, "name": "Reroll #2",
                               "started_at": "2026-09-23T09:00:00Z", "status": "active"}
    assert snapshot["members"][0]["leave_error"] == "identity_changed"
    assert "stop_failures" not in snapshot and "retire_state" not in snapshot["members"][0]
    assert snapshot["operation"] is None


def test_snapshot_marks_members_hidden_from_the_dashboard(tmp_path: Path) -> None:
    runs = _Runs()
    service = _service_with_runs(tmp_path, runs)
    assert service.reroll_snapshot()["members"][0]["hidden"] is False
    snapshot = service.reroll_hide(["Tiramisu64_20"], True)
    assert runs.calls == [("set_hidden", ["Tiramisu64_20"], True)]
    assert snapshot["members"][0]["hidden"] is True
    assert service.reroll_hide(["Tiramisu64_20"], False)["members"][0]["hidden"] is False


def test_new_run_validates_synchronously_then_runs_in_background(tmp_path: Path) -> None:
    runs = _Runs()
    service = _service_with_runs(tmp_path, runs)
    with pytest.raises(ValueError, match="keep_not_in_active_run"):
        service.reroll_new_run(["bad"], [], None)

    runs.release.clear()
    snapshot = service.reroll_new_run(["Tiramisu64_20"], ["Tiramisu64_22"], None)
    assert snapshot["operation"]["kind"] == "new_run"
    assert snapshot["operation"]["state"] == "running"
    runs.release.set()
    _wait_for(lambda: service.reroll_snapshot()["operation"]["state"] == "done")
    assert service.reroll_snapshot()["operation"]["results"][0]["name"] == "Tiramisu64_19"
    assert ("start_new", ["Tiramisu64_20"], ["Tiramisu64_22"], None) in runs.calls
    from fleet.reroll_journal import RerollJournal
    entries = RerollJournal(service.root).list_entries()
    assert any(entry["kind"] == "run_started"
               and entry["message"] == "Reroll #3 started: kept 1, added 1, stopped 1"
               for entry in entries)


def test_second_new_run_while_one_is_running_is_rejected(tmp_path: Path) -> None:
    runs = _Runs()
    service = _service_with_runs(tmp_path, runs)
    runs.release.clear()
    service.reroll_new_run([], ["Tiramisu64_22"], None)
    with pytest.raises(ValueError, match="reroll_start_in_progress"):
        service.reroll_new_run([], ["Tiramisu64_22"], None)
    with pytest.raises(ValueError, match="reroll_start_in_progress"):
        service.reroll_remove("Tiramisu64_20")
    runs.release.set()
    _wait_for(lambda: service.reroll_snapshot()["operation"]["state"] == "done")
    assert sum(call[0] == "start_new" for call in runs.calls) == 1


def test_remove_runs_in_the_background(tmp_path: Path) -> None:
    runs = _Runs()
    service = _service_with_runs(tmp_path, runs)
    assert service.reroll_remove("Tiramisu64_20")["operation"]["kind"] == "remove"
    _wait_for(lambda: ("remove_member", "Tiramisu64_20") in runs.calls)
    assert service.reroll_runs() == {"runs": [{"number": 2}, {"number": 1}]}


def test_reroll_run_routes_are_reachable_and_map_errors() -> None:
    class Pool:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def reroll_new_run(self, keep, add, name=None):
            self.calls.append(("new", keep, add, name))
            if keep == ["bad"]:
                raise ValueError("keep_not_in_active_run")
            return {"members": [], "run": {"number": 3}}

        def reroll_runs(self):
            return {"runs": [{"number": 1}]}

    pool = Pool()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=pool))
    assert client.post("/api/fleet/reroll/runs", json={"keep": ["A_1"], "add": ["A_2"]}).json()[
        "run"] == {"number": 3}
    denied = client.post("/api/fleet/reroll/runs", json={"keep": ["bad"], "add": []})
    assert (denied.status_code, denied.json()["detail"]) == (409, "keep_not_in_active_run")
    assert client.get("/api/fleet/reroll/runs").json() == {"runs": [{"number": 1}]}
    assert client.post("/api/fleet/reroll/members/A_1/retire").status_code in {404, 405}
    assert client.post("/api/fleet/reroll/members/A_1/stop-instance").status_code in {404, 405}
    assert pool.calls == [("new", ["A_1"], ["A_2"], None), ("new", ["bad"], [], None)]


def test_reroll_run_routes_are_503_without_the_capability() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=SimpleNamespace()))
    assert client.post("/api/fleet/reroll/runs", json={"keep": [], "add": []}).status_code == 503
    assert client.get("/api/fleet/reroll/runs").status_code == 503


def test_wait_for_android_boot_polls_until_boot_completes() -> None:
    answers = ["", "0", "1"]
    now = {"t": 0.0}

    class Device:
        def shell(self, command: str) -> str:
            assert command == "getprop sys.boot_completed"
            answer = answers.pop(0)
            if answer == "":
                raise ConnectionError("adb not ready")
            return answer + "\n"

    wait_for_android_boot(Device, timeout=10, poll=2, clock=lambda: now["t"],
                          sleep=lambda seconds: now.__setitem__("t", now["t"] + seconds))
    assert answers == [] and now["t"] == 4


def test_wait_for_android_boot_gives_up_at_the_deadline() -> None:
    now = {"t": 0.0}
    device = SimpleNamespace(shell=lambda command: "0")
    with pytest.raises(TimeoutError, match="android_boot_not_completed"):
        wait_for_android_boot(lambda: device, timeout=5, poll=2, clock=lambda: now["t"],
                              sleep=lambda seconds: now.__setitem__("t", now["t"] + seconds))


def test_add_runs_in_the_background_because_it_may_boot_emulators(tmp_path: Path) -> None:
    runs = _Runs()
    service = _service_with_runs(tmp_path, runs)
    snapshot = service.reroll_add(["Tiramisu64_22"])
    assert snapshot["operation"]["kind"] == "add"
    assert snapshot["operation"]["target"] == "Tiramisu64_22"
    _wait_for(lambda: service.reroll_snapshot()["operation"]["state"] == "done")
    assert ("add_members", ["Tiramisu64_22"]) in runs.calls


class _RunsWithVariantFailure(_Runs):
    def start_new(self, keep, add, name=None):
        outcome = super().start_new(keep, add, name)
        return {**outcome, "variants": {}, "variant_errors": {"Tiramisu64_22": "disk full"}}


def test_a_failed_variant_assignment_is_journaled(tmp_path: Path) -> None:
    runs = _RunsWithVariantFailure()
    service = _service_with_runs(tmp_path, runs)
    service.reroll_new_run([], ["Tiramisu64_22"], None)
    _wait_for(lambda: service.reroll_snapshot()["operation"]["state"] == "done")
    from fleet.reroll_journal import RerollJournal
    assert any(entry["kind"] == "variant_assign_failed" and entry["level"] == "warn"
               and entry["instance"] == "Tiramisu64_22" and "disk full" in entry["message"]
               for entry in RerollJournal(service.root).list_entries())


def test_the_snapshot_carries_the_variant_comparison(tmp_path: Path) -> None:
    service = _service_with_runs(tmp_path, _Runs())
    rows = service.reroll_snapshot()["variant_comparison"]
    assert [row["id"] for row in rows] == ["baseline", "income_first", "attack_heavy"]


def test_a_variant_file_alone_does_not_register_a_worker(tmp_path: Path) -> None:
    from web.account_catalog import registered_worker
    folder = tmp_path / "workers" / "Tiramisu64_40"
    folder.mkdir(parents=True)
    (folder / "reroll-variant.json").write_text('{"variant": "baseline"}')
    assert registered_worker(folder) is None
