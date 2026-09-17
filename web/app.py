"""The dashboard's HTTP layer: JSON for the machine, a built SPA for the eye.

Two sources, deliberately separate. "What is the bot doing right now" is a
memory question, answered from the shared BotState. "What did it do" is a
history question, answered by read-only SQLite connections opened per request.

Binds 127.0.0.1. No auth, /api/unknown serves screenshots of a live session,
and the control plane lets a caller start and stop the bot, rewrite what it
buys, and create or delete strategy files on disk - see the warning beside
config.WEB_HOST before changing the bind address.

"The web layer never writes" is no longer true in general, and the narrower
claim is the one that matters: it never writes to the DATABASE. db.reader()
opens mode=ro and nothing here can change that. Strategy profiles and local
advisor imports are written through their validating stores.
"""

from __future__ import annotations

from account_state import AccountRevision, AccountState

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from urllib.request import urlopen
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import director
import events
import knowledge
import objectives
import upgrades
from concepts import REGISTRY
from runtime_identity import API_VERSION, PROCESS_IDENTITY, read_frontend_identity
from advisor import AdvisorStore
from web.advisor import advisor_router
from web.account_catalog import AccountChoice, account_choices
from autopilot import AutopilotState
from policy import PRESETS, preset_rules
from progression import compare_tiers, rates as progression_rates
from control import ControlError, Controls
from events import EventBus
from frames import FrameBuffer
from fleet.dashboard import FleetController, FleetRequestError
from bluestacks import HostCapabilityError
from runner import BotRunner, RunnerError
from sinks.sse import SseSink, to_payload
from sinks.state import BotState
from strategy import Strategy, StrategyStore

STATIC_DIR = Path(__file__).parent / "static"

# One page of history is plenty for a dashboard, and it bounds the response
# whatever the query string asks for.
MAX_RUNS_PER_PAGE = 500
MAX_LEDGER_PER_PAGE = 200

# strategy.ControlError carries a `code` naming the KIND of failure, so the
# routes below map a status without matching on message text.
_STATUS_FOR_CODE = {"not_found": 404, "conflict": 409, "invalid": 422}


