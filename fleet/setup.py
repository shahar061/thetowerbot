"""Private, host-bound Fleet setup choices for the local dashboard."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fleet.dashboard import FleetPolicy


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
        if self.store.settings() is not None:
            try:
                self._build()
            except (OSError, ValueError, TypeError, KeyError):
                self._build_error = "saved_qualification_unavailable"

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
