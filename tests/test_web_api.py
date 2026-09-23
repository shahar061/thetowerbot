from __future__ import annotations

import asyncio
import itertools
import json
import threading
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import pytest
from fastapi.testclient import TestClient

import db
import events
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app, event_stream, resume_point


@pytest.fixture
def harness(tmp_path: Path) -> tuple[TestClient, BotState, SseSink, events.EventBus, Path, Path]:
    """An app over a real (empty) database, with nothing running behind it."""
    db_path = tmp_path / "bot.db"
    db.connect(db_path).close()
    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()

    state = BotState()
    sse = SseSink(capacity=50)
    bus = events.EventBus()
    bus.subscribe(sse)

    app = create_app(
        state=state, sse=sse, bus=bus, db_path=db_path, unknown_dir=unknown_dir
    )
    return TestClient(app), state, sse, bus, db_path, unknown_dir


def test_status_reports_the_live_state_and_the_dropped_count(harness) -> None:
    client, state, _, bus, _, _ = harness
    state.apply(bus.publish(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wallet=450)))
    bus.dropped = 3

    body = client.get("/api/status").json()

    assert body["screen"] == "IN_RUN"
    assert body["wallet"] == 450
    assert body["dropped"] == 3
    assert body["uptime"] >= 0


def test_concepts_endpoint_is_read_only_and_preserves_legacy_upgrades(harness) -> None:
    client, _, _, _, _, _ = harness
    response = client.get("/api/concepts")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["registry_version"] == "2026-09-05.1"
    assert len(body["glossary"]) == 159
    assert len(body["concepts"]) >= 575
    assert client.post("/api/concepts", json={}).status_code in (404, 405)
    legacy = client.get("/api/upgrades").json()
    assert len(legacy) == 59
    assert legacy[0]["id"] == "damage"
    assert legacy[0]["concept_id"] == "stats.damage"


