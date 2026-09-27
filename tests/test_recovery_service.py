"""Bounded service checks: synthetic credentials and in-memory HTTP only."""

import asyncio
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import threading
import time
from typing import Callable

import httpx2
import pytest

from recovery_budget import RecoveryBudget
from recovery_policy import RecoveryContext, RecoveryImage, RecoveryRequest, RecoveryScope, RecoverySettings


KEY = "synthetic-secret-never-log"
MODEL = "openai/gpt-5.4-nano"
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lN8AAAAASUVORK5CYII=")


def request(request_id: str = "request", worker: str = "worker", *,
            incident: str = "incident", seconds: float = 15) -> RecoveryRequest:
    now = time.monotonic()
    scope = RecoveryScope(worker=worker, account_id="private-account", lease_id="lease",
                          attempt_id="attempt", attempt_generation="generation", boot_id="boot",
                          worker_generation=1, strategy_revision=1, device_command_generation=1,
                          pending_transaction_generation=1)
    return RecoveryRequest(request_id=request_id, incident_id=incident, fingerprint="stall",
                           context=RecoveryContext(scope=scope, screen="home", screen_generation=1,
                               observation_generation=1, observed_at_monotonic=now, candidates=()),
                           created_at_monotonic=now, deadline_at_monotonic=now + seconds,
                           objective="Observe current screen",
                           image=RecoveryImage(media_type="image/png", data=TINY_PNG, width=1, height=1))


def metadata() -> dict:
    return {"data": {"id": MODEL, "architecture": {"input_modalities": ["text", "image"],
            "output_modalities": ["text"]}, "endpoints": [{"model_id": MODEL, "tag": "openai",
            "max_prompt_tokens": 400000, "max_completion_tokens": 4096,
            "supported_parameters": ["max_tokens", "response_format"],
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "pricing": {"prompt": "0.0000002", "completion": "0.0000002"}}]}}


def completion(cost: float | None = 0) -> dict:
    return {"model": MODEL, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "action_id": "observe", "candidate_id": None, "expected_postcondition": "fresh_observation",
        "explanation": "Read screen"})}}], "usage": {} if cost is None else {"cost": cost}}


def until(predicate: Callable[[], object], seconds: float = 3) -> object:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("condition did not complete")


class Exchange:
    def __init__(self, *, suspend: str | None = None, cost: float | None = 0,
                 resist_cancel: bool = False, get_delay: float = 0) -> None:
        self.suspend = suspend
        self.cost = cost
        self.resist_cancel = resist_cancel
        self.get_delay = get_delay
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests: list[str] = []
        self.closed = threading.Event()

    def factory(self) -> httpx2.AsyncClient:
        outer = self

        class Client(httpx2.AsyncClient):
            async def aclose(self) -> None:
                await super().aclose()
                outer.closed.set()

        return Client(transport=httpx2.MockTransport(self.handle))

    async def handle(self, item: httpx2.Request) -> httpx2.Response:
        self.requests.append(item.method)
        if item.method == "GET" and self.get_delay:
            await asyncio.sleep(self.get_delay)
        if item.method == self.suspend:
            self.entered.set()
            while not self.release.is_set():
                try:
                    await asyncio.sleep(0.005)
                except asyncio.CancelledError:
                    if not self.resist_cancel:
                        raise
        return httpx2.Response(200, json=metadata() if item.method == "GET" else completion(self.cost))


@pytest.fixture
def services(tmp_path: Path):
    from recovery_service import RecoveryService

    created = []
    exchanges = []

    def make(exchange: Exchange | None = None, *, worker: str = "worker", **kwargs: object):
        exchange = exchange or Exchange()
        exchanges.append(exchange)
        service = RecoveryService(tmp_path, worker=worker,
            settings=kwargs.pop("settings", RecoverySettings(mode="assist")),
            api_key=kwargs.pop("api_key", KEY), client_factory=exchange.factory, **kwargs)
        created.append(service)
        return service

    yield make
    for exchange in exchanges:
        exchange.release.set()
    for service in created:
        service.close()
    for service in created:
        until(lambda: service.status().shutdown_complete)


def test_submit_poll_and_scans_do_not_wait_for_suspended_network(services) -> None:
    exchange = Exchange(suspend="POST")
    service = services(exchange)
    req = request()
    assert service.submit(req)
    until(exchange.entered.is_set)
    start = time.monotonic()
    scans = 0
    for _ in range(100):
        assert service.poll(incident_id=req.incident_id) is None
        assert not service.submit(request("overflow"))
        scans += 1
    assert scans == 100 and time.monotonic() - start < 0.1
    exchange.release.set()
    reply = until(lambda: service.poll(incident_id=req.incident_id))
    assert reply.request_id == req.request_id and reply.proposal.action_id == "observe"
    assert service.poll(incident_id=req.incident_id) is None


