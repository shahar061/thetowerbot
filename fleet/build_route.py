"""Validated, account-bound Build Route documents for reroll workers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import re
from typing import Any, Mapping

import upgrades


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return value


def _keys(raw: Mapping[str, object], allowed: set[str]) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown route field: {sorted(unknown)[0]}")


def _percent(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError(f"{name} must be an integer from 0 to 100")
    return value


def _positive_weight(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("route weight must be a positive integer")
    return value


def _upgrade_ids(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of upgrade IDs")
    ids = tuple(value)
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate {name}")
    if any(upgrades.by_id(uid) is None for uid in ids):
        raise ValueError("unknown Workshop upgrade ID" if "Workshop" in name else "unknown battle upgrade ID")
    return ids


def _weights(value: object) -> dict[str, int]:
    raw = _mapping(value, "weights")
    if any(upgrades.by_id(uid) is None for uid in raw):
        raise ValueError("unknown weighted upgrade ID")
    return {uid: _positive_weight(weight) for uid, weight in raw.items()}


@dataclass(frozen=True)
class WorkshopRoute:
    id: str = "workshop.default"
    mode: str = "legacy_planner"
    priority_ids: tuple[str, ...] = ()
    banned_upgrade_ids: frozenset[str] = frozenset()
    coin_spend_limit_pct: int = 100
    draw_chance_pct: int = 0
    weights: dict[str, int] = field(default_factory=dict)
    blocks: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> WorkshopRoute:
        raw = _mapping(value, "workshop")
        _keys(raw, {"id", "mode", "priority_ids", "banned_upgrade_ids", "coin_spend_limit_pct",
                    "draw_chance_pct", "weights", "blocks"})
        mode = raw.get("mode", "legacy_planner")
        if mode not in {"legacy_planner", "priorities", "blocks"}:
            raise ValueError("unknown Workshop mode")
        priorities = _upgrade_ids(raw.get("priority_ids", []), "Workshop priority")
        banned = _upgrade_ids(raw.get("banned_upgrade_ids", []), "Workshop ban")
        chance = _percent(raw.get("draw_chance_pct", 0), "draw_chance_pct")
        weights = _weights(raw.get("weights", {}))
        if chance and (mode != "priorities" or not priorities):
            raise ValueError("weighted Workshop draw requires route priorities")
        if any(uid not in priorities for uid in weights):
            raise ValueError("weighted Workshop upgrade must be a priority")
        return cls(
            id=_rule_id(raw.get("id", "workshop.default")), mode=mode,
            priority_ids=priorities, banned_upgrade_ids=frozenset(banned),
            coin_spend_limit_pct=_percent(raw.get("coin_spend_limit_pct", 100), "coin_spend_limit_pct"),
            draw_chance_pct=chance,
            weights=weights,
            blocks=_blocks(raw.get("blocks", []), "workshop", mode),
        )


def _blocks(value: object, lane: str, mode: str) -> tuple[dict[str, Any], ...]:
    if mode != "blocks" and value:
        raise ValueError("blocks require blocks mode")
    if mode != "blocks":
        return ()
    from fleet.strategy_blocks import validate_program
    return validate_program(value, lane)


def _rule_id(value: object) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("route rule ID must be a nonempty token")
    return value


@dataclass(frozen=True)
class BattlePhase:
    id: str
    start_wave: int
    end_wave: int | None
    priority_ids: tuple[str, ...] = ()
    cash_spend_limit_pct: int = 100
    draw_chance_pct: int = 0
    weights: dict[str, int] = field(default_factory=dict)
    emergency_survival: bool = True

    @classmethod
    def from_dict(cls, value: object) -> BattlePhase:
        raw = _mapping(value, "battle phase")
        _keys(raw, {"id", "start_wave", "end_wave", "priority_ids", "cash_spend_limit_pct",
                    "draw_chance_pct", "weights", "emergency_survival"})
        start, end = raw.get("start_wave"), raw.get("end_wave")
        if type(start) is not int or start < 1 or (end is not None and
                                                (type(end) is not int or end < start)):
            raise ValueError("invalid battle wave phase")
        emergency = raw.get("emergency_survival", True)
        if type(emergency) is not bool:
            raise ValueError("emergency_survival must be boolean")
        chance = _percent(raw.get("draw_chance_pct", 0), "draw_chance_pct")
        priorities = _battle_ids(raw.get("priority_ids", []))
        weights = _weights(raw.get("weights", {}))
        if chance and not priorities:
            raise ValueError("weighted battle draw requires phase priorities")
        if any(uid not in priorities for uid in weights):
            raise ValueError("weighted battle upgrade must be a phase priority")
        return cls(
            _rule_id(raw.get("id")), start, end,
            priorities,
            _percent(raw.get("cash_spend_limit_pct", 100), "cash_spend_limit_pct"),
            chance,
            weights, emergency,
        )


def _battle_ids(value: object) -> tuple[str, ...]:
    ids = _upgrade_ids(value, "battle priority")
    if any(upgrades.by_id(uid).unlock for uid in ids):
        raise ValueError("Workshop unlock tiles cannot be battle priorities")
    return ids


@dataclass(frozen=True)
class BattleBranch:
    id: str
    min_best_tier_1_wave: int | None
    phases: tuple[BattlePhase, ...]

    @classmethod
    def from_dict(cls, value: object) -> BattleBranch:
        raw = _mapping(value, "battle branch")
        _keys(raw, {"id", "min_best_tier_1_wave", "phases"})
        minimum = raw.get("min_best_tier_1_wave")
        if minimum is not None and (type(minimum) is not int or minimum < 0):
            raise ValueError("invalid highest-wave gate")
        phases_raw = raw.get("phases", [])
        if not isinstance(phases_raw, (list, tuple)):
            raise ValueError("battle phases must be a list")
        phases = tuple(BattlePhase.from_dict(item) for item in phases_raw)
        ordered = sorted(phases, key=lambda phase: phase.start_wave)
        if any(a.end_wave is None or a.end_wave >= b.start_wave
               for a, b in zip(ordered, ordered[1:])):
            raise ValueError("overlapping wave phases")
        return cls(_rule_id(raw.get("id")), minimum, phases)


@dataclass(frozen=True)
class BattleRoute:
    mode: str = "legacy_policy"
    branches: tuple[BattleBranch, ...] = ()
    blocks: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> BattleRoute:
        raw = _mapping(value, "battle")
        _keys(raw, {"mode", "branches", "blocks"})
        mode = raw.get("mode", "legacy_policy")
        if mode not in {"legacy_policy", "phases", "blocks"}:
            raise ValueError("unknown battle mode")
        branches_raw = raw.get("branches", [])
        if not isinstance(branches_raw, (list, tuple)):
            raise ValueError("battle branches must be a list")
        branches = tuple(BattleBranch.from_dict(item) for item in branches_raw)
        if len({branch.id for branch in branches}) != len(branches):
            raise ValueError("duplicate battle branch")
        thresholds = [branch.min_best_tier_1_wave for branch in branches]
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("duplicate highest-wave gate")
        if mode == "phases" and not any(branch.min_best_tier_1_wave is None for branch in branches):
            raise ValueError("battle phases require a conservative fallback branch")
        if mode == "phases" and any(not branch.phases or
                                    any(not phase.priority_ids for phase in branch.phases)
                                    for branch in branches):
            raise ValueError("active battle phases require upgrade priorities")
        return cls(mode, branches, _blocks(raw.get("blocks", []), "battle", mode))


def _resource_blocks(value: object, lane: str, mode: str) -> tuple[dict[str, Any], ...]:
    if mode not in {"steps", "blocks"}:
        raise ValueError(f"unknown {lane} mode")
    if mode != "blocks" and value:
        raise ValueError("blocks require blocks mode")
    if mode != "blocks":
        return ()
    from fleet.resource_blocks import validate_gems, validate_labs
    return validate_gems(value) if lane == "gems" else validate_labs(value)


@dataclass(frozen=True)
class GemRoute:
    lab_slot2_reserve: int = 100
    spend_limit_pct: int = 100
    steps: tuple[str, ...] = ("unlock_lab_slot_2",)
    mode: str = "steps"
    blocks: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> GemRoute:
        raw = _mapping(value, "gems")
        _keys(raw, {"lab_slot2_reserve", "spend_limit_pct", "steps", "mode", "blocks"})
        reserve = raw.get("lab_slot2_reserve", 100)
        if type(reserve) is not int or reserve != 100:
            raise ValueError("lab slot 2 reserve must remain 100 gems")
        steps = raw.get("steps", ["unlock_lab_slot_2"])
        if not isinstance(steps, (list, tuple)) or any(not isinstance(item, str) for item in steps):
            raise ValueError("gem steps must be a list")
        allowed = {"unlock_lab_slot_2", "unlock_lab_slot_3", "unlock_lab_slot_4",
                   "unlock_lab_slot_5", "cards", "card_slot"}
        if (not steps or steps[0] != "unlock_lab_slot_2" or len(steps) != len(set(steps))
                or any(step not in allowed for step in steps)):
            raise ValueError("gem path must start with lab slot 2 and contain known unique steps")
        mode = raw.get("mode", "steps")
        return cls(reserve, _percent(raw.get("spend_limit_pct", 100), "gem spend_limit_pct"),
                   tuple(steps), mode, _resource_blocks(raw.get("blocks", []), "gems", mode))


@dataclass(frozen=True)
class LabRoute:
    slot1_research: str = "game_speed"
    steps: tuple[str, ...] = ("research_game_speed",)
    mode: str = "steps"
    blocks: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: object) -> LabRoute:
        raw = _mapping(value, "labs")
        _keys(raw, {"slot1_research", "steps", "mode", "blocks"})
        if raw.get("slot1_research", "game_speed") != "game_speed":
            raise ValueError("lab slot 1 must research Game Speed")
        steps = raw.get("steps", ["research_game_speed"])
        if not isinstance(steps, (list, tuple)) or any(not isinstance(item, str) for item in steps):
            raise ValueError("lab steps must be a list")
        if (not steps or steps[0] != "research_game_speed" or len(steps) != len(set(steps))
                or any(step not in {"research_game_speed", "slot2_research"} for step in steps)):
            raise ValueError("lab path must start with Game Speed and contain known unique steps")
        mode = raw.get("mode", "steps")
        return cls("game_speed", tuple(steps), mode,
                   _resource_blocks(raw.get("blocks", []), "labs", mode))


LAB_SHARE_MODES = ("when_affordable", "save_pct", "labs_first")
POOL_SELECTIONS = ("cheapest", "ordered", "shortest")
IDLE_FILLS = ("leave_idle", "shortest_under_30m")


def _ranged(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _flag(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


@dataclass(frozen=True)
class LabShareRule:
    mode: str = "when_affordable"
    pct: int = 25

    @classmethod
    def from_dict(cls, value: object) -> LabShareRule:
        raw = _mapping(value, "lab_share")
        _keys(raw, {"mode", "pct"})
        mode = raw.get("mode", "when_affordable")
        if mode not in LAB_SHARE_MODES:
            raise ValueError("unknown lab_share mode")
        return cls(mode, _ranged(raw.get("pct", 25), "lab_share pct", 5, 90))


@dataclass(frozen=True)
class CoinRules:
    lab_share: LabShareRule = field(default_factory=LabShareRule)
    workshop_spend_limit_pct: int = 100

    @classmethod
    def from_dict(cls, value: object) -> CoinRules:
        raw = _mapping(value, "coins rules")
        _keys(raw, {"lab_share", "workshop_spend_limit_pct"})
        # The rule inherits the moved field's 0-100 range so every existing
        # route stays loadable; the Studio control offers 10-100.
        return cls(LabShareRule.from_dict(raw.get("lab_share", {})),
                   _percent(raw.get("workshop_spend_limit_pct", 100), "workshop_spend_limit_pct"))


@dataclass(frozen=True)
class LabPoolRule:
    selection: str = "ordered"
    max_price_pct_of_wallet: int | None = None
    max_seconds: int | None = None

    @classmethod
    def from_dict(cls, value: object) -> LabPoolRule:
        raw = _mapping(value, "pool rules")
        _keys(raw, {"selection", "max_price_pct_of_wallet", "max_seconds"})
        selection = raw.get("selection", "ordered")
        if selection not in POOL_SELECTIONS:
            raise ValueError("unknown pool selection")
        price, seconds = raw.get("max_price_pct_of_wallet"), raw.get("max_seconds")
        # Imported lazily (and not redefined here) to avoid an import cycle
        # with fleet.resource_blocks, which is the single source of truth.
        from fleet.resource_blocks import MAX_POOL_SECONDS
        return cls(selection,
                   None if price is None else _ranged(price, "max_price_pct_of_wallet", 1, 100),
                   None if seconds is None else _ranged(seconds, "max_seconds", 60, MAX_POOL_SECONDS))


@dataclass(frozen=True)
class LabRules:
    auto_start: bool = True
    pool: LabPoolRule = field(default_factory=LabPoolRule)
    idle_fill: str = "leave_idle"

    @classmethod
    def from_dict(cls, value: object) -> LabRules:
        raw = _mapping(value, "labs rules")
        _keys(raw, {"auto_start", "pool", "idle_fill"})
        idle_fill = raw.get("idle_fill", "leave_idle")
        if idle_fill not in IDLE_FILLS:
            raise ValueError("unknown idle_fill")
        return cls(_flag(raw.get("auto_start", True), "auto_start"),
                   LabPoolRule.from_dict(raw.get("pool", {})), idle_fill)


@dataclass(frozen=True)
class GemRules:
    auto_unlock_lab_slots: bool = True
    spend_limit_pct: int = 100
    keep: int = 0

    @classmethod
    def from_dict(cls, value: object) -> GemRules:
        raw = _mapping(value, "gems rules")
        _keys(raw, {"auto_unlock_lab_slots", "spend_limit_pct", "keep"})
        return cls(_flag(raw.get("auto_unlock_lab_slots", True), "auto_unlock_lab_slots"),
                   _percent(raw.get("spend_limit_pct", 100), "gem spend_limit_pct"),
                   _ranged(raw.get("keep", 0), "keep", 0, 1_000_000))


@dataclass(frozen=True)
class RouteRules:
    """Whole-strategy settings. The defaults are today's behavior."""
    coins: CoinRules = field(default_factory=CoinRules)
    labs: LabRules = field(default_factory=LabRules)
    gems: GemRules = field(default_factory=GemRules)

    @classmethod
    def from_dict(cls, value: object) -> RouteRules:
        raw = _mapping(value, "rules")
        _keys(raw, {"coins", "labs", "gems"})
        return cls(CoinRules.from_dict(raw.get("coins", {})), LabRules.from_dict(raw.get("labs", {})),
                   GemRules.from_dict(raw.get("gems", {})))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_limits(self, workshop_pct: int, gem_pct: int) -> RouteRules:
        return replace(self, coins=replace(self.coins, workshop_spend_limit_pct=workshop_pct),
                       gems=replace(self.gems, spend_limit_pct=gem_pct))


