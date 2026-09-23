"""M05: evidence-backed, fail-closed qualification of one BlueStacks clone source.

There is no dashboard entry point. The installed ManualPool cannot qualify a
source; a live driver must explicitly attest its host capabilities and scope.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import ocr

from bluestacks import BlueStacksAdapter, HostBoundConnect, HostInstance, ProvisionMode
from fleet.account_creation import AccountFrame, StagingClone, create_staging_account
from fleet.account_observer import StagingAccountObserver
from fleet.clone_launch import launch_tower_from_game_center, wait_for_tower_ready
from fleet.identity import Attempt
from fleet.first_launch_account import create_first_launch_account, tower_is_unopened
from fleet.runtime import WorkerRuntime, reserve_endpoint, validate_isolation
from supervisor import DeviceSupervisor, RecoveryState


EVIDENCE_TTL = 300.
GAME_PACKAGE = "com.TechTreeGames.TheTower"


@dataclass(frozen=True)
class QualificationScope:
    host_id: str
    bluestacks_version: str
    source_lineage: str
    source_version: str
    game_version: str
    instance_config: dict[str, str]

    def valid(self) -> bool:
        return (all(isinstance(value, str) and value.strip() for value in (
            self.host_id, self.bluestacks_version, self.source_lineage,
            self.source_version, self.game_version,
        )) and isinstance(self.instance_config, dict) and bool(self.instance_config)
                and all(isinstance(key, str) and isinstance(value, str)
                        and key.strip() and value.strip()
                        for key, value in self.instance_config.items()))


@dataclass(frozen=True)
class CloneCandidate:
    instance: str
    runtime: WorkerRuntime
    attempt: Attempt
    connect: Callable[[], Any]
    observe: Callable[[Any], AccountFrame]


def _save(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(record, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _fresh(timestamp: Any, now: float, *, after: float = 0.) -> bool:
    return (isinstance(timestamp, (int, float)) and math.isfinite(timestamp)
            and after < timestamp <= now and now - timestamp <= EVIDENCE_TTL)


def _account_reading(frame: AccountFrame, account_id: str, version: str,
                     now: float, *, after: float = 0.) -> None:
    if frame.conflict_dialog:
        raise ValueError("session_conflict")
    if (frame.screen != "account" or frame.popup_title != "ACCOUNT"
            or frame.id_label != "ID:" or not frame.account_id
            or frame.account_id != account_id or frame.app_version != version
            or not frame.digest or not frame.evidence_ref
            or not _fresh(frame.observed_at, now, after=after)):
        raise ValueError("missing, ambiguous, or stale account evidence")


def probe_clone_worker(*, adapter: BlueStacksAdapter, candidate: CloneCandidate,
                       account_id: str, scope: QualificationScope,
                       clock: Callable[[], float] = time.time,
                       navigate: bool = False) -> dict[str, Any]:
    """Prove start, exact account, named host restart, and fresh recovery.

    With navigation enabled, only measured Home -> Settings -> Account taps
    are allowed after a worker restart. Any cloud/session prompt aborts.
    """
    attempt = candidate.attempt

    def read_account(device: Any) -> AccountFrame:
        for _ in range(120 if navigate else 1):
            shot = candidate.observe(device)
            if shot.conflict_dialog:
                raise ValueError("session_conflict")
            if shot.screen == "account":
                return shot
            if navigate and shot.screen in {"home", "settings", "google_play_profile",
                                            "link_account_prompt"}:
                control = ("settings" if shot.screen == "home" else
                           "account" if shot.screen == "settings" else
                           "close" if shot.screen == "link_account_prompt" else
                           "dismiss_google_play_profile")
                allowed = ({control}, {control, "close"}) if shot.screen == "settings" else ({control},)
                if set(shot.controls) not in allowed or clock() - shot.observed_at > 5:
                    raise ValueError("worker account navigation unavailable")
                device.click(*shot.controls[control])
            elif not navigate or shot.screen != "unknown":
                raise ValueError("worker account screen unavailable")
            time.sleep(1.)
        raise ValueError("worker account screen timed out")

    with candidate.runtime.reserve(attempt.endpoint):
        connector = HostBoundConnect(adapter, candidate.instance, attempt,
                                     candidate.connect, timeout=10., restart_after=2)
        supervisor = DeviceSupervisor(
            path=candidate.runtime.checkpoint_root / ".m05-recovery.json",
            endpoint=attempt.endpoint, connect=connector, expected_account=account_id,
            max_attempts=3, quarantine_on_exhaustion=True, game_package=GAME_PACKAGE,
            clock=clock,
        )
        if supervisor.recover() is RecoveryState.QUARANTINED:
            raise ValueError("worker startup quarantined")
        device = supervisor._device
        if device is None:
            raise ValueError("worker startup unavailable")
        before = read_account(device)
        _account_reading(before, account_id, scope.game_version, clock(),
                         after=attempt.created_at)
        supervisor.verify_account(account_id, observed_at=before.observed_at)
        if supervisor.observe(frame_digest=before.digest, observed_at=before.observed_at,
                              screen=before.screen, account_id=account_id,
                              session_conflict=bool(before.conflict_dialog)) is not RecoveryState.READY:
            raise ValueError("worker startup evidence blocked")

        supervisor.disconnected()
        adapter.stop(candidate.instance, attempt)
        adapter.start(candidate.instance, attempt)
        if supervisor.recover() is RecoveryState.QUARANTINED:
            raise ValueError("worker recovery quarantined")
        device = supervisor._device
        if device is None:
            raise ValueError("worker recovery exhausted")
        if navigate and getattr(device.app_current(), "package", None) not in {
                GAME_PACKAGE, "com.google.android.gms"}:
            launch_tower_from_game_center(device)
        after = read_account(device)
        _account_reading(after, account_id, scope.game_version, clock(),
                         after=before.observed_at)
        if after.digest == before.digest or after.evidence_ref == before.evidence_ref:
            raise ValueError("post-restart evidence was reused")
        supervisor.verify_account(account_id, observed_at=after.observed_at)
        if supervisor.observe(frame_digest=after.digest, observed_at=after.observed_at,
                              screen=after.screen, account_id=account_id,
                              session_conflict=bool(after.conflict_dialog)) is not RecoveryState.READY:
            raise ValueError("worker recovery evidence blocked")
        if navigate:
            # The account proof leaves two stacked dialogs over the game.
            # Use the controls located on each fresh dialog frame. Their
            # absolute positions change with the emulator height.
            if "close" not in after.controls:
                raise ValueError("account close control unavailable")
            device.click(*after.controls["close"])
            settings = candidate.observe(device)
            if settings.screen != "settings" or settings.conflict_dialog:
                raise ValueError("account dialog did not close after recovery")
            if "close" not in settings.controls:
                raise ValueError("settings close control unavailable")
            device.click(*settings.controls["close"])
            home = candidate.observe(device)
            if home.screen != "home" or home.conflict_dialog:
                raise ValueError("settings dialog did not close after recovery")
        adapter.designated(candidate.instance, attempt)
        return {"account_id": account_id, "started_at": before.observed_at,
                "startup_evidence_ref": before.evidence_ref,
                "recovered_at": after.observed_at, "evidence_ref": after.evidence_ref,
                "instance": candidate.instance, "endpoint": attempt.endpoint,
                "lease_id": attempt.lease_id, "worker_id": attempt.worker_id}


def qualify_clone_source(
    *, adapter: BlueStacksAdapter, scope: QualificationScope,
    source: CloneCandidate, clones: tuple[CloneCandidate, CloneCandidate],
    record_path: Path, registry: Path,
    account_creator: Callable[..., dict[str, Any]] = create_staging_account,
    worker_probe: Callable[..., dict[str, Any]] = probe_clone_worker,
    bind_created: Callable[[CloneCandidate, HostInstance], CloneCandidate] | None = None,
    protected_ids: frozenset[str] = frozenset(),
    reuse_attested_clones: bool = False,
    live: bool = False, r00_enabled: bool = False,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Run the controlled proof and persist every terminal outcome.

    Injected account creators and worker probes support simulations. A passed
    live record additionally requires a driver with an independent, measured
    qualification scope and an explicit live capability attestation.
    """
    record: dict[str, Any] = {
        "schema": 1, "state": "unqualified", "reason": "proof_not_started",
        "scope": asdict(scope), "source_instance": source.instance,
        "clone_instances": [item.instance for item in clones],
        "account_ids": [], "source_evidence": None, "account_audits": [],
        "worker_proofs": [], "evaluated_at": clock(), "live": live,
    }
    # Invalidate any older pass before touching the host. A process crash
    # during this run must leave the clone source closed.
    _save(Path(record_path), record)

    def finish(state: str, reason: str) -> dict[str, Any]:
        record.update(state=state, reason=reason, evaluated_at=clock())
        _save(Path(record_path), record)
        return record

    if not scope.valid():
        return finish("quarantined", "invalid_qualification_scope")
    if (getattr(adapter.driver, "supports_clone_staging", False) is not True
            or not adapter.supports_lifecycle):
        return finish("unqualified", "unsupported_host_capability")
    if live and (account_creator is not create_staging_account
                 or worker_probe is not probe_clone_worker
                 or not isinstance(source.observe, StagingAccountObserver)
                 or source.observe.endpoint != source.attempt.endpoint
                 or scope.game_version not in source.observe.allowed_versions):
        return finish("unqualified", "simulated_components_cannot_prove_live_host")
    if live and (getattr(adapter.driver, "supports_m05_live_qualification", False) is not True
                 or not callable(getattr(adapter.driver, "qualification_scope", None))
                 or adapter.driver.qualification_scope() != scope):
        return finish("unqualified", "unsupported_or_changed_live_scope")
    if account_creator is create_staging_account and not r00_enabled:
        return finish("unqualified", "r00_not_explicitly_enabled")

    try:
        candidates = (source, *clones)
        validate_isolation(item.runtime for item in candidates)
        if (len({item.instance for item in candidates}) != 3
                or len({item.attempt.worker_id for item in candidates}) != 3
                or any(item.runtime.worker_id != item.attempt.worker_id
                       for item in candidates)):
            raise ValueError("worker or instance identity is shared")
        designated_source = adapter.designated(source.instance, source.attempt)
        if designated_source.state != "running" or designated_source.source_lineage != scope.source_lineage:
            raise ValueError("source state or lineage changed")
        with reserve_endpoint(source.attempt.endpoint):
            device = source.connect()
            if getattr(device, "serial", None) != source.attempt.endpoint:
                raise ValueError("source endpoint unbound")
            source_frame = source.observe(device)
            if not source_frame.account_id:
                raise ValueError("source account identity missing")
            _account_reading(source_frame, source_frame.account_id, scope.game_version,
                             clock(), after=source.attempt.created_at)
        record["source_evidence"] = {
            "account_id": source_frame.account_id, "observed_at": source_frame.observed_at,
            "evidence_ref": source_frame.evidence_ref, "digest": source_frame.digest,
            "endpoint": source.attempt.endpoint, "lease_id": source.attempt.lease_id,
        }
        with adapter.staging_lease():
            bound_clones: list[CloneCandidate] = []
            for item in clones:
                if reuse_attested_clones:
                    created = adapter.designated(item.instance, item.attempt)
                    if created.source_lineage != scope.source_lineage:
                        raise ValueError("existing clone lineage unverified")
                    if (item.runtime.checkpoint_root / ".r00-account-creation.json").exists():
                        raise ValueError("existing clone has an R00 attempt")
                    for journal in item.runtime.root.parent.glob(
                            "*/checkpoints/.r00-account-creation.json"):
                        prior = json.loads(journal.read_text(encoding="utf-8"))
                        if (prior.get("instance") == item.instance
                                or prior.get("endpoint") == item.attempt.endpoint
                                or prior.get("lease_id") == item.attempt.lease_id):
                            pre_action = (
                                prior.get("instance") == item.instance
                                and prior.get("endpoint") == item.attempt.endpoint
                                and prior.get("lease_id") == item.attempt.lease_id
                                and prior.get("source_lineage") == scope.source_lineage
                                and prior.get("allowed_source_id") == source_frame.account_id
                                and prior.get("state") == "quarantined"
                                and prior.get("reason") in {
                                    "unexpected game package",
                                    "unexpected screen during account creation",
                                }
                                and prior.get("evidence") == []
                                and prior.get("started_from_account") is False
                                and all(prior.get(key) for key in (
                                    "worker_id", "attempt_id", "generation", "created_at",
                                    "started_at"))
                                and set(prior) <= {
                                    "worker_id", "endpoint", "lease_id", "attempt_id",
                                    "generation", "created_at", "instance", "source_lineage",
                                    "allowed_source_id", "state", "reason", "evidence",
                                    "started_at", "started_from_account",
                                }
                            )
                            if not pre_action:
                                raise ValueError("existing clone has an R00 attempt")
                    if Path(registry).exists():
                        bindings = json.loads(Path(registry).read_text(encoding="utf-8"))
                        if not isinstance(bindings, dict) or any(
                            not isinstance(row, dict)
                            or worker == item.attempt.worker_id
                            or row.get("instance") == item.instance
                            or row.get("endpoint") == item.attempt.endpoint
                            for worker, row in bindings.items()
                        ):
                            raise ValueError("existing clone has an active account binding")
                else:
                    created = adapter.provision(item.instance, mode=ProvisionMode.CLONE,
                                                source=source.instance)
                bound_item = bind_created(item, created) if bind_created is not None else item
                if (bound_item.instance != item.instance
                        or bound_item.runtime != item.runtime
                        or bound_item.attempt.worker_id != item.attempt.worker_id
                        or created.name != bound_item.instance
                        or created.endpoint != bound_item.attempt.endpoint
                        or created.lease_id != bound_item.attempt.lease_id
                        or created.source_lineage != scope.source_lineage):
                    raise ValueError("staged clone identity or lineage unbound")
                if live and (not isinstance(bound_item.observe, StagingAccountObserver)
                             or bound_item.observe.endpoint != bound_item.attempt.endpoint
                             or scope.game_version not in bound_item.observe.allowed_versions):
                    raise ValueError("staged clone observer is not live bound")
                bound_clones.append(bound_item)
        clones = (bound_clones[0], bound_clones[1])
        candidates = (source, *clones)
        inventory = adapter.inventory()
        for item in candidates:
            if (sum(row.endpoint == item.attempt.endpoint for row in inventory) != 1
                    or sum(row.lease_id == item.attempt.lease_id for row in inventory) != 1):
                raise ValueError("shared clone endpoint or lease")
        account_ids = [source_frame.account_id]
        for item in clones:
            bound = adapter.designated(item.instance, item.attempt)
            if bound.state == "stopped":
                adapter.start(item.instance, item.attempt)
                bound = adapter.designated(item.instance, item.attempt)
            if bound.state != "running" or bound.source_lineage != scope.source_lineage:
                raise ValueError("clone state or lineage changed")
            if live:
                with item.runtime.reserve(item.attempt.endpoint):
                    device = item.connect()
                    if getattr(device, "serial", None) != item.attempt.endpoint:
                        raise ValueError("clone launch endpoint unbound")
                    launch_tower_from_game_center(device)
                    wait_for_tower_ready(device, item.observe)
            audit = account_creator(
                runtime=item.runtime, attempt=item.attempt, adapter=adapter,
                policy=StagingClone(item.instance, scope.source_lineage,
                                    source_frame.account_id,
                                    frozenset(account_ids) | protected_ids, r00_enabled),
                connect=item.connect, observe=item.observe, registry=registry,
            )
            if (audit.get("state") != "verified" or audit.get("worker_id") != item.attempt.worker_id
                    or audit.get("endpoint") != item.attempt.endpoint
                    or audit.get("lease_id") != item.attempt.lease_id
                    or audit.get("generation") != item.attempt.generation
                    or audit.get("source_account_id") != source_frame.account_id
                    or audit.get("app_version") != scope.game_version
                    or not _fresh(audit.get("completed_at"), clock(), after=item.attempt.created_at)
                    or not audit.get("account_id") or not audit.get("evidence")):
                raise ValueError("R00 account audit incomplete or mismatched")
            account_ids.append(audit["account_id"])
            record["account_audits"].append(audit)
        record["account_ids"] = account_ids
        if len(set(account_ids)) != 3:
            raise ValueError("duplicate Tower Account IDs")

        # Both tasks are submitted before either result is consumed. Failure of
        # one worker does not cancel the other worker's audit/recovery attempt.
        start_barrier = threading.Barrier(2)

        def start_probe(item: CloneCandidate, account_id: str) -> dict[str, Any]:
            start_barrier.wait(timeout=15.)
            return worker_probe(adapter=adapter, candidate=item, account_id=account_id,
                                scope=scope, clock=clock)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="m05-worker") as pool:
            futures = [pool.submit(start_probe, item, account_ids[index + 1])
                       for index, item in enumerate(clones)]
            errors: list[str] = []
            for index, future in enumerate(futures):
                try:
                    proof = future.result()
                    if (proof.get("account_id") != account_ids[index + 1]
                            or not _fresh(proof.get("started_at"), clock(),
                                          after=clones[index].attempt.created_at)
                            or not _fresh(proof.get("recovered_at"), clock(),
                                          after=proof.get("started_at", 0.))
                            or not proof.get("evidence_ref")):
                        raise ValueError("worker recovery proof incomplete")
                    record["worker_proofs"].append(proof)
                except Exception as exc:  # collect both workers' outcomes
                    errors.append(f"{clones[index].instance}: {exc}")
            if errors:
                raise ValueError("; ".join(errors))
        for item in candidates:
            bound = adapter.designated(item.instance, item.attempt)
            if bound.source_lineage != scope.source_lineage or bound.state != "running":
                raise ValueError("source or clone drift during verification")
        if live and (
                getattr(adapter.driver, "supports_clone_staging", False) is not True
                or not adapter.supports_lifecycle
                or getattr(adapter.driver, "supports_m05_live_qualification", False) is not True
                or not callable(getattr(adapter.driver, "qualification_scope", None))
                or adapter.driver.qualification_scope() != scope):
            raise ValueError("live host capability drift")
        if (not _fresh(source_frame.observed_at, clock())
                or any(not _fresh(audit.get("completed_at"), clock())
                       for audit in record["account_audits"])
                or any(not _fresh(proof.get("recovered_at"), clock())
                       for proof in record["worker_proofs"])):
            raise ValueError("qualification evidence expired during verification")
        return finish("passed" if live else "simulated_pass", "concurrent_recovery_verified")
    except Exception as exc:  # any uncertain result keeps the source unavailable
        return finish("quarantined", str(exc) or "verification_failed")


