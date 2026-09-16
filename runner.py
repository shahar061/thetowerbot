"""Owns the bot's lifetime, so the dashboard does not have to share it.

Until now the scan loop and the web server lived and died together: one
`stop` Event brought both down, which is what kept Ctrl+C, --max-runs and
the browser's Stop button on a single shutdown path. That coupling also
meant a browser could never START a bot, because by the time anything was
serving, the bot was already running.

This is the thing that breaks the tie. It owns the device connection, the
TowerBot and the worker thread; the server owns this. A bot can now end -
by Stop, or by reaching its run cap - without the server ending, and a new
one can be started in its place.

Everything here is guarded by one lock, so two concurrent starts cannot both
spawn a thread. Imports the bot, never the web layer - `frames` and
`sinks.state` are shared plumbing the scan loop writes to, not part of the
dashboard, so naming their types below costs nothing the bot did not already
pay (device.py pulls cv2 in regardless).
"""

from __future__ import annotations

from account_collection import StatsCollection
from milestones_claim import MilestonesClaim
from milestones_screen import MilestonesReadings
from missions_claim import MissionsClaim
from missions_screen import MissionsReadings
from missions_visit import MissionsVisit
from account_state import AccountState

import logging
from dataclasses import asdict
import math
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import events
import vision
from autopilot import AutopilotState
from control import Controls
from device import EmulatorError, IdentityError
from bluestacks import BlueStacksAdapter, HostBoundConnect
from fleet.identity import Attempt, IdentityEvidence
from frames import FrameBuffer
from sinks.state import BotState
from supervisor import DeviceSupervisor, GuardedDevice, RecoveryState

logger = logging.getLogger(__name__)


class RunnerError(Exception):
    """A lifecycle request that could not be honoured.

    Carries the HTTP status the route should return, so the web layer maps
    one exception rather than distinguishing several.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _default_bot_factory(**kwargs: Any) -> Any:
    """Imported lazily: tower_bot imports control, and importing it at module
    scope here would drag the whole bot into any process that only wanted the
    runner's types."""
    from tower_bot import TowerBot

    return TowerBot(**kwargs)


