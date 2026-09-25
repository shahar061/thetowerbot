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

    @classmethod
    def from_dict(cls, value: object) -> WorkshopRoute:
        raw = _mapping(value, "workshop")
        _keys(raw, {"id", "mode", "priority_ids", "banned_upgrade_ids", "coin_spend_limit_pct",
                    "draw_chance_pct", "weights"})
        mode = raw.get("mode", "legacy_planner")
        if mode not in {"legacy_planner", "priorities"}:
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
        )


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

    @classmethod
    def from_dict(cls, value: object) -> BattleRoute:
        raw = _mapping(value, "battle")
        _keys(raw, {"mode", "branches"})
        mode = raw.get("mode", "legacy_policy")
        if mode not in {"legacy_policy", "phases"}:
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
        return cls(mode, branches)


@dataclass(frozen=True)
class GemRoute:
    lab_slot2_reserve: int = 100
    spend_limit_pct: int = 100
    steps: tuple[str, ...] = ("unlock_lab_slot_2",)

    @classmethod
    def from_dict(cls, value: object) -> GemRoute:
        raw = _mapping(value, "gems")
        _keys(raw, {"lab_slot2_reserve", "spend_limit_pct", "steps"})
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
        return cls(reserve, _percent(raw.get("spend_limit_pct", 100), "gem spend_limit_pct"),
                   tuple(steps))


@dataclass(frozen=True)
class LabRoute:
    slot1_research: str = "game_speed"
    steps: tuple[str, ...] = ("research_game_speed",)

    @classmethod
    def from_dict(cls, value: object) -> LabRoute:
        raw = _mapping(value, "labs")
        _keys(raw, {"slot1_research", "steps"})
        if raw.get("slot1_research", "game_speed") != "game_speed":
            raise ValueError("lab slot 1 must research Game Speed")
        steps = raw.get("steps", ["research_game_speed"])
        if not isinstance(steps, (list, tuple)) or any(not isinstance(item, str) for item in steps):
            raise ValueError("lab steps must be a list")
        if (not steps or steps[0] != "research_game_speed" or len(steps) != len(set(steps))
                or any(step not in {"research_game_speed", "slot2_research"} for step in steps)):
            raise ValueError("lab path must start with Game Speed and contain known unique steps")
        return cls("game_speed", tuple(steps))


@dataclass(frozen=True)
class RouteBaseline:
    workshop: WorkshopRoute
    battle: BattleRoute
    gems: GemRoute
    labs: LabRoute

    @classmethod
    def from_dict(cls, value: object) -> RouteBaseline:
        raw = _mapping(value, "baseline")
        _keys(raw, {"workshop", "battle", "gems", "labs"})
        return cls(WorkshopRoute.from_dict(raw.get("workshop", {})),
                   BattleRoute.from_dict(raw.get("battle", {})),
                   GemRoute.from_dict(raw.get("gems", {})),
                   LabRoute.from_dict(raw.get("labs", {})))


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
        _keys(raw, {"schema", "revision", "authored_at", "baseline", "overrides", "dependencies"})
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
                               "draw_chance_pct", "weights", "mode"}
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
        return cls(1, revision, authored_at, baseline, overrides, dependencies)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema, "revision": self.revision,
            "authored_at": self.authored_at,
            "baseline": {
                "workshop": _workshop_dict(self.baseline.workshop),
                "battle": asdict(self.baseline.battle),
                "gems": asdict(self.baseline.gems), "labs": asdict(self.baseline.labs),
            },
            "overrides": {worker: {"account_id": value.account_id,
                                  "patches": value.patches}
                          for worker, value in self.overrides.items()},
            "dependencies": {key: list(value) for key, value in self.dependencies.items()},
        }


def _workshop_dict(rule: WorkshopRoute) -> dict[str, object]:
    return {"id": rule.id, "mode": rule.mode,
            "priority_ids": list(rule.priority_ids),
            "banned_upgrade_ids": sorted(rule.banned_upgrade_ids),
            "coin_spend_limit_pct": rule.coin_spend_limit_pct,
            "draw_chance_pct": rule.draw_chance_pct,
            "weights": dict(rule.weights)}


@dataclass(frozen=True)
class EffectiveRoute:
    revision: int
    workshop: WorkshopRoute
    battle: BattleRoute
    gems: GemRoute
    labs: LabRoute
    override_state: str


def resolve_route(route: RouteDocument, worker: str, account_id: str) -> EffectiveRoute:
    """Apply only a patch proven to belong to this worker's current account."""
    baseline = route.baseline
    override = route.overrides.get(worker)
    if override is None:
        return EffectiveRoute(route.revision, baseline.workshop, baseline.battle,
                              baseline.gems, baseline.labs, "none")
    if override.account_id != account_id:
        return EffectiveRoute(route.revision, baseline.workshop, baseline.battle,
                              baseline.gems, baseline.labs, "inactive_account_changed")
    return apply_rule_patches(base=EffectiveRoute(
        route.revision, baseline.workshop, baseline.battle,
        baseline.gems, baseline.labs, "active"), patches=override.patches)


def apply_rule_patches(base: EffectiveRoute,
                       patches: Mapping[str, Mapping[str, object]]) -> EffectiveRoute:
    """Overlay validated stable rule IDs; unpatched fields stay inherited."""
    workshop = base.workshop
    if patch := patches.get(workshop.id):
        workshop = WorkshopRoute.from_dict({**_workshop_dict(workshop), **patch})
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
    return replace(base, workshop=workshop,
                   battle=replace(base.battle, branches=tuple(branches)))