def _migrated_rules(value: object, workshop_raw: Mapping[str, object],
                    gems_raw: Mapping[str, object], workshop: WorkshopRoute,
                    gems: GemRoute) -> RouteRules:
    """Rules own the moved limits. Without rules, the old fields fill them; with
    rules, an explicit old field that disagrees is a pre-rules writer's edit and
    wins, so an older client's change is never silently dropped."""
    if value is None:
        return RouteRules().with_limits(workshop.coin_spend_limit_pct, gems.spend_limit_pct)
    rules = RouteRules.from_dict(value)
    workshop_pct = (workshop.coin_spend_limit_pct if "coin_spend_limit_pct" in workshop_raw
                    else rules.coins.workshop_spend_limit_pct)
    gem_pct = gems.spend_limit_pct if "spend_limit_pct" in gems_raw else rules.gems.spend_limit_pct
    return rules.with_limits(workshop_pct, gem_pct)


@dataclass(frozen=True)
class RouteBaseline:
    workshop: WorkshopRoute
    battle: BattleRoute
    gems: GemRoute
    labs: LabRoute
    rules: RouteRules = field(default_factory=RouteRules)

    @classmethod
    def from_dict(cls, value: object) -> RouteBaseline:
        raw = _mapping(value, "baseline")
        _keys(raw, {"workshop", "battle", "gems", "labs", "rules"})
        workshop_raw = _mapping(raw.get("workshop", {}), "workshop")
        gems_raw = _mapping(raw.get("gems", {}), "gems")
        workshop = WorkshopRoute.from_dict(workshop_raw)
        battle = BattleRoute.from_dict(raw.get("battle", {}))
        gems = GemRoute.from_dict(gems_raw)
        labs = LabRoute.from_dict(raw.get("labs", {}))
        rules = _migrated_rules(raw.get("rules"), workshop_raw, gems_raw, workshop, gems)
        from fleet.resource_blocks import check_pool_limits
        check_pool_limits(labs.blocks, max_seconds=rules.labs.pool.max_seconds,
                          max_price_pct=rules.labs.pool.max_price_pct_of_wallet)
        # Dual-write for one release: older workers still read the old fields.
        return cls(replace(workshop, coin_spend_limit_pct=rules.coins.workshop_spend_limit_pct),
                   battle, replace(gems, spend_limit_pct=rules.gems.spend_limit_pct), labs, rules)

    def to_dict(self) -> dict[str, Any]:
        return {"workshop": _workshop_dict(self.workshop), "battle": asdict(self.battle),
                "gems": asdict(self.gems), "labs": asdict(self.labs),
                "rules": self.rules.to_dict()}


