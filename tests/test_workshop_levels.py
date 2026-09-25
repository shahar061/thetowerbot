from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import db
import workshop_levels
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app
from tests.test_account_catalog_api import _worker


def _fact(upgrade_id: str, raw: str, value: float) -> dict:
    return {"concept_id": f"stats.{upgrade_id}", "value": value, "status": "verified",
            "evidence": {"observed_at": 100., "raw_value": raw}}


def _row(stats: list[dict] | None, upgrade_id: str) -> dict:
    return next(row for row in workshop_levels.workshop_state(stats) if row["id"] == upgrade_id)


def test_every_levelled_catalog_upgrade_has_a_full_ladder() -> None:
    for upgrade_id, ladder in workshop_levels.ladders().items():
        assert len(ladder.values) == len(ladder.next_coins) == ladder.max_level + 1, upgrade_id
        assert ladder.next_coins[-1] is None and None not in ladder.next_coins[:-1], upgrade_id
    assert len(workshop_levels.workshop_state(None)) == len(workshop_levels.ladders()) == 48


def test_displayed_precision_pins_the_level_and_next_price() -> None:
    revision = [
        _fact("health", "154", 154.), _fact("attack_speed", "1.55", 1.55),
        _fact("critical_factor", "x1.20", 1.2), _fact("health_regen", "2.28/sec", 2.28)]
    assert {k: _row(revision, "health")[k] for k in ("status", "level_min", "level_max", "max_level", "next_coins")} \
        == {"status": "exact", "level_min": 14, "level_max": 14, "max_level": 6000, "next_coins": 1233}
    assert _row(revision, "attack_speed")["level_min"] == 11
    assert _row(revision, "critical_factor")["level_min"] == 0
    assert _row(revision, "health_regen")["level_min"] == 13


def test_coarse_read_is_a_range_and_an_off_ladder_read_is_unmatched() -> None:
    # "12K" is only as precise as 11.5K-12.5K, which spans Health levels 80-82.
    coarse = _row([_fact("health", "12K", 12000.)], "health")
    assert (coarse["status"], coarse["level_min"], coarse["level_max"]) == ("ambiguous", 80, 82)
    assert coarse["next_coins"] == workshop_levels.ladders()["health"].next_coins[coarse["level_min"]]
    off = _row([_fact("attack_speed", "1.57", 1.57)], "attack_speed")
    assert (off["status"], off["level_min"], off["next_coins"]) == ("unmatched", None, None)


def test_falling_ladders_suffixes_and_maxed() -> None:
    revision = [
        _fact("wall_rebuild", "1196s", 1196.), _fact("range", "30.50m", 30.5),
        _fact("orbs", "4", 4.)]
    assert _row(revision, "wall_rebuild")["level_min"] == 2
    assert _row(revision, "range")["level_min"] == 1
    assert (_row(revision, "orbs")["status"], _row(revision, "orbs")["next_coins"]) == ("maxed", None)
    assert _row(revision, "damage")["status"] == "unseen"


def test_endpoint_reports_a_workers_inferred_levels(tmp_path: Path) -> None:
    path = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    db.bind_account(path, "ACCOUNT_A")
    conn = db.connect(path)
    conn.execute("INSERT INTO account_revisions(detail) VALUES (?)", (json.dumps({
        "workshop_stats": [{"concept_id": "stats.thorns", "value": 6.0, "status": "verified", "evidence": {
            "observed_at": 5., "confidence": .9, "raw_name": "Thorns", "raw_value": "6%",
            "rect": [0, 0, 1, 1], "frame_width": 1, "frame_height": 1, "frame_digest": "d"}}]}),))
    conn.commit()
    conn.close()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
                                   fleet=type("Fleet", (), {"root": tmp_path})()))
    payload = client.get("/api/workshop-levels", headers={
        "x-account-scope": "worker:Tiramisu64_18", "x-expected-account-id": "ACCOUNT_A"}).json()
    assert payload["account_id"] == "ACCOUNT_A"
    thorns = next(row for row in payload["upgrades"] if row["id"] == "thorns")
    assert (thorns["level_min"], thorns["max_level"], thorns["next_coins"], thorns["observed_at"]) == (6, 99, 331, 5.)
