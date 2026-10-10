"""Tournament-only configuration and cash/combat rule resolution."""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from policy import AutopilotPolicy, PolicyError, UpgradeRule
from upgrades import by_id, target_reached

FORBIDDEN = frozenset({"coins_per_wave", "coins_per_kill_bonus"})
CASH_IDS = ("cash_bonus", "cash_per_wave")
UTILITY_IDS = frozenset({*CASH_IDS, "recovery_amount", "max_recovery", "package_chance",
                         "enemy_attack_level_skip", "enemy_health_level_skip"})
GROWTH_IDS = frozenset({"health", "attack_speed", "damage"})
DEFAULT_COMBAT_RULES = tuple(UpgradeRule(uid) for uid in ("health", "attack_speed", "damage"))


def _number(value: object, field: str, *, integral: bool = False) -> None:
    if value is None:
        return
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (integral and type(value) is not int)):
        raise PolicyError(field, f"{field} must be a finite non-negative {'integer' if integral else 'number'} or null")


def _keys(raw: Mapping[str, Any], allowed: set[str]) -> None:
    if not isinstance(raw, Mapping):
        raise PolicyError("tournament", "tournament configuration must be an object")
    for key in raw:
        if key not in allowed:
            raise PolicyError(key, f"unknown tournament field {key!r}")


@dataclass(frozen=True)
class OpeningCash:
    cash_bonus_target: float | None = None
    cash_per_wave_target: float | None = None
    cash_budget: int | None = None
    until_wave: int | None = None

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            _number(getattr(self, field.name), field.name,
                    integral=field.name in {"cash_budget", "until_wave"})

    @property
    def configured(self) -> bool:
        return all(getattr(self, field.name) is not None for field in dataclasses.fields(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OpeningCash:
        _keys(raw, {f.name for f in dataclasses.fields(cls)})
        return cls(**raw)


@dataclass(frozen=True)
class TournamentConfig:
    enabled: bool = True
    public_name: str | None = None
    opening_cash: OpeningCash = OpeningCash()
    rules: tuple[UpgradeRule, ...] = DEFAULT_COMBAT_RULES
    cash_reserve: int = 0
    cash_spend_limit_pct: int = 100

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise PolicyError("enabled", "enabled must be boolean")
        if self.public_name is not None and (not isinstance(self.public_name, str)
                or not self.public_name.strip() or len(self.public_name) > 24
                or not self.public_name.isascii() or not all(c.isalnum() or c in ' _-' for c in self.public_name)):
            raise PolicyError("public_name", "use 1–24 ASCII letters, numbers, spaces, underscore or hyphen")
        if not isinstance(self.opening_cash, OpeningCash):
            raise PolicyError("opening_cash", "opening_cash must be an object")
        _number(self.cash_reserve, "cash_reserve", integral=True)
        _number(self.cash_spend_limit_pct, "cash_spend_limit_pct", integral=True)
        if self.cash_reserve is None or self.cash_spend_limit_pct is None or self.cash_spend_limit_pct > 100:
            raise PolicyError("cash_spend_limit_pct", "reserve/percentage must be integers; percentage is 0–100")
        object.__setattr__(self, "rules", tuple(self.rules))
        for rule in self.rules:
            upgrade = by_id(rule.upgrade_id)
            if rule.upgrade_id in FORBIDDEN or upgrade is None or upgrade.unlock:
                raise PolicyError("rules", "coin upgrades and unlocks are forbidden in tournaments")
            if upgrade.category == "UTILITY" and rule.upgrade_id not in UTILITY_IDS:
                raise PolicyError("rules", "utility upgrade is not allowed in tournaments")
            if rule.upgrade_id in CASH_IDS:
                raise PolicyError("rules", "configure cash upgrades through opening_cash")
            if rule.upgrade_id not in GROWTH_IDS and rule.target is None:
                raise PolicyError("rules", "non-growth tournament rules need a finite target")
        ids = [rule.upgrade_id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise PolicyError("rules", "tournament rules must be unique")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TournamentConfig:
        _keys(raw, {f.name for f in dataclasses.fields(cls)})
        values = dict(raw)
        if "rules" in values:
            if not isinstance(values["rules"], (list, tuple)):
                raise PolicyError("rules", "rules must be an array")
            values["rules"] = tuple(UpgradeRule.from_dict(r) for r in values["rules"])
        if "opening_cash" in values:
            values["opening_cash"] = OpeningCash.from_dict(values["opening_cash"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "public_name": self.public_name,
                "opening_cash": dataclasses.asdict(self.opening_cash),
                "rules": [r.to_dict() for r in self.rules], "cash_reserve": self.cash_reserve,
                "cash_spend_limit_pct": self.cash_spend_limit_pct}


@dataclass(frozen=True)
class PurchaseCursor:
    opening_spent: int = 0
    growth_index: int = 0
    receipt_index: int = 0


def resolve_policy(config: TournamentConfig, cursor: PurchaseCursor,
                   observations: Mapping[str, Mapping[str, Any]],
                   combat: Mapping[str, Any]) -> AutopilotPolicy:
    # Validate again at the runtime boundary; no farm preset or build route is used.
    config = TournamentConfig.from_dict(config.to_dict())
    opening = config.opening_cash
    wave = combat.get("wave")
    opening_rules: list[UpgradeRule] = []
    if (opening.configured and isinstance(wave, (int, float)) and not isinstance(wave, bool)
            and math.isfinite(wave) and 0 <= wave <= opening.until_wave
            and not combat.get("tournament_survival_pressure", False)):
        remaining = opening.cash_budget - cursor.opening_spent
        for uid, target in zip(CASH_IDS, (opening.cash_bonus_target, opening.cash_per_wave_target)):
            row = observations.get(uid, {})
            price, value = row.get("price"), row.get("value")
            if (row.get("status") == "available" and type(price) is int and 0 < price <= remaining
                    and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and not target_reached(uid, float(value), target)):
                opening_rules.append(UpgradeRule(uid, target=target))
    fixed = tuple(r for r in config.rules if r.upgrade_id not in GROWTH_IDS or r.target is not None)
    growth = tuple(r for r in config.rules if r.upgrade_id in GROWTH_IDS and r.target is None)
    index = cursor.growth_index % len(growth) if growth else 0
    ordered = tuple(opening_rules) + fixed + growth[index:] + growth[:index]
    # While opening, one-level purchases ensure the remaining budget cannot be
    # exceeded by a burst. Combat rules remain available when cash is capped.
    return AutopilotPolicy(enabled=config.enabled, preset="manual", rules=ordered,
                           cash_reserve=config.cash_reserve,
                           cash_spend_limit_pct=config.cash_spend_limit_pct,
                           single_purchase=True, decision_token=f"tournament:{cursor.receipt_index}",
                           burst_price_ceiling=None)


def advance_purchase(cursor: PurchaseCursor, upgrade_id: str, verified_cost: int,
                     rules: tuple[UpgradeRule, ...]) -> PurchaseCursor:
    if type(verified_cost) is not int or verified_cost < 0:
        raise PolicyError("verified_cost", "purchase requires a verified non-negative cost")
    cursor = dataclasses.replace(cursor, receipt_index=cursor.receipt_index + 1)
    growth = [r.upgrade_id for r in rules if r.upgrade_id in GROWTH_IDS and r.target is None]
    if upgrade_id in CASH_IDS:
        return dataclasses.replace(cursor, opening_spent=cursor.opening_spent + verified_cost)
    if upgrade_id in growth:
        return dataclasses.replace(cursor, growth_index=(growth.index(upgrade_id) + 1) % len(growth))
    return cursor
