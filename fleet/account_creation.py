"""R00: explicit, fail-closed creation of a Tower account on a staging clone.

This module accepts only a caller's explicit staging designation and observed,
named screen controls; there is deliberately no default production entry point.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from bluestacks import BlueStacksAdapter
from fleet.identity import Attempt, IdentityEvidence
from fleet.runtime import WorkerRuntime


class StagingQuarantined(RuntimeError):
    """The staging worker needs operator review before any further input."""


@dataclass(frozen=True)
class StagingClone:
    instance: str
    source_lineage: str
    allowed_source_id: str
    protected_ids: frozenset[str]
    enabled: bool = False


@dataclass(frozen=True)
class AccountFrame:
    screen: str
    account_id: str | None
    app_version: str | None
    digest: str
    observed_at: float
    evidence_ref: str
    controls: dict[str, tuple[int, int]]
    conflict_dialog: str | None = None
    popup_title: str | None = None
    id_label: str | None = None


_CONFLICTS = ("new session detected", "cloud session different than local session")


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_staging_account(
    *, runtime: WorkerRuntime, attempt: Attempt, adapter: BlueStacksAdapter,
    policy: StagingClone, connect: Callable[[], Any],
    observe: Callable[[Any], AccountFrame], registry: Path,
    start_from_account: bool = False,
    resume_confirmation: bool = False,
    resume_after_confirmation: bool = False,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Walk home -> Settings -> Account -> New Account with write-ahead audit.

    The observer is a trusted screen reader supplied by a calibrated staging
    integration. It must return evidence from the device passed to it. A
    A production caller must not enable this until its clone's Account popup
    and completion flow have been reviewed.
    """
    if not policy.enabled:
        raise StagingQuarantined("R00 disabled: live Account popup proof unavailable")
    if (runtime.worker_id != attempt.worker_id or not policy.instance.strip()
            or not policy.source_lineage.strip() or not policy.allowed_source_id.strip()):
        raise StagingQuarantined("staging identity or source lineage missing")
    journal = runtime.checkpoint_root / ".r00-account-creation.json"
    with runtime.reserve(attempt.endpoint), adapter.staging_lease():
        try:
            instance = adapter.designated(policy.instance, attempt)
            if instance.source_lineage != policy.source_lineage:
                raise ValueError("source lineage mismatch")
            if instance.state != "running":
                raise ValueError("designated instance is not running")
            prior: dict[str, Any] | None = None
            if journal.exists():
                if resume_confirmation == resume_after_confirmation:
                    raise StagingQuarantined("previous R00 attempt requires review")
                try:
                    prior = json.loads(journal.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise StagingQuarantined("previous R00 audit unreadable") from exc
                expected_reason = ("ambiguous dialog" if resume_confirmation
                                   else "unexpected screen during account creation")
                initial_screens = (["account"] if start_from_account
                                   else ["home", "settings", "account"])
                expected_screens = (initial_screens if resume_confirmation
                                    else initial_screens + ["new_account_warning"])
                if (not isinstance(prior, dict)
                        or prior.get("state") != "quarantined"
                        or prior.get("reason") != expected_reason
                        or any(prior.get(key) != value for key, value in (
                            ("worker_id", attempt.worker_id), ("endpoint", attempt.endpoint),
                            ("lease_id", attempt.lease_id), ("attempt_id", attempt.attempt_id),
                            ("generation", attempt.generation), ("instance", policy.instance),
                            ("source_lineage", policy.source_lineage),
                            ("allowed_source_id", policy.allowed_source_id),
                        ))
                        or prior.get("source_account_id") != policy.allowed_source_id
                        or prior.get("started_from_account", False) != start_from_account
                        or not isinstance(prior.get("evidence"), list)
                        or any(not isinstance(row, dict) for row in prior["evidence"])
                        or [row.get("screen") for row in prior["evidence"]] !=
                        expected_screens
                        or prior["evidence"][len(initial_screens)-1].get("account_id") != policy.allowed_source_id
                        or (resume_after_confirmation and
                            prior["evidence"][-1].get("account_id") != policy.allowed_source_id)
                        or any(not isinstance(row.get("observed_at"), (int, float))
                               or not isinstance(row.get("digest"), str)
                               or not isinstance(row.get("app_version"), str)
                               for row in prior["evidence"])):
                    raise StagingQuarantined("previous R00 audit cannot resume")
            elif resume_confirmation or resume_after_confirmation:
                raise StagingQuarantined("previous R00 audit missing")
            device = connect()
            if getattr(device, "serial", None) != attempt.endpoint:
                raise ValueError("connected endpoint mismatch")
        except StagingQuarantined:
            raise
        except Exception as exc:
            _save(journal, {
                **asdict(attempt), "instance": policy.instance,
                "source_lineage": policy.source_lineage, "state": "quarantined",
                "reason": "host identity or lineage mismatch", "observed_at": clock(),
            })
            raise StagingQuarantined("host identity or lineage mismatch") from exc

        audit: dict[str, Any] = prior if prior is not None else {
            **asdict(attempt), "instance": policy.instance,
            "source_lineage": policy.source_lineage, "allowed_source_id": policy.allowed_source_id,
            "state": "started", "evidence": [], "started_at": clock(),
            "started_from_account": start_from_account,
        }
        if prior is None:
            _save(journal, audit)
        last_time = max([attempt.created_at, *(row["observed_at"] for row in audit["evidence"])])
        seen_digests: set[str] = {row["digest"] for row in audit["evidence"]}
        game_home_tapped = False

        def read(expected: str, *, previous: str | None = None,
                 changed_from: str | None = None,
                 loading_transition: bool = False) -> AccountFrame:
            nonlocal last_time, game_home_tapped
            limit = 120 if loading_transition else 6
            for index in range(limit):
                instance = adapter.designated(policy.instance, attempt)
                if instance.source_lineage != policy.source_lineage:
                    raise ValueError("source lineage mismatch")
                if getattr(device, "serial", None) != attempt.endpoint:
                    raise ValueError("endpoint mismatch")
                shot = observe(device)
                if shot.conflict_dialog and any(term in shot.conflict_dialog.lower() for term in _CONFLICTS):
                    raise ValueError("session_conflict")
                if shot.conflict_dialog:
                    raise ValueError("ambiguous dialog")
                if (not shot.digest or not shot.evidence_ref or not shot.app_version
                        or not math.isfinite(shot.observed_at)
                        or shot.observed_at <= last_time or shot.observed_at > clock()
                        or clock() - shot.observed_at > 5):
                    raise ValueError("unreadable or stale screen evidence")
                last_time = shot.observed_at
                if shot.screen == "google_play_profile":
                    if (expected != "home" or previous is not None or loading_transition
                            or audit["evidence"]
                            or set(shot.controls) != {"dismiss_google_play_profile"}):
                        raise ValueError("unexpected Google Play profile prompt")
                    audit["evidence"].append({
                        "screen": shot.screen, "digest": shot.digest,
                        "observed_at": shot.observed_at, "evidence_ref": shot.evidence_ref,
                        "account_id": None, "app_version": shot.app_version,
                        "popup_title": shot.popup_title,
                    })
                    seen_digests.add(shot.digest)
                    _save(journal, audit)
                    tap(shot, "dismiss_google_play_profile")
                    sleep(.5)
                    continue
                if loading_transition and shot.screen == "game_over" and not game_home_tapped:
                    if set(shot.controls) != {"home_from_game_over"}:
                        raise ValueError("game stats Home control unavailable")
                    audit["evidence"].append({
                        "screen": shot.screen, "digest": shot.digest,
                        "observed_at": shot.observed_at, "evidence_ref": shot.evidence_ref,
                        "account_id": None, "app_version": shot.app_version,
                    })
                    seen_digests.add(shot.digest)
                    _save(journal, audit)
                    tap(shot, "home_from_game_over")
                    game_home_tapped = True
                    sleep(1.)
                    continue
                if (shot.screen == previous and shot.screen != expected
                        or loading_transition and shot.screen in {
                            "unknown", "account", "settings", "game_over",
                        }):
                    if index < limit - 1:
                        sleep(1. if loading_transition else .5)
                    continue
                if shot.screen != expected:
                    raise ValueError("unexpected screen during account creation")
                if expected == "account" and (
                    shot.popup_title != "ACCOUNT" or shot.id_label != "ID:"
                ):
                    raise ValueError("account popup corroboration missing")
                if expected == "new_account_warning" and (
                    shot.popup_title != "Warning" or shot.id_label != "ID:"
                    or shot.account_id != policy.allowed_source_id
                    or set(shot.controls) != {"confirm_new_account"}
                ):
                    raise ValueError("new account warning corroboration missing")
                if changed_from is not None:
                    if not shot.account_id:
                        raise ValueError("unreadable post account ID")
                    if shot.account_id == changed_from:
                        if index < limit - 1:
                            sleep(.5)
                        continue
                if shot.digest in seen_digests:
                    raise ValueError("stale screen evidence")
                seen_digests.add(shot.digest)
                audit["evidence"].append({
                    "screen": shot.screen, "digest": shot.digest,
                    "observed_at": shot.observed_at, "evidence_ref": shot.evidence_ref,
                    "account_id": shot.account_id, "app_version": shot.app_version,
                    "popup_title": shot.popup_title, "id_label": shot.id_label,
                })
                _save(journal, audit)
                return shot
            raise ValueError("unchanged account ID" if changed_from is not None
                             else "screen transition unconfirmed")

        def tap(shot: AccountFrame, name: str) -> None:
            instance = adapter.designated(policy.instance, attempt)
            if instance.source_lineage != policy.source_lineage:
                raise ValueError("source lineage mismatch")
            if getattr(device, "serial", None) != attempt.endpoint:
                raise ValueError("endpoint mismatch")
            point = shot.controls.get(name)
            if (point is None or len(point) != 2 or any(not isinstance(x, int) for x in point)
                    or not 0 <= point[0] < 1080 or not 0 <= point[1] < 2400
                    or clock() - shot.observed_at > 5):
                raise ValueError(f"{name} control unavailable")
            audit["state"] = f"pending_{name}"
            _save(journal, audit)
            device.click(*point)

        try:
            if prior is None:
                if start_from_account:
                    before = read("account")
                else:
                    home = read("home")
                    tap(home, "settings")
                    settings = read("settings", previous="home")
                    tap(settings, "account")
                    before = read("account", previous="settings")
                source = (before.account_id or "").strip()
                if not source:
                    raise ValueError("unreadable source account ID")
                if source != policy.allowed_source_id:
                    raise ValueError("source lineage account mismatch")
                audit["source_account_id"] = source
                _save(journal, audit)
                tap(before, "new_account")
            else:
                source = prior["source_account_id"]
            if not resume_after_confirmation:
                warning = read("new_account_warning", previous="account" if prior is None else None)
                tap(warning, "confirm_new_account")
            home_after = read("home", previous="new_account_warning", loading_transition=True)
            tap(home_after, "settings")
            settings_after = read("settings", previous="home")
            tap(settings_after, "account")
            after = read("account", previous="settings", changed_from=source)
            new_id = (after.account_id or "").strip()
            if not new_id:
                raise ValueError("unreadable post account ID")
            if new_id == source:
                raise ValueError("unchanged account ID")
            if new_id in policy.protected_ids:
                raise ValueError("protected account ID")
            prior_versions = {row["app_version"] for row in audit["evidence"]}
            if len(prior_versions) != 1:
                raise ValueError("app version changed during creation")

            registry = Path(registry)
            registry.parent.mkdir(parents=True, exist_ok=True)
            with registry.with_suffix(".lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    records = json.loads(registry.read_text()) if registry.exists() else {}
                    if not isinstance(records, dict) or any(
                        not isinstance(row, dict) or not isinstance(row.get("account_id"), str)
                        for row in records.values()
                    ):
                        raise ValueError("unreadable active worker registry")
                    if any(row["account_id"] == new_id for row in records.values()):
                        raise ValueError("duplicate active worker account ID")
                    for path in runtime.root.parent.glob("*/checkpoints/*.json"):
                        if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                            continue
                        row = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(row, dict) or not isinstance(row.get("account_id"), str):
                            raise ValueError("unreadable worker account binding")
                        if row["account_id"] == new_id:
                            raise ValueError("duplicate active worker account ID")
                    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
                    attempt.persist(binding, IdentityEvidence(new_id, after.observed_at,
                                                              after.evidence_ref))
                    records[attempt.worker_id] = {"account_id": new_id,
                                                  "endpoint": attempt.endpoint,
                                                  "instance": policy.instance}
                    _save(registry, records)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            audit.update(state="verified", account_id=new_id,
                         app_version=after.app_version, completed_at=clock())
            audit.pop("reason", None)
            _save(journal, audit)
            return audit
        except Exception as exc:
            audit["state"] = "quarantined"
            audit["reason"] = str(exc) if isinstance(exc, ValueError) else "action outcome uncertain"
            _save(journal, audit)
            raise StagingQuarantined(audit["reason"]) from exc
