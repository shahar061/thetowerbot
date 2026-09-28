"""The Labs & Gems view reads only this account's records and never guesses."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import db
from events import EventBus
from fleet.labs_view import labs_snapshot
from fleet.setup import FleetSetupService
from lab_plan import LabCadence, LabDecision
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def _registered(root: Path, worker: str, account: str) -> Path:
    worker_root = root / "workers" / worker
    checkpoint = worker_root / "checkpoints" / ("a" * 32 + ".json")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"worker_id": worker, "account_id": account,
                                      "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker_root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": worker, "account_id": account,
        "web_port": 8001, "binding": str(checkpoint),
        "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    db.bind_account(worker_root / "tower_bot.db", account)
    return worker_root


def test_an_unregistered_worker_is_an_unknown_row(tmp_path: Path) -> None:
    snapshot = labs_snapshot(tmp_path, ["Air_1"], now=1000.)
    (row,) = snapshot["workers"]
    assert (row["worker"], row["state"], row["plan"]) == ("Air_1", "unknown", None)
    # Only Game Speed on slot 1 is executed; the Lab 2 gems unlock is
    # gate-controlled (lab_routes.unlock_gate) and stays uncalibrated.
    assert snapshot["automated"] == [
        {"lane": "labs", "type": "research", "lab_id": "labs.game-speed", "slot": 1}]
    assert len(snapshot["reference"]["game_speed"]) == 7


def test_a_fresh_worker_shows_five_unknown_slots_and_lab_two_next(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert row["state"] == "ok" and row["strategy_name"] == "Fleet baseline"
    assert [slot["now"]["state"] for slot in row["plan"]["slots"]] == ["unknown"] * 5
    assert row["plan"]["gems"]["next"]["type"] == "unlock_lab_slot"
    assert row["plan"]["gems"]["price"] == 100
    assert row["wallet"] == {"coins": None, "gems": None}
    assert row["unknown_slots"] == 5
    assert "Lab slots 1–5 have no observed ownership" in row["blockers"]


def test_a_row_uses_the_menu_wallet_cadence_and_recent_lab_activity(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_38", "account-a")
    (root / "build-route-resource-facts.json").write_text(json.dumps({
        "account_id": "account-a", "worker": "Air_38", "observed_at": 900.,
        "wallet_coins": 20000, "wallet_gems": 60}))
    cadence = LabCadence(root, "account-a")
    cadence.note(LabDecision("wait_coins", price=12000, wallet_coins=5000, game_speed_level=3), now=900.)
    cadence.note_slot2("locked", 60, 900.)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                     "VALUES(10,'LAB','Game Speed','RESEARCH','coins',-2500,2500,0,'{}')")
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                     "VALUES(20,'CARD_BUY','Card','CARDS','gems',-20,20,0,?)",
                     (json.dumps({"verdict": "bought", "reason": "Card mission"}),))
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                     "VALUES(30,'WORKSHOP_BUY','Damage','ATTACK','coins',-30,30,0,'{}')")
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert row["read_at"] == 900. and row["wallet"] == {"coins": 20000, "gems": 60}
    slot1 = row["plan"]["slots"][0]
    assert slot1["now"]["state"] == "idle"
    assert slot1["now"]["evidence_status"] == "historical"
    assert slot1["capabilities"]["execute"] is False
    assert row["freshness"] == "historical"
    assert (slot1["next"]["level"], slot1["next"]["price"], slot1["next"]["seconds"]) == (3, 12000, 35280)
    assert slot1["covered"] is True and slot1["automated"] is True
    assert [slot["now"]["state"] for slot in row["plan"]["slots"][1:]] == ["locked", "unknown", "unknown", "unknown"]
    assert (row["plan"]["gems"]["have"], row["plan"]["gems"]["need"]) == (60, 100)
    assert [(item["kind"], item["amount"], item["reason"]) for item in row["recent"]] == [
        ("CARD_BUY", 20, "Card mission"), ("LAB", 2500, None)]


def test_a_just_in_time_row_carries_its_saving_plan(tmp_path: Path) -> None:
    from fleet import resource_blocks as rb
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    root = _registered(tmp_path, "Air_38", "account-a")
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["baseline"]["rules"] = rb.template_lab_list_rules()
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    (root / "build-route-resource-facts.json").write_text(json.dumps({
        "account_id": "account-a", "worker": "Air_38", "observed_at": 900.,
        "wallet_coins": 1000, "wallet_gems": 60}))
    LabCadence(root, "account-a").note(LabDecision("wait_coins", price=2500, wallet_coins=100,
                                                   game_speed_level=2), now=900.)
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    saving = row["saving"]
    assert set(saving) == {"reserve", "workshop_budget", "targets", "why"}
    assert (saving["reserve"], saving["workshop_budget"]) == (1000, 0)
    assert [(t["slot"], t["lab_id"], t["price"]) for t in saving["targets"]] == [(1, "labs.game-speed", 2500)]
    assert saving["why"] == ["Slot 1: income unread, holding 2.5k for Game Speed L2",
                             "Reserve 1k; Workshop may spend 0"]
    # No on-disk jar here; the stale-jar case is covered below.
    assert row["plan"]["jar"] == 1000


def test_a_just_in_time_row_shows_its_hold_not_a_stale_save_pct_jar(tmp_path: Path) -> None:
    from fleet import resource_blocks as rb
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    root = _registered(tmp_path, "Air_38", "account-a")
    (root / "build-route-resource-facts.json").write_text(json.dumps({
        "account_id": "account-a", "worker": "Air_38", "observed_at": 900.,
        "wallet_coins": 1000, "wallet_gems": 60}))
    LabCadence(root, "account-a").note(LabDecision("wait_coins", price=2500, wallet_coins=100,
                                                   game_speed_level=2), now=900.)
    # Left over from the account's old save_pct strategy; just-in-time never reads it.
    (root / "lab-coin-jar.json").write_text(json.dumps({"account_id": "account-a", "amount": 700}))
    (before,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert before["saving"] is None and before["plan"]["jar"] == 700  # other modes keep the jar
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["baseline"]["rules"] = rb.template_lab_list_rules()
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "operator")
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert row["saving"]["reserve"] == 1000 and row["plan"]["jar"] == 1000


def test_rows_without_a_saving_plan_say_so(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    (row, unknown) = labs_snapshot(tmp_path, ["Air_38", "Air_1"], now=1000.)["workers"]
    assert row["state"] == "ok" and row["saving"] is None
    assert unknown["state"] == "unknown" and unknown["saving"] is None


def test_other_account_records_are_not_shown(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_38", "account-a")
    (root / "build-route-resource-facts.json").write_text(json.dumps({
        "account_id": "account-b", "worker": "Air_38", "observed_at": 900.,
        "wallet_coins": 20000, "wallet_gems": 60}))
    LabCadence(root, "account-b").note(LabDecision("wait_coins", price=12000, wallet_coins=5000,
                                                   game_speed_level=3), now=900.)
    LabCadence(root, "account-b").note_slot2("owned", 60, 900.)
    (root / "lab-coin-jar.json").write_text(json.dumps({"account_id": "account-b", "amount": 900}))
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert [slot["now"]["state"] for slot in row["plan"]["slots"]] == ["unknown"] * 5
    assert row["wallet"] == {"coins": None, "gems": None}
    assert row["plan"]["jar"] == 0


def test_a_corrupt_route_falls_back_to_defaults_and_says_why(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    (tmp_path / "build-route.json").write_text("{bad")
    (row,) = labs_snapshot(tmp_path, ["Air_38"], now=1000.)["workers"]
    assert row["state"] == "ok" and "Route unavailable" in row["reason"]


def test_a_worker_with_a_corrupt_database_is_an_unknown_row(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_38", "account-a")
    _registered(tmp_path, "Air_39", "account-b")
    (root / "tower_bot.db").write_bytes(b"not a sqlite file")
    rows = {row["worker"]: row for row in labs_snapshot(tmp_path, ["Air_38", "Air_39"], now=1000.)["workers"]}
    assert rows["Air_38"]["state"] == "unknown" and rows["Air_38"]["plan"] is None
    assert rows["Air_39"]["state"] == "ok"


def _client(root: Path, names: tuple[str, ...]) -> TestClient:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": name} for name in names])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set())
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def test_labs_endpoint_lists_visible_workers(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_38", "account-a")
    response = _client(tmp_path, ("Air_38",)).get("/api/fleet/labs")
    assert response.status_code == 200
    assert [row["worker"] for row in response.json()["workers"]] == ["Air_38"]
    assert response.json()["workers"][0]["saving"] is None  # the fleet baseline has no lab list


def test_labs_endpoint_is_unavailable_without_the_fleet() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None))
    assert client.get("/api/fleet/labs").status_code == 503