def resume_point(request: Request) -> int:
    """Where to start streaming from.

    A reconnecting browser sends Last-Event-ID and gets exactly what it
    missed. Anything else - a first visit, a hand-rolled curl, a garbled
    header - starts from 0 and gets the whole ring, so the feed is never
    blank on arrival.
    """
    raw = request.headers.get("last-event-id") or request.query_params.get("since")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def event_stream(
    sse: SseSink,
    cursor: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll: float = config.SSE_POLL_SECONDS,
    heartbeat: float = config.SSE_HEARTBEAT_SECONDS,
    shutdown: threading.Event | None = None,
    allowed: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    """Server-sent events, resumable through Last-Event-ID.

    Polling the ring rather than being pushed to: events arrive on the
    scan loop's thread and are consumed by the event loop, and a poll at
    four times a second against a 2s scan interval is imperceptible while
    needing no cross-thread wakeup at all.

    Kept as a standalone generator - rather than inlined in the route -
    so it can be driven directly in tests. FastAPI's TestClient buffers an
    entire ASGI response before handing anything back to the caller, so a
    generator whose whole point is to keep going until the browser
    disconnects can never be observed mid-stream that way; it has to be
    iterated on its own instead. `is_disconnected` is injected for exactly
    that reason too - production passes `request.is_disconnected`, tests
    pass a fake that reports disconnection after a bounded number of polls.

    `shutdown` is the other way this can end, and the one that matters at
    process shutdown: `is_disconnected()` only reports true once the browser
    closes the tab, which it never does on its own just because the process
    is going down. Without `shutdown`, a held-open dashboard tab and a
    uvicorn server with `should_exit = True` wait on each other forever - the
    response is still "in flight" as far as the server's graceful shutdown
    is concerned, so the transport never closes. Checking the flag lets the
    generator end itself, the response complete, and the connection close
    normally. It is the PROCESS going down, never merely the bot - the same
    Event create_app() and serve_web() take under that name, and the reason
    it is not called `stop` any more: the runner owns a per-bot stop, and one
    Event with two names was how the two got confused. Optional so existing
    callers (and every test predating this) keep working; a fresh Event()
    that nobody ever sets is exactly "never end this way", which is the old
    behaviour.
    """
    if shutdown is None:
        shutdown = threading.Event()

    latest = sse.latest_seq()
    if cursor > latest:
        # A Last-Event-ID from before a seq reset (--no-store, or a fresh
        # --db swap): since() filters on seq > cursor, so a stale cursor
        # higher than anything the ring holds would starve the feed until
        # enough new events accumulate to pass it - the tab just looks dead.
        # Replay everything currently buffered instead of nothing.
        cursor = 0

    idle = 0.0
    while not shutdown.is_set() and (allowed is None or allowed()) and not await is_disconnected():
        batch = sse.since(cursor)
        for event in batch:
            cursor = event.seq
            yield (
                f"id: {event.seq}\n"
                f"data: {json.dumps(to_payload(event))}\n\n"
            )
        idle = 0.0 if batch else idle + poll
        if idle >= heartbeat:
            idle = 0.0
            yield ": ping\n\n"  # keeps an idle connection alive
        await asyncio.sleep(poll)


BOUNDARY = "frame"


async def frame_stream(
    frames: FrameBuffer,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    shutdown: threading.Event,
    poll: float = config.FRAME_POLL_SECONDS,
    allowed: Callable[[], bool] | None = None,
) -> AsyncIterator[bytes]:
    """MJPEG: one connection, rendered natively by a plain <img>.

    Same two exits as event_stream(), and for the same reasons: the browser
    closing the tab, and the process shutting down. An <img> holds its
    response open indefinitely and never disconnects on its own, so without
    the `shutdown` check a held-open device view and a shutting-down uvicorn
    would wait on each other forever. Named for the process, not the bot,
    like every other holder of that Event: stopping the bot leaves the
    device view connected and waiting for the next Start.

    Only sends when the frame number moves, so an idle bot costs one send per
    scan rather than one per poll.
    """
    sent = 0
    while not shutdown.is_set() and (allowed is None or allowed()) and not await is_disconnected():
        current = frames.latest()
        if current is not None and current[0] != sent:
            sent, payload = current
            yield (
                f"--{BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n"
            ).encode("ascii") + payload + b"\r\n"
        await asyncio.sleep(poll)


class ControlPatch(BaseModel):
    """A partial update. Every field optional; absent means "leave it alone".

    Deliberately loose on types beyond the obvious - Controls.apply() is the
    single validator, so the rules live in one place rather than being spelled
    out here and there and drifting apart. `actions` is a list of raw objects
    for the same reason: mirroring ActionRule's fields here would be a second
    schema to keep in step with strategy.py.
    """

    paused: bool | None = None
    affordability: str | None = None
    interval: float | None = None
    click_cooldown: float | None = None
    auto_navigate: bool | None = None
    max_runs: int | None = None
    navigation_cooldown: float | None = None
    screen_confirmations: int | None = None
    tap_jitter_px: float | None = None
    timing_jitter: float | None = None
    tap_delay: float | None = None
    target_speed: float | None = None
    actions: list[dict[str, Any]] | None = None
    autopilot: dict[str, Any] | None = None


class AutopilotCommand(BaseModel):
    action: Literal["buy", "category", "scan"]
    category: Literal["ATTACK", "DEFENSE", "UTILITY"] | None = None
    upgrade_id: str | None = None


class CommandRequest(BaseModel):
    """One thing to do once, as opposed to a setting to hold.

    Loose on the value for the same reason ControlPatch is: Controls.request()
    owns the list of legal commands, and spelling it out here as an Enum
    would be a second copy to keep in step.
    """

    command: str


class FleetProvisionRequest(BaseModel):
    mode: Literal["fresh", "clone"]
    source: str | None = None
    count: int
    targets: list[str]


class FleetSetupRequest(BaseModel):
    capacity: int
    name_prefix: str
    qualification_id: str


class FleetStartInstanceRequest(BaseModel):
    name: str


_CATEGORY_BY_UPGRADE: dict[str, str] = {u.id: u.category for u in upgrades.CATALOG}


def _category_of(upgrade_id: str | None) -> str | None:
    """ATTACK / DEFENSE / UTILITY for an upgrade id, or None if unknown.

    BattlePurchased carries no category - the autopilot knows it from the
    panel it was reading, but the event does not record it - so it is joined
    from the catalog here rather than added to the event and migrated in.
    An id the catalog has never heard of gets None, not a guess.
    """
    return _CATEGORY_BY_UPGRADE.get(upgrade_id) if upgrade_id else None


def create_app(
    *,
    state: BotState,
    sse: SseSink,
    bus: EventBus,
    db_path: Path | None = config.DB_PATH,
    unknown_dir: Path = config.UNKNOWN_DIR,
    shutdown: threading.Event | None = None,
    controls: Controls | None = None,
    checks: Mapping[str, Any] | None = None,
    frames: FrameBuffer | None = None,
    runner: BotRunner | None = None,
    store: StrategyStore | None = None,
    shopping: Any | None = None,
    advisor: AdvisorStore | None = None,
    account_state: AccountState | None = None,
    fleet: FleetController | None = None,
) -> FastAPI:
    # See event_stream()'s docstring for why this exists: without it, an
    # open dashboard tab and a shutting-down uvicorn wait on each other
    # forever. Optional, and created fresh here rather than defaulted on the
    # function signature, so a caller that never passes one (every test
    # predating this finding) keeps the old "never stops this way" behaviour
    # without every call site sharing one mutable Event by accident.
    #
    # This is the PROCESS going down, not the bot - see runner.BotRunner for
    # that half. A dashboard can now outlive the bot it was watching, so the
    # two can no longer share one flag: stopping the bot must never trip
    # this one, and this one is not the runner's business at all.
    if shutdown is None:
        shutdown = threading.Event()

    app = FastAPI(title="The Tower bot")
    accounts = account_state or getattr(runner, "account_state", None) or AccountState()

    def _choices() -> list[AccountChoice]:
        root = getattr(fleet, "root", None)
        if root is None and db_path is not None and (db_path.parent / "fleet-registration.json").exists():
            root = db_path.parent.parent.parent
        return account_choices(Path(root) if root is not None else None, db_path)

    def _running_account(choice: AccountChoice) -> bool:
        if (choice.kind != "worker" or db_path is None or runner is None
                or choice.db_path.resolve() != db_path.resolve()
                or db.bound_account(choice.db_path) != choice.account_id
                or not runner.status().get("running")):
            return False
        try:
            return runner.verified_account() == choice.account_id
        except (RunnerError, AttributeError, OSError, ValueError, TypeError):
            return False

    def _selected(request: Request) -> AccountChoice | None:
        key = request.headers.get("x-account-scope")
        if key is None:
            return None  # Existing API callers retain the single-runtime contract.
        choice = next((item for item in _choices() if item.key == key), None)
        if choice is None:
            raise HTTPException(404, "account_scope_unavailable")
        return choice

    def _history_path(request: Request) -> Path | None:
        choice = _selected(request)
        path = choice.db_path if choice is not None else db_path
        if choice is not None and choice.kind == "worker" and db.bound_account(path) != choice.account_id:
            return None
        return path if path is not None and path.is_file() else None

    def _live_choice(request: Request) -> AccountChoice | None:
        key = request.query_params.get("scope")
        if key is None:
            return None  # Legacy non-browser clients retain the existing route.
        choice = next((item for item in _choices() if item.key == key), None)
        if choice is None or not _running_account(choice):
            raise HTTPException(409, "selected_account_not_running")
        return choice

    @app.get("/api/accounts")
    def account_catalog(local_only: bool = False) -> dict[str, Any]:
        choices = _choices()
        local = {choice.key for choice in choices if _running_account(choice)}

        def remote_running(choice: AccountChoice) -> bool:
            if (local_only or choice.kind != "worker" or choice.key in local
                    or choice.web_port is None):
                return False
            try:
                with urlopen(f"http://127.0.0.1:{choice.web_port}/api/accounts?local_only=true",
                             timeout=0.15) as response:
                    payload = json.load(response)
                return (payload.get("active") == choice.key
                        and any(item.get("key") == choice.key
                                and item.get("account_id") == choice.account_id
                                and item.get("running") is True
                                for item in payload.get("accounts", [])))
            except (OSError, ValueError, TypeError, KeyError):
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            remote = {choice.key for choice, running in zip(choices, pool.map(remote_running, choices))
                      if running}
        running = local | remote
        return {"accounts": [choice.payload(running=choice.key in running) for choice in choices],
                "active": (next(iter(local), None)
                           or next((choice.key for choice in choices if choice.key in remote), None))}

    @app.get("/api/account")
    def account_snapshot(request: Request) -> dict[str, Any]:
        # `collection` is absent, not null, on a backend with no runner: a
        # browser has to tell "cannot run transactions here" apart from "no
        # transaction has been run yet".
        choice = _selected(request)
        if choice is not None and not _running_account(choice):
            payload = AccountState().snapshot()
            if (choice.kind == "worker" and choice.db_path.is_file()
                    and db.bound_account(choice.db_path) == choice.account_id):
                with db.reader(choice.db_path) as conn:
                    row = conn.execute("SELECT id, detail FROM account_revisions ORDER BY id DESC LIMIT 1").fetchone()
                if row is not None:
                    revision = json.loads(row["detail"])
                    if revision.get("account_id") == choice.account_id:
                        revision["revision_id"] = row["id"]
                        payload["revision"] = revision
                payload["persistence_available"] = True
            return payload
        payload = accounts.snapshot()
        collection = getattr(runner, "collection", None)
        if collection is not None:
            payload["collection"] = collection.snapshot()
        return payload

    @app.post("/api/account/collect")
    def collect_stats() -> dict[str, Any]:
        """Arm the read-only Home -> Settings -> Stats -> Home transaction.

        Arms it only - the scan loop is what walks it, one verified step per
        frame - and every refusal comes from the runner rather than being
        re-derived here.
        """
        request = getattr(runner, "request_stats_collection", None)
        if request is None:
            raise HTTPException(412, "This backend cannot run device transactions")
        try:
            return request()
        except RunnerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    @app.get("/api/missions")
    def missions_snapshot() -> dict[str, Any]:
        """The last Daily Missions reading, and whether one is on screen now.

        Absent, not null, on a backend with no runner - the same distinction
        /api/account makes: a browser has to tell "this backend cannot read
        missions" apart from "nothing has been read yet".
        """
        missions = getattr(runner, "missions", None)
        visit = getattr(runner, "visit", None)
        claim = getattr(runner, "claim", None)
        payload: dict[str, Any] = {} if missions is None else missions.snapshot()
        if visit is not None:
            payload["visit"] = visit.snapshot()
        if claim is not None:
            payload["claim"] = claim.snapshot()
        return payload

    @app.post("/api/missions/visit")
    def visit_missions() -> dict[str, Any]:
        """Arm the read-only Home -> Missions -> read -> Home visit.

        Arms it only - the scan loop is what walks it, one verified step per
        frame - and every refusal comes from the runner rather than being
        re-derived here.
        """
        request = getattr(runner, "request_missions_visit", None)
        if request is None:
            raise HTTPException(412, "This backend cannot run device transactions")
        try:
            return request()
        except RunnerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    @app.post("/api/missions/claim")
    def claim_missions() -> dict[str, Any]:
        """Arm one Home -> Missions -> claim -> Home walk.

        Arms it only - the scan loop is what walks it, one verified step per
        frame - and every refusal comes from the runner rather than being
        re-derived here. getattr rather than an `if runner is not None`
        block, so a backend without a runner answers 412 ("this backend
        cannot") instead of 404 ("no such thing").
        """
        request = getattr(runner, "request_missions_claim", None)
        if request is None:
            raise HTTPException(412, "This backend cannot run device transactions")
        try:
            return request()
        except RunnerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    @app.get("/api/milestones")
    def milestones_snapshot() -> dict[str, Any]:
        """The last MILESTONES ladder reading, and whether a claim is walking.

        Absent, not null, on a backend with no runner - the same distinction
        /api/account and /api/missions make: a browser has to tell "this
        backend cannot read the ladder" apart from "nothing has been read yet".
        """
        milestones = getattr(runner, "milestones", None)
        claim = getattr(runner, "milestones_claim", None)
        payload: dict[str, Any] = {} if milestones is None else milestones.snapshot()
        if claim is not None:
            payload["claim"] = claim.snapshot()
        return payload

    @app.post("/api/milestones/claim")
    def claim_milestones() -> dict[str, Any]:
        """Arm one Home -> Milestones -> Claim All -> Home walk.

        Arms it only - the scan loop is what walks it, one verified step per
        frame - and every refusal comes from the runner rather than being
        re-derived here.
        """
        request = getattr(runner, "request_milestones_claim", None)
        if request is None:
            raise HTTPException(412, "This backend cannot run device transactions")
        try:
            return request()
        except RunnerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    autopilot_state = runner.autopilot_state if runner is not None else AutopilotState()
    advisor_store = advisor if advisor is not None else AdvisorStore(
        store.directory.parent / "advisor.json" if store is not None else None
    )
    app.include_router(advisor_router(advisor_store, store, autopilot_state))

    @app.middleware("http")
    async def require_compatible_browser(request: Request, call_next: Callable) -> Response:
        if request.url.path.startswith("/api/") and request.method in {
            "POST", "PUT", "PATCH", "DELETE",
        }:
            emergency = request.url.path == "/api/bot/stop"
            if request.url.path == "/api/control" and request.method == "PATCH":
                try:
                    emergency_body = json.loads(await request.body())
                    emergency = (
                        isinstance(emergency_body, dict)
                        and set(emergency_body) == {"paused"}
                        and emergency_body["paused"] is True
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    emergency = False
            if not emergency:
                backend_header = request.headers.get("x-tower-backend-hash")
                api_header = request.headers.get("x-tower-api-version")
                ui_header = request.headers.get("x-tower-ui-hash")
                frontend = await asyncio.to_thread(
                    read_frontend_identity, STATIC_DIR / ".build-manifest.json"
                )
                mismatch = (
                    (backend_header is not None and backend_header != PROCESS_IDENTITY.source_hash)
                    or (api_header is not None and api_header != str(API_VERSION))
                    or (ui_header is not None and ui_header != frontend.source_hash)
                )
                if mismatch:
                    return JSONResponse(
                        status_code=409,
                        content={"detail": "Dashboard build does not match this runtime; reload before retrying."},
                    )
        return await call_next(request)

    @app.get("/api/upgrades")
    async def upgrade_catalog() -> list[dict[str, Any]]:
        return upgrades.catalog_payload()

    @app.get("/api/concepts")
    async def concept_catalog() -> dict[str, Any]:
        return REGISTRY.payload()

    @app.get("/api/autopilot/presets")
    async def autopilot_presets() -> list[dict[str, Any]]:
        return [{"name": name, "rules": [r.to_dict() for r in preset_rules(name)]} for name in PRESETS]

    def _comparison() -> dict[str, Any]:
        if db_path is None:
            return compare_tiers([])
        with db.reader(db_path) as conn:
            return compare_tiers(db.list_runs(conn, limit=100))

    def _can_control() -> bool:
        return bool(runner is not None and runner.status()["running"] and controls is not None
                    and not controls.snapshot().paused and state.snapshot()["screen"] == "IN_RUN")

    @app.get("/api/autopilot")
    async def autopilot_status() -> dict[str, Any]:
        return {**autopilot_state.snapshot(), "can_control": _can_control(),
                "tier_comparison": await asyncio.to_thread(_comparison)}

    @app.post("/api/autopilot/command")
    async def autopilot_command(body: AutopilotCommand) -> dict[str, bool]:
        if not _can_control():
            raise HTTPException(409, "Manual controls require a running, unpaused battle")
        command = body.model_dump(exclude_none=True)
        if body.action == "category" and body.category is None:
            raise HTTPException(422, "Choose a category")
        if body.action == "buy":
            entry = upgrades.by_id(body.upgrade_id or "")
            if entry is None or entry.unlock:
                raise HTTPException(422, "Choose a standard battle upgrade")
            rows = autopilot_state.snapshot()["observations"]
            row = next((r for r in rows if r["context"] == "battle" and r["upgrade_id"] == entry.id), None)
            if row is None or row["status"] != "available" or time.time() - row["observed_at"] > 15:
                raise HTTPException(409, "Scan this upgrade before buying; a fresh observation is required")
        try:
            runner.request_autopilot(command)
        except RunnerError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        return {"queued": True}

    @app.get("/api/status")
    def status(request: Request) -> dict:
        choice = _selected(request)
        if choice is not None and not _running_account(choice):
            raise HTTPException(409, "selected_account_not_running")
        payload = state.snapshot()
        # Dropped events are the bus's business, not the state's: they were
        # never delivered to a sink, so no accumulator ever saw them.
        payload["dropped"] = bus.dropped
        # Overlay geometry rides along with the status poll rather than getting
        # its own endpoint: it changes exactly as often as the status does, and
        # a second poll would buy nothing.
        payload["boxes"] = frames.boxes() if frames is not None else []
        payload["frame_size"] = None
        if frames is not None:
            size = frames.size()
            if size is not None:
                payload["frame_size"] = {"width": size[0], "height": size[1]}
        # Always present, even with no runner (--once, --tui, and every test
        # predating the lifecycle split), so the browser needs no special case
        # for "this build cannot start a bot".
        payload["bot"] = (
            runner.status()
            if runner is not None
            else {"running": False, "since": None, "error": None}
        )
        runtime_capabilities: list[str] = []
        if controls is not None:
            runtime_capabilities.append("control")
        if runner is not None:
            runtime_capabilities.append("lifecycle")
        if store is not None:
            runtime_capabilities.append("strategies")
        if runner is not None:
            runtime_capabilities.append("autopilot")
        runtime_capabilities.append("advisor")
        if fleet is not None:
            runtime_capabilities.append("fleet")

        control_snapshot = controls.snapshot() if controls is not None else None
        strategy = control_snapshot.strategy if control_snapshot is not None else None
        running = payload["bot"]["running"]
        paused = control_snapshot.paused if control_snapshot is not None else False
        screen = payload["screen"]
        account_screen = accounts.screen_readings.snapshot()
        if not running:
            readiness = {"mode": "stopped", "reasons": ["bot is stopped"]}
        elif paused:
            readiness = {"mode": "paused", "reasons": ["automation is paused"]}
        elif controls is None:
            readiness = {
                "mode": "observing",
                "reasons": ["no automation controls are loaded"],
            }
        elif account_screen["current_screen_id"] is not None or account_screen["error"]:
            readiness = {
                "mode": "observing",
                "reasons": ["account screen observation or OCR error holds all actions"],
            }
        elif (
            shopping is not None
            and getattr(shopping, "active", False) is True
            and strategy.shopping.enabled
        ):
            readiness = {"mode": "automation_enabled", "reasons": []}
        elif screen == "IN_RUN" and not (
            any(action.enabled for action in strategy.actions)
            or strategy.autopilot.enabled
        ):
            readiness = {
                "mode": "observing",
                "reasons": ["all automation policies are disabled for IN_RUN"],
            }
        elif screen == "MAIN_MENU" and (
            strategy.auto_navigate or strategy.shopping.enabled
        ):
            readiness = {"mode": "automation_enabled", "reasons": []}
        elif screen in config.NAV_BUTTONS and strategy.auto_navigate:
            readiness = {"mode": "automation_enabled", "reasons": []}
        elif screen != "IN_RUN":
            readiness = {
                "mode": "observing",
                "reasons": [
                    "waiting for an in-run screen observation"
                    if screen == "UNKNOWN"
                    else f"automation policies are disabled for {screen}"
                ],
            }
        else:
            readiness = {"mode": "automation_enabled", "reasons": []}

        if store is not None and controls is not None:
            active = store.active_name()
            live = strategy.name
            if active != live:
                readiness["reasons"].append(
                    f"live profile {live!r} differs from active profile {active!r}"
                )
        identity = (
            runner.identity()
            if runner is not None and hasattr(runner, "identity")
            else {"serial": None, "game_version": None}
        )
        payload["runtime"] = {
            "api_version": API_VERSION,
            "backend": PROCESS_IDENTITY.payload(),
            "frontend": read_frontend_identity(
                STATIC_DIR / ".build-manifest.json"
            ).payload(),
            "capabilities": runtime_capabilities,
            "profile": strategy.name if strategy is not None else None,
            "device": identity,
            "readiness": readiness,
        }
        return payload

    @app.get("/api/runs")
    def runs(request: Request, limit: int = 50) -> list[dict]:
        # --no-store is a supported mode, not an error: there is no file to
        # read, so an empty history is the honest answer, not a 500.
        path = _history_path(request)
        if path is None:
            return []
        with db.reader(path) as conn:
            return db.list_runs(conn, limit=max(1, min(limit, MAX_RUNS_PER_PAGE)))

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: int, request: Request) -> list[dict]:
        # Same as /api/runs above: --no-store means there is nothing to read.
        path = _history_path(request)
        if path is None:
            return []
        with db.reader(path) as conn:
            return db.run_events(conn, run_id)

    @app.get("/api/runs/{run_id}/purchases")
    def run_purchases(run_id: int, request: Request) -> dict:
        """The in-run upgrades one run bought, with its totals.

        Serves a live run and a finished one alike - the store writes a
        BattlePurchased row as the purchase happens, so the browser polls
        this for the open run and fetches it once for a stored one.
        """
        # Same as the two routes above: --no-store means there is nothing to
        # read, and empty totals are the honest answer, not a 500.
        rows = []
        path = _history_path(request)
        if path is not None:
            with db.reader(path) as conn:
                rows = db.run_purchases(conn, run_id)

        purchases = [row | {"category": _category_of(row["upgrade_id"])} for row in rows]
        # `spent` sums only the prices that were actually read, and
        # `unpriced` counts the rest. A price of None is OCR that could not
        # read the number, not a free upgrade, and folding it in as zero
        # would report a total the run never spent.
        by_category: dict[str, int] = {}
        for purchase in purchases:
            if purchase["category"] is not None:
                by_category[purchase["category"]] = by_category.get(purchase["category"], 0) + 1
        return {
            "purchases": purchases,
            "totals": {
                "count": len(purchases),
                "spent": sum(p["price"] for p in purchases if p["price"] is not None),
                "unpriced": sum(1 for p in purchases if p["price"] is None),
                "by_category": by_category,
            },
        }

    @app.get("/api/events/stream")
    async def stream(request: Request) -> StreamingResponse:
        choice = _live_choice(request)
        return StreamingResponse(
            event_stream(
                sse, resume_point(request), request.is_disconnected,
                shutdown=shutdown,
                allowed=(lambda: _running_account(choice)) if choice is not None else None,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/unknown")
    def unknown(request: Request) -> list[dict]:
        choice = _selected(request)
        if choice is not None and not _running_account(choice):
            return []
        if not unknown_dir.exists():
            return []
        shots = sorted(
            unknown_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        return [
            {"name": p.name, "ts": p.stat().st_mtime, "url": f"/api/unknown/{p.name}"}
            for p in shots
        ]

    @app.get("/api/unknown/{name}")
    def unknown_image(name: str, request: Request) -> FileResponse:
        choice = _selected(request)
        if choice is not None and not _running_account(choice):
            raise HTTPException(status_code=404, detail="no such snapshot")
        # The name arrives off the URL, percent-decoded. Resolve it and check
        # the parent rather than trusting it: "../../etc/passwd" must 404, not
        # read the disk.
        path = (unknown_dir / name).resolve()
        if path.parent != unknown_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="no such snapshot")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/frame.jpg")
    def frame_still(request: Request) -> Response:
        _live_choice(request)
        # Same distinction frame_mjpeg already makes: no buffer at all (this
        # process was never given one - --once, --tui, or plain logging) is
        # a different fact than a buffer that simply has not been fed a
        # frame yet, and deserves its own message rather than one that
        # implies a frame is merely still on its way.
        if frames is None:
            raise HTTPException(status_code=404, detail="no frame buffer")
        current = frames.latest()
        if current is None:
            raise HTTPException(status_code=404, detail="no frame captured yet")
        return Response(content=current[1], media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/frame")
    async def frame_mjpeg(request: Request) -> StreamingResponse:
        choice = _live_choice(request)
        if frames is None:
            raise HTTPException(status_code=404, detail="no frame buffer")
        return StreamingResponse(
            frame_stream(frames, request.is_disconnected, shutdown=shutdown,
                         allowed=(lambda: _running_account(choice)) if choice is not None else None),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    if controls is not None:

        def _available_checks() -> Mapping[str, Any]:
            # A plain dict, resolved once: checks are per-PROCESS, not
            # per-bot. main() builds them (build_checks_and_controls) before
            # create_app and hands the same dict to the runner, which reuses
            # it for every bot it ever starts - so there is no moment, --idle
            # included, at which this could be empty and later fill in. Only
            # the None default (a caller that wired no checks at all) needs
            # covering.
            return checks or {}

        def _control_payload() -> dict:
            # The browser needs to know which affordability methods actually
            # built (see build_affordability()) so it can grey out one with no
            # atlas rather than let a switch to it silently do nothing. The
            # action list needs no separate advertisement any more: the
            # strategy carries every row, enabled or not.
            payload = controls.payload()
            payload["affordability_available"] = sorted(
                name for name, check in _available_checks().items()
                if check is not None
            )
            # Same reasoning as affordability_available above, for shopping:
            # a machine whose header atlas is incomplete has a ShoppingSession
            # that declines every visit forever (see build_shopping()), and
            # without this the dashboard has no way to say why enabling and
            # arming the feature produces total silence. None when shopping
            # is fully usable, or when this process never wired one at all
            # (--once, --tui, or a test that built create_app without it).
            payload["shopping_disabled_reason"] = getattr(shopping, "disabled_reason", None)
            # Which in-battle speeds a strategy may aim for. Same reasoning as
            # affordability_available: this list grows when a readout template
            # is harvested, and a copy hardcoded in the browser would quietly
            # disagree with the validator the moment it did.
            #
            # TARGET_SPEEDS, not SPEED_VALUES - the dropdown must not offer
            # x0.0, which the bot can read but must never be told to hold.
            payload["speed_values"] = list(config.TARGET_SPEEDS)
            return payload

        @app.get("/api/control")
        def read_control() -> dict:
            return _control_payload()

        @app.patch("/api/control")
        def patch_control(patch: ControlPatch) -> dict:
            # exclude_none means "absent" and "explicitly null" are the same
            # request, so max_runs cannot be cleared through this route. The
            # strategy page clears it by PUTting the whole profile instead.
            requested = patch.model_dump(exclude_none=True)

            # Refuse before applying, not after: build_affordability() falls
            # back to brightness on its own, so accepting this and letting the
            # loop pick would leave the browser showing "digits" while the bot
            # used brightness.
            affordability = requested.get("affordability")
            if affordability is not None and _available_checks().get(affordability) is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"affordability {affordability!r} is unavailable - no glyph "
                        "atlas is built"
                    ),
                )

            # Captured before apply() so a failed save (below) has something
            # to put back - the live object must never survive a failed
            # write, or Controls and the file disagree exactly the way "one
            # source of truth" exists to prevent.
            before = controls.snapshot().strategy

            try:
                # Controls.apply() validates everything a Strategy can know
                # about itself, which does not include whether a template is
                # a real file inside TEMPLATE_DIR - that is disk I/O, and
                # control.py is imported by the scan loop and must stay
                # I/O-free. So the filesystem half runs here, unconditionally
                # - not just when the patch touches "actions": a strategy
                # already resident in Controls is not guaranteed to still
                # pass this (a template file can vanish out from under a
                # running bot), so an interval-only patch needs the same
                # check. Without it a request body chooses which file
                # cv2.imread opens, and an unresolvable template raises out
                # of every single scan pass, forever, instead of once as the
                # 422 below.
                #
                # Yes, this merges twice - once to validate, once inside
                # apply(). merged() is pure and this runs once per request
                # rather than once per scan, so the duplicate costs nothing
                # that matters. And it runs BEFORE apply(), not after: apply()
                # commits to live state, so validating first is what keeps a
                # rejected patch from ever being observable as a live change.
                before.merged(requested).validated()
                changed = controls.apply(requested)
            except ControlError as exc:
                raise HTTPException(status_code=422, detail=f"{exc.field}: {exc}") from exc

            after = controls.snapshot().strategy
            # Gated on the STRATEGY having moved, not on `changed` being
            # non-empty. `paused` is session state that is deliberately never
            # persisted (see control.py's opening paragraph), so a
            # paused-only patch fills `changed` while leaving the file
            # correct as it stands - persisting there would rewrite the
            # profile for nothing and, on a failing save, hand back a 500 for
            # a request that fully succeeded, with the pause applied and no
            # ControlChanged to tell the other tabs.
            if after != before and store is not None:
                # A patch to the running policy is a save: otherwise the file
                # and the loop would disagree until the next explicit save,
                # which is exactly the drift "one source of truth" exists to
                # remove. Inlined rather than hoisted into a helper - the
                # store block below is the only other writer, and it already
                # has store.save(incoming) right there for the same reason.
                try:
                    store.save(after)
                except Exception as exc:
                    # The precheck above only rules out an invalid strategy;
                    # save() can still fail for reasons no precheck can catch
                    # (a full disk, a permissions change, a template deleted
                    # in the gap between validating and writing). apply()
                    # already committed the change to live state, so an
                    # uncaught failure here would leave the bot running on a
                    # policy the disk never agreed to and no ControlChanged
                    # to say so. Put the pre-patch strategy back before
                    # answering, so live state matches what's on disk again.
                    controls.replace(before)
                    # `paused` is deliberately NOT restored, and that is not
                    # an oversight to be tidied up later: it is session state
                    # with nothing on disk to diverge from, so there is no
                    # inconsistency for a rollback to repair. Un-pausing a
                    # bot because an unrelated disk write failed would send
                    # it back to tapping the game against the operator's
                    # explicit instruction - strictly worse than leaving it
                    # paused. It survives, so it is announced here rather
                    # than in the publish below, which this raise skips.
                    if "paused" in changed:
                        bus.publish(
                            events.ControlChanged(
                                changed={"paused": changed["paused"]}, source="web"
                            )
                        )
                    raise HTTPException(
                        status_code=500, detail=f"failed to persist strategy: {exc}"
                    ) from exc
            if changed:
                # Only when something actually moved. A no-op patch is not a
                # state change and must not fill the log with noise.
                bus.publish(events.ControlChanged(changed=changed, source="web"))

            return _control_payload()

        @app.post("/api/control/command")
        def post_command(body: CommandRequest) -> dict:
            """Queue one action for the scan loop's next pass.

            Deliberately separate from PATCH /api/control rather than another
            optional field on it. A patch is idempotent - re-sending it leaves
            the same settings - whereas re-sending a command taps again, and
            folding the two together would make a retried request after a
            dropped response silently double the taps.

            This route never touches the device. It cannot: the web layer has
            no handle on one, which is the boundary control.py exists to keep.
            """
            try:
                controls.request(body.command)
            except ControlError as exc:
                raise HTTPException(status_code=422, detail=f"{exc.field}: {exc}") from exc
            return {"queued": body.command}

    if runner is not None:

        @app.post("/api/bot/start")
        def start_bot() -> dict:
            try:
                return runner.start()
            except RunnerError as exc:
                # The runner already published a BotError for anything worth
                # seeing in the feed; this is just the caller's answer.
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        @app.post("/api/bot/stop")
        def stop_bot() -> dict:
            return runner.stop()

    if store is not None:

        @app.get("/api/strategies")
        def list_strategies() -> dict:
            return {"active": store.active_name(), "names": store.names()}

        @app.get("/api/strategies/{name}")
        def read_strategy(name: str) -> dict:
            try:
                return store.load(name).to_dict()
            except ControlError as exc:
                # A corrupt profile is not a missing one: load() raises
                # "not_found" for absent and the default "invalid" for
                # unparseable JSON, and the reader deserves to be told which.
                raise HTTPException(
                    status_code=_STATUS_FOR_CODE.get(exc.code, 422), detail=str(exc)
                ) from exc

        @app.put("/api/strategies/{name}")
        def write_strategy(name: str, body: dict[str, Any]) -> dict:
            # The URL's name wins. Otherwise a body naming something else
            # writes a different file and the caller has no way to know
            # where their profile went.
            try:
                incoming = Strategy.from_dict({**body, "name": name})
                store.save(incoming)
            except ControlError as exc:
                raise HTTPException(
                    status_code=422, detail=f"{exc.field}: {exc}"
                ) from exc

            # Saving the profile the bot is currently running IS a live edit.
            if controls is not None and name == store.active_name():
                changed = controls.replace(incoming)
                if changed:
                    bus.publish(events.ControlChanged(changed=changed, source="web"))
            return incoming.to_dict()

        @app.post("/api/strategies/{name}/activate")
        def activate_strategy(name: str) -> dict:
            try:
                # validated(), because this is the one path that puts a
                # profile straight from the disk into the running loop.
                # load() only parses; the filesystem half - does every
                # template still exist, inside TEMPLATE_DIR - is what PUT
                # gets for free from store.save() and PATCH does by hand
                # before apply(). Without it, activating a hand-edited
                # profile whose template was since deleted raises out of
                # every scan pass forever instead of once, here, as a 422.
                # Before set_active(), so a profile that cannot run does not
                # become the one the next launch loads either.
                loaded = store.load(name).validated()
                store.set_active(name)
            except ControlError as exc:
                # load() raises "not_found" for an absent profile but the
                # default "invalid" for one that exists and is corrupt JSON
                # - those are different facts, and a present-but-corrupt
                # profile deserves a 422 that says so, not a 404 that sends
                # the caller looking for a file that is sitting right there.
                raise HTTPException(
                    status_code=_STATUS_FOR_CODE.get(exc.code, 422), detail=str(exc)
                ) from exc
            if controls is not None:
                changed = controls.replace(loaded)
                if changed:
                    bus.publish(events.ControlChanged(changed=changed, source="web"))
            return {"active": name, "names": store.names()}

        @app.delete("/api/strategies/{name}")
        def remove_strategy(name: str) -> dict:
            try:
                store.delete(name)
            except ControlError as exc:
                # exc.code, not the message text. Plan 1's final review added
                # a discriminator precisely so this route does not string-match
                # its way to a status: "not_found" when the profile is absent,
                # "conflict" when it exists and the refusal is about what
                # deleting it would leave behind (active, or the last one).
                raise HTTPException(
                    status_code=_STATUS_FOR_CODE.get(exc.code, 422), detail=str(exc)
                ) from exc
            return {"active": store.active_name(), "names": store.names()}

    @app.post("/api/shutdown")
    def shutdown_all() -> dict:
        # This route has no handle on the worker thread or the Server - the
        # shared `shutdown` Event is the only thing it can reach from a
        # request handler. serve_web()'s watcher thread is the one actually
        # waiting on it: it stops the runner and sets server.should_exit,
        # which brings the loop and the server down together.
        #
        # Registered unconditionally, with no `runner is not None` guard: a
        # dashboard that cannot shut itself down is worse than one that can,
        # and ending the process needs no bot to be running at all.
        shutdown.set()
        return {"stopping": True}

    @app.get("/api/stats")
    def stats(request: Request) -> dict:
        # --no-store is a supported mode, not an error: empty aggregates are
        # the honest answer, and the page renders an explicit empty state.
        path = _history_path(request)
        if path is None:
            return {"runs": [], "taps": [], "screens": []}
        with db.reader(path) as conn:
            return {
                "runs": db.run_stats(conn),
                "taps": db.taps_by_action(conn),
                "screens": db.screen_histogram(conn),
            }

    @app.get("/api/errors")
    def errors(request: Request, limit: int = 100) -> list[dict]:
        path = _history_path(request)
        if path is None:
            return []
        with db.reader(path) as conn:
            return db.error_log(conn, limit=max(1, min(limit, 500)))

    # Above the catch-all below, like every other /api route. A route
    # registered after it is unreachable and 404s exactly as if it had never
    # been wired, which is a far more confusing failure than normal
    # shadowing - see that route's comment.
    @app.get("/api/ledger")
    def ledger_lines(
        request: Request,
        limit: int = 50,
        before: int | None = None,
        kind: str | None = None,
        currency: str | None = None,
        include_rehearsals: bool = False,
    ) -> dict:
        # --no-store: there is no file to read, so an empty account history
        # is the honest answer, the same as /api/runs and /api/errors.
        path = _history_path(request)
        if path is None:
            return {
                "lines": [],
                "balances": {"coins": None, "gems": None},
                "rehearsals": 0,
                "next": None,
            }
        capped = max(1, min(limit, MAX_LEDGER_PER_PAGE))
        with db.reader(path) as conn:
            lines = db.ledger_page(
                conn,
                limit=capped,
                before=before,
                kind=kind,
                currency=currency,
                include_rehearsals=include_rehearsals,
            )
            return {
                "lines": lines,
                "balances": db.last_balances(conn),
                "rehearsals": db.count_rehearsals(conn),
                # Only a full page can have more behind it. A short page is
                # the end, and claiming otherwise costs the client a request
                # that returns nothing.
                "next": lines[-1]["id"] if len(lines) == capped else None,
            }

    def _director_plan() -> director.Plan:
        # AccountState exposes no `latest_revision()` - the typed accessor
        # `director.plan` needs lives on `AccountRepository.latest()`, which
        # `accounts.repository` reaches directly; `accounts.snapshot()`
        # returns a plain dict, which is not the same thing and is not
        # substituted for one here. No repository (--no-store, or a fresh
        # AccountState with nothing restored yet) reads as "no revision has
        # ever been observed" - the empty AccountRevision() below - not an
        # error: a dashboard with no bot attached must still show what the
        # bot would do.
        revision = accounts.repository.latest() if accounts.repository is not None else None
        runs: list[dict[str, Any]] = []
        if db_path is not None:
            with db.reader(db_path) as conn:
                runs = db.list_runs(conn, limit=100)
        return director.plan(
            revision or AccountRevision(),
            knowledge=knowledge.KNOWLEDGE,
            graph=objectives.GRAPH,
            rates=progression_rates(runs),
            strategy=controls.snapshot().strategy if controls is not None else Strategy.from_config(),
        )

    @app.get("/api/director")
    async def director_plan() -> dict[str, Any]:
        """The next five objectives, why, and what is holding the rest.

        Read-only and armed-nothing: this phase computes the plan and shows
        it. Acting on it is the risk ladder's job. Blocking DB and revision
        reads run off the event loop via asyncio.to_thread, matching
        _comparison()'s use above for /api/autopilot.
        """
        result = await asyncio.to_thread(_director_plan)
        return director.as_payload(result, knowledge=knowledge.KNOWLEDGE, top_n=5)

    # A write verb against an /api path nothing above registered (e.g.
    # /api/bot/start with no runner wired) must read as "this route does
    # not exist" - 404 - not as the mount's own answer for a method it
    # rejects outright. StaticFiles refuses POST/PUT/PATCH/DELETE before it
    # ever checks whether a matching file exists, which would turn a
    # never-registered route into a misleading 405. GET/HEAD are left to
    # the mount below, which already answers unmatched paths with the SPA's
    # own 404 page.
    #
    # Starlette matches in registration order and this pattern
    # ("/api/{_path:path}") swallows every write-verb request under /api,
    # real or not - so it has to stay the LAST /api route registered. Any
    # new /api route (the strategy CRUD routes are next) MUST be added
    # above this one, not below: a route registered after this catch-all is
    # unreachable and will 404 as if it were never wired at all, which is a
    # much more confusing failure than a normal shadowing bug because it
    # looks identical to "the route was never registered." See
    # test_the_unmatched_api_catch_all_does_not_shadow_real_routes in
    # tests/test_lifecycle_api.py, which pins this ordering.
    @app.get("/api/fleet/setup")
    def fleet_setup_snapshot() -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "setup_snapshot", None)):
            raise HTTPException(status_code=503, detail="fleet_setup_unavailable")
        return fleet.setup_snapshot()

    @app.get("/api/fleet/instances")
    def fleet_instances() -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "instances_snapshot", None)):
            raise HTTPException(status_code=503, detail="fleet_instances_unavailable")
        try:
            return fleet.instances_snapshot()
        except HostCapabilityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/fleet/instances/start")
    def fleet_start_instance(body: FleetStartInstanceRequest) -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "start_instance", None)):
            raise HTTPException(status_code=503, detail="fleet_instances_unavailable")
        try:
            return fleet.start_instance(body.name)
        except (FleetRequestError, HostCapabilityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/fleet/setup")
    def fleet_setup_save(body: FleetSetupRequest) -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "configure", None)):
            raise HTTPException(status_code=503, detail="fleet_setup_unavailable")
        try:
            return fleet.configure(capacity=body.capacity, name_prefix=body.name_prefix,
                                   qualification_id=body.qualification_id)
        except (ValueError, HostCapabilityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/fleet/setup/start-source")
    def fleet_setup_start_source() -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "start_source", None)):
            raise HTTPException(status_code=503, detail="fleet_setup_unavailable")
        try:
            return fleet.start_source()
        except (FleetRequestError, HostCapabilityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/fleet")
    def fleet_snapshot() -> dict[str, Any]:
        if fleet is None:
            return {"sources": [], "jobs": [], "unavailable": "fleet_not_configured"}
        return fleet.snapshot()

    @app.get("/api/fleet/preview")
    def fleet_preview(mode: Literal["fresh", "clone"], count: int,
                      source: str | None = None) -> dict[str, Any]:
        if fleet is None:
            raise HTTPException(status_code=503, detail="fleet_not_configured")
        try:
            return fleet.preview(mode, source, count)
        except FleetRequestError as exc:
            return {"mode": mode, "source": source, "count": count,
                    "targets": [], "state": "blocked", "reason": str(exc)}

    @app.get("/api/fleet/qualification")
    def fleet_qualification_evidence() -> dict[str, Any]:
        if fleet is None:
            raise HTTPException(status_code=503, detail="fleet_not_configured")
        try:
            return fleet.qualification_evidence()
        except FleetRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/fleet/requests/{job_id}")
    def fleet_request_evidence(job_id: str) -> dict[str, Any]:
        if fleet is None:
            raise HTTPException(status_code=503, detail="fleet_not_configured")
        try:
            return fleet.request_evidence(job_id)
        except FleetRequestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/fleet/requests", status_code=202)
    def fleet_provision_request(body: FleetProvisionRequest,
                                background: BackgroundTasks) -> dict[str, Any]:
        if fleet is None:
            raise HTTPException(status_code=503, detail="fleet_not_configured")
        try:
            job = fleet.request(body.source, body.count, mode=body.mode,
                                expected_targets=body.targets)
        except FleetRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background.add_task(fleet.run_pending, job["id"])
        return job

    @app.post("/api/fleet/requests/{job_id}/targets/{index}/resume-first-launch")
    def fleet_resume_first_launch(job_id: str, index: int,
                                  background: BackgroundTasks) -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "resume_unopened_clone", None)):
            raise HTTPException(status_code=503, detail="fleet_setup_unavailable")
        background.add_task(fleet.resume_unopened_clone, job_id, index)
        return {"state": "resuming", "job_id": job_id, "index": index}

    @app.post("/api/fleet/requests/{job_id}/targets/{index}/{action}")
    def fleet_resolve_target(job_id: str, index: int, action: Literal["retry", "quarantine"],
                             background: BackgroundTasks) -> dict[str, Any]:
        if fleet is None:
            raise HTTPException(status_code=503, detail="fleet_not_configured")
        try:
            job = fleet.resolve(job_id, index, action)
        except FleetRequestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if action == "retry":
            background.add_task(fleet.run_pending, job_id)
        return job

    @app.api_route("/api/{_path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    def unmatched_api_route(_path: str) -> None:
        raise HTTPException(status_code=404, detail="no such route")

    # Last, deliberately. Starlette matches routes in registration order and a
    # mount at "/" matches everything, so every /api route above must already
    # be registered or the mount would swallow the whole API.
    #
    # html=True resolves "/runs/" to "runs/index.html", which is the layout
    # next.config.ts's trailingSlash:true produces.
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return (
                "<h1>Dashboard not built</h1>"
                "<p>Run <code>./run.sh</code> from the repository root to build and start it.</p>"
            )

    return app
