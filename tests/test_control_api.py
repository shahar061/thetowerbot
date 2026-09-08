"""The three routes that write - to memory, never to the database."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import config
import director
import knowledge
import objectives
from account_state import AccountRevision
from control import Controls
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from strategy import Strategy
from web.app import create_app


@pytest.fixture
def wired() -> tuple[TestClient, Controls, list, threading.Event]:
    seen: list = []

    class Recorder:
        def offer(self, event) -> bool:
            seen.append(event)
            return True

    bus = EventBus()
    bus.subscribe(Recorder())
    controls = Controls(strategy=replace(
        Strategy.from_config(), affordability="brightness"
    ))
    stop = threading.Event()
    app = create_app(
        state=BotState(), sse=SseSink(), bus=bus, db_path=None,
        unknown_dir=config.UNKNOWN_DIR, shutdown=stop,
        controls=controls, checks={"brightness": object(), "digits": None},
    )
    return TestClient(app), controls, seen, stop


def test_get_returns_the_whole_strategy(wired) -> None:
    client, _, _, _ = wired
    body = client.get("/api/control").json()
    assert body["paused"] is False
    assert body["strategy"]["affordability"] == "brightness"
    assert [row["name"] for row in body["strategy"]["actions"]] == [
        action.name for action in config.ACTIONS
    ]


def test_available_affordability_is_advertised_under_its_new_name(wired) -> None:
    client, _, _, _ = wired
    body = client.get("/api/control").json()
    # digits is None in the fixture's checks - no atlas on this machine - so
    # the browser must be told not to offer it.
    assert body["affordability_available"] == ["brightness"]


def test_shopping_disabled_reason_is_absent_when_no_session_is_wired(wired) -> None:
    """--once, --tui, and every test predating this finding build create_app
    with no `shopping=` at all - that must read as "fully usable", not crash
    the route."""
    client, _, _, _ = wired
    body = client.get("/api/control").json()
    assert body["shopping_disabled_reason"] is None


def test_shopping_disabled_reason_is_advertised_when_the_session_carries_one() -> None:
    """The whole point of the finding this closes: a machine whose header
    atlas cannot support a balance read must say why shopping does nothing,
    not stay silent forever - see ShoppingSession.disabled_reason and
    tower_bot.build_shopping()."""
    class FakeShopping:
        disabled_reason = "header atlas is missing 2, 3, 5, 6, 9"

    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, shutdown=threading.Event(),
        controls=Controls(strategy=Strategy.from_config()),
        checks={"brightness": object(), "digits": None},
        shopping=FakeShopping(),
    )
    body = TestClient(app).get("/api/control").json()
    assert body["shopping_disabled_reason"] == "header atlas is missing 2, 3, 5, 6, 9"


def test_patch_publishes_a_control_changed_event(wired) -> None:
    client, _, seen, _ = wired
    client.patch("/api/control", json={"interval": 5.0})
    published = [event for event in seen if event.type == "ControlChanged"]
    assert published and published[0].changed == {"interval": 5.0}


def test_a_no_op_patch_publishes_nothing(wired) -> None:
    client, _, seen, _ = wired
    client.patch("/api/control", json={"paused": False})
    assert not [event for event in seen if event.type == "ControlChanged"]


def test_an_invalid_patch_is_422_and_changes_nothing(wired) -> None:
    client, controls, seen, _ = wired
    response = client.patch("/api/control", json={"interval": -1})
    assert response.status_code == 422
    assert controls.snapshot().strategy.interval == pytest.approx(
        Strategy.from_config().interval
    )
    assert not [event for event in seen if event.type == "ControlChanged"]


def test_patching_an_unavailable_affordability_is_refused(wired) -> None:
    """Silently downgrading would misrepresent what the bot is doing.

    build_affordability() degrades digits -> brightness on its own, which is
    right for a startup flag and wrong for a live setting: you would ask for
    digits, get brightness, and never be told.
    """
    client, controls, _, _ = wired
    response = client.patch("/api/control", json={"affordability": "digits"})
    assert response.status_code == 422
    assert "atlas" in response.json()["detail"]
    assert controls.snapshot().strategy.affordability == "brightness"


def test_patching_the_action_list_reorders_the_strategy(wired) -> None:
    client, controls, _, _ = wired
    rows = [row for row in reversed(client.get("/api/control").json()["strategy"]["actions"])]
    response = client.patch("/api/control", json={"actions": rows})
    assert response.status_code == 200
    assert [r.name for r in controls.snapshot().strategy.actions] == [
        row["name"] for row in rows
    ]

    # A client never has to guess what was accepted: the response carries the
    # full new state, not just the delta this patch mentioned.
    body = response.json()
    assert [row["name"] for row in body["strategy"]["actions"]] == [
        row["name"] for row in rows
    ]
    # affordability_available and affordability itself were never part of
    # this patch, yet the response still carries them.
    assert body["affordability_available"] == ["brightness"]
    assert body["strategy"]["affordability"] == "brightness"


def test_an_absolute_template_is_refused(wired) -> None:
    """The request body must not get to choose which file the bot reads.

    vision.TemplateCache.get() hands its path straight to cv2.imread, and an
    unresolvable one raises out of every scan pass forever - so this has to
    fail once, here, with a field name, rather than recurring as a BotError.
    """
    client, controls, seen, _ = wired
    before = controls.snapshot().strategy
    response = client.patch("/api/control", json={"actions": [
        {"name": "Damage", "template": "/etc/passwd"},
    ]})
    assert response.status_code == 422
    assert "template" in response.json()["detail"]
    assert controls.snapshot().strategy == before
    assert not [event for event in seen if event.type == "ControlChanged"]


def test_a_template_that_is_not_on_disk_is_refused(wired) -> None:
    # Inside TEMPLATE_DIR but absent: a typo must be a 422 naming the field,
    # not a bot that runs and fails on every pass.
    client, controls, _, _ = wired
    before = controls.snapshot().strategy
    response = client.patch("/api/control", json={"actions": [
        {"name": "Damage", "template": "nope.png"},
    ]})
    assert response.status_code == 422
    assert "nope.png" in response.json()["detail"]
    assert controls.snapshot().strategy == before


def test_a_real_template_still_patches(wired) -> None:
    # The other half of the check above: refusing everything would "fix" the
    # traversal by breaking the feature.
    client, controls, _, _ = wired
    real = config.ACTIONS[0].template
    response = client.patch("/api/control", json={"actions": [
        {"name": "Damage", "template": real, "threshold": 0.9},
    ]})
    assert response.status_code == 200
    rows = controls.snapshot().strategy.actions
    assert [(r.name, r.template, r.threshold) for r in rows] == [
        ("Damage", real, 0.9)
    ]


def test_an_invalid_patch_names_the_field(wired) -> None:
    client, _, _, _ = wired
    response = client.patch("/api/control", json={"interval": 0})
    assert response.status_code == 422
    assert "interval" in response.json()["detail"]


def test_control_routes_are_absent_when_no_controls_were_wired(wired) -> None:
    """--once and the tests that predate this build an app with no controls."""
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, shutdown=threading.Event(),
    )
    assert TestClient(app).get("/api/control").status_code == 404


@pytest.mark.parametrize(
    "field, value",
    [("tap_jitter_px", 4.0), ("timing_jitter", 0.3), ("tap_delay", 0.4)],
)
def test_patching_jitter_reaches_the_strategy(wired, field: str, value: float) -> None:
    """Each jitter knob must survive the Pydantic model.

    A field missing from ControlPatch is dropped silently rather than
    rejected, so the dashboard would appear to save while the bot kept the
    old value - the worst of both outcomes.
    """
    client, controls, _, _ = wired

    response = client.patch("/api/control", json={field: value})

    assert response.status_code == 200
    assert getattr(controls.snapshot().strategy, field) == pytest.approx(value)


def test_an_out_of_range_jitter_patch_is_refused(wired) -> None:
    client, controls, _, _ = wired

    response = client.patch("/api/control", json={"tap_jitter_px": 500.0})

    assert response.status_code == 422
    assert controls.snapshot().strategy.tap_jitter_px == pytest.approx(
        Strategy.from_config().tap_jitter_px
    )


# -- game speed -------------------------------------------------------------


def test_the_target_speed_can_be_set_through_the_patch_route(wired) -> None:
    client, controls, _, _ = wired
    assert client.patch("/api/control", json={"target_speed": 1.0}).status_code == 200
    assert controls.snapshot().strategy.target_speed == 1.0


def test_an_unknown_target_speed_is_refused(wired) -> None:
    client, controls, _, _ = wired
    response = client.patch("/api/control", json={"target_speed": 7.5})

    assert response.status_code == 422
    assert controls.snapshot().strategy.target_speed is None


def test_a_speed_command_is_queued_for_the_scan_loop(wired) -> None:
    """The route must not touch the device - it queues, and the loop taps.
    That is the boundary the whole module is built around."""
    client, controls, _, _ = wired
    assert client.post("/api/control/command", json={"command": "speed_up"}).status_code == 200
    assert controls.drain() == ("speed_up",)


def test_the_payload_advertises_the_speeds_that_can_be_targeted(wired) -> None:
    """Same reasoning as affordability_available: the browser cannot know
    which speeds have readout templates, and a hardcoded list in the UI would
    drift from config the moment the harvest tool adds one.

    TARGET_SPEEDS, not SPEED_VALUES: x0.0 is readable but not offerable, so
    the dropdown must not list a value the validator will reject."""
    client, _, _, _ = wired
    body = client.get("/api/control").json()
    assert body["speed_values"] == list(config.TARGET_SPEEDS)
    assert 0.0 not in body["speed_values"]


def test_an_unknown_command_is_refused_by_the_route(wired) -> None:
    client, controls, _, _ = wired
    response = client.post("/api/control/command", json={"command": "launch"})

    assert response.status_code == 422
    assert controls.drain() == ()


# -- Collect stats transaction ---------------------------------------------
class _FakeRunner:
    """Only what the account routes touch: a transaction and one request gate."""

    def __init__(self, error: Exception | None = None) -> None:
        from account_collection import StatsCollection

        self.collection = StatsCollection()
        self.autopilot_state = None
        self._error = error

    def status(self) -> dict:
        return {"running": True, "since": 1.0, "error": None}

    def request_stats_collection(self) -> dict:
        if self._error is not None:
            raise self._error
        self.collection.request(now=100.0)
        return self.collection.snapshot()


def _account_client(runner: object | None) -> TestClient:
    from autopilot import AutopilotState

    if runner is not None:
        runner.autopilot_state = AutopilotState()
    return TestClient(create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        controls=Controls(strategy=Strategy.from_config()), runner=runner,
    ))


def test_collecting_stats_arms_the_transaction_and_reports_it_on_the_account() -> None:
    runner = _FakeRunner()
    with _account_client(runner) as client:
        assert client.get("/api/account").json()["collection"]["status"] == "idle"
        armed = client.post("/api/account/collect").json()
        assert armed["status"] == "running"
        assert armed["step"] == "open_settings"
        reported = client.get("/api/account").json()["collection"]
        assert reported["status"] == "running"
        assert reported["result"] is None


@pytest.mark.parametrize("status_code,message", [
    (409, "A stats collection is already running"),
    (503, "Stats collection is unavailable: the OCR engine could not be built"),
])
def test_a_refused_collection_keeps_the_runner_s_status_code_and_reason(
    status_code: int, message: str,
) -> None:
    from runner import RunnerError

    with _account_client(_FakeRunner(RunnerError(message, status_code))) as client:
        response = client.post("/api/account/collect")
        assert response.status_code == status_code
        assert response.json()["detail"] == message


def test_a_backend_without_a_runner_cannot_be_asked_to_walk_the_game() -> None:
    """Absent, not null: no runner means no transaction was ever possible."""
    with _account_client(None) as client:
        assert "collection" not in client.get("/api/account").json()
        assert client.post("/api/account/collect").status_code == 412


# -- Missions visit ---------------------------------------------------------
class _FakeMissionsRunner:
    """Only what the missions routes touch: a visit, a reader, a request gate."""

    def __init__(self, error: Exception | None = None) -> None:
        from missions_screen import MissionsReadings
        from missions_visit import MissionsVisit

        self.visit = MissionsVisit()
        self.missions = MissionsReadings()
        self.autopilot_state = None
        self._error = error

    def status(self) -> dict:
        return {"running": True, "since": 1.0, "error": None}

    def request_missions_visit(self) -> dict:
        if self._error is not None:
            raise self._error
        self.visit.request(now=100.0)
        return self.visit.snapshot()


def test_visiting_missions_arms_the_walk_and_reports_it_on_the_page() -> None:
    runner = _FakeMissionsRunner()
    with _account_client(runner) as client:
        before = client.get("/api/missions").json()
        assert before["visit"]["status"] == "idle"
        # Nothing read yet is a null reading, not an absent key: the browser
        # can tell "no missions page has been read" from "cannot read one".
        assert before["latest"] is None and before["scanned"] is False
        armed = client.post("/api/missions/visit").json()
        assert armed["status"] == "running" and armed["step"] == "open_missions"
        assert client.get("/api/missions").json()["visit"]["status"] == "running"


@pytest.mark.parametrize("status_code,message", [
    (409, "A missions visit is already running"),
    (409, "A stats collection is already walking the menus"),
    (503, "Missions visits are unavailable: the OCR engine could not be built"),
])
def test_a_refused_visit_keeps_the_runner_s_status_code_and_reason(
    status_code: int, message: str,
) -> None:
    from runner import RunnerError

    with _account_client(_FakeMissionsRunner(RunnerError(message, status_code))) as client:
        response = client.post("/api/missions/visit")
        assert response.status_code == status_code
        assert response.json()["detail"] == message


def test_a_backend_without_a_runner_cannot_be_asked_to_visit_missions() -> None:
    with _account_client(None) as client:
        assert client.get("/api/missions").json() == {}
        assert client.post("/api/missions/visit").status_code == 412


# -- Missions claim transaction --------------------------------------------
class _FakeClaimRunner:
    """Only what the claim routes touch: the transaction and one request gate."""

    def __init__(self, error: Exception | None = None) -> None:
        from missions_claim import MissionsClaim
        from missions_screen import MissionsReadings

        self.claim = MissionsClaim()
        self.missions = MissionsReadings()
        self.autopilot_state = None
        self._error = error

    def status(self) -> dict:
        return {"running": True, "since": 1.0, "error": None}

    def request_missions_claim(self) -> dict:
        if self._error is not None:
            raise self._error
        self.claim.request()
        return self.claim.snapshot()


def test_claiming_missions_arms_the_walk_and_reports_it_on_the_missions_page() -> None:
    runner = _FakeClaimRunner()
    with _account_client(runner) as client:
        assert client.get("/api/missions").json()["claim"]["status"] == "idle"
        armed = client.post("/api/missions/claim").json()
        assert armed["status"] == "running"
        assert armed["step"] == "open_missions"
        assert client.get("/api/missions").json()["claim"]["status"] == "running"


@pytest.mark.parametrize("status_code,message", [
    (409, "Claiming missions requires a confirmed main menu"),
    (409, "A milestones claim is already walking the menus"),
    (503, "Mission claims are unavailable: the OCR engine could not be built"),
])
def test_a_refused_missions_claim_keeps_the_runner_s_status_code_and_reason(
    status_code: int, message: str,
) -> None:
    from runner import RunnerError

    with _account_client(_FakeClaimRunner(RunnerError(message, status_code))) as client:
        response = client.post("/api/missions/claim")
        assert response.status_code == status_code
        assert response.json()["detail"] == message


def test_a_backend_without_a_runner_cannot_be_asked_to_claim_missions() -> None:
    """412, not 404: no runner means no device transaction was ever possible."""
    with _account_client(None) as client:
        assert client.post("/api/missions/claim").status_code == 412


# -- Milestones claim transaction ------------------------------------------
class _FakeMilestonesRunner:
    def __init__(self, error: Exception | None = None) -> None:
        from milestones_claim import MilestonesClaim
        from milestones_screen import MilestonesReadings

        self.milestones_claim = MilestonesClaim()
        self.milestones = MilestonesReadings()
        self.autopilot_state = None
        self._error = error

    def status(self) -> dict:
        return {"running": True, "since": 1.0, "error": None}

    def request_milestones_claim(self) -> dict:
        if self._error is not None:
            raise self._error
        self.milestones_claim.request()
        return self.milestones_claim.snapshot()


def test_claiming_milestones_arms_the_walk_and_reports_it_on_the_ladder() -> None:
    runner = _FakeMilestonesRunner()
    with _account_client(runner) as client:
        assert client.get("/api/milestones").json()["claim"]["status"] == "idle"
        armed = client.post("/api/milestones/claim").json()
        assert armed["status"] == "running"
        assert armed["step"] == "open_milestones"
        assert client.get("/api/milestones").json()["claim"]["status"] == "running"


@pytest.mark.parametrize("status_code,message", [
    (409, "Claiming milestones requires a confirmed main menu"),
    (409, "A missions claim is already walking the menus"),
    (503, "Milestone claims are unavailable: the OCR engine could not be built"),
])
def test_a_refused_milestones_claim_keeps_the_runner_s_status_code_and_reason(
    status_code: int, message: str,
) -> None:
    from runner import RunnerError

    with _account_client(_FakeMilestonesRunner(RunnerError(message, status_code))) as client:
        response = client.post("/api/milestones/claim")
        assert response.status_code == status_code
        assert response.json()["detail"] == message


def test_a_backend_without_a_runner_reports_no_ladder_rather_than_an_empty_one() -> None:
    """Absent, not null - the same distinction /api/account and /api/missions make."""
    with _account_client(None) as client:
        assert client.get("/api/milestones").json() == {}
        assert client.post("/api/milestones/claim").status_code == 412


# -- /api/director ---------------------------------------------------------
# Read-only and armed-nothing: this phase computes the plan and shows it. A
# human reads /api/director daily; nothing downstream of it in this phase
# acts on it. See director.py's own module docstring for the ranking and
# hold rules these tests are pinning at the HTTP boundary.

def test_the_director_route_ranks_objectives_with_citations() -> None:
    with _account_client(None) as client:
        payload = client.get("/api/director").json()
        assert payload["reason"]
        assert payload["candidates"]
        for candidate in payload["candidates"]:
            assert candidate["objective_id"]
            assert candidate["knowledge_refs"]
            assert candidate["why"]


def test_the_director_route_needs_no_runner() -> None:
    """It reads and recommends. A dashboard with no bot attached must still
    be able to show what the bot would do."""
    with _account_client(None) as client:
        assert client.get("/api/director").status_code == 200


def test_the_director_route_reports_held_objectives_even_below_the_top_five() -> None:
    """A hold is the most actionable thing on the page - it is a decision
    waiting for a human - so it is never truncated away."""
    with _account_client(None) as client:
        payload = client.get("/api/director").json()
        assert any(c["held_by"] for c in payload["candidates"])


def test_the_director_route_needs_no_db_either() -> None:
    """--no-store (db_path=None, exactly what _account_client(None) wires) is
    a supported mode, not an error - the same contract /api/stats, /api/runs
    and /api/errors already keep. An empty run history reads as "no rate has
    ever been measured", not a 500."""
    with _account_client(None) as client:
        response = client.get("/api/director")
        assert response.status_code == 200
        assert response.json()["revision_id"] is None