@dataclass(frozen=True)
class StrategyAssignment:
    account_id: str
    strategy_id: str
    strategy_version: int
    strategy_name: str
    baseline: RouteBaseline

    @classmethod
    def from_dict(cls, value: object) -> StrategyAssignment:
        raw = _mapping(value, "assignment")
        _keys(raw, {"account_id", "strategy_id", "strategy_version", "strategy_name", "baseline"})
        for key in ("account_id", "strategy_id", "strategy_name"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"assignment requires {key}")
        version = raw.get("strategy_version")
        if type(version) is not int or version < 1:
            raise ValueError("invalid strategy version")
        return cls(raw["account_id"], raw["strategy_id"], version, raw["strategy_name"],
                   RouteBaseline.from_dict(raw.get("baseline")))

    def to_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version, "strategy_name": self.strategy_name,
                "baseline": self.baseline.to_dict()}


@dataclass(frozen=True)
class AccountOverride:
    account_id: str
    patches: dict[str, dict[str, object]]

    @classmethod
    def from_dict(cls, value: object) -> AccountOverride:
        raw = _mapping(value, "override")
        _keys(raw, {"account_id", "patches"})
        account_id = raw.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("override requires account_id")
        patches = _mapping(raw.get("patches", {}), "override patches")
        return cls(account_id, {key: dict(_mapping(patch, "override patch"))
                                for key, patch in patches.items()})


