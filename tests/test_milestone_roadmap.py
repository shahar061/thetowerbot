"""A roadmap uses observed account progress without inventing claimed rewards."""

from milestone_roadmap import load_catalog, project


def test_catalog_has_chronological_feature_unlocks_and_valid_links() -> None:
    nodes = load_catalog()
    by_id = {node.id: node for node in nodes}
    assert len(by_id) == len(nodes)
    assert {
        "cards.available", "labs.unlocked", "tournaments.unlocked",
        "events.unlocked", "tier.unlock.2", "modules.unlocked",
    } <= by_id.keys()
    assert [(by_id[key].tier, by_id[key].wave) for key in (
        "labs.unlocked", "tournaments.unlocked", "events.unlocked",
        "tier.unlock.2", "modules.unlocked",
    )] == [(1, 30), (1, 60), (1, 70), (1, 100), (2, 90)]
    assert all(parent in by_id for node in nodes for parent in node.requires)
    assert set(range(2, 22)) == {
        int(node.id.removeprefix("tier.unlock.")) for node in nodes
        if node.id.startswith("tier.unlock.")
    }


def test_wave_progress_is_distinct_from_reward_claim_and_verification() -> None:
    nodes = load_catalog()
    early = {node["id"]: node for node in project(
        nodes, best_waves={1: 27}, claimed_rewards=set(), verified=set())}
    assert early["labs.unlocked"]["status"] == "in_progress"
    assert early["labs.unlocked"]["progress"] == {"current": 27, "target": 30}
    assert early["cards.available"]["status"] == "available"
    assert early["modules.unlocked"]["status"] == "locked"

    reached = {node["id"]: node for node in project(
        nodes, best_waves={1: 65}, claimed_rewards=set(), verified=set())}
    assert reached["labs.unlocked"]["status"] == "claimable"
    assert reached["tournaments.unlocked"]["status"] == "claimable"
    assert reached["events.unlocked"]["status"] == "in_progress"

    confirmed = {node["id"]: node for node in project(
        nodes, best_waves={1: 65}, claimed_rewards={"LABS"},
        verified={"labs.unlocked"})}
    assert confirmed["labs.unlocked"]["status"] == "verified"
    assert confirmed["tournaments.unlocked"]["status"] == "claimable"
