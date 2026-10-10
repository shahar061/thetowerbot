"""Account-bound cadence and round-trip tier selection for occasional push runs."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from policy import AutopilotPolicy, UpgradeRule, choose


@dataclass
class PushState:
    account: str
    farms: int = 0
    phase: str = "farming"
    farm_tier: int | None = None
    target_tier: int | None = None
    run_id: int | None = None
    purpose: str | None = None


class PushRuns:
    def __init__(self, path: Path | None, account: str, *, every: int = 10) -> None:
        self.path = path
        self.every = every
        self.state = PushState(account)
        self.blocker: str | None = None
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text())
                if raw["account"] == account:
                    state = PushState(**raw)
                    if (state.phase not in {"farming", "selecting", "ready", "pushing", "returning"}
                            or type(state.farms) is not int or state.farms < 0
                            or state.purpose not in {None, "farm", "milestone", "push"}
                            or any(value is not None and (type(value) is not int or value < 1)
                                   for value in (state.farm_tier, state.target_tier, state.run_id))
                            or state.phase in {"ready", "pushing", "returning"} and state.farm_tier is None):
                        raise ValueError("invalid push state")
                    if (state.phase in {"ready", "pushing", "returning"} and state.target_tier is None
                            or state.phase == "pushing" and (state.run_id is None or state.purpose != "push")
                            or state.run_id is None and state.purpose is not None):
                        raise ValueError("incomplete push identity")
                    self.state = state
            except (OSError, ValueError, TypeError, KeyError):
                self.blocker = "Push run state is unreadable; tier selection is held."

    def _save(self) -> None:
        if self.path is None or self.blocker:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w") as stream:
            json.dump(asdict(self.state), stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    @property
    def active(self) -> bool:
        return self.state.phase == "pushing"

    @property
    def needs_home(self) -> bool:
        return bool(self.blocker) or self.state.phase != "farming"

    def started(self, run_id: int, purpose: str) -> str:
        if self.blocker:
            return purpose
        if self.state.phase in {"ready", "pushing"}:
            self.state.phase = "pushing"
            self.state.purpose = "push"
            purpose = "milestone"
        elif self.state.run_id is None:
            self.state.purpose = purpose
        self.state.run_id = run_id
        self._save()
        return purpose

    def ended(self, *, abandoned: bool) -> None:
        if self.blocker:
            return
        if self.state.run_id is None:
            if self.state.phase == "ready" and not abandoned:
                # A death modal after a crash between BATTLE and RunStarted
                # still proves that the prepared push was attempted.
                self.state.phase = "returning"
                self._save()
            return
        if self.state.purpose == "push":
            self.state.phase = "returning"
        elif self.state.purpose == "farm" and not abandoned:
            self.state.farms += 1
            if self.every > 0 and self.state.farms >= self.every:
                self.state.phase = "selecting"
        self.state.run_id = None
        self.state.purpose = None
        self._save()

    def menu(self, tier: int | None, higher_available: bool | None) -> str:
        """Return a navigation intent using only observed tier/arrow evidence."""
        if self.blocker:
            return "hold"
        if self.state.phase == "farming":
            return "start"
        if tier is None:
            return "hold"
        if self.state.phase == "pushing":
            # A startup menu means the remembered push no longer runs.
            self.ended(abandoned=True)
        if self.state.phase == "returning":
            if tier != self.state.farm_tier:
                return "previous" if tier > self.state.farm_tier else "next"
            self.state = PushState(self.state.account)
            self._save()
            return "start"
        if self.state.farm_tier is None:
            self.state.farm_tier = tier
            self._save()  # Remember the return tier before the first tap.
        if higher_available is None:
            return "hold"
        if higher_available:
            self.state.phase = "selecting"
            self._save()
            return "next"
        self.state.target_tier = tier
        self.state.phase = "ready"
        self._save()
        return "start"

    def snapshot(self) -> dict[str, Any]:
        return {
            "account": self.state.account,
            "mode": "push" if self.active else "farm",
            "phase": self.state.phase,
            "every": self.every,
            "farms_remaining": max(0, self.every - self.state.farms),
            "farm_tier": self.state.farm_tier,
            "target_tier": self.state.target_tier,
            "blocker": self.blocker,
        }


def validated_snapshot(raw: Any, account: str) -> dict[str, Any] | None:
    """Only expose a well-formed snapshot belonging to the verified account."""
    if not isinstance(raw, dict) or raw.get("account") != account:
        return None
    if (raw.get("mode") not in {"farm", "push"}
            or raw.get("phase") not in {"farming", "selecting", "ready", "pushing", "returning"}
            or type(raw.get("every")) is not int or raw["every"] < 0
            or type(raw.get("farms_remaining")) is not int
            or not 0 <= raw["farms_remaining"] <= raw["every"]
            or "blocker" not in raw or raw["blocker"] is not None and not isinstance(raw["blocker"], str)
            or any(key not in raw or raw[key] is not None and (type(raw[key]) is not int or raw[key] < 1)
                   for key in ("farm_tier", "target_tier"))):
        return None
    return {key: raw[key] for key in ("account", "mode", "phase", "every", "farms_remaining",
                                     "farm_tier", "target_tier", "blocker")}


def push_policy(base: AutopilotPolicy, *, tier: int | None,
                rows: Mapping[str, Mapping[str, Any]] | None = None,
                combat: Mapping[str, float] | None = None) -> AutopilotPolicy:
    """Combat-only spending, balanced by observed price and urgent health."""
    ids = ["thorns", "defense_percent", "health", "attack_speed", "lifesteal",
           "knockback_chance", "knockback_force", "orbs", "orb_speed", "damage"]
    if tier == 1:
        ids.insert(2, "defense_absolute")
    # Keep the build's combat caps and exclusions. Economy rows never enter
    # the push pool, even when the farming guide includes them.
    configured = {rule.upgrade_id: rule for rule in base.effective_rules()}
    rules = [configured.get(uid, UpgradeRule(uid)) for uid in ids]
    rules = [replace(rule, enabled=False)
             if (rows or {}).get(rule.upgrade_id, {}).get("status") == "locked" else rule
             for rule in rules]
    if rows:
        def price(rule: UpgradeRule) -> float:
            value = rows.get(rule.upgrade_id, {}).get("price")
            return float(value) if type(value) in (int, float) and value >= 0 else float("inf")
        rules.sort(key=price)
    current, maximum = (combat or {}).get("health"), (combat or {}).get("max_health")
    if current is not None and maximum is not None and maximum > 0 and current / maximum < .75:
        rules.sort(key=lambda rule: rule.upgrade_id != "health")
    policy = replace(base, preset="manual", rules=tuple(rules), purpose="milestone",
                   economy_until_wave=0, cash_reserve=0, max_purchase_price=None,
                   single_purchase=False, decision_token=None, modeled_pool=False,
                   battle_price_quote=None, burst_price_ceiling=None)
    if current is not None and maximum is not None and maximum > 0 and current / maximum < .75:
        urgent = replace(policy, rules=tuple(rule for rule in rules if rule.upgrade_id == "health"))
        if choose(urgent, rows or {}, combat or {}).upgrade_id == "health":
            # Discovery of off-screen upgrades must not delay urgent healing.
            return urgent
    return policy