def _check_dependencies(edges: Mapping[str, tuple[str, ...]], rule_ids: set[str]) -> None:
    if any(key not in rule_ids or any(target not in rule_ids for target in targets)
           for key, targets in edges.items()):
        raise ValueError("unknown route dependency")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("cyclic route dependency")
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, ()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


@dataclass(frozen=True)
class RouteDocument:
    schema: int
    revision: int
    authored_at: float | None
    baseline: RouteBaseline
    overrides: dict[str, AccountOverride]
    dependencies: dict[str, tuple[str, ...]]
    assignments: dict[str, StrategyAssignment] = field(default_factory=dict)

    @classmethod
    def compatibility(cls) -> RouteDocument:
        return cls(
            1, 0, None,
            RouteBaseline(
                WorkshopRoute(),
                BattleRoute(branches=(BattleBranch(
                    "battle.opening", None,
                    (BattlePhase("battle.legacy", 1, None),)),)),
                GemRoute(), LabRoute()),
            {}, {},
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RouteDocument:
        raw = _mapping(value, "route")
        _keys(raw, {"schema", "revision", "authored_at", "baseline", "overrides", "dependencies", "assignments"})
        if raw.get("schema") != 1:
            raise ValueError("unsupported route schema")
        revision = raw.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid route revision")
        authored_at = raw.get("authored_at")
        if authored_at is not None and (type(authored_at) not in {int, float} or authored_at < 0):
            raise ValueError("invalid route timestamp")
        baseline = RouteBaseline.from_dict(raw.get("baseline", {}))
        overrides_raw = _mapping(raw.get("overrides", {}), "overrides")
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", worker) is None
               for worker in overrides_raw):
            raise ValueError("invalid route worker name")
        overrides = {worker: AccountOverride.from_dict(override)
                     for worker, override in overrides_raw.items()}
        assignments_raw = _mapping(raw.get("assignments", {}), "assignments")
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", worker) is None for worker in assignments_raw):
            raise ValueError("invalid route worker name")
        assignments = {worker: StrategyAssignment.from_dict(value)
                       for worker, value in assignments_raw.items()}
        dependencies_raw = _mapping(raw.get("dependencies", {}), "dependencies")
        dependencies: dict[str, tuple[str, ...]] = {}
        for key, targets in dependencies_raw.items():
            if not isinstance(targets, (list, tuple)) or any(not isinstance(t, str) for t in targets):
                raise ValueError("route dependencies must be lists of rule IDs")
            dependencies[key] = tuple(targets)
        rule_ids = {baseline.workshop.id}
        all_rule_ids = [baseline.workshop.id]
        for branch in baseline.battle.branches:
            rule_ids.add(branch.id)
            rule_ids.update(phase.id for phase in branch.phases)
            all_rule_ids.extend((branch.id, *(phase.id for phase in branch.phases)))
        if len(all_rule_ids) != len(set(all_rule_ids)):
            raise ValueError("duplicate route rule ID")
        _check_dependencies(dependencies, rule_ids)
        for override in overrides.values():
            for rule_id, patch in override.patches.items():
                if rule_id not in rule_ids:
                    raise ValueError("unknown override rule ID")
                if rule_id == baseline.workshop.id:
                    allowed = {"priority_ids", "banned_upgrade_ids", "coin_spend_limit_pct",
                               "draw_chance_pct", "weights", "mode", "blocks"}
                    _keys(patch, allowed)
                    WorkshopRoute.from_dict({**_workshop_dict(baseline.workshop), **patch})
                for branch in baseline.battle.branches:
                    if rule_id == branch.id:
                        _keys(patch, {"min_best_tier_1_wave"})
                        BattleBranch.from_dict({**asdict(branch), **patch})
                    for phase in branch.phases:
                        if rule_id == phase.id:
                            _keys(patch, {"priority_ids", "cash_spend_limit_pct",
                                          "draw_chance_pct", "weights", "emergency_survival"})
                            BattlePhase.from_dict({**asdict(phase), **patch})
        return cls(1, revision, authored_at, baseline, overrides, dependencies, assignments)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema, "revision": self.revision,
            "authored_at": self.authored_at,
            "baseline": self.baseline.to_dict(),
            "overrides": {worker: {"account_id": value.account_id,
                                  "patches": value.patches}
                          for worker, value in self.overrides.items()},
            "dependencies": {key: list(value) for key, value in self.dependencies.items()},
            "assignments": {worker: value.to_dict() for worker, value in self.assignments.items()},
        }


