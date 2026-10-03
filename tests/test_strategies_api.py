"""Named strategies over HTTP.

The store is the source of truth for what exists; Controls is the source of
truth for what is running. Activation is the one operation that touches both.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from control import Controls
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from strategy import Strategy, StrategyStore
from tests.conftest import seed_template_dir
from web.app import create_app


@pytest.fixture
def wired(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    seed_template_dir(templates)
    monkeypatch.setattr(config, "TEMPLATE_DIR", templates)

    store = StrategyStore(tmp_path / "strategies")
    active = store.ensure_seeded()
    controls = Controls(strategy=active)
    seen: list = []
    bus = EventBus()
    bus.subscribe(type("R", (), {"offer": lambda self, e: seen.append(e) or True})())
    app = create_app(
        state=BotState(), sse=SseSink(), bus=bus, db_path=None,
        unknown_dir=config.UNKNOWN_DIR, controls=controls,
        checks={"brightness": object(), "digits": object()}, store=store,
    )
    return TestClient(app), store, controls, seen


def test_listing_names_the_active_one(wired) -> None:
    client, _, _, _ = wired
    body = client.get("/api/strategies").json()
    assert body == {"active": "default", "names": ["default"]}


def test_reading_one_returns_the_whole_strategy(wired) -> None:
    client, _, _, _ = wired
    body = client.get("/api/strategies/default").json()
    assert body["name"] == "default"
    assert len(body["actions"]) == len(config.ACTIONS)


def test_reading_an_absent_one_is_a_404(wired) -> None:
    client, _, _, _ = wired
    assert client.get("/api/strategies/ghost").status_code == 404


def test_a_traversal_name_is_refused_not_resolved(wired) -> None:
    client, _, _, _ = wired
    # Starlette normalises some of these before routing, so any non-2xx is a
    # pass; what must never happen is a 200 carrying a file from outside.
    for name in ("..", "%2e%2e%2fetc%2fpasswd", "has space"):
        assert client.get(f"/api/strategies/{name}").status_code >= 400


def test_putting_a_new_strategy_creates_it(wired) -> None:
    client, store, _, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    body["interval"] = 4.0
    response = client.put("/api/strategies/crit", json=body)
    assert response.status_code == 200
    assert store.load("crit").interval == 4.0
    assert client.get("/api/strategies").json()["names"] == ["crit", "default"]


def test_the_url_name_wins_over_the_body_name(wired) -> None:
    """Otherwise PUT /api/strategies/a with a body naming "b" writes b.json,
    and the caller has no way to know where their profile went."""
    client, store, _, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "somethingelse"
    client.put("/api/strategies/crit", json=body)
    assert "crit" in store.names()
    assert "somethingelse" not in store.names()


def test_an_invalid_strategy_is_a_422_naming_the_field(wired) -> None:
    client, store, _, _ = wired
    body = client.get("/api/strategies/default").json()
    body["interval"] = 0
    response = client.put("/api/strategies/broken", json=body)
    assert response.status_code == 422
    assert "interval" in response.json()["detail"]
    assert "broken" not in store.names()


def test_putting_the_active_strategy_applies_it_live(wired) -> None:
    """Source of truth means one truth: a saved edit to the running profile
    must reach the loop, not wait for a restart."""
    client, _, controls, seen = wired
    body = client.get("/api/strategies/default").json()
    body["interval"] = 7.0
    client.put("/api/strategies/default", json=body)
    assert controls.snapshot().strategy.interval == 7.0
    assert any(e.type == "ControlChanged" for e in seen)


def test_putting_an_inactive_strategy_does_not_touch_the_loop(wired) -> None:
    client, _, controls, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    body["interval"] = 7.0
    client.put("/api/strategies/crit", json=body)
    assert controls.snapshot().strategy.interval != 7.0


def test_activating_loads_it_and_persists_the_pointer(wired) -> None:
    client, store, controls, seen = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    body["interval"] = 6.0
    client.put("/api/strategies/crit", json=body)

    response = client.post("/api/strategies/crit/activate")
    assert response.status_code == 200
    assert controls.snapshot().strategy.name == "crit"
    assert controls.snapshot().strategy.interval == 6.0
    assert store.active_name() == "crit"
    assert client.get("/api/strategies").json()["active"] == "crit"
    assert any(e.type == "ControlChanged" for e in seen)


def test_activating_an_absent_strategy_is_a_404(wired) -> None:
    client, _, controls, _ = wired
    assert client.post("/api/strategies/ghost/activate").status_code == 404
    assert controls.snapshot().strategy.name == "default"


def test_activating_a_corrupt_strategy_is_a_422_not_a_404(wired) -> None:
    """load() raises "not_found" for an absent profile but the default
    "invalid" for one that exists and is corrupt JSON - a 404 here would
    send the caller looking for a file that is sitting right there."""
    client, store, controls, _ = wired
    (store.directory / "broken.json").write_text("{not json")
    response = client.post("/api/strategies/broken/activate")
    assert response.status_code == 422
    assert controls.snapshot().strategy.name == "default"


def test_activating_a_profile_whose_template_vanished_is_a_422(wired) -> None:
    """Activation is the one path that puts a profile straight from the disk
    into the running loop. load() only parses - PUT gets the filesystem half
    from store.save() and PATCH does it by hand - so without validated()
    here, activating a hand-edited profile whose template was since deleted
    raises out of every scan pass forever instead of once, as this 422.
    """
    client, store, controls, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    client.put("/api/strategies/crit", json=body)
    (config.TEMPLATE_DIR / body["actions"][0]["template"]).unlink()

    response = client.post("/api/strategies/crit/activate")

    assert response.status_code == 422
    assert "template" in response.json()["detail"]
    # Neither the loop nor the pointer moved: a profile that cannot run must
    # not become the one the next launch loads either.
    assert controls.snapshot().strategy.name == "default"
    assert store.active_name() == "default"


def test_deleting_a_spare_strategy_works(wired) -> None:
    client, store, _, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    client.put("/api/strategies/crit", json=body)
    assert client.delete("/api/strategies/crit").status_code == 200
    assert store.names() == ["default"]


def test_deleting_the_active_strategy_is_a_409(wired) -> None:
    client, store, _, _ = wired
    body = client.get("/api/strategies/default").json()
    body["name"] = "crit"
    client.put("/api/strategies/crit", json=body)
    response = client.delete("/api/strategies/default")
    assert response.status_code == 409
    assert "default" in store.names()


def test_deleting_the_last_strategy_is_a_409(wired) -> None:
    client, store, _, _ = wired
    response = client.delete("/api/strategies/default")
    assert response.status_code == 409
    assert store.names() == ["default"]


def test_a_patch_persists_into_the_active_profile(wired) -> None:
    """The promise of "strategy is the source of truth": an edit that
    applied live but did not persist would leave the file and the loop
    disagreeing until the next explicit save."""
    client, store, _, _ = wired
    client.patch("/api/control", json={"interval": 8.0})
    assert store.load("default").interval == 8.0


def test_a_rejected_patch_writes_nothing(wired) -> None:
    client, store, _, _ = wired
    before = store.load("default").interval
    assert client.patch("/api/control", json={"interval": 0}).status_code == 422
    assert store.load("default").interval == before


def test_a_patch_unrelated_to_actions_is_rejected_when_a_template_goes_missing(
    wired,
) -> None:
    """The precheck must fire whether or not the patch touches "actions" -
    the live profile can go invalid on its own (a template deleted out from
    under a running bot), and an interval-only patch has to catch that too,
    not just ones that happen to mention actions."""
    client, store, controls, seen = wired
    strategy = store.load("default")
    (config.TEMPLATE_DIR / strategy.actions[0].template).unlink()

    response = client.patch("/api/control", json={"interval": 9.0})

    assert response.status_code == 422
    assert controls.snapshot().strategy.interval != 9.0
    assert not any(e.type == "ControlChanged" for e in seen)


def test_a_patch_whose_persist_fails_rolls_back_live_state(wired, monkeypatch) -> None:
    """One source of truth means the live object must not survive a failed
    write: apply() has already committed by the time save() can fail, so a
    save failure has to be put back, not just reported."""
    client, store, controls, seen = wired
    before = controls.snapshot().strategy.interval

    def broken_save(strategy) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", broken_save)

    response = client.patch("/api/control", json={"interval": 9.0})

    assert response.status_code == 500
    assert controls.snapshot().strategy.interval == before
    assert not any(e.type == "ControlChanged" for e in seen)


def test_a_pause_only_patch_never_touches_the_file(wired, monkeypatch) -> None:
    """`paused` is session state and is never persisted, so a pause has
    nothing to save. Gating the write on `changed` being non-empty (rather
    than on the strategy having moved) made a pure pause toggle write the
    profile anyway - and with a failing save it answered 500 for a request
    that had fully succeeded: the bot paused, the operator told it failed,
    and no ControlChanged for the other tabs.
    """
    client, store, controls, seen = wired

    def broken_save(strategy) -> None:
        raise AssertionError("a pause has nothing to persist")

    monkeypatch.setattr(store, "save", broken_save)

    response = client.patch("/api/control", json={"paused": True})

    assert response.status_code == 200
    assert response.json()["paused"] is True
    assert controls.snapshot().paused is True
    assert any(
        e.type == "ControlChanged" and e.changed == {"paused": True} for e in seen
    )


def test_a_mixed_patch_whose_persist_fails_still_announces_the_pause(
    wired, monkeypatch
) -> None:
    """The lossy half of a rolled-back patch, made honest. The strategy goes
    back to what the disk holds; `paused` deliberately does not, because it
    is session state with nothing on disk to diverge from and un-pausing a
    bot over an unrelated write failure would be worse. What survives has to
    be announced, or the operator's other tabs show a running bot that is
    actually paused.
    """
    client, store, controls, seen = wired
    before = controls.snapshot().strategy.interval

    def broken_save(strategy) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", broken_save)

    response = client.patch("/api/control", json={"paused": True, "interval": 9.0})

    assert response.status_code == 500
    assert controls.snapshot().strategy.interval == before
    assert controls.snapshot().paused is True
    announced = [e for e in seen if e.type == "ControlChanged"]
    assert len(announced) == 1
    assert announced[0].changed == {"paused": True}


def test_the_strategy_routes_are_absent_without_a_store() -> None:
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR,
    )
    assert not any(getattr(route, "path", "").startswith("/api/strategies") for route in app.routes)