def qualify_unopened_clone_source(
    *, adapter: BlueStacksAdapter, scope: QualificationScope,
    source: CloneCandidate, clones: tuple[CloneCandidate, CloneCandidate],
    record_path: Path, registry: Path,
    bind_created: Callable[[CloneCandidate, HostInstance], CloneCandidate],
    manual_lineage_evidence: Path | None = None,
    protected_ids: frozenset[str] = frozenset(),
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Qualify two unique first-launch clone IDs; never launch Tower on source."""
    record: dict[str, Any] = {
        "schema": 2, "state": "unqualified", "reason": "proof_not_started",
        "scope": asdict(scope), "source_instance": source.instance,
        "clone_instances": [item.instance for item in clones],
        "account_ids": [], "source_evidence": None,
        "account_audits": [], "worker_proofs": [],
        "evaluated_at": clock(), "live": True,
    }
    _save(Path(record_path), record)

    def finish(state: str, reason: str) -> dict[str, Any]:
        record.update(state=state, reason=reason, evaluated_at=clock())
        _save(Path(record_path), record)
        return record

    if not scope.valid():
        return finish("quarantined", "invalid_qualification_scope")
    driver = adapter.driver
    if (getattr(driver, "supports_clone_staging", False) is not True
            or getattr(driver, "supports_m05_live_qualification", False) is not True
            or not adapter.supports_lifecycle
            or not callable(getattr(driver, "qualification_scope", None))
            or driver.qualification_scope() != scope):
        return finish("unqualified", "unsupported_or_changed_live_scope")
    try:
        candidates = (source, *clones)
        validate_isolation(item.runtime for item in candidates)
        if (len({item.instance for item in candidates}) != 3
                or len({item.attempt.worker_id for item in candidates}) != 3
                or any(item.runtime.worker_id != item.attempt.worker_id
                       for item in candidates)):
            raise ValueError("worker or instance identity is shared")
        if (not isinstance(source.observe, StagingAccountObserver)
                or source.observe.endpoint != source.attempt.endpoint):
            raise ValueError("source observer endpoint unbound")
        source_host = adapter.designated(source.instance, source.attempt)
        if source_host.state == "stopped":
            adapter.start(source.instance, source.attempt)
            source_host = adapter.designated(source.instance, source.attempt)
        if source_host.state != "running" or source_host.source_lineage != scope.source_lineage:
            raise ValueError("source state or lineage changed")
        with reserve_endpoint(source.attempt.endpoint):
            device = source.connect()
            if getattr(device, "serial", None) != source.attempt.endpoint:
                raise ValueError("source endpoint unbound")
            info = device.app_info(GAME_PACKAGE)
            if getattr(info, "version_name", None) != scope.game_version:
                raise ValueError("source Tower version changed")
            if not tower_is_unopened(device):
                raise ValueError("source Tower has been launched")
            observed_at = clock()
        record["source_evidence"] = {
            "tower_unopened": True, "game_version": scope.game_version,
            "observed_at": observed_at, "endpoint": source.attempt.endpoint,
            "lease_id": source.attempt.lease_id,
        }
        _save(Path(record_path), record)
        with adapter.staging_lease():
            bound_clones: list[CloneCandidate] = []
            manual_rows: dict[str, Any] = {}
            if manual_lineage_evidence is not None:
                manifest = json.loads(Path(manual_lineage_evidence).read_text(encoding="utf-8"))
                if (manifest.get("source_instance") != source.instance
                        or manifest.get("source_lease") != source.attempt.lease_id
                        or not isinstance(manifest.get("clones"), list)
                        or {row.get("name") for row in manifest["clones"]}
                        != {item.instance for item in clones}):
                    raise ValueError("manual clone source attestation incomplete")
                manual_rows = {row["name"]: row for row in manifest["clones"]}
            for item in clones:
                if manual_rows:
                    created = adapter.designated(item.instance, replace(
                        item.attempt, endpoint=manual_rows[item.instance]["endpoint"],
                        lease_id=manual_rows[item.instance]["lease_id"]))
                    row = manual_rows[item.instance]
                    capture = Path(row["source_selection_capture"])
                    image = cv2.imread(str(capture))
                    if (image is None or created.state not in {"stopped", "running"}
                            or created.source_lineage != scope.source_lineage
                            or hashlib.sha256(capture.read_bytes()).hexdigest()
                            != row.get("capture_sha256")):
                        raise ValueError("manual clone lineage evidence changed")
                    labels = [box.text.replace(" ", "").lower()
                              for box in ocr.read(image, strict=True, min_confidence=.9)]
                    display_source = driver.inventory_source.display_name(source.instance)
                    if ("clonefrom" not in labels
                            or display_source.replace(" ", "").lower() not in labels
                            or "create" not in labels):
                        raise ValueError("manual clone source selection unrecognized")
                else:
                    created = adapter.provision(item.instance, mode=ProvisionMode.CLONE,
                                                source=source.instance)
                bound = bind_created(item, created)
                if (bound.instance != item.instance or bound.runtime != item.runtime
                        or bound.attempt.worker_id != item.attempt.worker_id
                        or created.endpoint != bound.attempt.endpoint
                        or created.lease_id != bound.attempt.lease_id
                        or created.source_lineage != scope.source_lineage
                        or not isinstance(bound.observe, StagingAccountObserver)
                        or bound.observe.endpoint != bound.attempt.endpoint
                        or scope.game_version not in bound.observe.allowed_versions):
                    raise ValueError("staged clone identity or observer unbound")
                bound_clones.append(bound)
        clones = (bound_clones[0], bound_clones[1])
        inventory = adapter.inventory()
        for item in (source, *clones):
            if (sum(row.endpoint == item.attempt.endpoint for row in inventory) != 1
                    or sum(row.lease_id == item.attempt.lease_id for row in inventory) != 1):
                raise ValueError("shared clone endpoint or lease")
        for item in clones:
            host = adapter.designated(item.instance, item.attempt)
            if host.state == "stopped":
                adapter.start(item.instance, item.attempt)
                host = adapter.designated(item.instance, item.attempt)
            if host.state != "running" or host.source_lineage != scope.source_lineage:
                raise ValueError("clone state or lineage changed")
            audit = create_first_launch_account(
                runtime=item.runtime, attempt=item.attempt, adapter=adapter,
                instance=item.instance, source_lineage=scope.source_lineage,
                protected_ids=frozenset(record["account_ids"]) | protected_ids,
                connect=item.connect, observe=item.observe, registry=registry, clock=clock,
            )
            if (audit.get("state") != "verified"
                    or audit.get("app_version") != scope.game_version
                    or not audit.get("account_id")
                    or not any(row.get("action") == "i_agree"
                               for row in audit.get("evidence", []))):
                raise ValueError("first-launch account audit incomplete")
            record["account_ids"].append(audit["account_id"])
            record["account_audits"].append(audit)
            _save(Path(record_path), record)
        if len(set(record["account_ids"])) != 2:
            raise ValueError("duplicate Tower Account IDs")
        for index, item in enumerate(clones):
            record["worker_proofs"].append(probe_clone_worker(
                adapter=adapter, candidate=item, account_id=record["account_ids"][index],
                scope=scope, clock=clock, navigate=True))
            _save(Path(record_path), record)
        if not all(_fresh(proof.get("recovered_at"), clock())
                   for proof in record["worker_proofs"]):
            raise ValueError("worker recovery evidence expired")
        if adapter.designated(source.instance, source.attempt).state == "stopped":
            adapter.start(source.instance, source.attempt)
        if not tower_is_unopened(source.connect()):
            raise ValueError("source Tower changed during qualification")
        if driver.qualification_scope() != scope:
            raise ValueError("live host scope drift")
        return finish("passed", "two_first_launch_accounts_and_worker_recovery_verified")
    except Exception as exc:
        return finish("quarantined", str(exc) or "verification_failed")


def qualification_is_current(path: Path, scope: QualificationScope, *, now: float | None = None) -> bool:
    """O07's future read-only gate; missing, simulated, or stale means closed."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        timestamp = time.time() if now is None else now
        ids = record["account_ids"]
        source = record["source_evidence"]
        audits = record["account_audits"]
        proofs = record["worker_proofs"]
        if record.get("schema") == 2:
            return (record.get("state") == "passed" and record.get("live") is True
                    and record.get("scope") == asdict(scope)
                    and len(ids) == 2 and len(set(ids)) == 2 and all(ids)
                    and _fresh(record.get("evaluated_at"), timestamp)
                    and source.get("tower_unopened") is True
                    and source.get("endpoint") and source.get("lease_id")
                    and _fresh(source.get("observed_at"), timestamp)
                    and len(audits) == 2 and len(proofs) == 2
                    and all(
                        audit.get("state") == "verified"
                        and audit.get("account_id") == ids[index]
                        and audit.get("app_version") == scope.game_version
                        and any(row.get("action") == "i_agree"
                                for row in audit.get("evidence", []))
                        and proof.get("account_id") == ids[index]
                        and proof.get("endpoint") == audit.get("endpoint")
                        and proof.get("lease_id") == audit.get("lease_id")
                        and proof.get("worker_id") == audit.get("worker_id")
                        and proof.get("instance") == record["clone_instances"][index]
                        and isinstance(audit.get("completed_at"), (int, float))
                        and math.isfinite(audit["completed_at"])
                        and 0 < audit["completed_at"] < proof.get("started_at", 0.)
                        and _fresh(proof.get("started_at"), timestamp)
                        and _fresh(proof.get("recovered_at"), timestamp,
                                   after=proof.get("started_at", 0.))
                        for index, (audit, proof) in enumerate(zip(audits, proofs))
                    ))
        return (record.get("schema") == 1 and record.get("state") == "passed"
                and record.get("live") is True and record.get("scope") == asdict(scope)
                and len(ids) == 3 and len(set(ids)) == 3 and all(ids)
                and _fresh(record.get("evaluated_at"), timestamp)
                and source.get("account_id") == ids[0]
                and source.get("endpoint") and source.get("lease_id")
                and source.get("digest") and source.get("evidence_ref")
                and _fresh(source.get("observed_at"), timestamp)
                and len(audits) == 2 and len(proofs) == 2
                and all(
                    audit.get("state") == "verified"
                    and audit.get("account_id") == ids[index + 1]
                    and audit.get("source_account_id") == ids[0]
                    and audit.get("app_version") == scope.game_version
                    and audit.get("endpoint") and audit.get("lease_id")
                    and audit.get("worker_id") and audit.get("generation")
                    and _fresh(audit.get("completed_at"), timestamp)
                    and proof.get("account_id") == ids[index + 1]
                    and proof.get("endpoint") == audit.get("endpoint")
                    and proof.get("lease_id") == audit.get("lease_id")
                    and proof.get("worker_id") == audit.get("worker_id")
                    and proof.get("instance") == record["clone_instances"][index]
                    and proof.get("evidence_ref")
                    and _fresh(proof.get("started_at"), timestamp)
                    and _fresh(proof.get("recovered_at"), timestamp,
                               after=proof.get("started_at", 0.))
                    for index, (audit, proof) in enumerate(zip(audits, proofs))
                ))
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False