@pytest.mark.parametrize(("mode", "key", "blocker"), [("off", KEY, "disabled"), ("assist", None, "credentials_missing")])
def test_disabled_service_never_builds_client(services, mode: str, key: str | None, blocker: str) -> None:
    exchange = Exchange()
    service = services(exchange, settings=RecoverySettings(mode=mode), api_key=key)
    assert not service.submit(request())
    assert service.status().blocker == blocker
    assert not exchange.requests


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_invalidation_cancels_and_preserves_possible_cost(services, tmp_path: Path, method: str) -> None:
    exchange = Exchange(suspend=method)
    service = services(exchange)
    assert service.submit(request())
    until(exchange.entered.is_set)
    start = time.monotonic()
    service.invalidate(reason="account changed raw secret /Users/private")
    assert time.monotonic() - start < 0.05
    until(lambda: not service.status().in_flight)
    assert service.poll(incident_id="incident") is None
    summary = RecoveryBudget(tmp_path).summary(day=datetime.now(timezone.utc).date().isoformat())
    assert (summary.reserved_microusd > 0) == (method == "POST")
    assert service.status().last_outcome == "invalidated"


def test_late_cancel_resistant_reply_cannot_be_delivered(services) -> None:
    exchange = Exchange(suspend="POST", resist_cancel=True)
    service = services(exchange)
    assert service.submit(request())
    until(exchange.entered.is_set)
    service.invalidate(reason="scope_changed")
    assert not service.submit(request("next"))
    exchange.release.set()
    until(lambda: not service.status().in_flight)
    assert service.poll(incident_id="incident") is None


def test_total_deadline_includes_capability_lookup(services, tmp_path: Path) -> None:
    exchange = Exchange(suspend="POST", get_delay=0.12)
    service = services(exchange, settings=RecoverySettings(mode="assist", deadline_seconds=0.20))
    req = request()
    assert service.submit(req)
    until(lambda: service.status().last_outcome == "timeout")
    assert time.monotonic() - req.created_at_monotonic < 0.35
    until(lambda: not service.status().in_flight)
    assert service.poll(incident_id="incident").error == "timeout"
    assert RecoveryBudget(tmp_path).summary(day=datetime.now(timezone.utc).date().isoformat()).reserved_microusd > 0


@pytest.mark.parametrize("cost", [None, 0])
def test_exact_zero_and_unknown_cost_are_distinct_and_calls_persist(services, tmp_path: Path, cost: float | None) -> None:
    exchange = Exchange(cost=cost)
    service = services(exchange)
    for index in range(2):
        assert service.submit(request(f"request-{index}"))
        reply = until(lambda: service.poll(incident_id="incident"))
        assert reply.cost_microusd == cost
    assert service.submit(request("third"))
    reply = until(lambda: service.poll(incident_id="incident"))
    assert reply.error == "budget_exhausted"
    assert exchange.requests.count("POST") == 2
    summary = RecoveryBudget(tmp_path).summary(day=datetime.now(timezone.utc).date().isoformat())
    assert (summary.reserved_microusd > 0) == (cost is None)
    assert summary.settled_microusd == 0


def test_same_request_cannot_send_twice_after_consumption(services) -> None:
    exchange = Exchange()
    service = services(exchange)
    req = request()
    assert service.submit(req)
    until(lambda: service.poll(incident_id="incident"))
    assert service.submit(req)
    assert until(lambda: service.poll(incident_id="incident")).error == "budget_exhausted"
    assert exchange.requests.count("POST") == 1


def test_fleet_and_worker_locks_bound_independent_services(services) -> None:
    first_exchange, second_exchange, rejected_exchange = (Exchange(suspend="GET") for _ in range(3))
    first, second = services(first_exchange), services(second_exchange, worker="other")
    same_worker, saturated = services(rejected_exchange), services(Exchange(), worker="third")
    assert first.submit(request())
    assert second.submit(request("second", "other", incident="incident-2"))
    until(first_exchange.entered.is_set)
    until(second_exchange.entered.is_set)
    assert same_worker.submit(request("duplicate-worker"))
    until(lambda: same_worker.status().blocker == "worker_busy")
    assert saturated.submit(request("third", "third", incident="incident-3"))
    until(lambda: saturated.status().blocker == "fleet_busy")
    assert not rejected_exchange.requests


