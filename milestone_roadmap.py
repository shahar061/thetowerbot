"""A sourced game milestone path, projected against one account's evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).parent / "roadmap" / "catalog.v1.json"


@dataclass(frozen=True)
class Milestone:
    id: str
    title: str
    group: str
    description: str
    kind: str
    tier: int | None
    wave: int | None
    requires: tuple[str, ...]
    claim_aliases: tuple[str, ...]
    source_url: str


def load_catalog() -> list[Milestone]:
    """Load curated milestones and generate the repeated tier unlock spine."""
    raw = json.loads(CATALOG_PATH.read_text())
    if raw["schema_version"] != 1:
        raise ValueError("Unsupported roadmap catalog version")
    source = raw["source_url"]
    entries = list(raw["nodes"])
    spine = raw["tier_spine"]
    for tier_number in range(spine["first"], spine["last"] + 1):
        prior = tier_number - 1
        entries.append({
            "id": f"tier.unlock.{tier_number}",
            "title": f"Unlock Tier {tier_number}",
            "group": "Tiers",
            "description": f"Reach Tier {prior} wave {spine['previous_tier_wave']} and claim the next tier milestone.",
            "kind": "unlock", "tier": prior, "wave": spine["previous_tier_wave"],
            "requires": [f"tier.unlock.{prior}"] if prior > 1 else [],
            "claim_aliases": [f"TIER {tier_number}", f"UNLOCK TIER {tier_number}"],
        })
    nodes = [Milestone(
        id=item["id"], title=item["title"], group=item["group"],
        description=item["description"], kind=item["kind"],
        tier=item.get("tier"), wave=item.get("wave"),
        requires=tuple(item.get("requires", ())),
        claim_aliases=tuple(item.get("claim_aliases", ())),
        source_url=item.get("source_url", source),
    ) for item in entries]
    ids = {node.id for node in nodes}
    if len(ids) != len(nodes) or any(parent not in ids for node in nodes for parent in node.requires):
        raise ValueError("Duplicate milestone or unknown prerequisite")
    return sorted(nodes, key=lambda node: (
        node.tier or 1, node.wave or 0, node.kind == "activity", node.id,
    ))


def project(
    nodes: list[Milestone], *, best_waves: dict[int, int],
    claimed_rewards: set[str], verified: set[str],
) -> list[dict[str, Any]]:
    """Keep reached wave, claim, and observed unlock separate in the UI."""
    claimed = {reward.strip().upper() for reward in claimed_rewards}
    result: list[dict[str, Any]] = []
    for node in nodes:
        progress = None
        if node.tier is not None and node.wave is not None:
            progress = {"current": best_waves.get(node.tier, 0), "target": node.wave}
        if node.id in verified:
            status = "verified"
        elif any(alias.upper() in claimed for alias in node.claim_aliases):
            status = "claimed"
        elif node.kind == "available":
            status = "available"
        elif node.kind == "activity":
            status = "unknown"
        elif progress is not None and progress["current"] >= progress["target"]:
            status = "claimable"
        elif node.tier is not None and node.tier > 1 and best_waves.get(node.tier, 0) == 0:
            status = "locked"
        elif progress is not None and progress["current"] > 0:
            status = "in_progress"
        else:
            status = "unknown"
        result.append({
            "id": node.id, "title": node.title, "group": node.group,
            "description": node.description, "kind": node.kind,
            "tier": node.tier, "wave": node.wave, "requires": node.requires,
            "source_url": node.source_url, "status": status, "progress": progress,
        })
    return result
