"""Pure, immutable decision policies for OCR-driven battle upgrades."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from upgrades import by_id, target_reached


PRESETS: tuple[str, ...] = ("manual", "turtle", "health")
PURPOSES: tuple[str, ...] = ("farm", "milestone")
_BUYABLE_STATUSES = frozenset({"available"})
_ECONOMY_IDS = frozenset(
    {"cash_per_wave", "coins_per_wave", "cash_bonus", "coins_per_kill_bonus"}
)
_HEALTH_SURVIVAL_IDS = frozenset(
    {
        "health",
        "defense_percent",
        "lifesteal",
        "attack_speed",
        "knockback_chance",
        "knockback_force",
        "orb_speed",
        "orbs",
    }
)


class PolicyError(ValueError):
    """Strict policy validation failure with the offending JSON field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class UpgradeRule:
    """One prioritized semantic upgrade, optionally capped at a target value."""

    upgrade_id: str
    enabled: bool = True
    target: float | None = None

    def __post_init__(self) -> None:
        upgrade = by_id(self.upgrade_id) if isinstance(self.upgrade_id, str) else None
        if upgrade is None:
            raise PolicyError("upgrade_id", f"unknown upgrade_id {self.upgrade_id!r}")
        if upgrade.unlock:
            raise PolicyError(
                "upgrade_id", "Workshop unlock tiles cannot be battle upgrade rules"
            )
        if not isinstance(self.enabled, bool):
            raise PolicyError("enabled", "enabled must be a boolean")
        if self.target is not None:
            if not _is_number(self.target):
                raise PolicyError("target", "target must be a number or null")
            if not math.isfinite(self.target) or self.target < 0:
                raise PolicyError("target", "target must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "upgrade_id": self.upgrade_id,
            "enabled": self.enabled,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> UpgradeRule:
        if not isinstance(raw, Mapping):
            raise PolicyError("rules", "each rule must be a mapping")
        known = {field.name for field in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise PolicyError(key, f"unknown rule field {key!r}")
        if "upgrade_id" not in raw:
            raise PolicyError("upgrade_id", "upgrade_id is required")
        try:
            return cls(**raw)
        except PolicyError:
            raise
        except TypeError as exc:
            raise PolicyError("rules", str(exc)) from None


_PRESET_RULES: dict[str, tuple[UpgradeRule, ...]] = {
    "manual": (),
    "turtle": (
        UpgradeRule("cash_per_wave", target=10),
        UpgradeRule("coins_per_wave", target=10),
        UpgradeRule("cash_bonus"),
        UpgradeRule("coins_per_kill_bonus"),
        UpgradeRule("defense_percent", target=50),
        UpgradeRule("defense_absolute"),
        UpgradeRule("thorns", target=51),
        UpgradeRule("health"),
        UpgradeRule("health_regen"),
    ),
    "health": (
        UpgradeRule("cash_bonus"),
        UpgradeRule("coins_per_kill_bonus"),
        UpgradeRule("cash_per_wave"),
        UpgradeRule("coins_per_wave"),
        UpgradeRule("health"),
        UpgradeRule("defense_percent"),
        UpgradeRule("lifesteal"),
        UpgradeRule("attack_speed"),
        UpgradeRule("knockback_chance"),
        UpgradeRule("knockback_force"),
        UpgradeRule("orb_speed"),
        UpgradeRule("orbs"),
    ),
}


def preset_rules(name: str) -> tuple[UpgradeRule, ...]:
    """Return the immutable, ordered rules for a named guide preset."""

    if not isinstance(name, str) or name not in _PRESET_RULES:
        raise PolicyError("preset", f"preset must be one of {PRESETS}")
    return _PRESET_RULES[name]


@dataclass(frozen=True)
class AutopilotPolicy:
    """Battle automation policy persisted inside a Strategy profile."""

    enabled: bool = False
    preset: str = "manual"
    rules: tuple[UpgradeRule, ...] = ()
    economy_until_wave: int = 20
    survival_buffer: float = 1.2
    cash_reserve: int = 0
    cash_spend_limit_pct: int = 100
    observe_only: bool = False
    max_scrolls: int = 8
    purpose: Literal["farm", "milestone"] = "farm"

    def __post_init__(self) -> None:
        try:
            rules = tuple(self.rules)
        except TypeError:
            raise PolicyError("rules", "rules must be a list or tuple") from None
        object.__setattr__(self, "rules", rules)
        if not isinstance(self.enabled, bool):
            raise PolicyError("enabled", "enabled must be a boolean")
        if not isinstance(self.preset, str) or self.preset not in PRESETS:
            raise PolicyError("preset", f"preset must be one of {PRESETS}")
        if not isinstance(self.purpose, str) or self.purpose not in PURPOSES:
            raise PolicyError("purpose", f"purpose must be one of {PURPOSES}")
        if any(not isinstance(rule, UpgradeRule) for rule in rules):
            raise PolicyError("rules", "rules must contain UpgradeRule values")
        ids = [rule.upgrade_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise PolicyError("rules", "rule upgrade_id values must be unique")
        if isinstance(self.economy_until_wave, bool) or not isinstance(
            self.economy_until_wave, int
        ):
            raise PolicyError("economy_until_wave", "economy_until_wave must be an integer")
        if self.economy_until_wave < 0:
            raise PolicyError("economy_until_wave", "economy_until_wave may not be negative")
        if not _is_number(self.survival_buffer):
            raise PolicyError("survival_buffer", "survival_buffer must be a number")
        if not math.isfinite(self.survival_buffer) or self.survival_buffer <= 0:
            raise PolicyError(
                "survival_buffer", "survival_buffer must be finite and above zero"
            )
        if isinstance(self.cash_reserve, bool) or not isinstance(self.cash_reserve, int):
            raise PolicyError("cash_reserve", "cash_reserve must be an integer")
        if self.cash_reserve < 0:
            raise PolicyError("cash_reserve", "cash_reserve may not be negative")
        if type(self.cash_spend_limit_pct) is not int or not 0 <= self.cash_spend_limit_pct <= 100:
            raise PolicyError("cash_spend_limit_pct", "cash_spend_limit_pct must be 0 to 100")
        if type(self.observe_only) is not bool:
            raise PolicyError("observe_only", "observe_only must be a boolean")
        if isinstance(self.max_scrolls, bool) or not isinstance(self.max_scrolls, int):
            raise PolicyError("max_scrolls", "max_scrolls must be an integer")
        if self.max_scrolls < 0:
            raise PolicyError("max_scrolls", "max_scrolls may not be negative")

    def effective_rules(self) -> tuple[UpgradeRule, ...]:
        """Use explicit draft rules, falling back to the selected guide."""

        return self.rules if self.rules else preset_rules(self.preset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "preset": self.preset,
            "rules": [rule.to_dict() for rule in self.rules],
            "economy_until_wave": self.economy_until_wave,
            "survival_buffer": self.survival_buffer,
            "cash_reserve": self.cash_reserve,
            "cash_spend_limit_pct": self.cash_spend_limit_pct,
            "observe_only": self.observe_only,
            "max_scrolls": self.max_scrolls,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AutopilotPolicy:
        if not isinstance(raw, Mapping):
            raise PolicyError("autopilot", "autopilot must be a mapping")
        known = {field.name for field in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise PolicyError(key, f"unknown autopilot field {key!r}")
        values = {key: value for key, value in raw.items() if key != "rules"}
        if "rules" in raw:
            raw_rules = raw["rules"]
            if not isinstance(raw_rules, (list, tuple)):
                raise PolicyError("rules", "rules must be a list or tuple")
            values["rules"] = tuple(UpgradeRule.from_dict(rule) for rule in raw_rules)
        try:
            return cls(**values)
        except PolicyError:
            raise
        except TypeError as exc:
            raise PolicyError("autopilot", str(exc)) from None


@dataclass(frozen=True)
class Decision:
    """The policy's explanation and optional semantic purchase target."""

    phase: str
    reason: str
    upgrade_id: str | None = None


def _number(mapping: Mapping[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if not _is_number(value):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _observation(
    observations: Mapping[str, Mapping[str, Any]], upgrade_id: str
) -> Mapping[str, Any] | None:
    value = observations.get(upgrade_id)
    return value if isinstance(value, Mapping) else None


def _rule_is_complete(rule: UpgradeRule, observation: Mapping[str, Any]) -> bool:
    if observation.get("status") == "maxed":
        return True
    value = _number(observation, "value")
    if rule.target is None or value is None:
        return False
    return target_reached(rule.upgrade_id, value, rule.target)


def _rule_is_buyable(
    rule: UpgradeRule,
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
    cash_reserve: int,
) -> bool:
    observation = _observation(observations, rule.upgrade_id)
    if observation is None or observation.get("status") not in _BUYABLE_STATUSES:
        return False
    if _number(observation, "value") is None:
        return False
    if _rule_is_complete(rule, observation):
        return False
    cash = _number(combat, "cash")
    price = _number(observation, "price")
    if price is None or price < 0:
        return False
    if cash is None:
        return cash_reserve == 0
    return cash - price >= cash_reserve


def _first_buyable(
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
    cash_reserve: int,
    allowed: frozenset[str] | None = None,
) -> UpgradeRule | None:
    for rule in rules:
        if not rule.enabled or (allowed is not None and rule.upgrade_id not in allowed):
            continue
        if _rule_is_buyable(rule, observations, combat, cash_reserve):
            return rule
    return None


def _name(upgrade_id: str) -> str:
    upgrade = by_id(upgrade_id)
    return upgrade.name if upgrade is not None else upgrade_id


def _finished_or_waiting(
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
) -> Decision:
    active = tuple(rule for rule in rules if rule.enabled)
    if active and all(
        (observation := _observation(observations, rule.upgrade_id)) is not None
        and _rule_is_complete(rule, observation)
        for rule in active
    ):
        return Decision("complete", "Every enabled upgrade target is complete.")
    return Decision("wait", "No enabled observed upgrade is currently purchasable.")


def _turtle_thorns_breakpoint(wave: float, economy_until_wave: int) -> int:
    """Stage discrete boss-hit breakpoints without mutable policy state."""

    stage = max(1, economy_until_wave)
    if wave <= stage * 2:
        return 11
    if wave <= stage * 4:
        return 21
    if wave <= stage * 8:
        return 34
    return 51


def _first_turtle_survival(
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
    cash_reserve: int,
    thorns_breakpoint: int,
) -> UpgradeRule | None:
    for rule in rules:
        if (
            not rule.enabled
            or rule.upgrade_id in _ECONOMY_IDS
            or rule.upgrade_id == "defense_absolute"
        ):
            continue
        if rule.upgrade_id == "thorns":
            thorns = _observation(observations, "thorns")
            thorns_value = _number(thorns, "value") if thorns is not None else None
            if thorns_value is None or thorns_value >= thorns_breakpoint:
                continue
        if _rule_is_buyable(rule, observations, combat, cash_reserve):
            return rule
    return None


def _choose_turtle(
    policy: AutopilotPolicy,
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
) -> Decision:
    enemy_damage = _number(combat, "enemy_damage")
    defense_percent_obs = _observation(observations, "defense_percent")
    defense_absolute_obs = _observation(observations, "defense_absolute")
    defense_percent = (
        _number(defense_percent_obs, "value") if defense_percent_obs is not None else None
    )
    defense_absolute = (
        _number(defense_absolute_obs, "value") if defense_absolute_obs is not None else None
    )
    if enemy_damage is None:
        return Decision("wait", "Enemy damage is unknown; Turtle cannot assess survival.")
    if defense_percent is None:
        return Decision("wait", "Defense % is unknown; Turtle cannot assess survival.")
    if defense_absolute is None:
        return Decision(
            "wait", "Defense Absolute is unknown; Turtle cannot assess survival."
        )

    remaining_damage = enemy_damage * max(0.0, 1.0 - defense_percent / 100.0)
    required_absolute = remaining_damage * policy.survival_buffer
    absolute_rule = next(
        (
            rule
            for rule in rules
            if rule.enabled and rule.upgrade_id == "defense_absolute"
        ),
        None,
    )
    if defense_absolute < required_absolute and absolute_rule is not None:
        if _rule_is_buyable(
            absolute_rule, observations, combat, policy.cash_reserve
        ):
            return Decision(
                "survival",
                "Defense Absolute is below the buffered post-mitigation enemy damage.",
                "defense_absolute",
            )
        return Decision(
            "wait", "Defense Absolute needs attention but is not currently purchasable."
        )

    wave = _number(combat, "wave")
    if wave is None:
        return Decision("wait", "Wave is unknown; Turtle cannot select its guide phase.")
    if wave <= policy.economy_until_wave:
        economy = _first_buyable(
            rules, observations, combat, policy.cash_reserve, _ECONOMY_IDS
        )
        if economy is not None:
            return Decision(
                "economy",
                f"Early economy is active through wave {policy.economy_until_wave}.",
                economy.upgrade_id,
            )

    thorns_breakpoint = _turtle_thorns_breakpoint(
        wave, policy.economy_until_wave
    )
    next_rule = _first_turtle_survival(
        rules,
        observations,
        combat,
        policy.cash_reserve,
        thorns_breakpoint,
    )
    if next_rule is None:
        return _finished_or_waiting(rules, observations)
    if next_rule.upgrade_id == "thorns":
        return Decision(
            "survival",
            f"Advance Thorns to the {thorns_breakpoint}% survival breakpoint.",
            "thorns",
        )
    return Decision(
        "survival",
        f"Turtle priority selects {_name(next_rule.upgrade_id)}.",
        next_rule.upgrade_id,
    )


def _choose_health(
    policy: AutopilotPolicy,
    rules: tuple[UpgradeRule, ...],
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
) -> Decision:
    health = _number(combat, "health")
    max_health = _number(combat, "max_health")
    if health is None or max_health is None or max_health <= 0:
        return Decision("wait", "Current and maximum health are required by the Health guide.")

    if health / max_health < 0.75:
        survival = _first_buyable(
            rules, observations, combat, policy.cash_reserve, _HEALTH_SURVIVAL_IDS
        )
        if survival is not None:
            return Decision(
                "survival",
                "Tower health is below the guide's survival threshold.",
                survival.upgrade_id,
            )
        return Decision("wait", "Survival upgrades are not currently purchasable.")

    wave = _number(combat, "wave")
    if wave is None:
        return Decision("wait", "Wave is unknown; Health cannot select its guide phase.")
    if wave <= policy.economy_until_wave:
        economy = _first_buyable(
            rules, observations, combat, policy.cash_reserve, _ECONOMY_IDS
        )
        if economy is not None:
            return Decision(
                "economy",
                "Tower health is stable, so economy continues through wave "
                f"{policy.economy_until_wave}.",
                economy.upgrade_id,
            )

    next_rule = _first_buyable(
        rules,
        observations,
        combat,
        policy.cash_reserve,
        _HEALTH_SURVIVAL_IDS,
    )
    if next_rule is None:
        return _finished_or_waiting(rules, observations)
    return Decision(
        "survival",
        f"Health priority selects {_name(next_rule.upgrade_id)}.",
        next_rule.upgrade_id,
    )


def choose(
    policy: AutopilotPolicy,
    observations: Mapping[str, Mapping[str, Any]],
    combat: Mapping[str, Any],
) -> Decision:
    """Choose the next semantic upgrade without navigation, I/O or mutation."""

    if not policy.enabled:
        return Decision("disabled", "Autopilot is disabled.")
    rules = policy.effective_rules()
    cash = _number(combat, "cash")
    if cash is not None and policy.cash_spend_limit_pct < 100:
        ceiling = int(cash * policy.cash_spend_limit_pct / 100)
        observations = {
            uid: ({**row, "status": "unaffordable"}
                  if _number(row, "price") is not None and _number(row, "price") > ceiling
                  else row)
            for uid, row in observations.items()
        }
    if policy.preset == "turtle":
        return _choose_turtle(policy, rules, observations, combat)
    if policy.preset == "health":
        return _choose_health(policy, rules, observations, combat)
    next_rule = _first_buyable(rules, observations, combat, policy.cash_reserve)
    if next_rule is None:
        return _finished_or_waiting(rules, observations)
    return Decision(
        "manual",
        f"Highest-priority available rule is {_name(next_rule.upgrade_id)}.",
        next_rule.upgrade_id,
    )