def test_close_is_nonblocking_and_reports_owned_thread_shutdown_failure(services) -> None:
    exchange = Exchange(suspend="POST", resist_cancel=True)
    service = services(exchange, shutdown_grace_seconds=0.03)
    assert service.submit(request())
    until(exchange.entered.is_set)
    start = time.monotonic()
    service.close()
    assert time.monotonic() - start < 0.05
    until(lambda: service.status().shutdown_failed)
    assert service.poll(incident_id="incident") is None
    assert not service.submit(request("after-close"))
    exchange.release.set()
    until(lambda: service.status().shutdown_complete)
    assert exchange.closed.is_set()


def test_status_is_sanitized_and_scope_must_match_worker(services) -> None:
    service = services()
    assert not service.submit(request(worker="foreign"))
    assert service.submit(request())
    until(lambda: service.status().last_outcome == "proposal")
    serialized = json.dumps(asdict(service.status()))
    for private in (KEY, "private-account", "explanation", "scope", "image", "headers", "response"):
        assert private not in serialized
    assert service.status().budget is not None
    assert service.status().budget_policy is not None


def test_exact_reserved_quote_is_passed_unchanged_and_marked_sent(services, tmp_path: Path, monkeypatch) -> None:
    import recovery_service as module
    import sqlite3

    original_quote, original_propose = module.get_model_capability, module.propose_recovery
    quotes = []

    async def quote(*args: object, **kwargs: object):
        result = await original_quote(*args, **kwargs)
        quotes.append(result)
        return result

    async def propose(*args: object, **kwargs: object):
        assert kwargs["reserved_capability"] is quotes[0]
        with sqlite3.connect(tmp_path / "recovery-budget.sqlite3") as db:
            row = db.execute("SELECT maximum_microusd, state FROM recovery_reservations").fetchone()
        assert row == (quotes[0].worst_case_microusd, "sent")
        return await original_propose(*args, **kwargs)

    monkeypatch.setattr(module, "get_model_capability", quote)
    monkeypatch.setattr(module, "propose_recovery", propose)
    service = services()
    assert service.submit(request())
    assert until(lambda: service.poll(incident_id="incident")).proposal is not None


def test_invalidate_after_reserve_before_send_releases_only_unsent_cost(services, tmp_path: Path, monkeypatch) -> None:
    original_reserve = RecoveryBudget.reserve
    reserved, proceed = threading.Event(), threading.Event()

    def reserve(self: RecoveryBudget, **kwargs: object) -> bool:
        result = original_reserve(self, **kwargs)
        reserved.set()
        assert proceed.wait(2)
        return result

    monkeypatch.setattr(RecoveryBudget, "reserve", reserve)
    exchange = Exchange()
    service = services(exchange)
    assert service.submit(request())
    try:
        until(reserved.is_set)
        service.invalidate(reason="stop")
    finally:
        proceed.set()
    until(lambda: not service.status().in_flight)
    assert "POST" not in exchange.requests
    assert RecoveryBudget(tmp_path).summary(day=datetime.now(timezone.utc).date().isoformat()).total_microusd == 0


def test_ready_reply_expires_and_wrong_identity_does_not_consume(services) -> None:
    service = services()
    assert service.submit(request(seconds=0.08))
    until(lambda: service.status().phase == "ready")
    assert service.poll(incident_id="other") is None
    assert service.poll(incident_id="incident", request_id="other") is None
    assert not service.submit(request("overflow"))
    time.sleep(0.09)
    assert service.poll(incident_id="incident") is None
    assert service.status().last_outcome == "stale_request"


def test_reserved_cost_is_visible_while_provider_is_suspended(services) -> None:
    exchange = Exchange(suspend="POST")
    service = services(exchange)
    assert service.submit(request())
    until(exchange.entered.is_set)
    assert service.status().budget.reserved_microusd > 0


def _process_holds_request(root: str, worker: str, ready, release) -> None:
    from recovery_service import RecoveryService

    exchange = Exchange(suspend="POST")
    service = RecoveryService(Path(root), worker=worker, settings=RecoverySettings(mode="assist"),
                              api_key=KEY, client_factory=exchange.factory)
    try:
        assert service.submit(request("request-" + worker, worker, incident="incident-" + worker))
        until(exchange.entered.is_set)
        ready.put("entered")
        assert release.wait(5)
    finally:
        exchange.release.set()
        service.close()
        until(lambda: service.status().shutdown_complete)


