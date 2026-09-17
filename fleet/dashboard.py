"""Fail-closed dashboard boundary for qualified BlueStacks clone staging."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from bluestacks import BlueStacksAdapter, HostCapabilityError, HostIdentityError, HostInstance, ProvisionMode
from fleet.clone_qualification import EVIDENCE_TTL, QualificationScope, qualification_is_current
from fleet.identity import Attempt


class FleetRequestError(ValueError):
    """A fleet action was refused before changing the host."""


@dataclass(frozen=True)
class FleetPolicy:
    capacity: int
    name_prefix: str

    def __post_init__(self) -> None:
        if (not isinstance(self.capacity, int) or isinstance(self.capacity, bool)
                or self.capacity < 1 or self.capacity > 100):
            raise ValueError("fleet capacity must be explicitly set from 1 to 100")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*_", self.name_prefix):
            raise ValueError("fleet name prefix must be an explicit safe prefix ending in underscore")


class FleetController:
    """Stage through the host driver; publish only evidence-backed state.

    Clone verification is an explicit first-launch or legacy R00 integration.
    A missing integration leaves a staged instance blocked, never usable.
    """

    def __init__(self, adapter: BlueStacksAdapter, *, qualification_path: Path,
                 state_path: Path, policy: FleetPolicy,
                 qualification_gate: Callable[[Path, QualificationScope], bool] = qualification_is_current,
                 verify_clone: Callable[[HostInstance, str], dict[str, Any]] | None = None,
                 verify_first_launch_clone: Callable[[HostInstance], dict[str, Any]] | None = None,
                 verify_fresh: Callable[[HostInstance], dict[str, Any]] | None = None,
                 register_worker: Callable[[HostInstance, dict[str, Any], str], dict[str, Any]] | None = None) -> None:
        self.adapter = adapter
        self.policy = policy
        self.qualification_path = Path(qualification_path)
        self.state_path = Path(state_path)
        self.qualification_gate = qualification_gate
        self.verify_clone = verify_clone
        self.verify_first_launch_clone = verify_first_launch_clone
        self.verify_fresh = verify_fresh
        self.register_worker = register_worker
        self._lock = threading.RLock()
        self._state_error = False
        self._jobs: list[dict[str, Any]] = self._load()
        if getattr(self, "_recovered_interrupted", False):
            self._save()

    def _load(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("invalid jobs")
            for job in rows:
                for clone in job["clones"]:
                    if clone["state"] in {"queued", "staging", "verifying"}:
                        self._recovered_interrupted = True
                        clone.update(state="quarantined", reason="server_restarted_during_provisioning")
                        clone.setdefault("steps", []).append({"at": time.time(), "state": "quarantined",
                                                               "reason": "server_restarted_during_provisioning"})
            return rows[-20:]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, KeyError):
            self._state_error = True
            return []

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self._jobs, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _source(self) -> tuple[str | None, str]:
        if self._state_error:
            return None, "fleet_audit_unreadable"
        driver = self.adapter.driver
        try:
            scope = getattr(driver, "qualification_scope", lambda: None)()
            attest = getattr(driver, "attest_clone_capabilities", None)
            capabilities = (attest() is True if callable(attest) else
                            (getattr(driver, "supports_clone_staging", False) is True
                             and getattr(driver, "supports_m05_live_qualification", False) is True
                             and self.adapter.supports_lifecycle))
            qualified = isinstance(scope, QualificationScope) and self.qualification_gate(self.qualification_path, scope)
        except Exception:
            return None, "live_host_evidence_unavailable"
        if not qualified or not capabilities:
            return None, "live_qualification_missing_stale_or_changed"
        name = scope.instance_config.get("source_instance")
        if not name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            return None, "qualified_source_missing"
        return name, "qualified"

    def snapshot(self) -> dict[str, Any]:
        name, reason = self._source()
        scope = getattr(self.adapter.driver, "scope", None)
        configured = (scope.instance_config.get("source_instance")
                      if isinstance(scope, QualificationScope) else None)
        try:
            used = len(self.adapter.inventory())
        except Exception:
            used = self.policy.capacity
        evidence_at = None
        if name is not None:
            try:
                record = json.loads(self.qualification_path.read_text(encoding="utf-8"))
                evidence_at = record.get("evaluated_at")
            except (OSError, ValueError, TypeError):
                pass
        with self._lock:
            jobs = json.loads(json.dumps(self._jobs[-20:]))
        return {"capacity": {"limit": self.policy.capacity, "used": used,
                             "available": max(0, self.policy.capacity - used)},
                "sources": [{"instance": name or configured or "Unavailable",
                             "state": "parallel_session_qualified" if name else "blocked",
                             "reason": reason, "evidence_at": evidence_at,
                             "evidence_url": "/api/fleet/qualification" if name else None}],
                "jobs": [{**job, "manager_result_url":
                          f"/api/fleet/requests/{job['id']}"} for job in jobs]}

    def qualification_evidence(self) -> dict[str, Any]:
        with self._lock:
            source, reason = self._source()
            if source is None:
                raise FleetRequestError(reason)
            try:
                record = json.loads(self.qualification_path.read_text(encoding="utf-8"))
                return {"state": "parallel_session_qualified", "source_instance": source,
                        "clone_instances": record["clone_instances"],
                        "evaluated_at": record["evaluated_at"],
                        "source_evidence_ref": record["source_evidence"].get("evidence_ref"),
                        "worker_evidence_refs": [proof["evidence_ref"]
                                                 for proof in record["worker_proofs"]]}
            except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                raise FleetRequestError("qualification_evidence_unavailable") from exc

    def request_evidence(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None:
                raise FleetRequestError("unknown_provisioning_request")
            return json.loads(json.dumps(job))

    def preview(self, mode: str, source: str | None, count: int, *,
                ignore_target: tuple[str, int] | None = None) -> dict[str, Any]:
        with self._lock:
            if mode not in {"fresh", "clone"} or not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 5:
                raise FleetRequestError("invalid_provisioning_request")
            if mode == "clone":
                qualified, reason = self._source()
                if qualified is None or source != qualified:
                    raise FleetRequestError(reason if qualified is None else "source_not_qualified")
                required_prefix = getattr(self.adapter.driver, "required_clone_prefix", None)
                if required_prefix is not None and required_prefix != self.policy.name_prefix:
                    raise FleetRequestError("configured_prefix_does_not_match_host")
            elif source is not None:
                raise FleetRequestError("fresh_provisioning_has_no_source")
            elif getattr(self.adapter.driver, "supports_fresh_provision", False) is not True:
                raise FleetRequestError("fresh_host_capability_unavailable")
            elif (required_prefix := getattr(self.adapter.driver, "required_fresh_prefix", None)) is not None \
                    and required_prefix != self.policy.name_prefix:
                raise FleetRequestError("configured_prefix_does_not_match_host")
            try:
                inventory = self.adapter.inventory()
            except Exception as exc:
                raise FleetRequestError("host_inventory_unavailable") from exc
            if len(inventory) + count > self.policy.capacity:
                raise FleetRequestError("fleet_capacity_exceeded")
            names = {row.name for row in inventory}
            names.update(clone.get("instance") for job in self._jobs
                         for index, clone in enumerate(job["clones"])
                         if (job["id"], index) != ignore_target)
            numbers = [int(found.group(1)) for name in names if isinstance(name, str)
                       if (found := re.fullmatch(re.escape(self.policy.name_prefix) + r"([1-9][0-9]*)", name))]
            next_number = max(numbers, default=0) + 1
            targets = [f"{self.policy.name_prefix}{next_number + index}" for index in range(count)]
            if any(name in names for name in targets):
                raise FleetRequestError("duplicate_instance_name")
            return {"mode": mode, "source": source, "count": count,
                    "targets": targets, "state": "eligible"}

    def request(self, source: str | None, count: int, *, mode: str = "clone",
                expected_targets: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            preview = self.preview(mode, source, count)
            if expected_targets is not None and expected_targets != preview["targets"]:
                raise FleetRequestError("provisioning_preview_changed")
            if any(clone["state"] in {"queued", "staging", "verifying", "quarantined"}
                   for job in self._jobs for clone in job["clones"]):
                raise FleetRequestError("provisioning_or_quarantine_requires_review")
            job = {"id": uuid4().hex, "source": source, "requested_at": time.time(),
                   "mode": mode, "clones": [{"instance": name, "state": "queued",
                                          "reason": "awaiting_staging", "steps": [
                                              {"at": time.time(), "state": "queued",
                                               "reason": "awaiting_staging"}]}
                                                 for name in preview["targets"]]}
            self._jobs.append(job)
            self._jobs = self._jobs[-20:]
            self._save()
            return json.loads(json.dumps(job))

    def _update(self, job: dict[str, Any], index: int, **values: Any) -> None:
        with self._lock:
            job["clones"][index].update(values)
            if "state" in values:
                job["clones"][index].setdefault("steps", []).append(
                    {"at": time.time(), "state": values["state"],
                     "reason": values.get("reason", ""),
                     "endpoint": values.get("endpoint")})
            self._save()

    def resolve(self, job_id: str, index: int, action: str) -> dict[str, Any]:
        """Resolve only the exact interrupted target; never replay an uncertain host create."""
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None or not 0 <= index < len(job["clones"]) or action not in {"retry", "quarantine"}:
                raise FleetRequestError("unknown_provisioning_target")
            target = job["clones"][index]
            if target["state"] not in {"blocked", "quarantined"}:
                raise FleetRequestError("target_does_not_require_review")
            if action == "retry":
                try:
                    if any(row.name == target["instance"] for row in self.adapter.inventory()):
                        raise FleetRequestError("existing_host_instance_requires_quarantine")
                except FleetRequestError:
                    raise
                except Exception as exc:
                    raise FleetRequestError("host_inventory_unavailable") from exc
                expected = self.preview(job["mode"], job["source"], 1,
                                        ignore_target=(job_id, index))["targets"][0]
                if expected != target["instance"]:
                    raise FleetRequestError("retry_target_name_changed")
                if self._source()[0] != job["source"] and job["mode"] == "clone":
                    raise FleetRequestError("qualification_scope_changed")
                self._update(job, index, state="queued", reason="operator_retry_requested")
            else:
                self._update(job, index, state="quarantined", reason="operator_quarantined")
            return json.loads(json.dumps(job))

    def run_pending(self, job_id: str) -> None:
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
        if job is None:
            return
        source_id: str | None = None
        first_launch_clone = False
        if job["mode"] == "clone":
            try:
                record = json.loads(self.qualification_path.read_text(encoding="utf-8"))
                first_launch_clone = record.get("schema") == 2
                if first_launch_clone:
                    if (record.get("source_instance") != job["source"]
                            or record.get("source_evidence", {}).get("tower_unopened") is not True):
                        raise ValueError("source_template_evidence_missing")
                else:
                    source_id = record["account_ids"][0]
                if (record["source_instance"] != job["source"]
                        or not first_launch_clone and
                        (not isinstance(source_id, str) or not source_id)):
                    raise ValueError("source_identity_evidence_missing")
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                for index, clone in enumerate(job["clones"]):
                    if clone["state"] == "queued":
                        self._update(job, index, state="blocked", reason="source_identity_evidence_missing")
                return
        for index, clone in enumerate(job["clones"]):
            if clone["state"] != "queued":
                continue
            source, _ = self._source()
            if job["mode"] == "clone" and source != job["source"]:
                self._update(job, index, state="blocked", reason="qualification_scope_changed")
                continue
            try:
                inventory = self.adapter.inventory()
                name = clone["instance"]
                if (any(row.name == name for row in inventory)
                        or len(inventory) >= self.policy.capacity):
                    raise ValueError("duplicate_name_or_capacity_changed")
                self._update(job, index, state="staging", reason="host_creation_in_progress", instance=name)
                if job["mode"] == "clone":
                    expected_scope = self.adapter.driver.qualification_scope()
                    if expected_scope is None:
                        raise ValueError("qualification_scope_changed")
                    with self.adapter.staging_lease():
                        staged = self.adapter.provision(name, mode=ProvisionMode.CLONE, source=source)
                else:
                    expected_scope = None
                    staged = self.adapter.provision(name, mode=ProvisionMode.FRESH)
                if (staged.name != name or not staged.endpoint or not staged.lease_id
                        or (expected_scope is not None
                            and staged.source_lineage != expected_scope.source_lineage)):
                    raise ValueError("clone_host_identity_unbound")
                designated = self.adapter.designated(name, Attempt.new(
                    name, staged.endpoint, staged.lease_id, job["id"]))
                if job["mode"] == "clone" and designated.state == "stopped":
                    self.adapter.start(name, Attempt.new(
                        name, staged.endpoint, staged.lease_id, job["id"]))
                    designated = self.adapter.designated(name, Attempt.new(
                        name, staged.endpoint, staged.lease_id, job["id"]))
                if designated.state != "running" and not (job["mode"] == "fresh"
                                                           and designated.state == "stopped"):
                    raise ValueError("created_endpoint_state_unproven")
                self._update(job, index, state="verifying", reason=(
                            "first_launch_identity_pending" if first_launch_clone or job["mode"] == "fresh"
                            else "identity_reset_and_evidence_pending"),
                             endpoint=staged.endpoint, lease_id=staged.lease_id)
                if job["mode"] == "fresh":
                    if self.verify_fresh is None:
                        self._update(job, index, state="blocked", reason="first_launch_identity_required")
                        continue
                    try:
                        proof = self.verify_fresh(staged)
                    except Exception:
                        self._update(job, index, state="quarantined", reason="first_launch_identity_requires_review")
                        continue
                    if (proof.get("state") != "verified" or not proof.get("first_launch_verified")
                            or proof.get("endpoint") != staged.endpoint
                            or proof.get("lease_id") != staged.lease_id
                            or not proof.get("account_id") or not proof.get("evidence_ref")):
                        raise ValueError("first_launch_identity_evidence_incomplete")
                    self._register(job, index, staged, proof, "first_launch_identity_verified")
                    continue
                if first_launch_clone:
                    if self.verify_first_launch_clone is None:
                        self._update(job, index, state="blocked", reason="first_launch_clone_verifier_required")
                        continue
                    try:
                        proof = self.verify_first_launch_clone(staged)
                    except Exception:
                        self._update(job, index, state="quarantined",
                                     reason="first_launch_clone_requires_review")
                        continue
                    valid_first_launch = (
                        proof.get("state") == "verified"
                        and proof.get("first_launch_verified") is True
                        and proof.get("consent_accepted") is True
                        and proof.get("recovery_verified") is True
                        and proof.get("account_id")
                        and proof.get("endpoint") == staged.endpoint
                        and proof.get("lease_id") == staged.lease_id
                        and proof.get("evidence_ref")
                        and proof.get("recovery_evidence_ref")
                        and proof.get("recovery_evidence_ref") != proof.get("evidence_ref")
                        and isinstance(proof.get("identity_observed_at"), (int, float))
                        and isinstance(proof.get("recovered_at"), (int, float))
                        and job["requested_at"] < proof["identity_observed_at"]
                        < proof["recovered_at"] <= time.time()
                        and time.time() - proof["identity_observed_at"] <= EVIDENCE_TTL
                    )
                    if not valid_first_launch:
                        raise ValueError("first_launch_clone_evidence_incomplete")
                    if self._source()[0] != source:
                        self._update(job, index, state="blocked",
                                     reason="qualification_scope_changed_after_verification")
                        continue
                    self._register(job, index, staged, proof, "first_launch_identity_verified")
                    continue
                if self.verify_clone is None:
                    self._update(job, index, state="blocked", reason="identity_reset_and_evidence_required")
                    continue
                try:
                    proof = self.verify_clone(staged, source_id)
                except Exception:
                    self._update(job, index, state="quarantined",
                                 reason="identity_confirmation_or_quarantine_requires_review")
                    continue
                valid = (proof.get("state") == "verified" and proof.get("identity_reset") is True
                         and proof.get("recovery_verified") is True and proof.get("account_id")
                         and proof.get("account_id") != source_id
                         and proof.get("source_account_id") == source_id
                         and proof.get("endpoint") == staged.endpoint
                         and proof.get("lease_id") == staged.lease_id
                         and proof.get("evidence_ref") and proof.get("recovery_evidence_ref")
                         and proof.get("recovery_evidence_ref") != proof.get("evidence_ref")
                         and isinstance(proof.get("identity_observed_at"), (int, float))
                         and isinstance(proof.get("recovered_at"), (int, float))
                         and job["requested_at"] < proof["identity_observed_at"] < proof["recovered_at"] <= time.time()
                         and time.time() - proof["identity_observed_at"] <= EVIDENCE_TTL)
                if not valid:
                    raise ValueError("identity_or_recovery_evidence_incomplete")
                if self._source()[0] != source:
                    self._update(job, index, state="blocked", reason="qualification_scope_changed_after_verification")
                    continue
                self._register(job, index, staged, proof, "identity_and_recovery_verified")
            except HostCapabilityError:
                self._update(job, index, state="quarantined", reason="host_capability_or_result_unavailable")
            except HostIdentityError:
                self._update(job, index, state="quarantined", reason="host_identity_unproven")
            except ValueError:
                self._update(job, index, state="quarantined", reason="provisioning_postcondition_unproven")
            except Exception:
                self._update(job, index, state="quarantined", reason="operator_review_required")

    def _register(self, job: dict[str, Any], index: int, staged: HostInstance,
                  proof: dict[str, Any], reason: str) -> None:
        current = self.adapter.designated(staged.name, Attempt.new(
            staged.name, staged.endpoint, staged.lease_id, job["id"]))
        if current.state != "running":
            raise ValueError("worker_endpoint_not_running")
        if self.register_worker is None:
            self._update(job, index, state="blocked", reason="worker_registration_required")
            return
        registered = self.register_worker(staged, proof, job["id"])
        if (registered.get("state") != "registered" or registered.get("instance") != staged.name
                or registered.get("endpoint") != staged.endpoint
                or registered.get("lease_id") != staged.lease_id
                or registered.get("account_id") != proof["account_id"]
                or not registered.get("evidence_ref")):
            raise ValueError("worker_registration_incomplete")
        self._update(job, index, state="ready", reason=reason, account_id=proof["account_id"],
                     evidence_ref=proof["evidence_ref"],
                     registration_evidence_ref=registered["evidence_ref"])
