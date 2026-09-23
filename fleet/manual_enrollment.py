"""First-launch and restart proof for an operator-prepared Air instance."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from bluestacks import BlueStacksAdapter
from fleet.account_observer import StagingAccountObserver
from fleet.clone_qualification import CloneCandidate, QualificationScope, probe_clone_worker
from fleet.first_launch_account import create_first_launch_account, tower_is_unopened
from fleet.identity import Attempt
from fleet.manual_air_worker import ManualAirWorker
from fleet.runtime import WorkerRuntime


def enroll_manual_instance(
    *, root: Path, member: dict[str, str], runtime: WorkerRuntime,
    attempt: Attempt, inventory: Any, connect: Callable[[str], Any],
    protected_names: set[str],
) -> dict[str, Any]:
    """Bind a never-opened Tower account only after an exact host restart proof."""
    name = member["name"]
    endpoint = member["endpoint"]
    lease_id = member["lease_id"]
    if (name in protected_names or not re.fullmatch(r"Tiramisu64_\d+", name)
            or attempt.worker_id != name or attempt.endpoint != endpoint
            or attempt.lease_id != lease_id):
        raise ValueError("manual_instance_identity_invalid")
    expected_port = 10000 + int(name.rsplit("_", 1)[-1])
    if runtime.worker_id != name or runtime.web_port != expected_port:
        raise ValueError("manual_worker_runtime_invalid")
    runtime.ensure_directories()
    driver = ManualAirWorker(name, endpoint, lease_id, inventory=inventory)
    adapter = BlueStacksAdapter(driver, staging_root=Path(root) / "locks")
    row = adapter.designated(name, attempt)
    if row.state != "running":
        raise ValueError("manual_instance_start_required")
    device = connect(endpoint)
    if getattr(device, "serial", None) != endpoint:
        raise ValueError("manual_instance_tower_already_opened")
    resumed = None if tower_is_unopened(device) else _verified_first_launch(
        root, runtime, name, endpoint, lease_id)
    if resumed is not None:
        attempt = resumed[0]
    version = device.app_info("com.TechTreeGames.TheTower").version_name
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manual_instance_game_version_unavailable")
    if resumed is not None and resumed[1].get("app_version") != version:
        raise ValueError("worker_registration_missing_for_opened_tower")
    lineage = f"manual:{lease_id}"
    observer = StagingAccountObserver(runtime.evidence_root, endpoint=endpoint,
                                      allowed_versions=frozenset({version}))
    candidate = CloneCandidate(name, runtime, attempt, lambda: connect(endpoint), observer)
    registry = Path(root) / "manual-first-launch-registry.json"
    existing_ids = frozenset(
        row.get("account_id") for row in json.loads(registry.read_text()).values()
        if isinstance(row, dict) and isinstance(row.get("account_id"), str)
    ) if registry.exists() else frozenset()
    audit = resumed[1] if resumed is not None else create_first_launch_account(
        runtime=runtime, attempt=attempt, adapter=adapter, instance=name,
        source_lineage=lineage, protected_ids=existing_ids, connect=candidate.connect,
        observe=observer, registry=registry,
    )
    scope = QualificationScope(
        host_id="manual-air", bluestacks_version="operator-provisioned",
        source_lineage=lineage, source_version=lease_id, game_version=version,
        instance_config={"instance": name, "endpoint": endpoint, "lease_id": lease_id},
    )
    proof = probe_clone_worker(adapter=adapter, candidate=candidate,
                               account_id=audit["account_id"], scope=scope,
                               navigate=True)
    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
    bound = json.loads(binding.read_text(encoding="utf-8"))
    if any(bound.get(key) != expected for key, expected in (
            ("account_id", audit["account_id"]), ("endpoint", endpoint),
            ("lease_id", lease_id))):
        raise ValueError("manual_worker_identity_binding_changed")
    registration = {
        "state": "registered", "instance": name, "endpoint": endpoint,
        "lease_id": lease_id, "account_id": audit["account_id"],
        "job_id": attempt.attempt_id, "web_port": runtime.web_port,
        "binding": str(binding), "evidence_ref": proof["evidence_ref"],
        "registered_at": time.time(),
    }
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
    return registration


def _verified_first_launch(root: Path, runtime: WorkerRuntime, name: str, endpoint: str,
                           lease_id: str) -> tuple[Attempt, dict[str, Any]]:
    """Resume a first launch whose account was verified but whose restart proof
    failed, so an opened Tower is registered instead of stranded."""
    try:
        audit = json.loads((runtime.checkpoint_root / ".first-launch-account.json")
                           .read_text(encoding="utf-8"))
        registry = json.loads((Path(root) / "manual-first-launch-registry.json")
                              .read_text(encoding="utf-8"))
        attempt = Attempt(**{key: audit[key] for key in (
            "worker_id", "endpoint", "lease_id", "attempt_id", "generation", "created_at")})
        bound = json.loads((runtime.checkpoint_root / f"{attempt.generation}.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("worker_registration_missing_for_opened_tower") from exc
    account = audit.get("account_id")
    recorded = registry.get(name) if isinstance(registry, dict) else None
    if (audit.get("state") != "verified" or audit.get("instance") != name
            or (attempt.worker_id, attempt.endpoint, attempt.lease_id) != (name, endpoint, lease_id)
            or not isinstance(account, str) or not account
            or not isinstance(recorded, dict) or recorded.get("account_id") != account
            or bound.get("account_id") != account
            or bound.get("attempt_id") != attempt.attempt_id):
        raise ValueError("worker_registration_missing_for_opened_tower")
    return attempt, audit
