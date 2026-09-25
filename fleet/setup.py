"""Private, host-bound Fleet setup choices for the local dashboard."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fleet.dashboard import FleetPolicy

logger = logging.getLogger(__name__)


def wait_for_android_boot(connect: Callable[[], Any], *, timeout: float = 180.,
                          poll: float = 2., clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> None:
    """Block until Android reports boot completed.

    ManualAirWorker.start returns once the emulator's adb port is up, not once
    Android has booted. Until then ``pm`` fails (read as "tower_state_unavailable")
    or answers empty, which would read as "Tower not installed".
    """
    deadline = clock() + timeout
    while True:
        try:
            if connect().shell("getprop sys.boot_completed").strip() == "1":
                return
        except Exception:
            pass
        if clock() >= deadline:
            raise TimeoutError("android_boot_not_completed")
        sleep(poll)


class FleetSetupError(ValueError):
    """A setup choice is unsafe or cannot be matched to local evidence."""


class FleetSetupStore:
    def __init__(self, root: Path, *, qualification_root: Path) -> None:
        self.root = Path(root)
        self.qualification_root = Path(qualification_root)

    def qualifications(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not self.qualification_root.is_dir():
            return result
        for path in sorted(self.qualification_root.glob("*/m05.json")):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", path.parent.name):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                source = record["source_instance"]
                evidence = record["source_evidence"]
                if (record.get("schema") != 2 or record.get("state") != "passed"
                        or record.get("live") is not True
                        or evidence.get("tower_unopened") is not True
                        or not isinstance(source, str)
                        or not re.fullmatch(r"[A-Za-z0-9_]+", source)):
                    continue
                result.append({"id": path.parent.name, "source_instance": source,
                               "evaluated_at": record.get("evaluated_at")})
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        return result

    def settings(self) -> dict[str, Any] | None:
        try:
            value = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"capacity", "name_prefix", "qualification_id"}:
                return None
            FleetPolicy(value["capacity"], value["name_prefix"])
            if not isinstance(value["qualification_id"], str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]+", value["qualification_id"]):
                return None
            return value
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def snapshot(self) -> dict[str, Any]:
        settings = self.settings()
        return {"configured": settings is not None, "settings": settings,
                "qualifications": self.qualifications()}

    def configure(self, *, capacity: int, name_prefix: str, qualification_id: str,
                  installed_prefix: str, host_count: int) -> dict[str, Any]:
        try:
            FleetPolicy(capacity, name_prefix)
        except (TypeError, ValueError) as exc:
            raise FleetSetupError("invalid_fleet_policy") from exc
        if name_prefix != installed_prefix:
            raise FleetSetupError("configured_prefix_does_not_match_host")
        if capacity <= host_count:
            raise FleetSetupError("fleet_capacity_exceeded")
        if (not isinstance(qualification_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", qualification_id)
                or qualification_id not in {item["id"] for item in self.qualifications()}):
            raise FleetSetupError("qualification_not_available")
        settings = {"capacity": capacity, "name_prefix": name_prefix,
                    "qualification_id": qualification_id}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.root / f".settings.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(settings, file, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.root / "settings.json")
        finally:
            temporary.unlink(missing_ok=True)
        return self.snapshot()


class FleetSetupService:
    """Bind saved UI choices to the installed Air host and first-launch flow."""

    def __init__(self, root: Path, *, qualification_root: Path) -> None:
        self.store = FleetSetupStore(root, qualification_root=qualification_root)
        self.root = Path(root)
        self.controller: Any | None = None
        self._verify_clone: Any | None = None
        self._connect: Any | None = None
        self._build_error: str | None = None
        self._reroll_pool: Any | None = None
        self._reroll_supervisor: Any | None = None
        self._reroll_start_thread: Any | None = None
        self._reroll_runs: Any | None = None
        self._reroll_operation: dict[str, Any] | None = None
        from threading import Lock
        self._reroll_dispatch_lock = Lock()
        if self.store.settings() is not None:
            try:
                self._build()
            except (OSError, ValueError, TypeError, KeyError):
                self._build_error = "saved_qualification_unavailable"

    def _manual_pool(self) -> Any:
        if self._reroll_pool is None:
            from adbutils import AdbClient
            import config
            from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows
            from fleet.first_launch_account import tower_is_unopened
            from fleet.reroll_pool import RerollPool
            from web.account_catalog import registered_worker

            inventory = BlueStacksAirInventory(
                Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
                _EXECUTABLE, live_process_rows)

            def device(endpoint: str) -> Any:
                client = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT)
                client.connect(endpoint, timeout=2.)
                return client.device(serial=endpoint)

            def package_state(endpoint: str) -> str:
                target = device(endpoint)
                if not target.shell("pm path com.TechTreeGames.TheTower").strip():
                    return "not_installed"
                return "installed_unopened" if tower_is_unopened(target) else "opened"

            def probe_stopped(row: Any) -> str:
                from fleet.manual_air_worker import ManualAirWorker
                from fleet.reroll_journal import RerollJournal

                journal = RerollJournal(self.root)
                journal.append(instance=row.name, level="info", kind="tower_check",
                               message="Booting to check The Tower has never been opened")
                worker = ManualAirWorker(row.name, row.endpoint, row.lease_id, inventory=inventory)
                # The worker serializes its own Manager presses with every other start/stop.
                worker.start(row.name)
                try:
                    wait_for_android_boot(lambda: device(row.endpoint))
                    state = package_state(row.endpoint)
                finally:
                    worker.stop(row.name)
                if state == "installed_unopened":
                    journal.append(instance=row.name, level="info", kind="tower_check",
                                   message="The Tower has never been opened; shut back down")
                else:
                    journal.append(instance=row.name, level="error", kind="tower_check",
                                   message="The Tower was already opened on this emulator, so it can't "
                                           "start a fresh account; not added" if state == "opened"
                                   else f"Not added: Tower state is {state}")
                return state

            def protected_names() -> set[str]:
                return {"Tiramisu64_6"} | {item["source_instance"]
                                          for item in self.store.qualifications()}

            def registered(row: Any) -> bool:
                worker_root = self.root / "workers" / row.name
                if registered_worker(worker_root) is None:
                    return False
                try:
                    record = json.loads((worker_root / "fleet-registration.json").read_text())
                    return (record.get("endpoint") == row.endpoint
                            and record.get("lease_id") == row.lease_id)
                except (OSError, ValueError, TypeError):
                    return False

            self._reroll_pool = RerollPool(
                self.root, inventory=inventory.instances,
                package_state=package_state, protected_names=protected_names,
                probe_stopped=probe_stopped, registered=registered)
        return self._reroll_pool

    def _air_inventory(self) -> Any:
        from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows
        return BlueStacksAirInventory(
            Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
            _EXECUTABLE, live_process_rows)

    def _runs(self) -> Any:
        if self._reroll_runs is None:
            from fleet.manual_air_worker import ManualAirWorker
            from fleet.reroll_journal import RerollJournal
            from fleet.reroll_retirement import remove_from_run, stop_bot
            from fleet.reroll_runs import RerollRuns
            from fleet.reroll_variants import assign_variant, opening_variants

            pool = self._manual_pool()
            supervisor = self._manual_supervisor()
            inventory = self._air_inventory()

            def instance_state(name: str) -> str | None:
                return next((row.state for row in inventory.instances() if row.name == name), None)

            def stop_instance(name: str, endpoint: str, lease_id: str) -> None:
                # ManualAirWorker holds the Manager lock for the press, so a start and a
                # stop never drive the BlueStacks Manager GUI at once.
                ManualAirWorker(name, endpoint, lease_id, inventory=inventory).stop(name)

            self._reroll_stop_instance = stop_instance
            self._reroll_runs = RerollRuns(
                self.root, pool=pool,
                release=lambda member: stop_bot(
                    member, supervisor=supervisor, instance_state=instance_state,
                    journal=RerollJournal(self.root)),
                remove=lambda member: remove_from_run(
                    member, supervisor=supervisor, stop_instance=stop_instance,
                    instance_state=instance_state, journal=RerollJournal(self.root)),
                assign=lambda name: assign_variant(
                    self.root / "workers" / name, workers_dir=self.root / "workers",
                    variants=opening_variants()))
        return self._reroll_runs

    def reroll_snapshot(self) -> dict[str, Any]:
        snapshot = self._manual_pool().snapshot()
        supervisor = self._manual_supervisor()
        statuses = supervisor.reconcile(snapshot)
        from web.account_catalog import registered_worker
        from fleet.reroll_metrics import observed_metrics
        from fleet.reroll_variants import compare, opening_variants, read_variant, variant_name
        variants = opening_variants()
        for member in snapshot["members"]:
            status = statuses.get(member["name"], {"state": "paused"})
            registration = registered_worker(self.root / "workers" / member["name"])
            member["host_state"] = member["state"]
            if status["state"] != "paused":
                member["state"] = status["state"]
            elif registration is not None:
                member["state"] = "paused"
            if registration is not None:
                member["account_id"] = registration.account_id
                member["account_key"] = registration.key
                try:
                    member.update(observed_metrics(self.root / "workers" / member["name"],
                        account_key=registration.key, account_id=registration.account_id or "",
                        web_port=registration.web_port or 0,
                        running=status["state"] == "running"))
                except Exception:  # noqa: BLE001 - one worker's evidence must not hide its peers
                    member["error"] = "metrics unavailable"
                    logger.exception("reroll metrics unavailable for member %s", member["name"])
            variant = read_variant(self.root / "workers" / member["name"])
            if variant is not None:
                member["variant"] = variant
                member["variant_name"] = variant_name(variant, variants)
            if status.get("error"):
                member["error"] = status["error"]
        snapshot["workers"] = statuses
        snapshot["pressure"] = supervisor.pressure(statuses)
        snapshot["concurrency_limit"] = supervisor.max_concurrent_workers
        runs = self._runs()
        active = runs.active()
        snapshot["run"] = None if active is None else {
            key: active[key] for key in ("number", "name", "started_at", "status")}
        failures = runs.leave_failures()
        hidden = runs.hidden_names()
        for member in snapshot["members"]:
            if member["name"] in failures:
                member["leave_error"] = failures[member["name"]]
            member["hidden"] = member["name"] in hidden
        snapshot["operation"] = (None if self._reroll_operation is None
                                 else {**self._reroll_operation,
                                       "results": list(self._reroll_operation["results"])})
        snapshot["variant_comparison"] = compare(self.root / "workers", variants)
        return snapshot

    def build_route_store(self) -> Any:
        """One coordinator-owned route store shared by all local workers."""
        from fleet.build_route_store import BuildRouteStore
        return BuildRouteStore(self.root)

    def strategy_library(self) -> Any:
        from fleet.strategy_library import StrategyLibrary
        return StrategyLibrary(self.root)

    def assign_strategy(self, *, expected_revision: int, strategy_id: str,
                        strategy_version: int, workers: list[dict[str, str]]) -> Any:
        """Pin one saved version for verified visible accounts in one publication."""
        from dataclasses import replace
        import db as bot_db
        from fleet.build_route import StrategyAssignment
        from fleet.build_route_store import RouteConflict
        from web.account_catalog import registered_worker

        saved = self.strategy_library().version(strategy_id, strategy_version)
        store = self.build_route_store()
        current = store.read()
        if current.revision != expected_revision:
            raise RouteConflict(current)
        visible = ({member["name"] for member in self._manual_pool().members()}
                   - self._runs().hidden_names())
        names = [worker.get("worker") for worker in workers]
        if not names or len(names) != len(set(names)):
            raise ValueError("assignment requires distinct visible workers")
        assignments = dict(current.assignments)
        overrides = dict(current.overrides)
        for worker in workers:
            name, account_id = worker.get("worker"), worker.get("account_id")
            if not isinstance(name, str) or name not in visible:
                raise ValueError("assignment requires a visible active pool member")
            registration = registered_worker(self.root / "workers" / name)
            if (not account_id or registration is None or registration.account_id != account_id
                    or bot_db.bound_account(registration.db_path) != account_id):
                raise ValueError(f"route_account_binding_changed:{name}")
            assignments[name] = StrategyAssignment.from_dict({
                "account_id": account_id, "strategy_id": saved["id"],
                "strategy_version": saved["version"], "strategy_name": saved["name"],
                "baseline": saved["baseline"],
            })
            overrides.pop(name, None)
        return store.publish(replace(current, assignments=assignments, overrides=overrides),
                             expected_revision, "operator")

    def build_route_validate_bindings(self, route: Any) -> None:
        """Reject a draft bound to an account that has since been replaced."""
        import db as bot_db
        from web.account_catalog import registered_worker
        # Assignment publication belongs to assign_strategy: it resolves the
        # canonical immutable version and verifies visible pool membership.
        # Ordinary route edits may carry existing snapshots without depending
        # on library availability, but cannot invent or rewrite them.
        if route.assignments != self.build_route_store().read().assignments:
            raise ValueError("strategy assignments must be published through the assignment endpoint")
        for name, override in {**route.overrides, **route.assignments}.items():
            registration = registered_worker(self.root / "workers" / name)
            if (registration is None or registration.account_id != override.account_id
                    or bot_db.bound_account(registration.db_path) != override.account_id):
                raise ValueError(f"route_account_binding_changed:{name}")

    def build_route_rebind_preview(self, route: Any, worker: str,
                                   old_account_id: str, new_account_id: str) -> dict[str, object]:
        """Show the inherited-to-rebound diff without changing the draft."""
        from dataclasses import asdict
        import db as bot_db
        from fleet.build_route import resolve_route
        from web.account_catalog import registered_worker

        override = route.overrides.get(worker)
        registration = registered_worker(self.root / "workers" / worker)
        if (override is None or override.account_id != old_account_id
                or registration is None or registration.account_id != new_account_id
                or bot_db.bound_account(registration.db_path) != new_account_id
                or old_account_id == new_account_id):
            raise ValueError(f"route_account_binding_changed:{worker}")
        return {
            "worker": worker, "old_account_id": old_account_id,
            "new_account_id": new_account_id,
            "patched_rules": sorted(override.patches),
            "old_effective": asdict(resolve_route(route, worker, old_account_id)),
            "new_effective": asdict(resolve_route(route, worker, new_account_id)),
        }

    def build_route_preview(self, draft: Any) -> dict[str, object]:
        """Evaluate current pool members only; never spend or persist."""
        from dataclasses import asdict, replace
        from fleet.build_route import resolve_route
        from fleet.build_route_eval import (RouteFacts, RouteEvaluation, ResourceEvaluation,
                                            ResourceStep, evaluate, evaluate_battle,
                                            evaluate_resources)
        from fleet.build_route_runtime import BuildRouteRuntime
        from fleet.build_route_preview_facts import load_preview_facts
        from web.account_catalog import registered_worker

        saved = self.build_route_store().read()
        proposed_document = replace(draft, revision=saved.revision + 1)
        members: list[dict[str, object]] = []
        current_names = {member["name"] for member in self._manual_pool().members()}
        workers = self.root / "workers"
        if workers.is_dir():
            for worker_root in sorted(workers.iterdir()):
                if (worker_root.name not in current_names or not worker_root.is_dir()
                        or worker_root.is_symlink()):
                    continue
                registration = registered_worker(worker_root)
                if registration is None or registration.account_id is None:
                    continue
                account_id = registration.account_id
                current_effective = resolve_route(saved, worker_root.name, account_id)
                proposed_effective = resolve_route(proposed_document, worker_root.name, account_id)
                runtime = BuildRouteRuntime(self.root, worker_root.name, account_id)
                evidence = load_preview_facts(self.root, worker_root, worker_root.name,
                                              account_id, registration.db_path)
                def lane_metadata(lane: Any) -> dict[str, object]:
                    return {"source": lane.source, "observed_at": lane.observed_at,
                            "reason": lane.reason, "status": lane.status}
                if evidence.workshop.facts is not None:
                    facts = evidence.workshop.facts
                    current = evaluate(current_effective, facts, runtime.pending())
                    proposed = evaluate(proposed_effective, facts, None)
                else:
                    facts = RouteFacts(account_id, worker_root.name)
                    reason = evidence.workshop.reason or "Workshop evidence unavailable"
                    current = RouteEvaluation.unknown(reason, facts)
                    proposed = RouteEvaluation.unknown(reason, facts)
                if evidence.battle.facts is not None:
                    battle_facts = evidence.battle.facts
                    current_battle = evaluate_battle(current_effective, battle_facts,
                                                    runtime.battle_pending(battle_facts, saved.revision))
                    proposed_battle = evaluate_battle(proposed_effective, battle_facts, None)
                else:
                    reason = evidence.battle.reason or "Battle evidence unavailable"
                    unknown = RouteEvaluation.unknown(reason, RouteFacts(account_id, worker_root.name))
                    current_battle = proposed_battle = unknown
                if evidence.resources.facts is not None and evidence.resources.status != "stale":
                    resource_facts = evidence.resources.facts
                    current_resources = evaluate_resources(current_effective, resource_facts)
                    proposed_resources = evaluate_resources(proposed_effective, resource_facts)
                else:
                    reason = evidence.resources.reason or "Resource evidence unavailable"
                    unknown_step = ResourceStep("unknown", "unknown", reason)
                    current_resources = proposed_resources = ResourceEvaluation(unknown_step, unknown_step)
                members.append({"worker": worker_root.name, "account_id": account_id,
                                "current": asdict(current), "proposed": asdict(proposed),
                                "current_battle": asdict(current_battle),
                                "proposed_battle": asdict(proposed_battle),
                                "current_resources": asdict(current_resources),
                                "proposed_resources": asdict(proposed_resources),
                                "evidence": {"workshop": lane_metadata(evidence.workshop),
                                             "battle": lane_metadata(evidence.battle),
                                             "resources": lane_metadata(evidence.resources)}})
        return {"saved_revision": saved.revision, "proposed_revision": saved.revision + 1,
                "members": members}

    def reroll_add(self, names: list[str]) -> dict[str, Any]:
        """Runs in the background: a stopped emulator is booted to check its Tower."""
        runs = self._runs()
        with self._reroll_dispatch_lock:
            if runs.busy or (self._reroll_start_thread is not None
                             and self._reroll_start_thread.is_alive()):
                raise ValueError("reroll_start_in_progress")
        self._manual_pool().validate_add(names)

        def operation() -> dict[str, Any]:
            runs.add_members(names)
            return {"results": []}

        return self._reroll_background("add", names[0] if len(names) == 1 else None, operation)

    def reroll_hide(self, names: list[str], hidden: bool) -> dict[str, Any]:
        """Hide or restore devices on the dashboard; workers and emulators are untouched."""
        self._runs().set_hidden(names, hidden)
        return self.reroll_snapshot()

    def reroll_remove(self, name: str) -> dict[str, Any]:
        """Take ``name`` out of the reroll and shut it down; it can be added back."""
        runs = self._runs()
        with self._reroll_dispatch_lock:
            if runs.busy or (self._reroll_start_thread is not None
                             and self._reroll_start_thread.is_alive()):
                raise ValueError("reroll_start_in_progress")
        runs.validate_remove(name)
        return self._reroll_background("remove", name, lambda: runs.remove_member(name))

    def _manual_supervisor(self) -> Any:
        if self._reroll_supervisor is None:
            from adbutils import AdbClient
            import config
            from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows
            from fleet.manual_air_worker import ManualAirWorker
            from fleet.manual_enrollment import enroll_manual_instance
            from fleet.reroll_journal import RerollJournal
            from fleet.reroll_supervisor import RerollSupervisor

            inventory = BlueStacksAirInventory(
                Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
                _EXECUTABLE, live_process_rows)

            def connect(endpoint: str) -> Any:
                client = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT)
                client.connect(endpoint, timeout=2.)
                device = client.device(serial=endpoint)
                device.shell("getprop ro.serialno")
                return device

            def protected_names() -> set[str]:
                return {"Tiramisu64_6"} | {item["source_instance"]
                                          for item in self.store.qualifications()}

            def enroll(member: dict[str, str], runtime: Any, attempt: Any) -> dict[str, Any]:
                journal = RerollJournal(self.root)
                journal.append(instance=member["name"], level="info",
                               kind="first_launch", message="Starting first launch verification")
                try:
                    registration = enroll_manual_instance(root=self.root, member=member,
                        runtime=runtime, attempt=attempt, inventory=inventory,
                        connect=connect, protected_names=protected_names())
                except Exception as exc:
                    journal.append(instance=member["name"], level="error",
                                   kind="first_launch_failed", message=str(exc))
                    raise
                journal.append(instance=member["name"], level="info",
                               kind="account_verified",
                               message=f"Account {registration['account_id']} verified after restart")
                return registration

            def start_instance(member: dict[str, str]) -> None:
                if member["name"] in protected_names():
                    raise ValueError("protected_template")
                ManualAirWorker(member["name"], member["endpoint"],
                                member["lease_id"], inventory=inventory).start(member["name"])

            try:
                limit = int((self.root / "reroll-concurrency.txt").read_text().strip())
            except FileNotFoundError:
                limit = 2
            self._reroll_supervisor = RerollSupervisor(
                self.root, pool_snapshot=self._manual_pool().snapshot,
                enroll=enroll, start_instance=start_instance,
                wait_booted=lambda member: wait_for_android_boot(
                    lambda: connect(member["endpoint"])),
                max_concurrent_workers=limit, start_stagger_seconds=1.0)
        return self._reroll_supervisor

    def reroll_set_concurrency(self, limit: int) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
            raise ValueError("invalid_reroll_capacity")
        supervisor = self._manual_supervisor()
        pressure = supervisor.pressure()
        if limit < pressure["running"] + pressure["starting"]:
            raise ValueError("pause_workers_before_reducing_capacity")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".reroll-concurrency.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(f"{limit}\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.root / "reroll-concurrency.txt")
        finally:
            temporary.unlink(missing_ok=True)
        supervisor.max_concurrent_workers = limit
        return self.reroll_snapshot()

    def _reroll_dispatch(self, operation: Any, *, exclusive_start: bool = False) -> dict[str, Any]:
        from threading import Thread
        from fleet.reroll_journal import RerollJournal

        def run() -> None:
            journal = RerollJournal(self.root)
            try:
                results = operation()
                for name, status in results.items():
                    journal.append(instance=name,
                                   level="error" if status["state"] == "failed" else "info",
                                   kind="worker_state", message=status.get("error", status["state"]))
            except Exception as exc:
                journal.append(instance="Fleet", level="error", kind="operation_failed",
                               message=str(exc))

        thread = Thread(target=run, daemon=True, name="reroll-operation")
        with self._reroll_dispatch_lock:
            if (exclusive_start and self._reroll_start_thread is not None
                    and self._reroll_start_thread.is_alive()):
                raise ValueError("reroll_start_in_progress")
            if exclusive_start:
                self._reroll_start_thread = thread
            thread.start()
        return self.reroll_snapshot()

    def reroll_start(self, name: str | None = None) -> dict[str, Any]:
        supervisor = self._manual_supervisor()
        return self._reroll_dispatch(
            (lambda: {name: supervisor.start(name)}) if name else supervisor.start_all,
            exclusive_start=True)

    def reroll_pause(self, name: str | None = None) -> dict[str, Any]:
        supervisor = self._manual_supervisor()
        return self._reroll_dispatch(
            (lambda: {name: supervisor.pause(name)}) if name else supervisor.pause_all)

    def _reroll_background(self, kind: str, target: str | None,
                           operation: Any) -> dict[str, Any]:
        """Run one exclusive long operation and expose its progress in the snapshot."""
        from datetime import datetime, timezone
        from threading import Thread
        from fleet.reroll_journal import RerollJournal

        record: dict[str, Any] = {
            "kind": kind, "state": "running", "target": target, "results": [], "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z")}

        def run() -> None:
            try:
                outcome = operation()
                record["results"] = list(outcome.get("results", [outcome]))
                record["state"] = "done"
            except Exception as exc:
                record["state"] = "failed"
                record["error"] = str(exc)
                RerollJournal(self.root).append(instance=target or "Fleet", level="error",
                                                kind="operation_failed", message=str(exc))

        thread = Thread(target=run, daemon=True, name=f"reroll-{kind}")
        with self._reroll_dispatch_lock:
            if self._reroll_start_thread is not None and self._reroll_start_thread.is_alive():
                raise ValueError("reroll_start_in_progress")
            self._reroll_start_thread = thread
            self._reroll_operation = record
            thread.start()
        return self.reroll_snapshot()

    def reroll_new_run(self, keep: list[str], add: list[str],
                       name: str | None = None) -> dict[str, Any]:
        from fleet.reroll_journal import RerollJournal

        runs = self._runs()
        with self._reroll_dispatch_lock:
            if runs.busy or (self._reroll_start_thread is not None
                             and self._reroll_start_thread.is_alive()):
                raise ValueError("reroll_start_in_progress")
        runs.validate_new(keep, add)

        def operation() -> dict[str, Any]:
            outcome = runs.start_new(keep, add, name)
            stopped = sum(1 for result in outcome["results"] if "error" not in result)
            RerollJournal(self.root).append(
                instance="Fleet", level="info", kind="run_started",
                message=f"Reroll #{outcome['number']} started: kept {len(keep)}, "
                        f"added {len(add)}, stopped {stopped}")
            for failed, error in outcome.get("variant_errors", {}).items():
                RerollJournal(self.root).append(
                    instance=failed, level="warn", kind="variant_assign_failed",
                    message=f"No opening variant assigned; plays the opening's own caps: {error}")
            return outcome

        return self._reroll_background("new_run", None, operation)

    def reroll_runs(self) -> dict[str, Any]:
        return {"runs": self._runs().summaries()}

    def reroll_journal(self, *, cursor: int | None = None,
                       instances: list[str] | None = None,
                       levels: list[str] | None = None) -> dict[str, Any]:
        from fleet.reroll_journal import RerollJournal
        entries = RerollJournal(self.root).list_entries(
            cursor=cursor, instances=instances, levels=levels)
        return {"entries": entries, "next_cursor": max(
            (entry["sequence"] for entry in entries), default=cursor or 0)}

    def setup_snapshot(self) -> dict[str, Any]:
        from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows

        result = self.store.snapshot()
        if self._build_error is not None:
            result["error"] = self._build_error
        try:
            inventory = BlueStacksAirInventory(
                Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
                _EXECUTABLE, live_process_rows)
            result["host"] = {"installed_prefix": inventory.installed_image_prefix(),
                              "instance_count": len(inventory.instances())}
        except Exception:
            result["host"] = {"unavailable": "host_inventory_unavailable"}
        return result

    def instances_snapshot(self) -> dict[str, Any]:
        """List exact Manager instances without opening any game app."""
        from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows

        inventory = BlueStacksAirInventory(
            Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
            _EXECUTABLE, live_process_rows)
        source = None
        settings = self.store.settings()
        if settings is not None:
            source = next((item["source_instance"] for item in self.store.qualifications()
                           if item["id"] == settings["qualification_id"]), None)
            if source is None:
                try:
                    saved = json.loads((self.store.qualification_root /
                                        settings["qualification_id"] / "m05.json").read_text(encoding="utf-8"))
                    candidate = saved.get("source_instance")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_]+", candidate):
                        source = candidate
                except (OSError, ValueError, TypeError):
                    pass
        elif len(self.store.qualifications()) == 1:
            source = self.store.qualifications()[0]["source_instance"]
        return {"instances": [{"name": item.name, "endpoint": item.endpoint,
                               "state": item.state, "template": item.name == source}
                              for item in inventory.instances()],
                "can_start": self.controller is not None}

    def start_instance(self, name: str) -> dict[str, Any]:
        """Start one exact Manager row; leave The Tower unopened."""
        from fleet.dashboard import FleetRequestError

        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise FleetRequestError("invalid_instance_name")
        controller = self._require_controller()
        snapshot = self.instances_snapshot()
        row = next((item for item in snapshot["instances"] if item["name"] == name), None)
        if row is None:
            raise FleetRequestError("instance_not_installed")
        if row["template"]:
            self.start_source()
        elif row["state"] == "stopped":
            controller.adapter.driver.start(name)
        elif row["state"] != "running":
            raise FleetRequestError("instance_state_unavailable")
        return self.instances_snapshot()

    def configure(self, *, capacity: int, name_prefix: str,
                  qualification_id: str) -> dict[str, Any]:
        from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows
        from fleet.clone_qualification import QualificationScope, qualification_is_current

        if (not isinstance(qualification_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", qualification_id)):
            raise FleetSetupError("qualification_not_available")
        proof_path = self.store.qualification_root / qualification_id / "m05.json"
        try:
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            scope = QualificationScope(**proof["scope"])
            if (proof.get("schema") != 2 or not scope.valid()
                    or not qualification_is_current(
                        proof_path, scope, now=proof["evaluated_at"] + 1.)):
                raise ValueError("qualification proof incomplete")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise FleetSetupError("qualification_not_available") from exc

        inventory = BlueStacksAirInventory(
            Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
            _EXECUTABLE, live_process_rows)
        self.store.configure(capacity=capacity, name_prefix=name_prefix,
                             qualification_id=qualification_id,
                             installed_prefix=inventory.installed_image_prefix(),
                             host_count=len(inventory.instances()))
        self._build()
        self._build_error = None
        return self.setup_snapshot()

    def _build(self) -> None:
        from adbutils import AdbClient
        import config
        from bluestacks import BlueStacksAdapter
        from fleet.account_observer import StagingAccountObserver
        from fleet.bluestacks_air import (
            BlueStacksAirDriver, BlueStacksAirInventory, LiveAirVersionObserver,
            MacOSMultiInstanceManager, _EXECUTABLE, live_process_rows,
        )
        from fleet.clone_qualification import (
            CloneCandidate, QualificationScope, probe_clone_worker,
            qualification_is_current,
        )
        from fleet.dashboard import FleetController
        from fleet.first_launch_account import create_first_launch_account, tower_is_unopened
        from fleet.identity import Attempt
        from fleet.runtime import WorkerRuntime

        settings = self.store.settings()
        if settings is None:
            self.controller = None
            return
        qualification_path = self.store.qualification_root / settings["qualification_id"] / "m05.json"
        record = json.loads(qualification_path.read_text(encoding="utf-8"))
        scope = QualificationScope(**record["scope"])
        source = scope.instance_config["source_instance"]
        qualification_dir = qualification_path.parent
        inventory = BlueStacksAirInventory(
            Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
            _EXECUTABLE, live_process_rows)
        driver = BlueStacksAirDriver(
            scope=scope, inventory_source=inventory, manager=MacOSMultiInstanceManager(),
            lineage_path=qualification_dir / "lineage.json",
            version_observer=LiveAirVersionObserver(inventory, source),
            fresh_prefix=settings["name_prefix"], timeout=120.,
        )
        adapter = BlueStacksAdapter(driver, staging_root=self.root / "locks")

        def connect(endpoint: str) -> Any:
            last_error: Exception | None = None
            for _ in range(10):
                try:
                    client = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT)
                    client.connect(endpoint, timeout=2.)
                    device = client.device(serial=endpoint)
                    device.shell("getprop ro.serialno")
                    return device
                except Exception as exc:
                    last_error = exc
                    time.sleep(1.)
            raise FleetSetupError("worker_adb_endpoint_unavailable") from last_error

        def qualified(path: Path, current: QualificationScope) -> bool:
            try:
                proof = json.loads(path.read_text(encoding="utf-8"))
                evaluated = proof["evaluated_at"]
                evidence = proof["source_evidence"]
                if (current != scope or not qualification_is_current(path, current,
                        now=evaluated + 1.) or evidence["endpoint"] != scope.instance_config["source_endpoint"]
                        or evidence["lease_id"] != scope.instance_config["source_lease"]):
                    return False
                designated = next(row for row in adapter.inventory() if row.name == source)
                return (designated.state == "running"
                        and designated.endpoint == evidence["endpoint"]
                        and designated.lease_id == evidence["lease_id"]
                        and tower_is_unopened(connect(designated.endpoint)))
            except Exception:
                return False

        def verify_clone(staged: Any, *, resume_after_consent: bool = False,
                         resume_verified: bool = False) -> dict[str, Any]:
            # Keep the attempt alive across the consent audit and restart proof.
            number = int(staged.name.rsplit("_", 1)[-1])
            runtime = WorkerRuntime.for_worker(self.root / "workers", staged.name, 10000 + number)
            runtime.ensure_directories()
            if resume_after_consent or resume_verified:
                journal = runtime.checkpoint_root / ".first-launch-account.json"
                existing = json.loads(journal.read_text(encoding="utf-8"))
                attempt = Attempt(**{key: existing[key] for key in (
                    "worker_id", "endpoint", "lease_id", "attempt_id", "generation", "created_at")})
            else:
                attempt = Attempt.new(staged.name, staged.endpoint, staged.lease_id, uuid4().hex)
            observer = StagingAccountObserver(runtime.evidence_root, endpoint=staged.endpoint,
                                              allowed_versions=frozenset({scope.game_version}))
            candidate = CloneCandidate(staged.name, runtime, attempt,
                                       lambda: connect(staged.endpoint), observer)
            registry = qualification_dir / "first-launch-registry.json"
            if resume_verified:
                audit = existing
            else:
                audit = create_first_launch_account(
                    runtime=runtime, attempt=attempt, adapter=adapter, instance=staged.name,
                    source_lineage=scope.source_lineage,
                    protected_ids=frozenset(record["account_ids"]),
                    connect=candidate.connect, observe=observer, registry=registry,
                    resume_after_consent=resume_after_consent,
                )
            proof = probe_clone_worker(adapter=adapter, candidate=candidate,
                                       account_id=audit["account_id"], scope=scope,
                                       navigate=True)
            return {"state": "verified", "first_launch_verified": True,
                    "consent_accepted": True, "recovery_verified": True,
                    "account_id": audit["account_id"], "endpoint": staged.endpoint,
                    "lease_id": staged.lease_id, "evidence_ref": proof["startup_evidence_ref"],
                    "recovery_evidence_ref": proof["evidence_ref"],
                    "identity_observed_at": proof["started_at"],
                    "recovered_at": proof["recovered_at"]}

        def register(staged: Any, proof: dict[str, Any], job_id: str) -> dict[str, Any]:
            runtime = WorkerRuntime.for_worker(
                self.root / "workers", staged.name,
                10000 + int(staged.name.rsplit("_", 1)[-1]))
            bindings = list(runtime.checkpoint_root.glob("[0-9a-f]" * 32 + ".json"))
            if len(bindings) != 1:
                raise ValueError("worker_identity_binding_missing_or_ambiguous")
            binding = json.loads(bindings[0].read_text(encoding="utf-8"))
            if (binding.get("account_id") != proof["account_id"]
                    or binding.get("endpoint") != staged.endpoint
                    or binding.get("lease_id") != staged.lease_id):
                raise ValueError("worker_identity_binding_changed")
            registration = {"state": "registered", "instance": staged.name,
                            "endpoint": staged.endpoint, "lease_id": staged.lease_id,
                            "account_id": proof["account_id"], "job_id": job_id,
                            "web_port": runtime.web_port, "binding": str(bindings[0])}
            path = runtime.root / "fleet-registration.json"
            temporary = runtime.root / f".registration.{uuid4().hex}.tmp"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(registration, file, sort_keys=True)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {**registration, "evidence_ref": str(path)}

        self.controller = FleetController(
            adapter, qualification_path=qualification_path,
            state_path=self.root / "jobs.json",
            policy=FleetPolicy(settings["capacity"], settings["name_prefix"]),
            qualification_gate=qualified, verify_first_launch_clone=verify_clone,
            register_worker=register,
        )
        self._verify_clone = verify_clone
        self._connect = connect

    def snapshot(self) -> dict[str, Any]:
        if self.controller is None:
            return {"sources": [], "jobs": [], "unavailable":
                    self._build_error or "fleet_not_configured"}
        return self.controller.snapshot()

    def start_source(self) -> dict[str, Any]:
        """Start the selected template by its exact Manager row, never Tower."""
        from fleet.dashboard import FleetRequestError
        controller = self._require_controller()
        driver = controller.adapter.driver
        scope = driver.scope
        if scope is None:
            raise FleetRequestError("qualified_source_missing")
        name = scope.instance_config["source_instance"]
        endpoint = scope.instance_config["source_endpoint"]
        lease = scope.instance_config["source_lease"]
        driver.manager.activate()
        digest, row = driver._exact_instance(name)
        if row.endpoint != endpoint or row.lease_id != lease:
            raise FleetRequestError("source_host_identity_changed")
        if row.state == "stopped":
            if driver._endpoint_has_state(endpoint, present=True):
                raise FleetRequestError("source_endpoint_state_ambiguous")
            control = driver._row_control(name, action="Start")
            again_digest, again = driver._exact_instance(name, required_state="stopped")
            if (again_digest != digest or again.endpoint != endpoint
                    or again.lease_id != lease
                    or driver._row_control(name, action="Start") != control):
                raise FleetRequestError("source_manager_row_changed")
            driver.manager.press(control.window_id, control.point, "Start")
            driver._wait_for(name, digest=digest, endpoint=endpoint,
                             lease=lease, state="running")
        elif row.state != "running":
            raise FleetRequestError("source_state_unavailable")
        deadline = time.monotonic() + 30.
        while controller._source()[0] != name:
            if time.monotonic() >= deadline:
                raise FleetRequestError("source_unopened_evidence_unavailable")
            time.sleep(1.)
        return self.snapshot()

    def resume_unopened_clone(self, job_id: str, index: int) -> dict[str, Any]:
        """Resume an exact clone from its durable first-launch audit."""
        from fleet.dashboard import FleetRequestError
        from fleet.first_launch_account import tower_is_unopened

        controller = self._require_controller()
        with controller._lock:
            job = next((item for item in controller._jobs if item["id"] == job_id), None)
            if job is None or not 0 <= index < len(job["clones"]):
                raise FleetRequestError("unknown_provisioning_target")
            target = job["clones"][index]
            if (job["mode"] != "clone" or target["state"] != "quarantined"
                    or target["reason"] not in {"first_launch_clone_requires_review",
                                                "resume_first_launch_requires_review",
                                                "server_restarted_during_provisioning"}
                    or controller._source()[0] != job["source"]):
                raise FleetRequestError("clone_not_safe_to_resume")
            matches = [row for row in controller.adapter.inventory()
                       if row.name == target["instance"]]
            if (len(matches) != 1 or matches[0].state != "running"
                    or matches[0].endpoint != target.get("endpoint")
                    or matches[0].lease_id != target.get("lease_id")
                    or matches[0].source_lineage != controller.adapter.driver.scope.source_lineage):
                raise FleetRequestError("clone_host_identity_changed")
            clone = matches[0]
            journal = self.root / "workers" / clone.name / "checkpoints" / ".first-launch-account.json"
            try:
                audit = json.loads(journal.read_text(encoding="utf-8"))
                actions = [row.get("action") for row in audit.get("evidence", [])]
                resume_after_consent = actions == ["i_agree"]
                resume_verified = audit.get("state") == "verified"
                if (audit.get("state") not in {"quarantined", "pending_i_agree", "verified"}
                        or audit.get("instance") != clone.name
                        or audit.get("endpoint") != clone.endpoint
                        or audit.get("lease_id") != clone.lease_id
                        or (not resume_after_consent and not resume_verified and actions != [])
                        or (resume_after_consent and audit.get("state") == "quarantined"
                            and audit.get("reason") not in {"closed", "first-launch onboarding did not reach Tower home"})
                        or (not resume_after_consent and not resume_verified
                            and not tower_is_unopened(
                            self._connect(clone.endpoint)))):
                    raise ValueError("first launch has begun")
                if resume_verified:
                    proof_record = json.loads(controller.qualification_path.read_text(encoding="utf-8"))
                    registry = json.loads((controller.qualification_path.parent /
                                           "first-launch-registry.json").read_text(encoding="utf-8"))
                    binding = (self.root / "workers" / clone.name / "checkpoints" /
                               f"{audit['generation']}.json")
                    bound = json.loads(binding.read_text(encoding="utf-8"))
                    account = audit.get("account_id")
                    if (not account or account in proof_record["account_ids"]
                            or "i_agree" not in actions
                            or audit.get("app_version") != controller.adapter.driver.scope.game_version
                            or registry.get(clone.name, {}).get("account_id") != account
                            or registry[clone.name].get("endpoint") != clone.endpoint
                            or bound.get("account_id") != account
                            or bound.get("endpoint") != clone.endpoint
                            or bound.get("lease_id") != clone.lease_id):
                        raise ValueError("verified identity binding changed")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise FleetRequestError("clone_first_launch_state_changed") from exc
            if not resume_after_consent and not resume_verified:
                archived = journal.with_name(f".first-launch-quarantined-{uuid4().hex}.json")
                os.replace(journal, archived)
            controller._update(job, index, state="verifying", reason="clone_proof_retry_pending")
        try:
            proof = self._verify_clone(clone, resume_after_consent=resume_after_consent,
                                       resume_verified=resume_verified)
            if (proof.get("state") != "verified" or proof.get("first_launch_verified") is not True
                    or proof.get("consent_accepted") is not True
                    or proof.get("recovery_verified") is not True
                    or proof.get("endpoint") != clone.endpoint
                    or proof.get("lease_id") != clone.lease_id
                    or not proof.get("account_id") or not proof.get("evidence_ref")
                    or not proof.get("recovery_evidence_ref")
                    or proof["evidence_ref"] == proof["recovery_evidence_ref"]
                    or not isinstance(proof.get("identity_observed_at"), (int, float))
                    or not isinstance(proof.get("recovered_at"), (int, float))
                    or not job["requested_at"] < proof["identity_observed_at"]
                    < proof["recovered_at"] <= time.time()
                    or time.time() - proof["identity_observed_at"] > 300.):
                raise ValueError("clone_first_launch_proof_incomplete")
            if controller._source()[0] != job["source"]:
                raise ValueError("qualified_source_changed")
            controller._register(job, index, clone, proof, "first_launch_identity_verified")
        except Exception as exc:
            controller._update(job, index, state="quarantined",
                               reason="resume_first_launch_requires_review")
            raise FleetRequestError("resume_first_launch_requires_review") from exc
        return controller.request_evidence(job_id)

    def _require_controller(self) -> Any:
        if self.controller is None:
            from fleet.dashboard import FleetRequestError
            raise FleetRequestError("fleet_not_configured")
        return self.controller

    def preview(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and args[0] == "fresh":
            from fleet.dashboard import FleetRequestError
            raise FleetRequestError("fresh_first_launch_not_configured")
        return self._require_controller().preview(*args, **kwargs)

    def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("mode") == "fresh":
            from fleet.dashboard import FleetRequestError
            raise FleetRequestError("fresh_first_launch_not_configured")
        return self._require_controller().request(*args, **kwargs)

    def run_pending(self, job_id: str) -> None:
        self._require_controller().run_pending(job_id)

    def qualification_evidence(self) -> dict[str, Any]:
        return self._require_controller().qualification_evidence()

    def request_evidence(self, job_id: str) -> dict[str, Any]:
        return self._require_controller().request_evidence(job_id)

    def resolve(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._require_controller().resolve(*args, **kwargs)