def test_runs_come_back_newest_first(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    for run_id in (1, 2):
        db.start_run(conn, run_id, started_at=float(run_id))
    conn.close()

    body = client.get("/api/runs").json()

    assert [run["id"] for run in body] == [2, 1]


def test_the_run_limit_is_clamped_to_something_sane(harness) -> None:
    """?limit=999999 must not try to serialise the whole history, and
    ?limit=0 must not come back empty - both asserted on the actual body,
    not just a 200, which would still pass with max(1, ...) deleted."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    for run_id in (1, 2, 3):
        db.start_run(conn, run_id, started_at=float(run_id))
    conn.close()

    assert client.get("/api/runs?limit=999999").status_code == 200

    body = client.get("/api/runs?limit=0").json()
    assert len(body) == 1


def test_one_runs_events_come_back_with_detail_decoded(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.start_run(conn, 1, started_at=1.0)
    db.insert_event(conn, {
        "seq": 1, "run_id": 1, "ts": 2.0, "type": "Tapped", "screen": "IN_RUN",
        "action": "Damage", "reason": None, "score": 0.9, "price": 10,
        "wallet": 90, "detail": '{"x": 1, "y": 2}',
    })
    conn.close()

    body = client.get("/api/runs/1/events").json()

    assert body[0]["type"] == "Tapped"
    assert body[0]["detail"] == {"x": 1, "y": 2}


def a_battle_purchase(seq: int, **overrides: object) -> dict[str, object]:
    """A stored BattlePurchased row, as sinks.store.to_row writes one."""
    detail = {"item": "Damage", "upgrade_id": "damage", "value": 42.0}
    detail.update(overrides.pop("detail", {}))  # type: ignore[arg-type]
    row: dict[str, object] = {
        "seq": seq, "run_id": 1, "ts": 100.0 + seq, "type": "BattlePurchased",
        "screen": "IN_RUN", "action": None, "reason": None, "score": None,
        "price": 120, "wallet": None, "detail": json.dumps(detail),
    }
    row.update(overrides)
    return row


def test_run_purchases_carry_the_category_the_event_does_not(harness) -> None:
    """BattlePurchased has no category field; it is joined from the catalog."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.start_run(conn, 1, started_at=1.0)
    db.insert_event(conn, a_battle_purchase(1))
    conn.close()

    body = client.get("/api/runs/1/purchases").json()

    assert [p["item"] for p in body["purchases"]] == ["Damage"]
    assert body["purchases"][0]["category"] == "ATTACK"
    assert body["purchases"][0]["price"] == 120


def test_a_purchase_of_an_uncatalogued_upgrade_reports_no_category(harness) -> None:
    """An upgrade_id the catalog does not know gets None, not a guess."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_event(conn, a_battle_purchase(1, detail={"upgrade_id": "nonesuch"}))
    conn.close()

    body = client.get("/api/runs/1/purchases").json()

    assert body["purchases"][0]["category"] is None


def test_run_purchase_totals_count_every_buy_and_split_by_category(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_event(conn, a_battle_purchase(1, price=100))
    db.insert_event(conn, a_battle_purchase(2, price=250))
    db.insert_event(conn, a_battle_purchase(
        3, price=900, detail={"item": "Health", "upgrade_id": "health"}))
    conn.close()

    totals = client.get("/api/runs/1/purchases").json()["totals"]

    assert totals["count"] == 3
    assert totals["spent"] == 1250
    assert totals["by_category"] == {"ATTACK": 2, "DEFENSE": 1}


def test_an_unreadable_price_is_counted_apart_from_the_total_spent(harness) -> None:
    """None is "we could not read it", not zero. Summing it as zero would
    report a total the run did not actually spend."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_event(conn, a_battle_purchase(1, price=100))
    db.insert_event(conn, a_battle_purchase(2, price=None))
    conn.close()

    totals = client.get("/api/runs/1/purchases").json()["totals"]

    assert totals["count"] == 2
    assert totals["spent"] == 100
    assert totals["unpriced"] == 1


def test_a_run_that_bought_nothing_reports_zeroed_totals(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.start_run(conn, 1, started_at=1.0)
    conn.close()

    body = client.get("/api/runs/1/purchases").json()

    assert body["purchases"] == []
    assert body["totals"] == {"count": 0, "spent": 0, "unpriced": 0, "by_category": {}}


def test_unknown_lists_snapshots_newest_first(harness) -> None:
    client, _, _, _, _, unknown_dir = harness
    (unknown_dir / "1000.png").write_bytes(b"one")
    (unknown_dir / "2000.png").write_bytes(b"two")

    body = client.get("/api/unknown").json()

    assert [shot["name"] for shot in body] == ["2000.png", "1000.png"]
    assert body[0]["url"] == "/api/unknown/2000.png"


def test_a_snapshot_is_served_as_a_png(harness) -> None:
    client, _, _, _, _, unknown_dir = harness
    (unknown_dir / "1000.png").write_bytes(b"not really a png")

    response = client.get("/api/unknown/1000.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_an_encoded_slash_never_reaches_the_snapshot_route(harness) -> None:
    """A percent-encoded slash is rejected by Starlette's routing itself, before
    the handler runs - so this only proves the router's own behaviour. The
    guard's real protective value (a name that stays inside one path segment
    but still escapes the directory, e.g. via a symlink) is exercised by
    test_a_symlink_escaping_the_directory_is_refused below.
    """
    client, *_ = harness

    assert client.get("/api/unknown/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_a_symlink_escaping_the_directory_is_refused(harness, tmp_path: Path) -> None:
    """A name with no slash in it at all - passes the router - can still name a
    symlink that resolves outside unknown_dir. resolve() must catch that."""
    client, _, _, _, _, unknown_dir = harness
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"outside the sandbox")
    (unknown_dir / "escape.png").symlink_to(secret)

    assert client.get("/api/unknown/escape.png").status_code == 404


def test_a_missing_snapshot_directory_is_an_empty_list_not_an_error(tmp_path: Path) -> None:
    """A fresh checkout has never seen an unknown screen."""
    db_path = tmp_path / "bot.db"
    db.connect(db_path).close()
    app = create_app(
        state=BotState(), sse=SseSink(), bus=events.EventBus(),
        db_path=db_path, unknown_dir=tmp_path / "never-created",
    )

    assert TestClient(app).get("/api/unknown").json() == []


# --- /api/events/stream -----------------------------------------------------
#
# event_stream() is driven directly here instead of through TestClient.stream():
# Starlette's TestClient buffers an entire ASGI response before returning
# anything to the caller (portal.call(self.app, ...) in its testclient runs
# the app coroutine to completion first), so a generator that only ends on
# disconnect can never be observed mid-stream through it - the `with
# client.stream(...)` call itself just hangs forever, no matter what the test
# body does afterwards. Calling event_stream() as a plain async generator
# sidesteps that entirely and cannot hang: `_disconnect_after` guarantees it
# terminates within a bounded number of polls.


def _disconnect_after(n: int) -> Callable[[], Awaitable[bool]]:
    """A fake `is_disconnected`: reports connected for the first `n` polls,
    then disconnected - so event_stream() drains what it should and then
    terminates, instead of the infinite loop it runs in production."""
    calls = itertools.count()

    async def is_disconnected() -> bool:
        return next(calls) >= n

    return is_disconnected


def _drain(gen: AsyncIterator[str]) -> list[str]:
    """Run an event_stream() generator to completion and return the SSE
    lines it produced, blank lines dropped - the same shape iter_lines()
    would hand a real HTTP client, since each yield is one `id:`/`data:`
    pair joined by embedded newlines rather than two separate messages."""

    async def collect() -> list[str]:
        lines: list[str] = []
        async for chunk in gen:
            lines.extend(line for line in chunk.splitlines() if line)
        return lines

    return asyncio.run(collect())


def test_the_stream_replays_what_is_buffered_to_a_fresh_client() -> None:
    """A pre-filled ring, drained from cursor 0, replays oldest first - and
    carries no `event:` field, since the type lives in the JSON body."""
    sse = SseSink(capacity=50)
    sse.offer(events.RunStarted(run_id=1, seq=1))
    sse.offer(events.Tapped(action="Damage", x=1, y=2, score=0.9, seq=2))

    lines = _drain(event_stream(sse, 0, _disconnect_after(1), poll=0.001, heartbeat=100.0))

    assert not any(line.startswith("event:") for line in lines)
    ids = [line[len("id:"):].strip() for line in lines if line.startswith("id:")]
    payloads = [json.loads(line[len("data:"):]) for line in lines if line.startswith("data:")]
    assert ids == ["1", "2"]
    assert [payload["type"] for payload in payloads] == ["RunStarted", "Tapped"]
    assert payloads[1]["action"] == "Damage"


def test_a_reconnecting_browser_resumes_from_its_last_event_id() -> None:
    """This is what seq is for: no gap and no duplicate across a reconnect."""
    sse = SseSink(capacity=50)
    for seq in (1, 2, 3):
        sse.offer(events.Navigated(target="RETRY", seq=seq))
    sse.offer(events.RunStarted(run_id=7, seq=4))

    lines = _drain(event_stream(sse, 3, _disconnect_after(1), poll=0.001, heartbeat=100.0))

    payloads = [json.loads(line[len("data:"):]) for line in lines if line.startswith("data:")]
    assert [payload["type"] for payload in payloads] == ["RunStarted"]
    assert payloads[0]["seq"] == 4


def test_resume_point_prefers_last_event_id_then_since_then_zero() -> None:
    """A tiny stub stands in for Request: resume_point only ever touches
    .headers and .query_params, and there is no live ASGI scope here to
    build a real Request from."""

    class _RequestStub:
        def __init__(self, headers: dict[str, str], params: dict[str, str]) -> None:
            self.headers = headers
            self.query_params = params

    assert resume_point(_RequestStub({"last-event-id": "7"}, {})) == 7
    assert resume_point(_RequestStub({}, {"since": "5"})) == 5
    assert resume_point(_RequestStub({"last-event-id": "not-a-number"}, {})) == 0
    assert resume_point(_RequestStub({}, {})) == 0


def test_an_idle_stream_sends_a_heartbeat_ping() -> None:
    sse = SseSink(capacity=50)

    lines = _drain(event_stream(sse, 0, _disconnect_after(1), poll=0.001, heartbeat=0.001))

    assert lines == [": ping"]


def test_the_generator_stops_once_the_client_has_disconnected() -> None:
    """Once is_disconnected reports True the generator ends instead of
    spinning - this is what keeps every other test above from hanging."""
    sse = SseSink(capacity=50)
    gen = event_stream(sse, 0, _disconnect_after(0), poll=0.001, heartbeat=100.0)

    async def step() -> None:
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(step())


async def _is_never_disconnected() -> bool:
    return False


def test_an_already_set_shutdown_flag_ends_the_stream_immediately() -> None:
    """Finding 1: the shutdown deadlock happens because is_disconnected()
    never fires while a tab is open. `shutdown` has to be able to end the
    generator on its own, with is_disconnected() stuck reporting False."""
    sse = SseSink(capacity=50)
    shutdown = threading.Event()
    shutdown.set()
    gen = event_stream(sse, 0, _is_never_disconnected, poll=0.001, shutdown=shutdown)

    async def step() -> None:
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(step())


def test_shutdown_set_mid_stream_ends_it_on_the_next_poll() -> None:
    """The actual shutdown scenario: the flag flips after the generator has
    already yielded at least one frame, with the browser still connected.
    A generator that only checked `shutdown` before its first yield would
    pass the test above and still hang here."""
    sse = SseSink(capacity=50)
    sse.offer(events.RunStarted(run_id=1, seq=1))
    shutdown = threading.Event()
    gen = event_stream(sse, 0, _is_never_disconnected, poll=0.001, shutdown=shutdown)

    async def step() -> None:
        first = await gen.__anext__()
        assert "RunStarted" in first
        shutdown.set()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(step())


def test_a_stale_last_event_id_past_the_ring_is_reset_to_replay_everything() -> None:
    """M3: --no-store (or a fresh --db) restarts seq at 1 while an already
    open tab still sends its old, now-unreachably-high Last-Event-ID. Without
    a reset, since() filters on seq > cursor forever and the feed looks dead
    until enough new events accumulate to pass the stale cursor."""
    sse = SseSink(capacity=50)
    sse.offer(events.RunStarted(run_id=1, seq=1))
    sse.offer(events.Tapped(action="Damage", x=1, y=2, score=0.9, seq=2))

    stale_cursor = 900  # higher than anything the ring holds
    lines = _drain(
        event_stream(sse, stale_cursor, _disconnect_after(1), poll=0.001, heartbeat=100.0)
    )

    payloads = [json.loads(line[len("data:"):]) for line in lines if line.startswith("data:")]
    assert [payload["type"] for payload in payloads] == ["RunStarted", "Tapped"]


def test_the_dashboard_is_served_at_the_root(harness) -> None:
    client, *_ = harness

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/_next/" in response.text  # the built app is served, not the placeholder file


def test_run_routes_degrade_to_empty_history_under_no_store() -> None:
    """--no-store is a supported mode, not an error: there is no database
    file, so an empty history is the honest answer, not a 500."""
    app = create_app(
        state=BotState(), sse=SseSink(), bus=events.EventBus(), db_path=None,
    )
    client = TestClient(app)

    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/runs").json() == []
    assert client.get("/api/runs/1/events").status_code == 200
    assert client.get("/api/runs/1/events").json() == []
    assert client.get("/api/runs/1/purchases").status_code == 200
    assert client.get("/api/runs/1/purchases").json() == {
        "purchases": [], "totals": {"count": 0, "spent": 0, "unpriced": 0,
                                    "by_category": {}},
    }


def a_ledger_line(seq: int, **overrides: object) -> dict[str, object]:
    line: dict[str, object] = {
        "seq": seq, "ts": 1000.0 + seq, "kind": "WORKSHOP_BUY", "item": "Health",
        "category": "DEFENSE", "currency": "coins", "delta": -75, "price": 75,
        "balance_after": 1695, "observed": 1770, "dry_run": 0, "run_id": None,
        "visit": 1, "reason": None, "detail": None,
    }
    line.update(overrides)
    return line


def test_the_ledger_route_returns_lines_newest_first_with_balances(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_ledger(conn, a_ledger_line(1))
    db.insert_ledger(conn, a_ledger_line(2, balance_after=1620, observed=1695))
    conn.close()

    body = client.get("/api/ledger").json()

    assert [line["seq"] for line in body["lines"]] == [2, 1]
    assert body["balances"] == {"coins": 1620, "gems": None}
    assert body["rehearsals"] == 0


def test_the_ledger_route_reports_every_currency_the_history_holds(harness) -> None:
    """Stones are stored today, from a milestone's generic currency field,
    and a route that only ever answered for coins and gems left the page no
    way to show or filter them."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_ledger(conn, a_ledger_line(1))
    db.insert_ledger(conn, a_ledger_line(
        2, kind="MILESTONE_CLAIM", item="Tier 2 Wave 50", category=None,
        currency="stones", delta=10, price=None, balance_after=None, observed=None,
    ))
    conn.close()

    body = client.get("/api/ledger").json()

    assert body["currencies"] == ["coins", "stones"]
    assert body["balances"] == {"coins": 1695, "gems": None, "stones": None}
    # Stones' None means "never tracked", coins' would mean "not read yet" -
    # and this is how the page tells the two apart.
    assert body["balanced"] == ["coins", "gems"]


def test_the_ledger_route_hides_rehearsals_but_counts_them(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_ledger(conn, a_ledger_line(1))
    db.insert_ledger(conn, a_ledger_line(2, dry_run=1, delta=0))
    conn.close()

    hidden = client.get("/api/ledger").json()
    shown = client.get("/api/ledger?include_rehearsals=true").json()

    assert [line["seq"] for line in hidden["lines"]] == [1]
    assert hidden["rehearsals"] == 1
    assert [line["seq"] for line in shown["lines"]] == [2, 1]


def test_the_ledger_route_pages_with_a_cursor(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    for seq in range(1, 4):
        db.insert_ledger(conn, a_ledger_line(seq))
    conn.close()

    first = client.get("/api/ledger?limit=2").json()
    assert len(first["lines"]) == 2
    assert first["next"] == first["lines"][-1]["id"]

    second = client.get(f"/api/ledger?limit=2&before={first['next']}").json()
    assert len(second["lines"]) == 1
    assert second["next"] is None


def test_the_ledger_route_keeps_paging_after_finishing_an_event(harness) -> None:
    """A page extended to finish an event holds MORE than the limit. A cursor
    computed with `== limit` would report the history as finished there, and
    everything older would be unreachable."""
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.insert_ledger(conn, a_ledger_line(1))
    db.insert_ledger(conn, a_ledger_line(2, currency="coins"))
    db.insert_ledger(conn, a_ledger_line(2, currency="gems"))
    db.insert_ledger(conn, a_ledger_line(3))
    conn.close()

    first = client.get("/api/ledger?limit=2").json()
    assert [line["seq"] for line in first["lines"]] == [3, 2, 2]
    assert first["next"] is not None

    second = client.get(f"/api/ledger?limit=2&before={first['next']}").json()
    assert [line["seq"] for line in second["lines"]] == [1]


def test_the_ledger_route_answers_no_store_with_an_empty_ledger(harness) -> None:
    """--no-store is a supported mode. An empty account is the honest
    answer; a 500 is not."""
    client, state, sse, bus, _, unknown_dir = harness
    app = create_app(state=state, sse=sse, bus=bus, db_path=None,
                     unknown_dir=unknown_dir)

    body = TestClient(app).get("/api/ledger").json()

    assert body == {
        "lines": [], "balances": {"coins": None, "gems": None},
        "currencies": [], "balanced": ["coins", "gems"],
        "rehearsals": 0, "next": None,
    }
