"""Workshop availability shared by planning and presentation.

Groups follow https://the-tower-idle-tower-defense.game-vault.net/wiki/Workshop
and /wiki/Interest. Costs are fixed per unlock: reference prices for display
and planning, never permission to spend. A group without an executable identity does not add a shopping action.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import upgrades


@dataclass(frozen=True)
class UnlockGroup:
    id: str
    category: str
    name: str
    upgrade_ids: tuple[str, ...]
    executable_upgrade_id: str | None
    cost: int | None


STARTER_IDS = frozenset({"damage", "attack_speed", "critical_chance", "critical_factor",
                         "health", "health_regen"})

GROUPS = (
    UnlockGroup("unlock_range_upgrades", "ATTACK", "Unlock Range Upgrades",
                ("range", "damage_per_meter"), "unlock_range_upgrades", 50),
    UnlockGroup("unlock_multishot", "ATTACK", "Unlock Multishot",
                ("multishot_chance", "multishot_targets"), "unlock_multishot", 400),
    UnlockGroup("unlock_rapid_fire", "ATTACK", "Unlock Rapid Fire",
                ("rapid_fire_chance", "rapid_fire_duration"), "unlock_rapid_fire", 1500),
    UnlockGroup("unlock_bounce_shot", "ATTACK", "Unlock Bounce Shot",
                ("bounce_shot_chance", "bounce_shot_targets", "bounce_shot_range"), "unlock_bounce_shot", 10_000),
    UnlockGroup("unlock_super_crit", "ATTACK", "Unlock Super Crit",
                ("super_crit_chance", "super_crit_mult"), None, 100_000_000),
    UnlockGroup("unlock_rend_armor", "ATTACK", "Unlock Rend Armor",
                ("rend_armor_chance", "rend_armor_mult"), None, 500_000_000_000),
    UnlockGroup("unlock_defense_upgrades", "DEFENSE", "Unlock Defense Upgrades",
                ("defense_percent", "defense_absolute"), "unlock_defense_upgrades", 75),
    UnlockGroup("unlock_thorns", "DEFENSE", "Unlock Thorns", ("thorns",), "unlock_thorns", 500),
    UnlockGroup("unlock_lifesteal", "DEFENSE", "Unlock Lifesteal",
                ("lifesteal",), "unlock_lifesteal", 2000),
    UnlockGroup("unlock_knockback", "DEFENSE", "Unlock Knockback",
                ("knockback_chance", "knockback_force"), "unlock_knockback", 5000),
    UnlockGroup("unlock_orbs", "DEFENSE", "Unlock Orbs",
                ("orb_speed", "orbs"), "unlock_orbs", 15_000),
    UnlockGroup("unlock_shockwave", "DEFENSE", "Unlock Shockwave",
                ("shockwave_size", "shockwave_frequency"), None, 100_000),
    UnlockGroup("unlock_land_mines", "DEFENSE", "Unlock Land Mines",
                ("land_mine_chance", "land_mine_damage", "land_mine_radius"), None, 400_000),
    UnlockGroup("unlock_death_defy", "DEFENSE", "Unlock Death Defy",
                ("death_defy",), None, 1_500_000),
    UnlockGroup("unlock_wall", "DEFENSE", "Unlock Wall",
                ("wall_health", "wall_rebuild"), None, 500_000_000),
    UnlockGroup("unlock_cash_bonuses", "UTILITY", "Unlock Cash Bonuses",
                ("cash_bonus", "cash_per_wave"), "unlock_cash_bonuses", 40),
    UnlockGroup("unlock_coin_bonuses", "UTILITY", "Unlock Coin Bonuses",
                ("coins_per_kill_bonus", "coins_per_wave"), "unlock_coin_bonuses", 100),
    UnlockGroup("unlock_free_upgrades", "UTILITY", "Unlock Free Upgrades",
                ("free_attack_upgrade", "free_defense_upgrade", "free_utility_upgrade"),
                "unlock_free_upgrades", 800),
    UnlockGroup("unlock_interest", "UTILITY", "Unlock Interest",
                ("interest_per_wave",), "unlock_interest", 5000),
    UnlockGroup("unlock_recovery_packages", "UTILITY", "Unlock Recovery Packages",
                ("recovery_amount", "max_recovery", "package_chance"), "unlock_recovery_packages", 1_500_000),
    UnlockGroup("unlock_enemy_level_skips", "UTILITY", "Unlock Enemy Level Skips",
                ("enemy_attack_level_skip", "enemy_health_level_skip"), None, 1_000_000_000),
)

_BY_CHILD = {uid: group for group in GROUPS for uid in group.upgrade_ids}
_LEVELLED = {entry.id for entry in upgrades.CATALOG if not entry.unlock}
if (set(_BY_CHILD) | STARTER_IDS != _LEVELLED or set(_BY_CHILD) & STARTER_IDS
        or len(_BY_CHILD) != sum(len(group.upgrade_ids) for group in GROUPS)):
    raise RuntimeError("every Workshop skill must be a starter or belong to exactly one unlock group")
for _group in GROUPS:
    if _group.executable_upgrade_id is not None:
        _entry = upgrades.by_id(_group.executable_upgrade_id)
        if _entry is None or not _entry.unlock or _entry.unlocks != _group.upgrade_ids:
            raise RuntimeError("Workshop unlock metadata disagrees with the executable catalog")


def gate_for(upgrade_id: str) -> UnlockGroup | None:
    return _BY_CHILD.get(upgrade_id)


def owned_groups(*, visible_ids: Iterable[str] = (), purchased_ids: Iterable[str] = (),
                 explicit_unlocks: Iterable[str] = ()) -> set[str]:
    """Resolve already-positive evidence; missing rows never establish ownership.

    Callers filter observations and confirmed purchase counts. Seeing an
    unlock tile means it can be bought, so only visible children count.
    Metadata-only group IDs cannot masquerade as executed purchases.
    """
    visible, purchased, explicit = set(visible_ids), set(purchased_ids), set(explicit_unlocks)
    return {group.id for group in GROUPS
            if (group.id in explicit
                or group.executable_upgrade_id is not None and group.executable_upgrade_id in purchased
                or any(uid in visible or uid in purchased for uid in group.upgrade_ids))}


def next_group(category: str, owned: set[str]) -> UnlockGroup | None:
    return next((group for group in GROUPS if group.category == category.upper()
                 and group.id not in owned), None)


def path_to(upgrade_id: str, owned: set[str]) -> tuple[UnlockGroup, ...]:
    """Unlock groups still to buy, in tab order, before ``upgrade_id`` is available."""
    target = gate_for(upgrade_id)
    if target is None or target.id in owned:
        return ()
    chain = [group for group in GROUPS if group.category == target.category]
    return tuple(group for group in chain[:chain.index(target) + 1] if group.id not in owned)


def locked_upgrade_ids(owned: set[str]) -> frozenset[str]:
    return frozenset(uid for group in GROUPS if group.id not in owned for uid in group.upgrade_ids)


def available(upgrade_id: str, owned: set[str]) -> bool:
    """An owned skill or the tab's next supported unlock can be sought."""
    if upgrade_id in STARTER_IDS:
        return True
    group = gate_for(upgrade_id)
    if group is not None:
        return group.id in owned
    return any(group.executable_upgrade_id == upgrade_id
               and next_group(group.category, owned) == group for group in GROUPS)