class BotRunner:
    """Start, stop and report on one bot at a time."""

    def __init__(
        self,
        *,
        bus: events.EventBus,
        controls: Controls,
        state: BotState,
        templates: vision.TemplateCache,
        device_factory: Callable[[], Any],
        checks: dict[str, Any],
        frames: FrameBuffer | None = None,
        first_run_id: int = 1,
        best_wave: int | None = None,
        bot_factory: Callable[..., Any] = _default_bot_factory,
        shopping: Any | None = None,
        account_state: AccountState | None = None,
        unknown_dir: Path | None = None,
        attempt: Attempt | None = None,
        binding_path: Path | None = None,
        supervisor_path: Path | None = None,
        game_package: str | None = None,
        host_adapter: BlueStacksAdapter | None = None,
        host_instance: str | None = None,
        host_popup_checker: Callable[[str], str] | None = None,
    ) -> None:
        self._bus = bus
        self._controls = controls
        self._state = state
        self._templates = templates
        self._device_factory = device_factory
        self._unknown_dir = unknown_dir
        self._attempt = attempt
        self._binding_path = binding_path
        self._supervisor_path = supervisor_path
        self._game_package = game_package
        if (host_adapter is None) != (host_instance is None) or (
            host_adapter is not None and (attempt is None or supervisor_path is None)
        ):
            raise ValueError("named host requires a supervised worker attempt")
        self._host_adapter = host_adapter
        self._host_instance = host_instance
        self._host_popup_checker = host_popup_checker
        self._supervisor: DeviceSupervisor | None = None
        self._attempt_started = False
        self._checks = checks
        self._frames = frames
        self._bot_factory = bot_factory
        # Built once per process by tower_bot.build_shopping(), the same way
        # `checks` is - see that function's docstring. Reused for every bot
        # this runner ever starts rather than rebuilt per Start, because
        # rebuilding the header glyph atlas on every Start would make the
        # button slow for no gain.
        self._shopping = shopping
        self.autopilot_state = AutopilotState()
        self.account_state = account_state or AccountState()
        # One transaction object for the runner's whole lifetime, handed to
        # every bot it starts - the same reason account_state is shared. The
        # browser arms it here; the scan loop is what walks it.
        self.collection = StatsCollection()
        # The Missions visit and the reader it takes its arrival evidence
        # from, owned here for the same reason and on the same terms.
        self.visit = MissionsVisit()
        # The claim walk, owned on the same terms as the read-only visit: one
        # instance, cancelled by a restart or a pause, never queued.
        self.claim = MissionsClaim()
        self.missions = MissionsReadings()
        # The MILESTONES claim walk and its reader, owned on the same terms:
        # one instance for the runner's lifetime, cancelled by a restart, a
        # stop or a pause, never queued.
        self.milestones_claim = MilestonesClaim()
        self.milestones = MilestonesReadings()

        self._lock = threading.Lock()
        self._bot: Any | None = None
        self._thread: threading.Thread | None = None
        self._since: float | None = None
        self._error: str | None = None
        self._device_serial: str | None = None
        # Seeded from the database once, at launch. Carried forward from each
        # bot's own tracker after that - see _harvest_locked().
        self._next_run_id = first_run_id
        # The same seed-then-carry-forward arrangement as _next_run_id, for
        # the same reason: without it, a bot started fresh through the
        # dashboard would begin with best_wave=None regardless of what the
        # database holds, and a milestones claim already owed would sit
        # unoffered until that bot's own first RunEnded. Carried forward
        # (never re-seeded from the DB) so a restart mid-session does not
        # regress what an earlier bot in this same runner already learned -
        # see _harvest_locked().
        self._best_wave: int | None = best_wave

    # -- reporting ---------------------------------------------------------
    def _running_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._running_locked()
            status = {
                "running": running,
                "since": self._since if running else None,
                "error": self._error,
            }
            if self._supervisor is not None:
                status["recovery"] = asdict(self._supervisor.status())
            return status

    def identity(self) -> dict[str, str | None]:
        """Identity already learned during start; this never performs ADB I/O."""
        with self._lock:
            return {"serial": self._device_serial, "game_version": None}

    def record_identity_evidence(self, evidence: IdentityEvidence) -> None:
        """Commit an account binding only after a connected attempt is observed."""
        with self._lock:
            if not self._running_locked() or self._attempt is None or self._binding_path is None:
                raise RunnerError("No running attempt can accept identity evidence", 409)
            if self._supervisor is not None:
                self._supervisor.verify_account(evidence.account_id,
                                                observed_at=evidence.observed_at)
            try:
                if self._binding_path.exists():
                    import json

                    saved = json.loads(self._binding_path.read_text(encoding="utf-8"))
                    if saved.get("account_id") != evidence.account_id:
                        raise RunnerError("identity incident: binding mismatch", 409)
                else:
                    self._attempt.persist(self._binding_path, evidence)
            except Exception:
                if self._supervisor is not None:
                    self._supervisor.invalidate_identity("identity_persist_failed")
                raise

    def _verified_account(self) -> str | None:
        """Retain only a consistent account bound to this worker attempt."""
        if self._binding_path is None or self._attempt is None:
            return None
        import json

        staging_journal = self._binding_path.parent / ".r00-account-creation.json"
        host_adapter = getattr(self, "_host_adapter", None)
        host_instance = getattr(self, "_host_instance", None)
        if host_adapter is not None and host_instance is not None:
            try:
                designated = host_adapter.designated(host_instance, self._attempt)
            except IdentityError:
                # HostBoundConnect will pass the mismatch to DeviceSupervisor,
                # which durably quarantines this exact worker before a frame.
                return None
            if designated.source_lineage is not None and not staging_journal.exists():
                raise RunnerError("identity incident: R00 staging account unverified", 503)

        accounts: set[str] = set()
        for path in self._binding_path.parent.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                continue
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RunnerError("identity incident: unreadable account binding", 503) from None
            if all(row.get(key) == getattr(self._attempt, key)
                   for key in ("worker_id", "endpoint", "lease_id", "attempt_id")):
                account = row.get("account_id")
                created = row.get("created_at")
                observed = row.get("observed_at")
                reference = row.get("evidence_ref")
                if (not isinstance(account, str) or not account.strip()
                        or not isinstance(reference, str) or not reference.strip()
                        or not isinstance(row.get("generation"), str)
                        or not isinstance(created, (int, float)) or not math.isfinite(created)
                        or not isinstance(observed, (int, float)) or not math.isfinite(observed)
                        or created <= 0 or observed < created):
                    raise RunnerError("identity incident: incomplete account binding", 503)
                accounts.add(account)
        if len(accounts) > 1:
            raise RunnerError("identity incident: conflicting account bindings", 503)
        account = next(iter(accounts), None)
        if staging_journal.exists():
            try:
                row = json.loads(staging_journal.read_text(encoding="utf-8"))
                if (row.get("state") != "verified" or row.get("account_id") != account
                        or any(row.get(key) != getattr(self._attempt, key)
                               for key in ("worker_id", "endpoint", "lease_id", "attempt_id"))
                        or not row.get("source_lineage") or not row.get("source_account_id")
                        or not row.get("app_version")
                        or [item.get("screen") for item in row.get("evidence", [])] not in (
                            ["home", "settings", "account", "new_account_warning",
                             "home", "settings", "account"],
                            ["home", "settings", "account", "new_account_warning",
                             "game_over", "home", "settings", "account"],
                            ["account", "new_account_warning", "home", "settings", "account"],
                            ["account", "new_account_warning", "game_over", "home",
                             "settings", "account"],
                        )):
                    raise ValueError("unverified staging journal")
            except (OSError, ValueError, TypeError, AttributeError):
                raise RunnerError("identity incident: R00 staging account unverified", 503) from None
        return account

    def request_autopilot(self, command: dict[str, Any]) -> None:
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before sending commands", 409)
            if self._controls.snapshot().paused or self._bot.screen_state.value != "IN_RUN":
                raise RunnerError("Manual upgrades require an unpaused battle", 409)
            try:
                self._bot.autopilot.submit(command)
            except ValueError as exc:
                raise RunnerError(str(exc), 409) from None

    def request_stats_collection(self) -> dict[str, Any]:
        """Arm one read-only Home -> Settings -> Stats -> Home transaction.

        Every refusal here is about evidence the caller cannot supply
        later: a stopped or paused loop would never walk the steps, a
        battle is the wrong screen to leave, and a reader that could not
        load cannot identify the panels the steps must verify. None of
        them queues - a command the loop cannot honour now is refused, not
        banked, exactly as request_autopilot refuses.
        """
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before collecting stats", 409)
            unavailable = getattr(self._shopping, "disabled_reason", None)
            if unavailable:
                raise RunnerError(f"Stats collection is unavailable: {unavailable}", 503)
            if self._controls.snapshot().paused:
                raise RunnerError("Collecting stats requires an unpaused bot", 409)
            if self._bot.screen_state.value != "MAIN_MENU":
                raise RunnerError("Collecting stats requires a confirmed main menu", 409)
            if self.visit.active:
                raise RunnerError("A missions visit is already walking the menus", 409)
            if self.claim.active:
                raise RunnerError("A missions claim is already walking the menus", 409)
            if self.milestones_claim.active:
                raise RunnerError("A milestones claim is already walking the menus", 409)
            if not self.collection.request():
                raise RunnerError("A stats collection is already running", 409)
            return self.collection.snapshot()

    def request_missions_visit(self) -> dict[str, Any]:
        """Arm one read-only Home -> Missions -> read -> Home visit.

        The same refusals as request_stats_collection, for the same reason:
        a command the loop cannot honour NOW is refused rather than banked.
        Two transactions may never be armed at once - they would walk the
        same menus from different steps, and the second one's evidence would
        be the first one's screen.
        """
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before visiting missions", 409)
            unavailable = getattr(self._shopping, "disabled_reason", None)
            if unavailable:
                raise RunnerError(f"Missions visits are unavailable: {unavailable}", 503)
            if self._controls.snapshot().paused:
                raise RunnerError("Visiting missions requires an unpaused bot", 409)
            if self._bot.screen_state.value != "MAIN_MENU":
                raise RunnerError("Visiting missions requires a confirmed main menu", 409)
            if self.collection.active:
                raise RunnerError("A stats collection is already walking the menus", 409)
            if self.claim.active:
                raise RunnerError("A missions claim is already walking the menus", 409)
            if self.milestones_claim.active:
                raise RunnerError("A milestones claim is already walking the menus", 409)
            if not self.visit.request():
                raise RunnerError("A missions visit is already running", 409)
            return self.visit.snapshot()

    def request_missions_claim(self) -> dict[str, Any]:
        """Arm one Home -> Missions -> claim -> Home walk.

        The same refusals as request_missions_visit, and one more object to
        refuse against: three transactions now walk the same menus, and any
        two of them armed at once would each read the other's screen as its
        own evidence.

        Unlike its two siblings this walk TAPS things that change the
        account, so the refusals are load-bearing rather than tidy.
        """
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before claiming missions", 409)
            unavailable = getattr(self._shopping, "disabled_reason", None)
            if unavailable:
                raise RunnerError(f"Mission claims are unavailable: {unavailable}", 503)
            if self._controls.snapshot().paused:
                raise RunnerError("Claiming missions requires an unpaused bot", 409)
            if self._bot.screen_state.value != "MAIN_MENU":
                raise RunnerError("Claiming missions requires a confirmed main menu", 409)
            if self.collection.active:
                raise RunnerError("A stats collection is already walking the menus", 409)
            if self.visit.active:
                raise RunnerError("A missions visit is already walking the menus", 409)
            if self.milestones_claim.active:
                raise RunnerError("A milestones claim is already walking the menus", 409)
            if not self.claim.request():
                raise RunnerError("A missions claim is already running", 409)
            return self.claim.snapshot()

    def request_milestones_claim(self) -> dict[str, Any]:
        """Arm one Home -> Milestones -> Claim All -> Home walk.

        The same refusals as request_missions_claim and one more object to
        refuse against: FOUR transactions now walk the same menus, and any two
        of them armed at once would each read the other's screen as its own
        evidence.

        Like the missions claim and unlike the two read-only walks, this one
        TAPS things that change the account, so the refusals are load-bearing
        rather than tidy.
        """
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before claiming milestones", 409)
            unavailable = getattr(self._shopping, "disabled_reason", None)
            if unavailable:
                raise RunnerError(f"Milestone claims are unavailable: {unavailable}", 503)
            if self._controls.snapshot().paused:
                raise RunnerError("Claiming milestones requires an unpaused bot", 409)
            if self._bot.screen_state.value != "MAIN_MENU":
                raise RunnerError("Claiming milestones requires a confirmed main menu", 409)
            if self.collection.active:
                raise RunnerError("A stats collection is already walking the menus", 409)
            if self.visit.active:
                raise RunnerError("A missions visit is already walking the menus", 409)
            if self.claim.active:
                raise RunnerError("A missions claim is already walking the menus", 409)
            if not self.milestones_claim.request():
                raise RunnerError("A milestones claim is already running", 409)
            return self.milestones_claim.snapshot()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> dict[str, Any]:
        """Connect, build a bot, spawn the worker. Raises RunnerError.

        The device connection happens HERE rather than at launch, which is
        the whole difference: a dead emulator becomes a 503 and a BotError on
        the feed, not exit 1 from a process that never served anything.
        """
        with self._lock:
            if self._running_locked():
                raise RunnerError("the bot is already running", 409)
            # Reap a finished thread before reusing the slot, so a bot that
            # ended on its own does not block the next start.
            self._reap_locked()

            if self._attempt is not None and self._attempt_started:
                previous = self._attempt
                self._attempt = Attempt.new(
                    previous.worker_id, previous.endpoint,
                    previous.lease_id, previous.attempt_id,
                )
                if self._binding_path is not None:
                    self._binding_path = (
                        self._binding_path.parent / f"{self._attempt.generation}.json"
                    )

            try:
                if self._supervisor_path is not None and self._attempt is not None:
                    connect = self._device_factory
                    if self._host_adapter is not None and self._host_instance is not None:
                        def close_host_upgrade() -> None:
                            if self._host_popup_checker is None:
                                return
                            result = self._host_popup_checker(self._host_instance)
                            if result not in {"absent", "closed"}:
                                logger.warning("BlueStacks upgrade dialog close %s for %s",
                                               result, self._host_instance)

                        connect = HostBoundConnect(
                            self._host_adapter, self._host_instance, self._attempt,
                            self._device_factory,
                            before_connect=(close_host_upgrade if self._host_popup_checker
                                            is not None else None),
                        )
                    self._supervisor = DeviceSupervisor(
                        path=self._supervisor_path, endpoint=self._attempt.endpoint,
                        connect=connect,
                        expected_account=self._verified_account(),
                        game_package=self._game_package,
                        quarantine_on_exhaustion=self._host_adapter is not None,
                    )
                    self._supervisor.recover()
                    if self._supervisor.device is None:
                        failure = self._supervisor.status()
                        if failure.state is RecoveryState.QUARANTINED:
                            raise IdentityError(f"identity incident: {failure.reason}")
                        raise EmulatorError(failure.reason)
                    device = GuardedDevice(self._supervisor)
                else:
                    device = self._device_factory()
            except RunnerError as exc:
                self._error = str(exc)
                self._bus.publish(events.IdentityIncident(message=str(exc)))
                self._bus.publish(events.BotError(message=str(exc)))
                raise
            except EmulatorError as exc:
                self._error = str(exc)
                # Published outside the lock would be tidier, but publish()
                # never blocks (see events.EventBus) so holding it is safe.
                if isinstance(exc, IdentityError):
                    self._bus.publish(events.IdentityIncident(message=str(exc)))
                self._bus.publish(events.BotError(message=str(exc)))
                raise RunnerError(str(exc), 503) from None
            except Exception as exc:  # noqa: BLE001 - anything else is still fatal to a start
                self._error = str(exc)
                self._bus.publish(
                    events.BotError(message=str(exc), traceback=traceback.format_exc())
                )
                raise RunnerError(str(exc), 503) from None

            self._device_serial = getattr(device, "serial", None)
            if self._attempt is not None:
                endpoint = self._attempt.endpoint
                aliases = {endpoint}
                host, _, port_text = endpoint.rpartition(":")
                if host in {"127.0.0.1", "localhost", "::1"} and port_text.isdigit():
                    port = int(port_text)
                    if port % 2 == 1:
                        aliases.add(f"emulator-{port - 1}")
                if self._device_serial not in aliases:
                    message = f"identity incident: connected transport does not match {endpoint}"
                    self._bus.publish(events.IdentityIncident(message=message))
                    self._error = message
                    self._device_serial = None
                    raise RunnerError(message, 503)

            # A fresh bot's counters start at zero; the state sink's must too,
            # or the status bar mixes this bot's uptime with the last one's
            # scan count. The BUS is deliberately not reset - seq keeps
            # climbing so a reconnecting browser resumes across the restart.
            #
            # Known and accepted: this reset is not synchronised with
            # StateSink's consumer queue. A fast Stop-then-Start can reset
            # the state while the previous bot's last few events are still
            # queued, and those then land on the NEW bot's counters and
            # inflate them for as long as it runs (nothing recomputes them).
            # Left alone deliberately - the damage is display-only, it needs
            # a restart inside one queue drain to happen at all, and draining
            # the sink here would put a cross-thread handshake on the Start
            # path to tidy up a scan count.
            self._state.reset()
            self.account_state.reset_confirmation()
            self.account_state.screen_readings.reset_current()
            # A transaction half-walked by the previous bot must not resume
            # under a new one: its remembered step assumes a panel the
            # emulator may no longer be showing. Cancelling keeps the failure
            # visible instead of silently rearming.
            self.collection.cancel(
                "bot_restarted",
                "A new bot replaced the one walking this transaction; it was not resumed.",
            )
            self.visit.cancel(
                "bot_restarted",
                "A new bot replaced the one walking this visit; it was not resumed.",
            )
            self.claim.cancel(
                "bot_restarted",
                "A new bot replaced the one walking this claim; it was not resumed.",
            )
            self.milestones_claim.cancel(
                "bot_restarted",
                "A new bot replaced the one walking this claim; it was not resumed.",
            )
            self.autopilot_state.clear_battle()
            self.autopilot_state.decision("idle", "Waiting for a fresh battle observation")

            # A visit left mid-errand by the previous bot (Stop pressed
            # mid-visit, or run_forever ending because max_runs was lowered
            # from the dashboard while shopping was under way) must not
            # resume against stale state under a NEW bot: `_step` would
            # still be non-IDLE, so the new bot's very first run_once() would
            # call advance() directly - skipping begin() entirely, and with
            # it the enabled check, the cadence, the run cap and the
            # MAIN_MENU precondition - while the emulator may not even be on
            # the page that stale `_categories` list assumes. reset() forces
            # IDLE with no return tap of its own; if the emulator is still
            # sitting on a menu page, the new bot's own begin()/advance()
            # cycle deals with that from a clean slate exactly as it would
            # after any other restart.
            if self._shopping is not None:
                self._shopping.reset()

            strategy = self._controls.snapshot().strategy
            bot = self._bot_factory(
                device=device,
                templates=self._templates,
                bus=self._bus,
                controls=self._controls,
                checks=self._checks,
                shopping=self._shopping,
                frames=self._frames,
                first_run_id=self._next_run_id,
                best_wave=self._best_wave,
                screen_confirmations=strategy.screen_confirmations,
                navigation_cooldown=strategy.navigation_cooldown,
                autopilot_state=self.autopilot_state,
                account_state=self.account_state,
                collection=self.collection,
                visit=self.visit,
                claim=self.claim,
                missions=self.missions,
                milestones_claim=self.milestones_claim,
                milestones=self.milestones,
                unknown_dir=self._unknown_dir,
                supervisor=self._supervisor,
            )

            self._bot = bot
            self._error = None
            self._since = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(bot,), name="scan-loop", daemon=True
            )
            self._thread.start()
            self._attempt_started = True
            return {
                "running": True,
                "since": self._since,
                "error": None,
            }

    def _run(self, bot: Any) -> None:
        try:
            bot.run_forever()
        except Exception as exc:  # noqa: BLE001 - a crashed loop must not be silent
            logger.exception("the scan loop stopped with an error")
            self._bus.publish(
                events.BotError(message=str(exc), traceback=traceback.format_exc())
            )
            with self._lock:
                self._error = str(exc)
        finally:
            # Whether it ended by Stop, by its run cap, or by raising, the
            # next bot must not reissue this one's run ids.
            with self._lock:
                self.account_state.screen_readings.reset_current()
                self.collection.cancel(
                    "bot_stopped",
                    "The scan loop ended before the transaction finished.",
                )
                self.visit.cancel(
                    "bot_stopped",
                    "The scan loop ended before the visit finished.",
                )
                self.claim.cancel(
                    "bot_stopped",
                    "The scan loop ended before the claim finished.",
                )
                self.milestones_claim.cancel(
                    "bot_stopped",
                    "The scan loop ended before the claim finished.",
                )
                self._harvest_locked(bot)

    def _harvest_locked(self, bot: Any) -> None:
        try:
            self._next_run_id = max(self._next_run_id, bot.runs.next_id)
        except AttributeError:
            pass
        # Max-forward only, same as _next_run_id, and the same None guard
        # TowerBot itself applies to a RunEnded's wave (see
        # tower_bot.py's run_once): a bot that ended with nothing read must
        # not lower or clear what an earlier bot in this runner already knew.
        try:
            harvested = bot._best_wave
        except AttributeError:
            harvested = None
        if harvested is not None and (
            self._best_wave is None or harvested > self._best_wave
        ):
            self._best_wave = harvested

    def _reap_locked(self) -> None:
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._bot = None
            self._since = None

    def stop(self, timeout: float = 7.0) -> dict[str, Any]:
        """End the current bot. Idempotent, and never brings the server down.

        Bounded join: bot.stop() interrupts the between-scan wait immediately
        whatever the interval, so this only ever waits out a scan already in
        flight. It must NOT be sized to the interval itself - the browser can
        set that as high as MAX_INTERVAL, and a Stop must not inherit an hour
        as its deadline.

        Deliberately does NOT reap `_thread`/`_bot` here: `status()` already
        reports "running": False the instant the thread is no longer alive
        (see `_running_locked()`), so nothing depends on clearing them early,
        and a caller inspecting the thread right after stop() - as a test
        proving it is genuinely gone, not merely flagged - should still find
        it. The slot is reclaimed lazily, by the next start()'s own
        `_reap_locked()` call.

        The join is bounded, so the thread is NOT guaranteed to be gone when
        this returns, and the answer is derived from `_running_locked()` for
        exactly that reason rather than hardcoded to False. Reporting
        "stopped" after a join that timed out would contradict the very
        thing the timeout just proved: `status()` would keep saying running
        (same predicate), the next `start()` would refuse with 409, and the
        bot would still be tapping the game while the browser was told it
        had stopped. A honest "running": True says instead that the request
        was made and the loop has not honoured it yet.
        """
        with self._lock:
            bot, thread = self._bot, self._thread
            if bot is None or thread is None:
                self._reap_locked()
                return {"running": False, "since": None, "error": self._error}

        bot.stop()
        thread.join(timeout=timeout)
        with self._lock:
            running = self._running_locked()
            if running:
                # Daemon thread, so it cannot keep the process alive - but a
                # loop that ignored stop() is worth saying out loud.
                logger.warning("the scan loop did not stop within %.1fs", timeout)
            self._harvest_locked(bot)
            return {
                "running": running,
                "since": self._since if running else None,
                "error": self._error,
            }