def test_an_infinite_horizon_never_reaches_the_wire_as_the_bare_json_token() -> None:
    """math.inf is not valid JSON - json.dumps happily emits the bare,
    non-standard token `Infinity` for it, which a strict parser (a browser's
    own JSON.parse, notably) rejects outright. Reading the route through
    TestClient's `.json()` (Python's own permissive json.loads) would not
    catch a regression here, so this asserts on the raw response TEXT: the
    literal substring "Infinity" must never appear on the wire, and every
    hours_to_afford must instead be an explicit, distinguishable object."""
    with _account_client(None) as client:
        response = client.get("/api/director")
        assert "Infinity" not in response.text
        assert "NaN" not in response.text
        payload = response.json()
        kinds = {c["hours_to_afford"]["kind"] for c in payload["candidates"]}
        assert kinds <= {"unknown", "infinite", "hours"}


def test_an_unknown_horizon_is_never_confused_with_an_infinite_one_on_the_wire() -> None:
    """`None` ("we cannot say - go gather data") and `math.inf` ("measured:
    not at this rate, ever") must stay two distinct `kind`s across the JSON
    boundary - collapsing them is exactly the mistake this phase exists to
    prevent. With no db and no account revision wired, every priced
    objective's rate is unmeasured, so "unknown" must actually appear; the
    committed graph has no coin-priced objective at all (only uw.slot.* is
    priced, in stones, which progression.rates never measures - see
    director.py's own _priced_objective test fixture note), so "infinite"
    is not expected to appear here and this only pins that it COULD without
    ever being mistaken for "unknown"."""
    with _account_client(None) as client:
        payload = client.get("/api/director").json()
        kinds = {c["hours_to_afford"]["kind"] for c in payload["candidates"]}
        assert "unknown" in kinds
        assert "infinite" not in kinds  # not reachable from this fixture; see docstring


def test_the_route_reason_matches_an_equivalent_direct_call_to_director_plan() -> None:
    """CurrencyRates.reason is the only thing that tells a human WHY a
    horizon is unknown, and it reaches the screen through `Plan.reason`
    (quoted when nothing is `top`) and through a priced candidate's `why` -
    director.py's own test suite (test_director.py) pins that quoting
    directly and exhaustively. What THIS route can uniquely get wrong is
    discarding it on the way out: `director.as_payload` only reshapes
    `candidates` (the horizon and knowledge citations), so the route's
    `reason` must be byte-identical to what a direct `director.plan()` call
    on the same (empty account, no runs, default strategy) inputs produces
    - not a re-derived or generic string."""
    from progression import rates as progression_rates_

    with _account_client(None) as client:
        payload = client.get("/api/director").json()

    expected = director.plan(
        AccountRevision(), knowledge=knowledge.KNOWLEDGE, graph=objectives.GRAPH,
        rates=progression_rates_([]), strategy=Strategy.from_config(),
    )
    assert payload["reason"] == expected.reason
