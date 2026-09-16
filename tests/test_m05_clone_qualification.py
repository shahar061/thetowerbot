"""M05 simulations. A passing simulation is never live host qualification."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bluestacks import BlueStacksAdapter, HostInstance, ManualPool
from fleet.account_creation import AccountFrame
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
