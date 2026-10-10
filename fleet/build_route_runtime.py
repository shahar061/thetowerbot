"""Worker-side Build Route reload and account-bound acknowledgement."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from fleet.build_route import RouteDocument
from fleet.build_route_eval import PendingDecision
from fleet.build_route_eval import RouteFacts
from fleet.build_route_eval import RouteEvaluation
from fleet.build_route_eval import ResourceEvaluation
from fleet.build_route_store import BuildRouteStore, RouteUnavailable, _write_json_atomic
from fleet.workshop_plan_history import save_snapshot
import db
import upgrades


WORKSHOP_PLAN_FILE = "build-route-workshop.json"


def read_applied_revision(path: Path, account_id: str) -> int | None:
    """The route revision this worker last acknowledged, if it is this account's."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    revision = raw.get("revision") if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or raw.get("account_id") != account_id
            or type(revision) is not int or revision < 0):
        return None
    return revision


class BuildRouteRuntime:
    def __init__(self, root: Path, worker: str, account_id: str) -> None:
        self.root = Path(root)
        self.worker = worker
        self.account_id = account_id
        self.store = BuildRouteStore(self.root)
        self.target_context = None
        self.expected_revision: int | None = None
        self.state_dir = self.root / "workers" / worker
        self.db_path = self.state_dir / "tower_bot.db"
        self._error: str | None = None
        self.ack_path = self.root / "workers" / worker / "build-route-applied.json"
        self.choice_path = self.root / "workers" / worker / "build-route-choice.json"
        self.facts_path = self.root / "workers" / worker / "build-route-facts.json"

    @classmethod
    def for_target(cls, context: Any) -> BuildRouteRuntime:
        context.verify_current()
        runtime = cls(context.assignment_root, context.target_id, context.account_id)
        runtime.target_context = context
        runtime.state_dir = context.state_dir
        runtime.db_path = context.db_path
        runtime.ack_path = context.state_dir / "build-route-applied.json"
        runtime.choice_path = context.state_dir / "build-route-choice.json"
        runtime.facts_path = context.state_dir / "build-route-facts.json"
        return runtime

    def verify_binding(self) -> None:
        if self.target_context is not None:
            self.target_context.verify_current()
            return
        from web.account_catalog import registered_worker
        registration = registered_worker(self.state_dir)
        if (registration is None or registration.account_id != self.account_id
                or db.bound_account(self.db_path) != self.account_id):
            raise ValueError("route requires a verified worker account")

    def current(self) -> RouteDocument:
        try:
            if self.target_context is not None:
                self.verify_binding()
            route = self.store.read()
            if self.expected_revision is not None and route.revision != self.expected_revision:
                raise RouteUnavailable("strategy_revision_changed_during_decision")
        except RouteUnavailable as exc:
            self._error = exc.reason
            raise
        self._error = None
        return route

    def error(self) -> str | None:
        return self._error

    def pending(self) -> PendingDecision | None:
        raw = self._choice_state().get("pending")
        if not isinstance(raw, dict):
            return None
        try:
            pending = PendingDecision(**raw)
        except (TypeError, ValueError):
            return None
        return pending if pending.account_id == self.account_id else None

    def purchase_counts(self, lane: str, run_id: int | None = None) -> dict[str, int] | None:
        """Count confirmed debits/events, never taps or inferred skill levels."""
        path = self.db_path
        if db.bound_account(path) != self.account_id:
            return None
        counts: dict[str, int] = {}
        with db.reader(path) as connection:
            if lane == "battle":
                if run_id is None:
                    return None
                for purchase in db.run_purchases(connection, run_id):
                    uid = purchase.get("upgrade_id")
                    if isinstance(uid, str) and upgrades.by_id(uid) is not None:
                        counts[uid] = counts.get(uid, 0) + 1
                return counts
            if lane != "workshop":
                raise ValueError("unknown purchase count lane")
            rows = connection.execute(
                "SELECT item,category,detail FROM ledger WHERE kind='WORKSHOP_BUY' "
                "AND currency='coins' AND dry_run=0").fetchall()
        for row in rows:
            try:
                verdict = json.loads(row["detail"] or "{}").get("verdict")
            except (ValueError, TypeError, AttributeError):
                continue
            upgrade = upgrades.resolve(row["item"], row["category"])
            if verdict in {"bought", "free"} and upgrade is not None:
                counts[upgrade.id] = counts.get(upgrade.id, 0) + 1
        return counts

    def battle_pending(self, facts: RouteFacts, revision: int) -> PendingDecision | None:
        path = self.choice_path.with_name("build-route-battle-choice.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if self.target_context is not None and raw.get("scope") != self._choice_scope():
                return None
            raw.pop("scope", None)
            pending = PendingDecision(**raw)
        except (OSError, ValueError, TypeError):
            return None
        if (pending.account_id == self.account_id and pending.revision == revision
                and pending.visit_id == facts.visit_id and pending.sequence == facts.decision_sequence):
            return pending
        return None

    def remember_battle_pending(self, pending: PendingDecision) -> None:
        if pending.account_id != self.account_id:
            raise ValueError("battle choice belongs to another account")
        from fleet.build_route_store import _write_json_atomic
        self.verify_binding() if self.target_context is not None else None
        raw = asdict(pending)
        if self.target_context is not None:
            raw["scope"] = self._choice_scope()
        _write_json_atomic(self.choice_path.with_name("build-route-battle-choice.json"), raw)

    def sequence(self) -> int:
        value = self._choice_state().get("sequence")
        return value if type(value) is int and value >= 0 else 0

    def _choice_state(self) -> dict[str, object]:
        try:
            raw = json.loads(self.choice_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict) or raw.get("account_id") != self.account_id:
            return {}
        if self.target_context is not None and raw.get("scope") != self._choice_scope():
            return {}
        return raw

    def _choice_scope(self) -> dict[str, object]:
        scope = self.target_context.scope
        return {"generation": scope.generation, "epoch": scope.epoch, "lease_id": scope.lease_id}

    def _write_choice(self, value: dict[str, object]) -> None:
        if self.target_context is not None:
            self.verify_binding()
            value = {**value, "scope": self._choice_scope()}
        self.choice_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.choice_path.with_name(f".build-route-choice.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(value, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.choice_path)
        finally:
            temporary.unlink(missing_ok=True)

    def remember_pending(self, pending: PendingDecision) -> None:
        if pending.account_id != self.account_id or pending.sequence != self.sequence():
            raise ValueError("pending decision belongs to another account or sequence")
        existing = self.pending()
        if existing is not None and existing != pending:
            raise ValueError("pending decision requires explicit abandonment")
        self._write_choice({"account_id": self.account_id,
                            "sequence": pending.sequence, "pending": asdict(pending),
                            "created_at": time.time()})

    def sync_confirmed(self) -> None:
        """Advance a draw only after the shopping ledger proves its purchase."""
        pending = self.pending()
        if pending is None:
            return
        created_at = self._choice_state().get("created_at")
        if not isinstance(created_at, (int, float)):
            return
        db_path = self.db_path
        if db.bound_account(db_path) != self.account_id:
            return
        with db.reader(db_path) as connection:
            rows = connection.execute(
                "SELECT id,item,category,detail FROM ledger WHERE kind='WORKSHOP_BUY' "
                "AND dry_run=0 AND ts>=? ORDER BY id", (created_at,)).fetchall()
        for row in rows:
            upgrade = upgrades.resolve(row["item"], row["category"])
            if upgrade is not None and upgrade.id == pending.chosen_id:
                try:
                    self.confirm_purchase(row["id"], pending.chosen_id)
                except ValueError:
                    continue
                return

    def publish_facts(self, facts: RouteFacts) -> None:
        if facts.account_id != self.account_id or facts.worker != self.worker:
            raise ValueError("route facts belong to another account or worker")
        self.verify_binding()
        self.facts_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.facts_path.with_name(f".build-route-facts.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(asdict(facts), output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.facts_path)
        finally:
            temporary.unlink(missing_ok=True)

    def publish_battle_evaluation(self, evaluation: RouteEvaluation,
                                  facts: RouteFacts | None = None) -> None:
        if evaluation.account_id != self.account_id:
            raise ValueError("battle evaluation belongs to another account")
        path = self.state_dir / "build-route-battle.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".build-route-battle.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(asdict(evaluation), output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if facts is not None:
            if facts.account_id != self.account_id or facts.worker != self.worker:
                raise ValueError("battle facts belong to another account")
            facts_path = path.with_name("build-route-battle-facts.json")
            temporary = facts_path.with_name(f".build-route-battle-facts.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(asdict(facts), output, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, facts_path)
            finally:
                temporary.unlink(missing_ok=True)

    def publish_workshop_plan(self, record: dict[str, Any]) -> str:
        """Save the trace behind this worker's latest Workshop decision (Plan graph tab)."""
        if record.get("account_id") != self.account_id:
            raise ValueError("workshop plan belongs to another account")
        worker_dir = self.state_dir
        saved = save_snapshot(worker_dir, record)
        _write_json_atomic(worker_dir / WORKSHOP_PLAN_FILE, saved)
        return saved["id"]

    def publish_resources(self, evaluation: ResourceEvaluation,
                          facts: RouteFacts | None = None) -> None:
        path = self.state_dir / "build-route-resources.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".build-route-resources.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"account_id": self.account_id,
                           "revision": self.current().revision,
                           "observed_at": time.time(),
                           **asdict(evaluation)}, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if facts is not None:
            if facts.account_id != self.account_id or facts.worker != self.worker:
                raise ValueError("resource facts belong to another account")
            facts_path = path.with_name("build-route-resource-facts.json")
            temporary = facts_path.with_name(f".build-route-resource-facts.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(asdict(facts), output, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, facts_path)
            finally:
                temporary.unlink(missing_ok=True)

    def confirm_purchase(self, event_id: int, upgrade_id: str) -> None:
        pending = self.pending()
        if pending is None or pending.chosen_id != upgrade_id:
            raise ValueError("no matching pending purchase")
        db_path = self.db_path
        if db.bound_account(db_path) != self.account_id:
            raise ValueError("worker database account changed")
        with db.reader(db_path) as connection:
            row = connection.execute(
                "SELECT kind,item,category,currency,dry_run,detail FROM ledger WHERE id=?",
                (event_id,)).fetchone()
        if row is None or row["kind"] != "WORKSHOP_BUY" or row["currency"] != "coins" or row["dry_run"]:
            raise ValueError("purchase event is not a confirmed Workshop debit")
        try:
            verdict = json.loads(row["detail"] or "{}").get("verdict")
        except (ValueError, TypeError, AttributeError):
            verdict = None
        upgrade = upgrades.resolve(row["item"], row["category"])
        if verdict not in {"bought", "free"} or upgrade is None or upgrade.id != upgrade_id:
            raise ValueError("purchase event does not confirm selected upgrade")
        total = sum(pending.eligible_weights.values())
        detail = {
            "route_revision": pending.revision,
            "matched_rule_id": pending.matched_rule_id,
            "purchase_event_id": event_id,
            "selected_upgrade_id": upgrade_id,
            "draw_gate": pending.draw_gate,
            "eligible_weights": dict(pending.eligible_weights),
            "eligible_odds": {uid: weight / total for uid, weight in pending.eligible_weights.items()}
            if total else {},
        }
        reason = f"purchase_event:{event_id}"
        with db.connect(db_path) as connection:
            existing = connection.execute(
                "SELECT id FROM ledger WHERE kind='ROUTE_DECISION' AND reason=?",
                (reason,)).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,reason,detail) "
                    "VALUES(?,?,?,?,?,0,?,?)",
                    (time.time(), "ROUTE_DECISION", upgrade.name, upgrade.category,
                     "coins", reason, json.dumps(detail, sort_keys=True)))
        self._write_choice({"account_id": self.account_id, "sequence": pending.sequence + 1,
                            "pending": None, "confirmed_event_id": event_id})

    def abandon_decision(self, reason: str) -> None:
        pending = self.pending()
        if pending is None or not reason:
            raise ValueError("pending decision and abandonment reason required")
        self._write_choice({"account_id": self.account_id, "sequence": pending.sequence + 1,
                            "pending": None, "abandoned_reason": reason})

    def acknowledge(self, revision: int, account_id: str) -> None:
        if account_id != self.account_id:
            raise ValueError("worker account changed before route acknowledgement")
        route = self.current()
        if route.revision != revision:
            raise ValueError("route changed before acknowledgement")
        metadata = {}
        if self.target_context is not None:
            self.verify_binding()
            assignment = route.assignments.get(self.worker)
            if assignment is None or assignment.account_id != self.account_id:
                raise ValueError("route assignment no longer matches account")
            metadata = {"target_id": self.worker, "strategy_id": assignment.strategy_id,
                        "strategy_version": assignment.strategy_version,
                        "generation": self.target_context.scope.generation,
                        "epoch": self.target_context.scope.epoch}
        cards = None
        overlay = route.card_assignments.get(account_id)
        if overlay is not None:
            from fleet.build_route import resolve_route
            self.verify_binding()
            effective = resolve_route(route, self.worker, account_id)
            if effective.cards == overlay.program:
                cards = {"overlay_id": overlay.overlay_id,
                         "published_revision": overlay.published_revision}
        self.ack_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ack_path.with_name(f".build-route-applied.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"account_id": account_id, "revision": revision,
                           "observed_at": time.time(), "cards": cards, **metadata}, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.ack_path)
        finally:
            temporary.unlink(missing_ok=True)

    def applied_revision(self) -> int | None:
        return read_applied_revision(self.ack_path, self.account_id)
