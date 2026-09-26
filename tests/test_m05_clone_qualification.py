"""M05 simulations. A passing simulation is never live host qualification."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bluestacks import BlueStacksAdapter, HostInstance, ManualPool
import fleet.clone_qualification as clone_qualification_module
from fleet.account_creation import AccountFrame, StagingClone
from fleet.account_observer import StagingAccountObserver
from fleet.clone_qualification import (CloneCandidate, QualificationScope,
                                       probe_clone_worker, qualify_clone_source,
                                       qualification_is_current)
from fleet.identity import Attempt
from fleet.runtime import WorkerRuntime


class Host:
    supports_lifecycle = True
    supports_clone_staging = True
    supports_m05_live_qualification = False

    def __init__(self) -> None:
        self.instances = [HostInstance("seed", "127.0.0.1:5555", "source-lease", "running", "seed-v1")]
        self.calls: list[tuple[str, str]] = []

    def inventory(self) -> list[HostInstance]:
        return list(self.instances)

    def stage_clone(self, name: str, source: str) -> HostInstance:
        self.calls.append(("clone", name))
        item = HostInstance(name, f"127.0.0.1:{5557 if name == 'clone-a' else 5559}",
                            f"lease-{name}", "running", "seed-v1")
        self.instances.append(item)
        return item

    def start(self, name: str) -> None:
        self.calls.append(("start", name))
        self.instances = [replace(x, state="running") if x.name == name else x for x in self.instances]

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        self.instances = [replace(x, state="stopped") if x.name == name else x for x in self.instances]


class AttestedDriver(Host):
    def __init__(self, scope: QualificationScope) -> None:
        super().__init__()
        self.scope = scope
        self.drifted = False
        self.live_attested = True

    def qualification_scope(self) -> QualificationScope | None:
        return None if self.drifted else self.scope

    @property
    def supports_lifecycle(self) -> bool:
        return self.live_attested and self.qualification_scope() == self.scope

    @property
    def supports_clone_staging(self) -> bool:
        return self.live_attested and self.qualification_scope() == self.scope

    @property
    def supports_m05_live_qualification(self) -> bool:
        return self.live_attested and self.qualification_scope() == self.scope


class FakeLiveObserver(StagingAccountObserver):
    def __init__(self, endpoint: str, account_id: str) -> None:
        self.endpoint = endpoint
        self.account_id = account_id
        self.allowed_versions = frozenset({"29.0.2"})

    def __call__(self, _: object) -> AccountFrame:
        return AccountFrame("account", self.account_id, "29.0.2", "source-digest", 101.,
                            "capture://source", {}, popup_title="ACCOUNT", id_label="ID:")


def candidate(tmp_path: Path, name: str, worker: str, endpoint: str, lease: str) -> CloneCandidate:
    attempt = replace(Attempt.new(worker, endpoint, lease, "m05"), created_at=100.)
    runtime = WorkerRuntime.for_worker(tmp_path / "fleet", worker,
                                       {"seed": 8765, "clone-a": 8766, "clone-b": 8767}[name])
    device = SimpleNamespace(serial=endpoint, app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"))
    def observe(_: object) -> AccountFrame:
        return AccountFrame("account", "SOURCE", "29.0.2", f"digest-{name}", 101.,
                            f"capture://{name}", {}, popup_title="ACCOUNT", id_label="ID:")
    return CloneCandidate(name, runtime, attempt, lambda: device, observe)


def setup(tmp_path: Path):
    host = Host()
    adapter = BlueStacksAdapter(host, staging_root=tmp_path / "staging")
    source = candidate(tmp_path, "seed", "source-worker", "127.0.0.1:5555", "source-lease")
    clones = (candidate(tmp_path, "clone-a", "worker-a", "127.0.0.1:5557", "lease-clone-a"),
              candidate(tmp_path, "clone-b", "worker-b", "127.0.0.1:5559", "lease-clone-b"))
    scope = QualificationScope("mac-a", "BlueStacks-Air-2", "seed-v1", "image-1", "29.0.2",
                               {"abi": "arm64", "resolution": "1080x2400"})
    return host, adapter, source, clones, scope


def account_creator(*, attempt: Attempt, **_: object) -> dict:
    return {"state": "verified", "worker_id": attempt.worker_id,
            "endpoint": attempt.endpoint, "lease_id": attempt.lease_id,
            "generation": attempt.generation, "source_account_id": "SOURCE",
            "account_id": "ACCOUNT-A" if attempt.worker_id == "worker-a" else "ACCOUNT-B",
            "app_version": "29.0.2", "completed_at": 102.,
            "evidence": [{"observed_at": 101.5, "evidence_ref": f"capture://{attempt.worker_id}"}]}


def test_simulated_end_to_end_success_stages_twice_and_probes_concurrently(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    barrier = threading.Barrier(2)
    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        barrier.wait(timeout=2)
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}
    path = tmp_path / "qualification.json"
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  account_creator=account_creator, worker_probe=probe,
                                  clock=lambda: 105.)
    assert record["state"] == "simulated_pass"
    assert record["account_ids"] == ["SOURCE", "ACCOUNT-A", "ACCOUNT-B"]
    assert host.calls[:2] == [("clone", "clone-a"), ("clone", "clone-b")]
    assert not qualification_is_current(path, scope, now=105.)


def test_stopped_clones_are_started_before_account_creation(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    original_stage = host.stage_clone

    def stage_stopped(name: str, source_name: str) -> HostInstance:
        created = original_stage(name, source_name)
        host.instances[-1] = replace(created, state="stopped")
        return host.instances[-1]

    host.stage_clone = stage_stopped
    def create(*, attempt: Attempt, **kwargs: object) -> dict:
        assert {row.name: row.state for row in host.inventory()}[
            "clone-a" if attempt.worker_id == "worker-a" else "clone-b"] == "running"
        return account_creator(attempt=attempt, **kwargs)
    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=tmp_path / "qualification.json",
                                  registry=tmp_path / "registry.json", account_creator=create,
                                  worker_probe=probe, clock=lambda: 105.)
    assert record["state"] == "simulated_pass"
    assert ("start", "clone-a") in host.calls
    assert ("start", "clone-b") in host.calls


def test_attested_existing_clones_skip_new_staging(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    host.stage_clone("clone-a", "seed")
    host.stage_clone("clone-b", "seed")
    host.calls.clear()

    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    record = qualify_clone_source(
        adapter=adapter, scope=scope, source=source, clones=clones,
        record_path=tmp_path / "qualification.json", registry=tmp_path / "registry.json",
        account_creator=account_creator, worker_probe=probe,
        reuse_attested_clones=True, clock=lambda: 105.,
    )
    assert record["state"] == "simulated_pass"
    assert not any(action == "clone" for action, _ in host.calls)


def test_existing_clone_with_prior_r00_attempt_is_rejected(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    host.stage_clone("clone-a", "seed")
    host.stage_clone("clone-b", "seed")
    prior = clones[0].runtime.checkpoint_root / ".r00-account-creation.json"
    prior.parent.mkdir(parents=True)
    prior.write_text(json.dumps({"instance": "clone-a", "evidence": []}))

    record = qualify_clone_source(
        adapter=adapter, scope=scope, source=source, clones=clones,
        record_path=tmp_path / "qualification.json", registry=tmp_path / "registry.json",
        account_creator=account_creator, reuse_attested_clones=True, clock=lambda: 105.,
    )
    assert record["state"] == "quarantined"
    assert record["reason"] == "existing clone has an R00 attempt"
    assert record["account_audits"] == []


@pytest.mark.parametrize("reason", ["unexpected game package",
                                    "unexpected screen during account creation"])
def test_existing_clone_allows_exact_pre_action_quarantine_in_old_runtime(
        tmp_path: Path, reason: str) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    host.stage_clone("clone-a", "seed")
    host.stage_clone("clone-b", "seed")
    prior = WorkerRuntime.for_worker(tmp_path / "fleet", "old-worker", 8799)
    prior.checkpoint_root.mkdir(parents=True)
    old_attempt = Attempt.new("old-worker", clones[0].attempt.endpoint,
                              clones[0].attempt.lease_id, "previous")
    (prior.checkpoint_root / ".r00-account-creation.json").write_text(json.dumps({
        **asdict(old_attempt),
        "instance": "clone-a", "endpoint": clones[0].attempt.endpoint,
        "lease_id": clones[0].attempt.lease_id, "source_lineage": scope.source_lineage,
        "allowed_source_id": "SOURCE", "state": "quarantined",
        "reason": reason, "evidence": [],
        "started_at": 101., "started_from_account": False,
    }))

    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    record = qualify_clone_source(
        adapter=adapter, scope=scope, source=source, clones=clones,
        record_path=tmp_path / "qualification.json", registry=tmp_path / "registry.json",
        account_creator=account_creator, worker_probe=probe,
        reuse_attested_clones=True, clock=lambda: 105.,
    )
    assert record["state"] == "simulated_pass"


def test_live_clones_open_tower_before_r00(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, source, clones, scope = setup(tmp_path)
    driver = AttestedDriver(scope)
    adapter = BlueStacksAdapter(driver, staging_root=tmp_path / "staging")
    source = replace(source, observe=FakeLiveObserver(source.attempt.endpoint, "SOURCE"))
    clones = tuple(replace(item, observe=FakeLiveObserver(item.attempt.endpoint, "SOURCE"))
                   for item in clones)
    opened: list[str] = []

    def launch(device: object) -> None:
        opened.append(device.serial)

    def create(*, attempt: Attempt, **kwargs: object) -> dict:
        assert opened[-1] == attempt.endpoint
        return account_creator(attempt=attempt, **kwargs)

    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}",
                "instance": candidate.instance, "endpoint": candidate.attempt.endpoint,
                "lease_id": candidate.attempt.lease_id,
                "worker_id": candidate.attempt.worker_id}

    monkeypatch.setattr(clone_qualification_module, "launch_tower_from_game_center", launch)
    monkeypatch.setattr(clone_qualification_module, "wait_for_tower_ready",
                        lambda device, observer: None)
    monkeypatch.setattr(clone_qualification_module, "create_staging_account", create)
    monkeypatch.setattr(clone_qualification_module, "probe_clone_worker", probe)
    record = qualify_clone_source(
        adapter=adapter, scope=scope, source=source, clones=clones,
        record_path=tmp_path / "qualification.json", registry=tmp_path / "registry.json",
        account_creator=create, worker_probe=probe, live=True, r00_enabled=True,
        clock=lambda: 105.,
    )
    assert record["state"] == "passed"
    assert opened == [clones[0].attempt.endpoint, clones[1].attempt.endpoint]


def test_clone_attempts_can_bind_after_manager_creation(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    plans = tuple(replace(item, attempt=replace(item.attempt, endpoint="pending",
                                                 lease_id="pending")) for item in clones)
    def bind(item: CloneCandidate, created: HostInstance) -> CloneCandidate:
        return replace(item, attempt=replace(item.attempt, endpoint=created.endpoint,
                                                  lease_id=created.lease_id))
    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        assert candidate.attempt.endpoint != "pending"
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=plans,
                                  record_path=tmp_path / "qualification.json",
                                  registry=tmp_path / "registry.json",
                                  bind_created=bind, account_creator=account_creator,
                                  worker_probe=probe, clock=lambda: 105.)
    assert record["state"] == "simulated_pass"
    assert [audit["endpoint"] for audit in record["account_audits"]] == [
        "127.0.0.1:5557", "127.0.0.1:5559"]


def test_unopened_source_gate_requires_both_consent_audits(tmp_path: Path) -> None:
    _, _, _, _, scope = setup(tmp_path)
    path = tmp_path / "first-launch.json"
    audits = []
    proofs = []
    for index, account_id in enumerate(("A", "B")):
        endpoint = f"127.0.0.1:{5557 + index * 2}"
        audits.append({"state": "verified", "account_id": account_id,
                       "app_version": scope.game_version,
                       "evidence": [{"action": "i_agree"}],
                       "endpoint": endpoint, "lease_id": f"lease-{index}",
                       "worker_id": f"worker-{index}", "completed_at": 102.})
        proofs.append({"account_id": account_id, "endpoint": endpoint,
                       "lease_id": f"lease-{index}", "worker_id": f"worker-{index}",
                       "instance": f"clone-{index}", "started_at": 103.,
                       "recovered_at": 104.})
    record = {"schema": 2, "state": "passed", "live": True,
              "scope": asdict(scope), "account_ids": ["A", "B"],
              "source_evidence": {"tower_unopened": True, "endpoint": "127.0.0.1:5555",
                                  "lease_id": "source-lease", "observed_at": 101.},
              "clone_instances": ["clone-0", "clone-1"],
              "account_audits": audits, "worker_proofs": proofs, "evaluated_at": 105.}
    path.write_text(json.dumps(record))
    assert qualification_is_current(path, scope, now=105.)
    record["account_audits"][0]["completed_at"] = 1.
    path.write_text(json.dumps(record))
    assert qualification_is_current(path, scope, now=105.)
    record["account_audits"][0]["completed_at"] = 104.
    path.write_text(json.dumps(record))
    assert not qualification_is_current(path, scope, now=105.)
    record["account_audits"][0]["completed_at"] = 102.
    record["account_audits"][1]["evidence"] = []
    path.write_text(json.dumps(record))
    assert not qualification_is_current(path, scope, now=105.)


def test_external_protected_account_is_passed_to_each_r00_policy(tmp_path: Path) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    protected_seen: list[frozenset[str]] = []
    def create(*, attempt: Attempt, policy: StagingClone, **kwargs: object) -> dict:
        protected_seen.append(policy.protected_ids)
        return account_creator(attempt=attempt, **kwargs)
    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=tmp_path / "qualification.json",
                                  registry=tmp_path / "registry.json",
                                  protected_ids=frozenset({"ESTABLISHED"}),
                                  account_creator=create, worker_probe=probe,
                                  clock=lambda: 105.)
    assert record["state"] == "simulated_pass"
    assert all("ESTABLISHED" in ids for ids in protected_seen)


@pytest.mark.parametrize("failure", ["duplicate", "conflict", "worker_failure", "recovery_failure",
                                      "stale", "unbound"])
def test_failure_quarantines_and_cannot_qualify(tmp_path: Path, failure: str) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    def create(*, attempt: Attempt, **kwargs: object) -> dict:
        row = account_creator(attempt=attempt, **kwargs)
        if failure == "duplicate" and attempt.worker_id == "worker-b":
            row["account_id"] = "ACCOUNT-A"
        if failure == "stale":
            row["completed_at"] = 80.
        return row
    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        if candidate.instance == "clone-a" and failure == "conflict":
            raise ValueError("session_conflict")
        if candidate.instance == "clone-a" and failure == "worker_failure":
            raise RuntimeError("worker failed")
        if candidate.instance == "clone-a" and failure == "recovery_failure":
            raise RuntimeError("recovery exhausted")
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}
    if failure == "unbound":
        clones = (replace(clones[0], attempt=replace(clones[0].attempt, lease_id="wrong")), clones[1])
    path = tmp_path / "qualification.json"
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  account_creator=create, worker_probe=probe, clock=lambda: 105.)
    assert record["state"] == "quarantined"
    assert not qualification_is_current(path, scope, now=105.)
    if failure in {"conflict", "worker_failure", "recovery_failure"}:
        assert len(record["worker_proofs"]) == 1


def test_manual_host_persists_bounded_unqualified_result_without_cloning(tmp_path: Path) -> None:
    _, _, source, clones, scope = setup(tmp_path)
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({"instances": [{"name": "seed", "endpoint": source.attempt.endpoint,
                                                  "lease_id": source.attempt.lease_id, "state": "running",
                                                  "source_lineage": "seed-v1"}]}))
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps({"state": "passed", "live": True}))
    record = qualify_clone_source(adapter=BlueStacksAdapter(ManualPool(pool), staging_root=tmp_path),
                                  scope=scope, source=source, clones=clones, record_path=path,
                                  registry=tmp_path / "registry.json", clock=lambda: 105.)
    assert record["state"] == "unqualified"
    assert record["reason"] == "unsupported_host_capability"
    assert json.loads(path.read_text())["state"] == "unqualified"


def test_live_qualification_rechecks_attested_scope_before_simulated_components(
        tmp_path: Path) -> None:
    _, _, source, clones, scope = setup(tmp_path)
    driver = AttestedDriver(scope)
    adapter = BlueStacksAdapter(driver, staging_root=tmp_path / "staging")
    path = tmp_path / "qualification.json"

    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  live=True, r00_enabled=True, clock=lambda: 105.)
    assert record["reason"] == "simulated_components_cannot_prove_live_host"

    driver.drifted = True
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  live=True, r00_enabled=True, clock=lambda: 105.)
    assert record["state"] == "unqualified"
    assert record["reason"] == "unsupported_host_capability"
    assert driver.calls == []


def test_live_qualification_quarantines_late_full_capability_drift(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, source, clones, scope = setup(tmp_path)
    driver = AttestedDriver(scope)
    adapter = BlueStacksAdapter(driver, staging_root=tmp_path / "staging")
    source = replace(source, observe=FakeLiveObserver(source.attempt.endpoint, "SOURCE"))
    clones = tuple(replace(item, observe=FakeLiveObserver(item.attempt.endpoint, "SOURCE"))
                   for item in clones)

    def create(*, attempt: Attempt, **kwargs: object) -> dict:
        return account_creator(attempt=attempt, **kwargs)

    def probe(*, candidate: CloneCandidate, account_id: str, **_: object) -> dict:
        driver.live_attested = False
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{candidate.instance}"}

    monkeypatch.setattr(clone_qualification_module, "create_staging_account", create)
    monkeypatch.setattr(clone_qualification_module, "probe_clone_worker", probe)
    monkeypatch.setattr(clone_qualification_module, "wait_for_tower_ready",
                        lambda device, observer: None)
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=tmp_path / "qualification.json",
                                  registry=tmp_path / "registry.json", account_creator=create,
                                  worker_probe=probe, live=True, r00_enabled=True,
                                  clock=lambda: 105.)

    assert record["state"] == "quarantined"
    assert record["reason"] == "live host capability drift"


def test_worker_probe_restarts_exact_instance_and_reads_fresh_account(tmp_path: Path) -> None:
    host, adapter, _, clones, scope = setup(tmp_path)
    host.instances.extend([
        HostInstance(item.instance, item.attempt.endpoint, item.attempt.lease_id,
                     "running", scope.source_lineage) for item in clones
    ])
    item = clones[0]
    attempt = replace(item.attempt, created_at=time.time() - 1.)
    reads: list[float] = []
    device = SimpleNamespace(serial=attempt.endpoint,
                             app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"))
    def observe(_: object) -> AccountFrame:
        stamp = time.time()
        reads.append(stamp)
        return AccountFrame("account", "ACCOUNT-A", scope.game_version,
                            f"digest-{len(reads)}", stamp, f"capture://{len(reads)}", {},
                            popup_title="ACCOUNT", id_label="ID:")
    proof = probe_clone_worker(adapter=adapter,
                               candidate=replace(item, attempt=attempt, connect=lambda: device,
                                                 observe=observe),
                               account_id="ACCOUNT-A", scope=scope)
    assert proof["recovered_at"] > proof["started_at"]
    assert proof["endpoint"] == attempt.endpoint
    assert host.calls == [("stop", "clone-a"), ("start", "clone-a")]
    assert device.serial == attempt.endpoint


def test_worker_probe_dismisses_measured_play_games_sheet_on_recovery(tmp_path: Path) -> None:
    host, adapter, _, clones, scope = setup(tmp_path)
    item = clones[0]
    host.instances.append(HostInstance(item.instance, item.attempt.endpoint,
                                       item.attempt.lease_id, "running", scope.source_lineage))
    attempt = replace(item.attempt, created_at=time.time() - 1.)
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(
        serial=attempt.endpoint,
        app_current=lambda: SimpleNamespace(package="com.google.android.gms"),
        app_start=lambda _: None,
        click=lambda x, y: taps.append((x, y)),
    )
    screens = iter(["google_play_profile", "home", "settings", "account"] * 2
                   + ["settings", "home"])
    def observe(_: object) -> AccountFrame:
        screen = next(screens)
        controls = {
            "google_play_profile": {"dismiss_google_play_profile": (176, 2289)},
            "home": {"settings": (1000, 200)},
            "settings": {"account": (800, 300), "close": (910, 490)},
            "account": {"close": (940, 585)},
        }[screen]
        stamp = time.time()
        return AccountFrame(screen, "ACCOUNT-A" if screen == "account" else None,
                            scope.game_version, f"digest-{stamp}", stamp,
                            f"capture://{stamp}", controls,
                            popup_title="ACCOUNT" if screen == "account" else None,
                            id_label="ID:" if screen == "account" else None)
    proof = probe_clone_worker(adapter=adapter,
                               candidate=replace(item, attempt=attempt,
                                                 connect=lambda: device, observe=observe),
                               account_id="ACCOUNT-A", scope=scope, navigate=True)
    assert proof["recovered_at"] > proof["started_at"]
    assert taps.count((176, 2289)) == 2
    assert taps[-2:] == [(940, 585), (910, 490)]
    assert host.calls == [("stop", "clone-a"), ("start", "clone-a")]


def test_worker_probe_waits_for_dialogs_to_finish_closing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host, adapter, _, clones, scope = setup(tmp_path)
    item = clones[0]
    host.instances.append(HostInstance(item.instance, item.attempt.endpoint,
                                       item.attempt.lease_id, "running", scope.source_lineage))
    attempt = replace(item.attempt, created_at=time.time() - 1.)
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(
        serial=attempt.endpoint,
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        click=lambda x, y: taps.append((x, y)),
    )
    # The first frame after each close tap still shows the old or a
    # transitional screen, as on a slow emulator.
    screens = iter(["account", "account", "account", "settings", "unknown", "home"])
    def observe(_: object) -> AccountFrame:
        screen = next(screens)
        controls = {"account": {"close": (940, 585)}, "settings": {"close": (910, 490)},
                    "unknown": {}, "home": {}}[screen]
        stamp = time.time()
        return AccountFrame(screen, "ACCOUNT-A" if screen == "account" else None,
                            scope.game_version, f"digest-{stamp}", stamp,
                            f"capture://{stamp}", controls,
                            popup_title="ACCOUNT" if screen == "account" else None,
                            id_label="ID:" if screen == "account" else None)
    monkeypatch.setattr(clone_qualification_module.time, "sleep", lambda _: None)
    proof = probe_clone_worker(adapter=adapter,
                               candidate=replace(item, attempt=attempt,
                                                 connect=lambda: device, observe=observe),
                               account_id="ACCOUNT-A", scope=scope, navigate=True)
    assert proof["recovered_at"] > proof["started_at"]
    assert taps == [(940, 585), (910, 490)]


def test_worker_probe_conflict_never_taps_and_does_not_restart(tmp_path: Path) -> None:
    host, adapter, _, clones, scope = setup(tmp_path)
    item = clones[0]
    host.instances.append(HostInstance(item.instance, item.attempt.endpoint,
                                       item.attempt.lease_id, "running", scope.source_lineage))
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(serial=item.attempt.endpoint,
                             app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
                             click=lambda x, y: taps.append((x, y)))
    observed = lambda _: AccountFrame("unknown", None, scope.game_version, "digest", time.time(),
                                      "capture://conflict", {}, "cloud session different than local session")
    with pytest.raises(ValueError, match="session_conflict"):
        probe_clone_worker(adapter=adapter, candidate=replace(item,
            attempt=replace(item.attempt, created_at=time.time() - 1.),
            connect=lambda: device, observe=observed), account_id="ACCOUNT-A", scope=scope)
    assert taps == [] and host.calls == []


@pytest.mark.parametrize("changed", [
    {"host_id": "mac-b"}, {"bluestacks_version": "BlueStacks-Air-3"},
    {"source_lineage": "seed-v2"}, {"source_version": "image-2"},
    {"game_version": "29.0.3"}, {"instance_config": {"abi": "x86"}},
])
def test_restart_audit_retrieval_rejects_scope_drift_and_stale_evidence(
        tmp_path: Path, changed: dict) -> None:
    host, adapter, source, clones, scope = setup(tmp_path)
    host.supports_m05_live_qualification = True
    path = tmp_path / "qualification.json"
    def probe(*, account_id: str, **_: object) -> dict:
        return {"account_id": account_id, "started_at": 103., "recovered_at": 104.,
                "evidence_ref": f"capture://recovered/{account_id}"}
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  account_creator=account_creator, worker_probe=probe,
                                  live=True, clock=lambda: 105.)
    assert record["state"] == "unqualified"
    assert record["reason"] == "simulated_components_cannot_prove_live_host"
    assert not qualification_is_current(path, scope, now=105.)

    # A synthetic persisted record tests the future O07 read gate after process
    # restart; it is not the result of this simulation or a live qualification.
    record = qualify_clone_source(adapter=adapter, scope=scope, source=source, clones=clones,
                                  record_path=path, registry=tmp_path / "registry.json",
                                  account_creator=account_creator, worker_probe=probe,
                                  clock=lambda: 105.)
    record.update(state="passed", live=True)
    for index, proof in enumerate(record["worker_proofs"]):
        proof.update(instance=clones[index].instance, endpoint=clones[index].attempt.endpoint,
                     lease_id=clones[index].attempt.lease_id,
                     worker_id=clones[index].attempt.worker_id)
    path.write_text(json.dumps(record))
    assert qualification_is_current(path, scope, now=105.)
    assert not qualification_is_current(path, replace(scope, **changed), now=105.)
    assert not qualification_is_current(path, scope, now=106. + 3600.)
