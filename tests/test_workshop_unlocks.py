from __future__ import annotations

import upgrades
from workshop_unlocks import (GROUPS, STARTER_IDS, available, gate_for, locked_upgrade_ids,
                              next_group, owned_groups, path_to)


def test_all_levelled_skills_have_one_explicit_availability_rule() -> None:
    levelled = {entry.id for entry in upgrades.CATALOG if not entry.unlock}
    grouped = [uid for group in GROUPS for uid in group.upgrade_ids]
    assert len(set(grouped)) == len(grouped)
    assert set(grouped).isdisjoint(STARTER_IDS)
    assert set(grouped) | STARTER_IDS == levelled
    assert len(upgrades.CATALOG) == 62
    assert locked_upgrade_ids(set()) == levelled - STARTER_IDS


def test_confirmed_child_purchase_unlocks_its_group_without_inventing_prior_ownership() -> None:
    owned = owned_groups(purchased_ids={"wall_health"})
    assert owned == {"unlock_wall"}
    assert "wall_rebuild" not in locked_upgrade_ids(owned)
    assert "shockwave_size" in locked_upgrade_ids(owned)
    assert next_group("defense", owned).id == "unlock_defense_upgrades"


def test_only_visible_children_prove_ownership_and_metadata_is_not_an_action() -> None:
    assert not owned_groups(visible_ids={"unlock_thorns"})
    assert not owned_groups(purchased_ids={"unlock_wall", "unknown"})
    assert owned_groups(purchased_ids={"unlock_thorns"}) == {"unlock_thorns"}
    assert owned_groups(visible_ids={"wall_rebuild"}) == {"unlock_wall"}
    wall = gate_for("wall_health")
    assert wall.executable_upgrade_id is None
    assert upgrades.by_id(wall.id) is None


def test_next_group_advances_in_game_order_including_unsupported_late_groups() -> None:
    owned: set[str] = set()
    for group in (group for group in GROUPS if group.category == "DEFENSE"):
        assert next_group("DEFENSE", owned) == group
        owned.update(owned_groups(explicit_unlocks={group.id}))
    assert next_group("DEFENSE", owned) is None
    assert next_group("attack", set()).upgrade_ids == ("range", "damage_per_meter")
    assert next_group("utility", set()).upgrade_ids == ("cash_bonus", "cash_per_wave")


def test_only_next_executable_unlock_is_available_and_visible_unlock_is_not_owned() -> None:
    assert available("unlock_defense_upgrades", set())
    assert not available("unlock_thorns", set())
    owned = owned_groups(purchased_ids={"unlock_defense_upgrades"})
    assert available("unlock_thorns", owned)
    assert not available("unlock_defense_upgrades", owned)
    assert not available("unlock_wall", set())
    assert not available("unknown", set())


def test_interest_and_recovery_unlock_tiles_are_executable_and_named_apart() -> None:
    assert upgrades.resolve("Unlock Interest", "UTILITY").id == "unlock_interest"
    assert upgrades.resolve("Interest", "UTILITY").id == "interest_per_wave"
    assert upgrades.resolve("Unlock Recovery Packages", "UTILITY").id == "unlock_recovery_packages"
    owned = owned_groups(purchased_ids={"unlock_cash_bonuses", "unlock_coin_bonuses", "unlock_free_upgrades"})
    assert available("unlock_interest", owned)
    owned |= owned_groups(purchased_ids={"unlock_interest"})
    assert available("unlock_recovery_packages", owned)
    assert gate_for("max_recovery").executable_upgrade_id == "unlock_recovery_packages"


def test_path_to_lists_missing_unlocks_in_tab_order() -> None:
    assert path_to("damage", set()) == ()
    assert path_to("unknown", set()) == ()
    assert [group.id for group in path_to("cash_bonus", set())] == ["unlock_cash_bonuses"]
    owned = owned_groups(purchased_ids={"unlock_defense_upgrades", "unlock_thorns", "unlock_lifesteal"})
    assert path_to("lifesteal", owned) == ()
    assert [group.id for group in path_to("orbs", owned)] == ["unlock_knockback", "unlock_orbs"]
    assert [group.id for group in path_to("land_mine_chance", owned)] == [
        "unlock_knockback", "unlock_orbs", "unlock_shockwave", "unlock_land_mines"]