def _workshop_dict(rule: WorkshopRoute) -> dict[str, object]:
    return {"id": rule.id, "mode": rule.mode,
            "priority_ids": list(rule.priority_ids),
            "banned_upgrade_ids": sorted(rule.banned_upgrade_ids),
            "coin_spend_limit_pct": rule.coin_spend_limit_pct,
            "draw_chance_pct": rule.draw_chance_pct,
            "weights": dict(rule.weights), "blocks": list(rule.blocks)}


@dataclass(frozen=True)
class EffectiveRoute:
    revision: int
    workshop: WorkshopRoute
    battle: BattleRoute
    gems: GemRoute
    labs: LabRoute
    override_state: str
    rules: RouteRules = field(default_factory=RouteRules)


def resolve_route(route: RouteDocument, worker: str, account_id: str) -> EffectiveRoute:
    """Apply only a patch proven to belong to this worker's current account."""
    baseline = route.baseline
    assignment = route.assignments.get(worker)
    if assignment is not None:
        matches = assignment.account_id == account_id
        baseline = assignment.baseline if matches else RouteDocument.compatibility().baseline
        return EffectiveRoute(route.revision, baseline.workshop, baseline.battle,
                              baseline.gems, baseline.labs,
                              "assigned" if matches else "inactive_account_changed",
                              baseline.rules)
    override = route.overrides.get(worker)
    if override is None:
        return EffectiveRoute(route.revision, baseline.workshop, baseline.battle,
                              baseline.gems, baseline.labs, "none", baseline.rules)
    if override.account_id != account_id:
        return EffectiveRoute(route.revision, baseline.workshop, baseline.battle,
                              baseline.gems, baseline.labs, "inactive_account_changed",
                              baseline.rules)
    return apply_rule_patches(base=EffectiveRoute(
        route.revision, baseline.workshop, baseline.battle,
        baseline.gems, baseline.labs, "active", baseline.rules), patches=override.patches)


def apply_rule_patches(base: EffectiveRoute,
                       patches: Mapping[str, Mapping[str, object]]) -> EffectiveRoute:
    """Overlay validated stable rule IDs; unpatched fields stay inherited."""
    workshop = base.workshop
    rules = base.rules
    if patch := patches.get(workshop.id):
        workshop = WorkshopRoute.from_dict({**_workshop_dict(workshop), **patch})
        if "coin_spend_limit_pct" in patch:
            # An account override of the old field maps to the rule.
            rules = replace(rules, coins=replace(
                rules.coins, workshop_spend_limit_pct=workshop.coin_spend_limit_pct))
    branches: list[BattleBranch] = []
    for branch in base.battle.branches:
        branch_patch = patches.get(branch.id, {})
        phases = tuple(
            BattlePhase.from_dict({**asdict(phase), **patches.get(phase.id, {})})
            for phase in branch.phases)
        branches.append(BattleBranch.from_dict({
            **asdict(branch), **branch_patch,
            "phases": [asdict(phase) for phase in phases],
        }))
    return replace(base, workshop=workshop, rules=rules,
                   battle=replace(base.battle, branches=tuple(branches)))