def test_two_separate_processes_bound_fleet_and_stable_worker(services, tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, release = context.Queue(), context.Event()
    children = [context.Process(target=_process_holds_request,
                args=(str(tmp_path), worker, ready, release)) for worker in ("one", "two")]
    try:
        for child in children:
            child.start()
        assert ready.get(timeout=5) == "entered"
        assert ready.get(timeout=5) == "entered"
        exchange = Exchange()
        third = services(exchange, worker="three")
        duplicate = services(exchange, worker="one")
        assert third.submit(request("third", "three"))
        assert duplicate.submit(request("duplicate", "one"))
        until(lambda: third.status().blocker == "fleet_busy")
        until(lambda: duplicate.status().blocker == "worker_busy")
        assert not exchange.requests
    finally:
        release.set()
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.terminate()
                child.join(timeout=2)
        ready.close()
    assert all(child.exitcode == 0 for child in children)


def test_deadline_cancels_metadata_without_reserving_or_sending(services, tmp_path: Path) -> None:
    exchange = Exchange(suspend="GET")
    service = services(exchange, settings=RecoverySettings(mode="shadow", deadline_seconds=0.06))
    assert service.submit(request())
    reply = until(lambda: service.poll(incident_id="incident"))
    assert reply.error == "timeout"
    assert exchange.requests == ["GET"]
    assert RecoveryBudget(tmp_path).summary(day=datetime.now(timezone.utc).date().isoformat()).total_microusd == 0


def test_timeout_resistant_transport_never_delivers_late_proposal(services) -> None:
    exchange = Exchange(suspend="POST", resist_cancel=True)
    service = services(exchange, settings=RecoverySettings(mode="assist", deadline_seconds=0.05),
                       shutdown_grace_seconds=0.02)
    assert service.submit(request())
    until(lambda: service.status().shutdown_failed)
    assert service.poll(incident_id="incident") is None
    assert not service.submit(request("new"))
    exchange.release.set()
    reply = until(lambda: service.poll(incident_id="incident"))
    assert reply.error == "timeout" and reply.proposal is None
    assert service.status().budget.total_microusd == 0  # Exact returned zero can reconcile accounting.


def test_overcharge_disables_fleet_and_never_releases_cost(services) -> None:
    service = services(Exchange(cost=0.06))
    assert service.submit(request())
    assert until(lambda: service.poll(incident_id="incident")).error == "budget_exhausted"
    assert service.status().budget.disabled
    assert service.status().budget.settled_microusd == 60_000
    assert service.submit(request("new", incident="other-incident"))
    assert until(lambda: service.poll(incident_id="other-incident")).error == "budget_exhausted"


def test_client_initialization_failure_is_sanitized_and_scan_remains_available(services, monkeypatch) -> None:
    def broken(self: Exchange) -> httpx2.AsyncClient:
        raise RuntimeError(KEY + " provider raw blob /Users/private")

    monkeypatch.setattr(Exchange, "factory", broken)
    service = services()
    until(lambda: service.status().shutdown_complete)
    assert service.status().blocker == "service_unavailable"
    assert not service.submit(request())
    assert KEY not in json.dumps(asdict(service.status()))


def test_cancelled_call_still_counts_after_service_restart(services) -> None:
    exchange = Exchange(suspend="POST")
    service = services(exchange)
    assert service.submit(request())
    until(exchange.entered.is_set)
    service.close()
    until(lambda: service.status().shutdown_complete)
    next_exchange = Exchange()
    restarted = services(next_exchange)
    assert restarted.submit(request("second"))
    assert until(lambda: restarted.poll(incident_id="incident")).proposal is not None
    assert restarted.submit(request("third"))
    assert until(lambda: restarted.poll(incident_id="incident")).error == "budget_exhausted"
    assert next_exchange.requests.count("POST") == 1


def test_transport_itself_receives_total_deadline_not_a_new_send_window(services, monkeypatch) -> None:
    import recovery_service as module

    original_quote, original_propose = module.get_model_capability, module.propose_recovery
    quoted = []

    async def quote(req: RecoveryRequest, *args: object, **kwargs: object):
        quoted.append(req)
        return await original_quote(req, *args, **kwargs)

    async def propose(req: RecoveryRequest, *args: object, **kwargs: object):
        assert req is quoted[0]
        assert req.deadline_at_monotonic <= req.created_at_monotonic + 0.5
        return await original_propose(req, *args, **kwargs)

    monkeypatch.setattr(module, "get_model_capability", quote)
    monkeypatch.setattr(module, "propose_recovery", propose)
    service = services(settings=RecoverySettings(mode="assist", deadline_seconds=0.5))
    assert service.submit(request())
    assert until(lambda: service.poll(incident_id="incident")).proposal is not None


def test_invalidation_remains_authoritative_after_original_deadline(services) -> None:
    exchange = Exchange(suspend="POST", resist_cancel=True)
    service = services(exchange, settings=RecoverySettings(mode="assist", deadline_seconds=0.05))
    assert service.submit(request())
    until(exchange.entered.is_set)
    service.invalidate(reason="scope changed")
    time.sleep(0.07)
    assert service.status().last_outcome == "invalidated"
    exchange.release.set()
    until(lambda: not service.status().in_flight)
    assert service.poll(incident_id="incident") is None


@pytest.mark.parametrize("release", ["consume", "invalidate", "close", "expire"])
def test_unconsumed_reply_holds_shared_worker_until_released(services, release: str) -> None:
    first_exchange, second_exchange = Exchange(), Exchange()
    first, second = services(first_exchange), services(second_exchange)
    assert first.submit(request("first", seconds=0.25 if release == "expire" else 15))
    until(lambda: first.status().phase == "ready")
    assert second.submit(request("second"))
    assert until(lambda: second.poll(incident_id="incident")).error == "disabled"
    assert second.status().blocker == "worker_busy"
    assert second_exchange.requests == []
    if release == "consume":
        assert first.poll(incident_id="incident").proposal is not None
    elif release == "invalidate":
        first.invalidate(reason="new scope")
    elif release == "close":
        first.close()
    # Lock cleanup runs on the background owner. A caller may retry a temporary
    # worker_busy rejection; no budget or network work occurred for that ID.
    def submit_after_release() -> object:
        second.submit(request("second"))
        reply = second.poll(incident_id="incident")
        return reply if reply and reply.proposal else None

    assert until(submit_after_release).proposal is not None
    assert second_exchange.requests == ["GET", "POST"]
    if release == "expire":
        assert first.poll(incident_id="incident") is None
        assert first.status().last_outcome == "stale_request"


def test_unconsumed_reply_does_not_hold_fleet_network_capacity(services) -> None:
    first = services()
    assert first.submit(request("first"))
    until(lambda: first.status().phase == "ready")
    exchanges = [Exchange(suspend="GET"), Exchange(suspend="GET")]
    others = [services(exchange, worker=f"other-{index}") for index, exchange in enumerate(exchanges)]
    for index, other in enumerate(others):
        assert other.submit(request(f"other-{index}", f"other-{index}", incident=f"other-{index}"))
    for exchange in exchanges:
        until(exchange.entered.is_set)


def test_invalidated_cancel_resistant_request_retains_shared_worker(services) -> None:
    first_exchange, second_exchange = Exchange(suspend="POST", resist_cancel=True), Exchange()
    first, second = services(first_exchange), services(second_exchange)
    assert first.submit(request("first"))
    until(first_exchange.entered.is_set)
    first.invalidate(reason="stop")
    assert second.submit(request("second"))
    assert until(lambda: second.poll(incident_id="incident")).error == "disabled"
    assert second.status().blocker == "worker_busy"
    assert second_exchange.requests == []
    first_exchange.release.set()
    until(lambda: not first.status().in_flight)
    assert second.submit(request("second"))
    assert until(lambda: second.poll(incident_id="incident")).proposal is not None


def test_transient_send_marker_failure_retains_unknown_cost_and_allows_fresh_reservation(services, monkeypatch) -> None:
    original_mark_sent = RecoveryBudget.mark_sent
    attempts = 0

    def mark_sent(self: RecoveryBudget, request_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic storage failure")
        original_mark_sent(self, request_id)

    monkeypatch.setattr(RecoveryBudget, "mark_sent", mark_sent)
    exchange = Exchange()
    service = services(exchange)
    assert service.submit(request("first"))
    assert until(lambda: service.poll(incident_id="incident")).error == "transport"
    retained = service.status().budget.reserved_microusd
    assert retained > 0 and "POST" not in exchange.requests
    assert service.submit(request("second"))
    assert until(lambda: service.poll(incident_id="incident")).proposal is not None
    assert service.status().budget.reserved_microusd == retained
    assert exchange.requests.count("POST") == 1
    assert service.submit(request("third"))
    assert until(lambda: service.poll(incident_id="incident")).error == "budget_exhausted"
