"""Fail-closed dashboard boundary for qualified BlueStacks clone staging."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from bluestacks import BlueStacksAdapter, HostCapabilityError, HostIdentityError, HostInstance, ProvisionMode
from fleet.clone_qualification import EVIDENCE_TTL, QualificationScope, qualification_is_current


class FleetRequestError(ValueError):
    """A fleet action was refused before changing the host."""


class FleetController:
    """Stage through the host driver; publish only evidence-backed state.

    ``verify_clone`` is an explicit R00 plus fresh recovery integration. A
    missing integration leaves a staged instance blocked, never usable.
    """

    def __init__(self, adapter: BlueStacksAdapter, *, qualification_path: Path,
                 state_path: Path, qualification_gate: Callable[[Path, QualificationScope], bool] = qualification_is_current,
                 verify_clone: Callable[[HostInstance, str], dict[str, Any]] | None = None) -> None:
        self.adapter = adapter
        self.qualification_path = Path(qualification_path)
        self.state_path = Path(state_path)
        self.qualification_gate = qualification_gate
        self.verify_clone = verify_clone
        self._lock = threading.RLock()
        self._state_error = False
        self._jobs: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("invalid jobs")
            for job in rows:
                for clone in job["clones"]:
                    if clone["state"] in {"queued", "staging", "verifying"}:
                        clone.update(state="quarantined", reason="server_restarted_during_provisioning")
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
            capabilities = (getattr(driver, "supports_clone_staging", False) is True
                            and getattr(driver, "supports_m05_live_qualification", False) is True
                            and self.adapter.supports_lifecycle)
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
        with self._lock:
            name, reason = self._source()
            try:
                scope = self.adapter.driver.qualification_scope()
                configured = scope.instance_config.get("source_instance") if scope else None
            except Exception:
                configured = None
            return {"sources": [{"instance": name or configured or "Unavailable",
                                 "state": "qualified" if name else "blocked", "reason": reason}],
                    "jobs": json.loads(json.dumps(self._jobs[-20:]))}

    def request(self, source: str, count: int) -> dict[str, Any]:
        with self._lock:
            qualified, reason = self._source()
            if qualified is None or source != qualified:
                raise FleetRequestError(reason if qualified is None else "source_not_qualified")
            if not 1 <= count <= 5:
                raise FleetRequestError("clone_count_must_be_1_to_5")
            if any(clone["state"] in {"queued", "staging", "verifying", "quarantined"}
                   for job in self._jobs for clone in job["clones"]):
                raise FleetRequestError("provisioning_or_quarantine_requires_review")
            job = {"id": uuid4().hex, "source": source, "requested_at": time.time(),
                   "clones": [{"state": "queued", "reason": "awaiting_staging"}
                              for _ in range(count)]}
            self._jobs.append(job)
            self._jobs = self._jobs[-20:]
            self._save()
            return json.loads(json.dumps(job))

    def _update(self, job: dict[str, Any], index: int, **values: Any) -> None:
        with self._lock:
            job["clones"][index].update(values)
            self._save()

    def run_pending(self, job_id: str) -> None:
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
        if job is None:
            return
        try:
            record = json.loads(self.qualification_path.read_text(encoding="utf-8"))
            source_id = record["account_ids"][0]
            if record["source_instance"] != job["source"] or not isinstance(source_id, str) or not source_id:
                raise ValueError("source_identity_evidence_missing")
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            for index, clone in enumerate(job["clones"]):
                if clone["state"] == "queued":
                    self._update(job, index, state="blocked", reason="source_identity_evidence_missing")
            return
        for index, clone in enumerate(job["clones"]):
            if clone["state"] != "queued":
                continue
            source, reason = self._source()
            if source != job["source"]:
                self._update(job, index, state="blocked", reason="qualification_scope_changed")
                continue
            try:
                inventory = self.adapter.inventory()
                match = re.fullmatch(r"(.+_)([1-9][0-9]*)", source)
                prefix = match.group(1) if match else f"{source}_"
                numbers = [int(found.group(1)) for row in inventory
                           if (found := re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", row.name))]
                name = f"{prefix}{max(numbers, default=0) + 1}"
                self._update(job, index, state="staging", reason="host_creation_in_progress", instance=name)
                expected_scope = self.adapter.driver.qualification_scope()
                if expected_scope is None:
                    raise ValueError("qualification_scope_changed")
                with self.adapter.staging_lease():
                    staged = self.adapter.provision(name, mode=ProvisionMode.CLONE, source=source)
                if (staged.name != name or staged.source_lineage != expected_scope.source_lineage
                        or not staged.endpoint or not staged.lease_id):
                    raise ValueError("clone_host_identity_unbound")
                self._update(job, index, state="verifying", reason="identity_reset_and_evidence_pending",
                             endpoint=staged.endpoint)
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
                self._update(job, index, state="ready", reason="identity_and_recovery_verified",
                             account_id=proof["account_id"])
            except (HostCapabilityError, HostIdentityError, ValueError) as exc:
                self._update(job, index, state="quarantined", reason=str(exc) or "provisioning_uncertain")
            except Exception:
                self._update(job, index, state="quarantined", reason="operator_review_required")
