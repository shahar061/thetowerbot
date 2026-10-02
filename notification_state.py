"""Durable, capture-confirmed notification edges and bounded retry cadence."""
from __future__ import annotations

import json
import copy
from dataclasses import replace
import hashlib
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import events
from evidence_scope import ScopeContinuity


KINDS = ("labs", "missions", "mail", "milestones")
BASE_RETRY_SECONDS = 60.0
MAX_RETRY_SECONDS = 3600.0


def _empty_kind() -> dict[str, Any]:
    return {"visible": None, "latest_reading": None, "generation": 0, "candidate": None,
            "candidate_frame": None, "attempts": 0, "retry_at": 0.0,
            "claimed_generation": None, "in_flight": False, "uncertain": False,
            "prior_uncertain": False}


class NotificationState:
    """One worker's account/attempt-scoped, atomic notification record.

    An observation needs two different capture IDs. Two captures can have
    identical pixels; passing the same capture twice cannot create an edge.
    A missing/unknown read cannot retire an already confirmed generation.
    """

    def __init__(self, path: Path | None = None, *,
                 scope: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.scope = dict(scope or {})
        self._row: dict[str, Any] = {
            "scope": self.scope, "observed_at": None, "state": "unknown",
            "reason": "Mission evidence unavailable", "last_claim_at": None,
            "receipt_attempt": None, "pending_claim": None, "receipts": [],
            "resolved_intents": [],
            "kinds": {kind: _empty_kind() for kind in KINDS},
        }
        if self.path is not None:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                saved = None
            if self._valid_saved(saved):
                self._row = saved
                self._row.setdefault("resolved_intents", [])
            elif self.path.exists():
                self._archive_previous()
                # A new worker attempt has no current claim, but an
                # unresolved tap by the same account remains a block. Keep
                # the old record for reconciliation and expose unknown here.
                if (isinstance(saved, dict)
                        and isinstance(saved.get("scope"), dict)
                        and saved["scope"].get("account_id") == self.scope.get("account_id")
                        and isinstance(saved.get("kinds"), dict)
                        and isinstance(saved["kinds"].get("missions"), dict)):
                    old = saved["kinds"]["missions"]
                    if (saved.get("pending_claim") is not None
                            or old.get("uncertain") is True):
                        mission = self._row["kinds"]["missions"]
                        mission["uncertain"] = True
                        mission["prior_uncertain"] = True
                        self._row["pending_claim"] = saved.get("pending_claim")
                        self._row["receipt_attempt"] = saved.get("receipt_attempt")
                        self._row["reason"] = "Prior mission claim outcome unresolved"
                        self._save()
                elif saved is None:
                    # An unreadable sidecar cannot prove that no reward tap
                    # was prepared; preserve a fail-closed current guard.
                    self._row["kinds"]["missions"].update(
                        uncertain=True, prior_uncertain=True)
                    self._summary(time.time())
                    self._save()
            if self._row["pending_claim"] is None:
                self._recover_archived_claim()

    def _recover_archived_claim(self) -> None:
        """Restore any account-owned tap not resolved by a later durable row."""
        assert self.path is not None
        rows = [self._row]
        for archive in self.path.parent.glob(
                f"{self.path.stem}-archive-*{self.path.suffix}"):
            try:
                row = json.loads(archive.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        account = self.scope.get("account_id")
        resolved = {item for row in rows if row.get("scope", {}).get("account_id") == account
                    for item in row.get("resolved_intents", ()) if isinstance(item, str)}
        resolved.update(receipt.get("intent_id") for row in rows
                        if row.get("scope", {}).get("account_id") == account
                        for receipt in row.get("receipts", ())
                        if isinstance(receipt, dict) and isinstance(receipt.get("intent_id"), str))
        for row in rows[1:]:
            if row.get("scope", {}).get("account_id") != account:
                continue
            intent = row.get("pending_claim")
            if not isinstance(intent, dict) or intent.get("intent_id") in resolved:
                continue
            self._row["pending_claim"] = intent
            self._row["receipt_attempt"] = row.get("receipt_attempt")
            self._row["kinds"]["missions"].update(
                uncertain=True, prior_uncertain=True)
            self._summary(time.time())
            self._save()
            return

    def _archive_previous(self) -> None:
        assert self.path is not None
        archive = self.path.with_name(
            f"{self.path.stem}-archive-{uuid.uuid4().hex}{self.path.suffix}")
        # Keep the current guard until the replacement has been fsynced.
        # A crash or failed save leaves an actionable current record, while
        # R4 can still ingest receipts from the copied archive.
        descriptor, name = tempfile.mkstemp(prefix=f".{archive.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self.path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, archive)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _valid_saved(self, saved: Any) -> bool:
        if not isinstance(saved, dict) or saved.get("scope") != self.scope:
            return False
        kinds = saved.get("kinds")
        if not isinstance(kinds, dict):
            return False
        if (saved.get("receipt_attempt") is not None
                and not isinstance(saved["receipt_attempt"], str)):
            return False
        if not isinstance(saved.get("receipts"), list):
            return False
        if not isinstance(saved.get("resolved_intents", []), list):
            return False
        if saved.get("pending_claim") is not None and not isinstance(saved["pending_claim"], dict):
            return False
        if any(not isinstance(receipt, dict) or not isinstance(receipt.get("key"), str)
               for receipt in saved["receipts"]):
            return False
        for kind in KINDS:
            row = kinds.get(kind)
            if not isinstance(row, dict) or row.get("visible") not in (None, True, False):
                return False
            if row.get("latest_reading") not in (None, True, False):
                return False
            if type(row.get("generation")) is not int or row["generation"] < 0:
                return False
            if type(row.get("attempts")) is not int or row["attempts"] < 0:
                return False
            if (type(row.get("retry_at")) not in (int, float)
                    or not math.isfinite(row["retry_at"])):
                return False
            if type(row.get("in_flight")) is not bool or type(row.get("uncertain")) is not bool:
                return False
            if type(row.get("prior_uncertain")) is not bool:
                return False
            if row.get("candidate") not in (None, True, False):
                return False
            if row.get("claimed_generation") is not None and (
                    type(row["claimed_generation"]) is not int
                    or row["claimed_generation"] > row["generation"]):
                return False
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self._row, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _summary(self, now: float) -> None:
        row = self._row["kinds"]["missions"]
        self._row["observed_at"] = now
        if row["prior_uncertain"]:
            self._row.update(state="unknown", reason="Prior mission claim outcome unresolved")
        elif row["uncertain"] or row["in_flight"]:
            self._row.update(state="claiming", reason=("Claim outcome uncertain"
                             if row["uncertain"] else "Mission claim in progress"))
        elif row["latest_reading"] is None:
            self._row.update(state="unknown", reason="Mission notification unreadable")
        elif (row["visible"] is True and row["latest_reading"] is True
              and row["claimed_generation"] != row["generation"]):
            self._row.update(state="pending", reason=None)
        elif row["visible"] is False or row["claimed_generation"] == row["generation"] and row["generation"]:
            self._row.update(state="clear", reason=None)
        else:
            self._row.update(state="unknown", reason="Mission notification unreadable")

    def observe(self, kind: str, visible: bool | None, now: float, *,
                frame_id: str | int | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown notification kind: {kind}")
        if not math.isfinite(now) or (visible is not None and type(visible) is not bool):
            raise ValueError("finite time and tri-state visibility required")
        row = self._row["kinds"][kind]
        row["latest_reading"] = visible
        if visible is None:
            row["candidate"] = None
            row["candidate_frame"] = None
        elif visible == row["visible"]:
            row["candidate"] = None
            row["candidate_frame"] = None
        elif row["candidate"] != visible:
            row["candidate"] = visible
            row["candidate_frame"] = frame_id if frame_id is not None else now
        elif row["candidate_frame"] != (frame_id if frame_id is not None else now):
            row["visible"] = visible
            row["candidate"] = None
            row["candidate_frame"] = None
            if visible:
                row["generation"] += 1
                row["attempts"] = 0
                row["retry_at"] = 0.0
                row["claimed_generation"] = None
        if kind == "missions":
            self._summary(now)
        self._save()

    def eligible(self, kind: str, now: float) -> bool:
        if kind not in KINDS or not math.isfinite(now):
            return False
        row = self._row["kinds"][kind]
        return (row["visible"] is True and row["latest_reading"] is True
                and row["generation"] > 0
                and row["claimed_generation"] != row["generation"]
                and not row["in_flight"] and not row["uncertain"]
                and not row["prior_uncertain"]
                and now >= row["retry_at"])

    def begin(self, kind: str, now: float) -> None:
        if kind not in KINDS or not math.isfinite(now):
            raise ValueError("valid notification kind and finite time required")
        row = self._row["kinds"][kind]
        row["in_flight"] = True
        if kind == "missions":
            self._row["receipt_attempt"] = uuid.uuid4().hex
            self._summary(now)
        self._save()

    def prepare_claim(self, *, mission: str, mission_id: str | None,
                      coins: int | None, gems: int | None, completed_before: int,
                      completed_target: int | None, visible_before: list[Any],
                      now: float) -> None:
        """Fsync a possibly-sent reward intent before touching CLAIM."""
        if (not self._row["kinds"]["missions"]["in_flight"]
                or self._row["pending_claim"] is not None
                or not mission or type(completed_before) is not int):
            raise ValueError("mission tap requires one active, unambiguous walk")
        previous = copy.deepcopy(self._row)
        self._row["pending_claim"] = {
            "intent_id": uuid.uuid4().hex,
            "mission": mission, "mission_id": mission_id, "coins": coins, "gems": gems,
            "completed_before": completed_before, "completed_target": completed_target,
            "visible_before": visible_before, "scope": self.scope,
            "fact_epoch": (self.scope.get("fact_epoch")
                           if type(self.scope.get("fact_epoch")) is int else None),
            "prepared_at": now, "verification_attempts": 0,
            "verification_retry_at": 0.0, "no_reward_frame": None,
        }
        self._summary(now)
        try:
            self._save()
        except Exception:
            self._row = previous
            raise

    def discard_prepared_claim(self, now: float) -> None:
        """Only for a control that was positively refused before device.click."""
        self._row["pending_claim"] = None
        self._summary(now)
        self._save()

    def prepare_chest(self, *, threshold: int, completed_before: int,
                      now: float) -> None:
        """Fsync a weekly chest tap before the device can receive it."""
        if (not self._row["kinds"]["missions"]["in_flight"]
                or self._row["pending_claim"] is not None
                or threshold not in range(5, 36, 5)
                or type(completed_before) is not int or completed_before < threshold
                or not math.isfinite(now)):
            raise ValueError("weekly chest tap requires one active, eligible walk")
        previous = copy.deepcopy(self._row)
        self._row["pending_claim"] = {
            "kind": "weekly_chest", "intent_id": uuid.uuid4().hex,
            "threshold": threshold, "completed_before": completed_before,
            "rewards": [], "reward_texts": [], "unreadable_rewards": 0,
            "index": 0, "total": None, "final_tapped": False,
            "scope": self.scope,
            "fact_epoch": (self.scope.get("fact_epoch")
                           if type(self.scope.get("fact_epoch")) is int else None),
            "prepared_at": now, "verification_attempts": 0,
            "verification_retry_at": 0.0, "no_reward_frame": None,
        }
        self._summary(now)
        try:
            self._save()
        except Exception:
            self._row = previous
            raise

    def observe_chest_reward(self, *, index: int, total: int,
                             currency: str | None, amount: int | None,
                             reward_text: str | None, final_tapped: bool,
                             now: float) -> None:
        """Save each reward page before its NEXT or CLAIM control is tapped."""
        pending = self._row["pending_claim"]
        if (not isinstance(pending, dict) or pending.get("kind") != "weekly_chest"
                or type(index) is not int or index != pending["index"] + 1
                or type(total) is not int or not 1 <= index <= total <= 8
                or pending["total"] is not None and pending["total"] != total
                or final_tapped != (index == total)
                or (currency is None) != (amount is None)
                or amount is not None and (type(amount) is not int or amount < 0)
                or not math.isfinite(now)):
            raise ValueError("weekly chest reward page is not sequential")
        previous = copy.deepcopy(self._row)
        pending["index"] = index
        pending["total"] = total
        pending["final_tapped"] = final_tapped
        if reward_text is not None:
            pending["reward_texts"].append(reward_text)
        if currency is not None and amount is not None:
            pending["rewards"].append([currency, amount])
        else:
            pending["unreadable_rewards"] += 1
        try:
            self._save()
        except Exception:
            self._row = previous
            raise

    def record_chest_receipt(self, event: events.WeeklyChestClaimed,
                             now: float) -> events.WeeklyChestClaimed | None:
        """Durably bind a checked chest to its saved tap and reward pages."""
        pending = self._row["pending_claim"]
        token = self._row["receipt_attempt"]
        if (not isinstance(pending, dict) or pending.get("kind") != "weekly_chest"
                or not token or not math.isfinite(now)
                or not pending.get("final_tapped")
                or event.threshold != pending.get("threshold")
                or event.confirmation != "chest_marked_claimed"
                or tuple(tuple(reward) for reward in pending["rewards"]) != tuple(event.rewards)
                or event.unreadable_rewards != pending["unreadable_rewards"]
                or event.reward_text != (", ".join(pending["reward_texts"]) or None)):
            raise ValueError("weekly chest receipt does not match durable tap intent")
        source = [token, "weekly_chest", event.threshold]
        key = hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()
        if any(row["key"] == key for row in self._row["receipts"]):
            return None
        previous = copy.deepcopy(self._row)
        self._row["receipts"].append({
            "key": key, "kind": "weekly_chest", "intent_id": pending["intent_id"],
            "schema_version": 1, "receipt_token": token,
            "intent_scope": copy.deepcopy(pending["scope"]),
            "prepared_at": pending["prepared_at"], "threshold": event.threshold,
            "rewards": copy.deepcopy(pending["rewards"]),
            "unreadable_rewards": event.unreadable_rewards,
            "reward_text": event.reward_text, "confirmed_at": now,
        })
        self._row["last_claim_at"] = now
        self._row["pending_claim"] = None
        self._row["kinds"]["missions"].update(prior_uncertain=False, uncertain=False)
        self._summary(now)
        try:
            self._save()
        except Exception:
            self._row = previous
            raise
        return replace(event, receipt_key=key)

    def record_receipt(self, event: events.MissionClaimed, now: float) -> bool:
        """Save a counter-confirmed credit before forwarding a lossy event.

        Amounts are the card's read values, possibly unknown. No balance is
        inferred here. The same observed reward in one walk has one key even
        if publication or reconciliation retries after a restart.
        """
        token = self._row["receipt_attempt"]
        pending = self._row["pending_claim"]
        before, after = event.completed_before, event.completed_after
        if (not token or not math.isfinite(now) or type(before) is not int
                or type(after) is not int or after != before + 1
                or not event.mission):
            raise ValueError("mission receipt requires an active, counter-confirmed claim")
        for amount in (event.coins, event.gems):
            if amount is not None and (type(amount) is not int or amount < 0):
                raise ValueError("mission amount must be nonnegative or unknown")
        source = [token, event.mission_id, event.mission, before, after]
        key = hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()
        if any(row["key"] == key for row in self._row["receipts"]):
            return False
        if (pending is None or pending.get("mission") != event.mission
                or pending.get("mission_id") != event.mission_id
                or pending.get("completed_before") != before
                or pending.get("coins") != event.coins or pending.get("gems") != event.gems):
            raise ValueError("mission receipt does not match durable tap intent")
        previous = copy.deepcopy(self._row)
        self._row["receipts"].append({
            "key": key, "mission_id": event.mission_id, "mission": event.mission,
            "intent_id": pending["intent_id"],
            "schema_version": 1, "receipt_token": token,
            "intent_scope": copy.deepcopy(pending["scope"]), "prepared_at": pending["prepared_at"],
            "coins": event.coins, "gems": event.gems,
            "completed_before": before, "completed_after": after,
            "confirmed_at": now,
        })
        self._row["last_claim_at"] = now
        self._row["pending_claim"] = None
        self._row["kinds"]["missions"]["prior_uncertain"] = False
        self._row["kinds"]["missions"]["uncertain"] = False
        self._summary(now)
        try:
            self._save()
        except Exception:
            self._row = previous
            raise
        return True

    def finish(self, kind: str, now: float, claimed: bool | None) -> None:
        if kind not in KINDS or not math.isfinite(now) or claimed not in (True, False, None):
            raise ValueError("valid kind, finite time and tri-state outcome required")
        row = self._row["kinds"][kind]
        if kind == "missions" and self._row["pending_claim"] is not None:
            claimed = None
        if claimed is True:
            if row["claimed_generation"] != row["generation"] or not row["generation"]:
                row["claimed_generation"] = row["generation"]
                if kind == "missions":
                    self._row["last_claim_at"] = now
            row["uncertain"] = False
            row["in_flight"] = False
        elif claimed is None:
            row["uncertain"] = True
            row["in_flight"] = False
        else:
            row["in_flight"] = False
            if not row["uncertain"]:
                row["attempts"] += 1
                delay = min(MAX_RETRY_SECONDS, BASE_RETRY_SECONDS * 2 ** min(row["attempts"] - 1, 10))
                row["retry_at"] = now + delay
        if kind == "missions":
            self._summary(now)
        self._save()

    def snapshot(self) -> dict[str, Any]:
        """Return a detached copy suitable for diagnostics or read-only UI."""
        return json.loads(json.dumps(self._row))

    def _retain(self, claim: dict[str, Any], now: float, *, reason: str,
                operator: str | None = None, evidence: str | None = None) -> None:
        """Stop blocking on an unproven intent; keep it on record, uncredited.

        No receipt is written and no balance is minted: an unproven reward is
        never credited. The intent id joins ``resolved_intents`` so archive
        recovery on a later worker attempt does not restore the block.
        """
        previous = copy.deepcopy(self._row)
        self._row.setdefault("retained_intents", []).append({
            **copy.deepcopy(claim), "retained_at": now, "retained_reason": reason,
            "operator": operator, "operator_evidence": evidence, "credited": False})
        self._row["resolved_intents"].append(claim["intent_id"])
        self._row["pending_claim"] = None
        self._row["receipt_attempt"] = None
        self._row["kinds"]["missions"].update(uncertain=False, prior_uncertain=False,
                                              in_flight=False)
        self._summary(now)
        try:
            self._save()
        except Exception:
            self._row = previous
            raise

    def retain_exhausted_claim(self, now: float) -> bool:
        """After three bounded verification walks and their last backoff, retain."""
        claim = self._row["pending_claim"]
        if (not isinstance(claim, dict)
                or claim.get("scope", {}).get("account_id") != self.scope.get("account_id")
                or claim.get("verification_attempts", 0) < 3
                or now < claim.get("verification_retry_at", 0)):
            return False
        self._retain(claim, now, reason="verification_exhausted")
        return True

    def operator_resolve_claim(self, intent_id: str, *, operator: str, evidence: str,
                               now: float) -> None:
        """Auditable operator exit for an unproven mission intent (never credits)."""
        claim = self._row["pending_claim"]
        if not isinstance(claim, dict) or claim.get("intent_id") != intent_id:
            raise ValueError("no such unresolved mission intent")
        if not operator.strip() or not evidence.strip():
            raise ValueError("operator and evidence are required")
        self._retain(claim, now, reason="operator_resolved", operator=operator, evidence=evidence)

    def verification_due(self, now: float) -> bool:
        claim = self._row["pending_claim"]
        return (isinstance(claim, dict) and claim.get("scope", {}).get("account_id")
                == self.scope.get("account_id")
                and claim.get("verification_attempts", 0) < 3
                and now >= claim.get("verification_retry_at", 0))

    def note_verification_visit(self, now: float) -> None:
        claim = self._row["pending_claim"]
        if not self.verification_due(now):
            return
        claim["verification_attempts"] += 1
        claim["verification_retry_at"] = now + min(
            MAX_RETRY_SECONDS, BASE_RETRY_SECONDS * 2 ** (claim["verification_attempts"] - 1))
        self._save()

    def reconcile_claim(self, evidence: Mapping[str, Any], *, frame_id: str,
                        now: float, continuity: ScopeContinuity | None = None
                        ) -> str | events.WeeklyChestClaimed:
        """Reconcile a durable card or chest tap from fresh same-account evidence."""
        claim = self._row["pending_claim"]
        if claim is None or claim.get("scope", {}).get("account_id") != self.scope.get("account_id"):
            return "unknown"
        if claim.get("kind") == "weekly_chest":
            return self._reconcile_chest(claim, evidence, frame_id=frame_id,
                                         now=now, continuity=continuity)
        if (evidence.get("screen_id") != "missions.daily" or evidence.get("error") is not None
                or type(evidence.get("completed")) is not int
                or type(evidence.get("completed_target")) is not int
                or evidence["completed_target"] != claim.get("completed_target")):
            return "unknown"
        before = claim["completed_before"]
        after = evidence["completed"]
        visible = evidence.get("visible", ())
        identities = {(item[0], item[1]) for item in visible if isinstance(item, (list, tuple))
                      and len(item) >= 2}
        identity = (claim["mission_id"], claim["mission"])
        claimable = {(target.mission_id, target.raw_text)
                     for target in evidence.get("claims", ())}
        source = claim.get("scope", {})
        proven = (isinstance(continuity, ScopeContinuity)
                  and type(claim.get("fact_epoch")) is int
                  and type(self.scope.get("fact_epoch")) is int
                  and continuity.original.account_id == source.get("account_id")
                  and continuity.original.lease_id == source.get("lease_id")
                  and continuity.original.generation == source.get("generation")
                  and continuity.original.epoch == claim["fact_epoch"]
                  and continuity.current.account_id == self.scope.get("account_id")
                  and continuity.current.lease_id == self.scope.get("lease_id")
                  and continuity.current.generation == self.scope.get("generation")
                  and continuity.current.epoch == self.scope["fact_epoch"]
                  and continuity.valid(now=now))
        scope_continuous = source == self.scope or proven
        if (scope_continuous and after == before and identity in identities
                and identity in claimable):
            if claim.get("no_reward_frame") is None:
                claim["no_reward_frame"] = frame_id
                self._save()
                return "unknown"
            if claim["no_reward_frame"] != frame_id and now - claim["prepared_at"] >= 2:
                previous = copy.deepcopy(self._row)
                self._row["pending_claim"] = None
                self._row["resolved_intents"].append(claim["intent_id"])
                self._row["kinds"]["missions"].update(
                    uncertain=False, prior_uncertain=False, in_flight=False)
                try:
                    self.finish("missions", now, claimed=False)
                except Exception:
                    self._row = previous
                    raise
                return "no_reward"
        # A counter move after worker rotation can be external activity. Only
        # the original scoped walk may bind the move to this intent, and the
        # vanished card plus an unchanged neighboring card corroborate it.
        # A typed continuity proof checks the predecessor chain, current
        # lease, epoch and fresh identity. Account equality is insufficient.
        original_scope = scope_continuous
        neighbors = {(item[0], item[1]) for item in claim.get("visible_before", ())
                     if isinstance(item, (list, tuple)) and len(item) >= 2} - {identity}
        if (original_scope and after == before + 1 and identity not in identities
                and bool(neighbors & identities)):
            event = events.MissionClaimed(
                mission=claim["mission"], mission_id=claim["mission_id"],
                coins=claim["coins"], gems=claim["gems"],
                completed_before=before, completed_after=after)
            self.record_receipt(event, now)
            self.finish("missions", now, claimed=True)
            return "claimed"
        return "unknown"

    def _reconcile_chest(self, claim: Mapping[str, Any], evidence: Mapping[str, Any], *,
                         frame_id: str, now: float,
                         continuity: ScopeContinuity | None
                         ) -> str | events.WeeklyChestClaimed:
        """Resolve a restarted chest only from its saved final tap and same-scope art."""
        if (evidence.get("screen_id") != "missions.daily"
                or evidence.get("error") is not None
                or type(evidence.get("completed")) is not int):
            return "unknown"
        source = claim.get("scope", {})
        proven = (isinstance(continuity, ScopeContinuity)
                  and type(claim.get("fact_epoch")) is int
                  and type(self.scope.get("fact_epoch")) is int
                  and continuity.original.account_id == source.get("account_id")
                  and continuity.original.lease_id == source.get("lease_id")
                  and continuity.original.generation == source.get("generation")
                  and continuity.original.epoch == claim["fact_epoch"]
                  and continuity.current.account_id == self.scope.get("account_id")
                  and continuity.current.lease_id == self.scope.get("lease_id")
                  and continuity.current.generation == self.scope.get("generation")
                  and continuity.current.epoch == self.scope["fact_epoch"]
                  and continuity.valid(now=now))
        if source != self.scope and not proven:
            return "unknown"
        state = dict(evidence.get("milestones", ())).get(claim["threshold"])
        if (state == "claimed" and claim.get("final_tapped") is True
                and evidence["completed"] >= claim["completed_before"]):
            event = events.WeeklyChestClaimed(
                threshold=claim["threshold"],
                rewards=tuple(tuple(reward) for reward in claim["rewards"]),
                unreadable_rewards=claim["unreadable_rewards"],
                reward_text=", ".join(claim["reward_texts"]) or None,
                confirmation="chest_marked_claimed")
            stored = self.record_chest_receipt(event, now)
            self.finish("missions", now, claimed=True)
            return stored if stored is not None else "unknown"
        if (state == "claimable" and evidence["completed"] == claim["completed_before"]
                and now - claim["prepared_at"] >= 2):
            if claim.get("no_reward_frame") is None:
                claim["no_reward_frame"] = frame_id
                self._save()
                return "unknown"
            if claim["no_reward_frame"] != frame_id:
                previous = copy.deepcopy(self._row)
                self._row["pending_claim"] = None
                self._row["resolved_intents"].append(claim["intent_id"])
                self._row["kinds"]["missions"].update(
                    uncertain=False, prior_uncertain=False, in_flight=False)
                try:
                    self.finish("missions", now, claimed=False)
                except Exception:
                    self._row = previous
                    raise
                return "no_reward"
        return "unknown"


class MissionReceiptBus:
    """Publish mission events only after their confirmed credit is durable."""

    def __init__(self, bus: Any, notifications: NotificationState) -> None:
        self.bus = bus
        self.notifications = notifications

    def publish(self, event: events.Event) -> events.Event:
        if isinstance(event, events.MissionClaimed):
            if not self.notifications.record_receipt(event, time.time()):
                return event
            key = self.notifications.snapshot()["receipts"][-1]["key"]
            return self.bus.publish(replace(event, receipt_key=key))
        if isinstance(event, events.WeeklyChestClaimed):
            stored = self.notifications.record_chest_receipt(event, time.time())
            return self.bus.publish(stored) if stored is not None else event
        return self.bus.publish(event)

    def prepare_claim(self, **kwargs: Any) -> None:
        self.notifications.prepare_claim(**kwargs)

    def discard_prepared_claim(self, now: float) -> None:
        self.notifications.discard_prepared_claim(now)

    def prepare_chest(self, **kwargs: Any) -> None:
        self.notifications.prepare_chest(**kwargs)

    def observe_chest_reward(self, **kwargs: Any) -> None:
        self.notifications.observe_chest_reward(**kwargs)
