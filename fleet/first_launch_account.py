"""Assign a clone's account through its first Tower launch, without New Account."""

from __future__ import annotations

import fcntl
import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from bluestacks import BlueStacksAdapter
from fleet.account_creation import AccountFrame, StagingQuarantined, _save
from fleet.clone_launch import complete_first_launch_onboarding, launch_tower_from_game_center
from fleet.identity import Attempt, IdentityEvidence
from fleet.runtime import WorkerRuntime


_PACKAGE = "com.TechTreeGames.TheTower"
_UNOPENED = re.compile(r"\bstopped=true\s+notLaunched=true\b")


def tower_is_unopened(device: Any) -> bool:
    """Read Android package state without starting Tower."""
    state = device.shell(f"dumpsys package {_PACKAGE}")
    return bool(_UNOPENED.search(state))


def create_first_launch_account(
    *, runtime: WorkerRuntime, attempt: Attempt, adapter: BlueStacksAdapter,
    instance: str, source_lineage: str, protected_ids: frozenset[str],
    connect: Callable[[], Any], observe: Callable[[Any], AccountFrame],
    registry: Path, clock: Callable[[], float] = time.time,
    resume_after_consent: bool = False,
) -> dict[str, Any]:
    """Journal clone consent and first run before binding its observed account ID."""
    journal = runtime.checkpoint_root / ".first-launch-account.json"
    # Shared: first launches may overlap each other but never a clone staging.
    with runtime.reserve(attempt.endpoint), adapter.staging_lease(shared=True):
        if journal.exists() and not resume_after_consent:
            raise StagingQuarantined("previous first-launch attempt requires review")
        if resume_after_consent:
            try:
                audit = json.loads(journal.read_text(encoding="utf-8"))
                if (audit.get("state") not in {"pending_i_agree", "quarantined"}
                        or audit.get("instance") != instance
                        or audit.get("source_lineage") != source_lineage
                        or any(audit.get(key) != value for key, value in asdict(attempt).items())
                        or [row.get("action") for row in audit.get("evidence", [])] != ["i_agree"]):
                    raise ValueError("consent journal changed")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise StagingQuarantined("consent journal unavailable") from exc
        else:
            audit = {
                **asdict(attempt), "instance": instance, "source_lineage": source_lineage,
                "state": "started", "evidence": [], "started_at": clock(),
            }
            _save(journal, audit)
        try:
            bound = adapter.designated(instance, attempt)
            if bound.state != "running" or bound.source_lineage != source_lineage:
                raise ValueError("clone lineage or state changed")
            device = connect()
            if getattr(device, "serial", None) != attempt.endpoint or (
                    not resume_after_consent and not tower_is_unopened(device)):
                raise ValueError("clone Tower was already launched or endpoint changed")
            if resume_after_consent and observe(device).screen != "google_play_profile":
                raise ValueError("expected post-consent Play Games sheet unavailable")

            def before_action(action: str, frame: AccountFrame) -> None:
                if (frame.screen not in {"google_play_profile", "tower_consent", "game_over"}
                        or set(frame.controls) != {action} or not frame.evidence_ref
                        or not frame.digest or frame.observed_at < attempt.created_at
                        or clock() - frame.observed_at > 5):
                    raise ValueError("first-launch action evidence unavailable")
                audit["evidence"].append({
                    "screen": frame.screen, "action": action,
                    "digest": frame.digest, "observed_at": frame.observed_at,
                    "evidence_ref": frame.evidence_ref, "app_version": frame.app_version,
                })
                audit["state"] = f"pending_{action}"
                _save(journal, audit)

            if not resume_after_consent:
                launch_tower_from_game_center(device)
            complete_first_launch_onboarding(
                device, observe, before_action=before_action,
                consent_already_recorded=resume_after_consent,
            )

            def read_screen(expected: str, *, previous: str | None = None) -> AccountFrame:
                for _ in range(12):
                    frame = observe(device)
                    if frame.conflict_dialog:
                        raise ValueError("session conflict during account navigation")
                    if frame.screen == expected:
                        return frame
                    if frame.screen not in {"unknown", previous}:
                        raise ValueError("unexpected account navigation screen")
                    time.sleep(.5)
                raise ValueError("account navigation screen unavailable")

            for screen, control, previous in (("home", "settings", None),
                                              ("settings", "account", "home")):
                frame = read_screen(screen, previous=previous)
                # The observer adds `close` to Settings whenever its X is visible.
                allowed = ({control}, {control, "close"}) if screen == "settings" else ({control},)
                if (frame.screen != screen or set(frame.controls) not in allowed
                        or frame.conflict_dialog or clock() - frame.observed_at > 5):
                    raise ValueError("account navigation unavailable")
                before_action_for_navigation = {
                    "screen": screen, "action": control, "digest": frame.digest,
                    "observed_at": frame.observed_at, "evidence_ref": frame.evidence_ref,
                    "app_version": frame.app_version,
                }
                audit["evidence"].append(before_action_for_navigation)
                audit["state"] = f"pending_{control}"
                _save(journal, audit)
                device.click(*frame.controls[control])
            account = read_screen("account", previous="settings")
            if (account.screen != "account" or account.popup_title != "ACCOUNT"
                    or account.id_label != "ID:" or not account.account_id
                    or account.account_id in protected_ids or account.conflict_dialog
                    or account.observed_at < attempt.created_at
                    or clock() - account.observed_at > 5):
                raise ValueError("fresh clone account ID unavailable")
            if not any(row["action"] == "i_agree" for row in audit["evidence"]):
                raise ValueError("I Agree was not verified")
            if any(row["app_version"] != account.app_version for row in audit["evidence"]):
                raise ValueError("game version changed during first launch")
            audit["evidence"].append({
                "screen": "account", "account_id": account.account_id,
                "digest": account.digest, "observed_at": account.observed_at,
                "evidence_ref": account.evidence_ref, "app_version": account.app_version,
            })
            _save(journal, audit)
            registry = Path(registry)
            registry.parent.mkdir(parents=True, exist_ok=True)
            with registry.with_suffix(".lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    records = json.loads(registry.read_text()) if registry.exists() else {}
                    if (not isinstance(records, dict) or any(
                            not isinstance(row, dict) or not isinstance(row.get("account_id"), str)
                            for row in records.values())):
                        raise ValueError("unreadable active worker registry")
                    if account.account_id in {row["account_id"] for row in records.values()}:
                        raise ValueError("duplicate active worker account ID")
                    for path in runtime.root.parent.glob("*/checkpoints/*.json"):
                        if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                            continue
                        row = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(row, dict) or not isinstance(row.get("account_id"), str):
                            raise ValueError("unreadable worker account binding")
                        if row["account_id"] == account.account_id:
                            raise ValueError("duplicate active worker account ID")
                    attempt.persist(runtime.checkpoint_root / f"{attempt.generation}.json",
                                    IdentityEvidence(account.account_id, account.observed_at,
                                                     account.evidence_ref))
                    records[attempt.worker_id] = {"account_id": account.account_id,
                                                  "endpoint": attempt.endpoint,
                                                  "instance": instance}
                    _save(registry, records)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            audit.update(state="verified", account_id=account.account_id,
                         app_version=account.app_version, completed_at=clock())
            _save(journal, audit)
            return audit
        except Exception as exc:
            audit.update(state="quarantined", reason=str(exc))
            _save(journal, audit)
            raise StagingQuarantined(str(exc)) from exc
