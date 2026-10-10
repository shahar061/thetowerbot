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
from card_models import CardCommand, CardOperation, CardPlanContext

import logging
import hashlib
import os
from dataclasses import asdict
import math
import re
import threading
import time
import traceback
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

import events
import vision
from autopilot import AutopilotState
from control import Controls
from device import EmulatorError, IdentityError
from bluestacks import BlueStacksAdapter, HostBoundConnect
from fleet.identity import Attempt, IdentityEvidence
from evidence_scope import FactScope
from fleet.input_lease import InputLease, InputLeaseExpired
from runtime_identity import PROCESS_IDENTITY
from runtime_records import PROCESS_BOOT_ID, RuntimeRecords
from runtime_progress import ProgressRecorder
from frames import FrameBuffer
from sinks.state import BotState
from supervisor import DeviceSupervisor, GuardedDevice, RecoveryBlocked, RecoveryState

logger = logging.getLogger(__name__)

# Seconds a host-bound worker rests after exhausting reconnects before retrying.
HOST_RECOVERY_COOLDOWN = 30.0


class RunnerError(Exception):
    """A lifecycle request that could not be honoured.

    Carries the HTTP status the route should return, so the web layer maps
    one exception rather than distinguishing several.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _openrouter_key() -> str | None:
    """The only recovery key source. Called lazily by the coordinator, and only
    when the effective recovery mode is shadow or assist; never logged."""
    return os.environ.get("CLAUDE_OPENROUTER_API_KEY") or None


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
        tier_best_waves: Mapping[int, int] | None = None,
        bot_factory: Callable[..., Any] = _default_bot_factory,
        shopping: Any | None = None,
        account_state: AccountState | None = None,
        unknown_dir: Path | None = None,
        attempt: Attempt | None = None,
        binding_path: Path | None = None,
        supervisor_path: Path | None = None,
        game_package: str | None = None,
        standalone_expected_account: str | None = None,
        host_adapter: BlueStacksAdapter | None = None,
        host_instance: str | None = None,
        host_popup_checker: Callable[[str], str] | None = None,
        reroll_progress: Any | None = None,
        runtime_records_path: Path | None = None,
        recovery_root: Path | None = None,
        recovery_key_loader: Callable[[], str | None] = _openrouter_key,
        recovery_service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._bus = bus
        # O4 recovery. The coordinator is built per bot on a supervised,
        # attempt-scoped worker; settings default to off (no incident, no
        # transport, no key read). One instance is retained until its
        # shutdown is confirmed and is never hidden by a replacement.
        self._recovery_root = recovery_root
        self._recovery_key_loader = recovery_key_loader
        self._recovery_service_factory = recovery_service_factory
        self._recovery: Any | None = None
        self._controls = controls
        self._state = state
        self._templates = templates
        self._device_factory = device_factory
        self._unknown_dir = unknown_dir
        self._reroll_progress = reroll_progress
        self._runtime_records = (RuntimeRecords(runtime_records_path)
                                 if runtime_records_path is not None else None)
        self._progress: ProgressRecorder | None = None
        self._attempt = attempt
        self._binding_path = binding_path
        self._supervisor_path = supervisor_path
        self._game_package = game_package
        self._standalone_expected_account = standalone_expected_account
        if standalone_expected_account is not None and (
                not standalone_expected_account.strip() or attempt is None
                or binding_path is None or supervisor_path is None or unknown_dir is None
                or not game_package or host_adapter is not None):
            raise ValueError("standalone identity requires a private supervised attempt")
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
        from transactions import TransactionJournal
        safety_journal = getattr(shopping, 'journal', None)
        if isinstance(safety_journal, TransactionJournal):
            self.account_state.attach_safety_storage(safety_journal.path)
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
        # Per tier, for tier promotion; seeded and carried forward the same way.
        self._tier_best_waves: dict[int, int] = dict(tier_best_waves or {})

    # -- reporting ---------------------------------------------------------
    def _running_locked(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tournament_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(getattr(self._bot, "_tournament_status", {"stage": "idle", "reason": None}))

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
            runtime = getattr(self._bot, 'card_runtime', None)
            if runtime is not None:
                status['cards'] = runtime.status()
            push_runs = getattr(self._bot, 'push_runs', None)
            if push_runs is not None:
                status['push_runs'] = push_runs.snapshot()
            return status

    def identity(self) -> dict[str, str | None]:
        """Identity already learned during start; this never performs ADB I/O."""
        with self._lock:
            return {"serial": self._device_serial, "game_version": None}

    def verified_account(self) -> str | None:
        """Account bound to the current supervised attempt, if verified."""
        with self._lock:
            if not self._running_locked():
                return None
            return self._verified_account()

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
                self._bind_fact_scope(evidence)
                if self._progress is not None:
                    self._progress.bind_account(evidence.account_id)
            except Exception:
                if self._supervisor is not None:
                    self._supervisor.invalidate_identity("identity_persist_failed")
                raise

    def _verify_account_walk(self, expected_account: str) -> None:
        """Verify through Settings and return home or to battle under the input fence."""
        from fleet.account_observer import StagingAccountObserver
        from fleet.restart_account import verify_restart_account
        from supervisor import IdentityWalkDevice

        supervisor, attempt = self._supervisor, self._attempt
        if supervisor is None or attempt is None or self._unknown_dir is None:
            raise RecoveryBlocked("account evidence unavailable")
        raw_device = supervisor.device
        if raw_device is None:
            raise RecoveryBlocked("device unavailable for account verification")
        info = raw_device.app_info(self._game_package)
        version = getattr(info, "version_name", None)
        if not isinstance(version, str) or not version.strip():
            raise RecoveryBlocked("reroll game version unavailable")
        observer = StagingAccountObserver(
            self._unknown_dir, endpoint=attempt.endpoint,
            allowed_versions=frozenset({version}))
        identity_evidence = verify_restart_account(
            device=IdentityWalkDevice(supervisor), observe=observer,
            supervisor=supervisor, expected_account=expected_account)
        if self._standalone_expected_account is not None:
            if identity_evidence.account_id != self._standalone_expected_account:
                supervisor.invalidate_identity("standalone_account_mismatch")
                raise RecoveryBlocked("standalone account mismatch")
            try:
                import db
                path = self.account_state.safety_path
                if path is None or self._binding_path is None:
                    raise ValueError("standalone safety database unavailable")
                db.bind_account(path, identity_evidence.account_id)
                if not self._binding_path.exists():
                    attempt.persist(self._binding_path, identity_evidence)
                if self._progress is not None:
                    self._progress.bind_account(identity_evidence.account_id)
            except Exception:
                supervisor.invalidate_identity("identity_persist_failed")
                raise
        self._bind_fact_scope(identity_evidence)

    def reverify_identity(self) -> None:
        """Bounded mid-run re-verification, called from the scan thread only.

        Paced by ``identity_reverify.IdentityReverifier``. It re-proves the
        registered account and re-binds the (possibly epoch-advanced) scope;
        it never invents an identity for a worker that has none.
        """
        supervisor = self._supervisor
        expected = supervisor.expected_account if supervisor is not None else None
        if (self._reroll_progress is None and self._standalone_expected_account is None) or not expected:
            raise RecoveryBlocked("in-process account re-verification unavailable")
        self._verify_account_walk(expected)

    def _bind_fact_scope(self, evidence: IdentityEvidence) -> None:
        if self._attempt is None:
            return
        self.account_state.bind_scope(FactScope(
            evidence.account_id, self._attempt.lease_id, self._attempt.generation,
            self.account_state.persisted_epoch), identity=evidence,
            runtime_root=self._runtime_records.path.parent if self._runtime_records else None)

    def _verified_account(self) -> str | None:
        """Retain only a consistent account bound to this worker attempt."""
        if self._binding_path is None or self._attempt is None:
            return None
        import json

        staging_journal = self._binding_path.parent / ".r00-account-creation.json"
        first_launch_journal = self._binding_path.parent / ".first-launch-account.json"
        host_adapter = getattr(self, "_host_adapter", None)
        host_instance = getattr(self, "_host_instance", None)
        designated = None
        if host_adapter is not None and host_instance is not None:
            try:
                designated = host_adapter.designated(host_instance, self._attempt)
            except IdentityError:
                # HostBoundConnect will pass the mismatch to DeviceSupervisor,
                # which durably quarantines this exact worker before a frame.
                return None
            if designated.source_lineage is not None:
                if staging_journal.exists() and first_launch_journal.exists():
                    raise RunnerError("identity incident: conflicting account audits", 503)
                if not staging_journal.exists() and not first_launch_journal.exists():
                    label = ("first-launch" if designated.source_lineage.startswith("manual:")
                             else "R00 staging")
                    raise RunnerError(f"identity incident: {label} account unverified", 503)

        accounts: set[str] = set()
        bindings_by_generation: dict[str, dict[str, Any]] = {}
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
                bindings_by_generation[row["generation"]] = row
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
        if first_launch_journal.exists():
            try:
                row = json.loads(first_launch_journal.read_text(encoding="utf-8"))
                evidence = row.get("evidence")
                final = evidence[-1] if isinstance(evidence, list) and evidence else None
                original_binding = bindings_by_generation.get(row.get("generation"))
                if (designated is None or designated.source_lineage is None
                        or row.get("state") != "verified" or row.get("account_id") != account
                        or row.get("instance") != host_instance
                        or row.get("source_lineage") != designated.source_lineage
                        or any(row.get(key) != getattr(self._attempt, key)
                               for key in ("worker_id", "endpoint", "lease_id", "attempt_id"))
                        or not row.get("app_version") or original_binding is None
                        or row.get("created_at") != original_binding.get("created_at")
                        or not isinstance(evidence, list)
                        or not any(isinstance(item, dict) and item.get("action") == "i_agree"
                                   for item in evidence)
                        or not isinstance(final, dict) or final.get("screen") != "account"
                        or final.get("account_id") != account
                        or final.get("app_version") != row["app_version"]
                        or final.get("observed_at") != original_binding["observed_at"]
                        or final.get("evidence_ref") != original_binding["evidence_ref"]):
                    raise ValueError("unverified first-launch journal")
            except (OSError, ValueError, TypeError, AttributeError):
                raise RunnerError("identity incident: first-launch account unverified", 503) from None
        return account

    def cards_context(self) -> CardPlanContext | None:
        """Read current Cards scope/program without dispatch or implicit budget creation."""
        with self._lock:
            runtime = getattr(self._bot, 'card_runtime', None)
            return runtime.context() if runtime is not None else None

    def request_cards(self, command: CardCommand) -> CardOperation:
        """Durably queue in battle or pause; execution belongs to the scan scheduler."""
        with self._lock:
            runtime = getattr(self._bot, 'card_runtime', None)
            if not self._running_locked() or runtime is None:
                raise RunnerError('Start the account bot before requesting Cards work', 409)
            try:
                return runtime.request(command)
            except (ValueError, KeyError) as exc:
                raise RunnerError(str(exc), 409) from None

    def update_cards_automation(self, *, scope: FactScope, program_revision: str,
                                apply: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Check current Cards authority and patch existing strategy controls atomically.

        The callback must not re-enter runner methods. The web control adapter
        owns validation, persistence, rollback and ControlChanged publication.
        """
        return self.with_cards_authority(scope=scope, program_revision=program_revision, apply=apply)

    def with_cards_authority(self, *, scope: FactScope, program_revision: str,
                             apply: Callable[[], dict[str, Any]], require_local: bool = False) -> dict[str, Any]:
        """Shared identity/route/control guard. Callback never re-enters the runner."""
        with self._lock:
            runtime = getattr(self._bot, 'card_runtime', None)
            if not self._running_locked() or runtime is None:
                raise RunnerError('cards_runtime_unavailable', 503)
            route_runtime = getattr(getattr(self._bot, 'reroll_progress', None), 'route_runtime', None)
            route_guard = route_runtime.store._locked() if route_runtime is not None else nullcontext()
            # All mutators participate in these guards. File publication may
            # change the effective program independently of the runner lock.
            try:
                with route_guard, self.account_state.guard_scope(scope), self._controls.transaction():
                    context = runtime.context()
                    if (context is None or context.scope != scope
                            or context.program_revision != program_revision):
                        raise RunnerError('cards_preconditions_changed', 409)
                    if require_local and context.config_owner != 'local':
                        raise RunnerError('cards_configuration_owner_changed', 409)
                    return apply()
            except ValueError as exc:
                raise RunnerError(str(exc), 409) from None

    def start_cards_cycle(self, *, scope: FactScope, program_revision: str,
                          cycle_id: str, cap: int) -> dict[str, Any]:
        """Explicit user initiation only; replay never reactivates a closed cycle."""
        with self._lock:
            runtime = getattr(self._bot, 'card_runtime', None)
            if not self._running_locked() or runtime is None:
                raise RunnerError('Start the account bot before starting a Cards cycle', 409)
            try:
                return runtime.start_cycle(scope=scope, program_revision=program_revision,
                                           cycle_id=cycle_id, cap=cap)
            except (ValueError, KeyError) as exc:
                raise RunnerError(str(exc), 409) from None

    def request_autopilot(self, command: dict[str, Any]) -> None:
        with self._lock:
            if not self._running_locked() or self._bot is None:
                raise RunnerError("Start the bot before sending commands", 409)
            if self._controls.snapshot().paused or self._bot.screen_state.value != "IN_RUN":
                raise RunnerError("Manual upgrades require an unpaused battle", 409)
            if command.get("action") == "buy" and getattr(self._bot, "_tournament_status", {}).get("stage") == "playing":
                raise RunnerError("Tournament purchases follow the tournament strategy", 409)
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
            if getattr(self._bot, 'card_runtime', None) is not None and self._bot.card_runtime.active:
                raise RunnerError('A Cards visit owns the menus', 409)
            if getattr(self._bot, "cards_intro", None) is not None and self._bot.cards_intro.active:
                raise RunnerError("The first Cards visit is already walking the menus", 409)
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
            if getattr(self._bot, 'card_runtime', None) is not None and self._bot.card_runtime.active:
                raise RunnerError('A Cards visit owns the menus', 409)
            if getattr(self._bot, "cards_intro", None) is not None and self._bot.cards_intro.active:
                raise RunnerError("The first Cards visit is already walking the menus", 409)
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
            if getattr(self._bot, 'card_runtime', None) is not None and self._bot.card_runtime.active:
                raise RunnerError('A Cards visit owns the menus', 409)
            if getattr(self._bot, "cards_intro", None) is not None and self._bot.cards_intro.active:
                raise RunnerError("The first Cards visit is already walking the menus", 409)
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
            if getattr(self._bot, 'card_runtime', None) is not None and self._bot.card_runtime.active:
                raise RunnerError('A Cards visit owns the menus', 409)
            if getattr(self._bot, "cards_intro", None) is not None and self._bot.cards_intro.active:
                raise RunnerError("The first Cards visit is already walking the menus", 409)
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
    def _record_start_failure(self, exc: Exception) -> None:
        if self._runtime_records is None or self._attempt is None:
            return
        reason = str(exc)
        fingerprint = hashlib.sha256(
            f"startup:{type(exc).__name__}:{reason}".encode()
        ).hexdigest()
        try:
            self._runtime_records.incident(
                generation=self._attempt.generation, fingerprint=fingerprint,
                phase="startup", reason=reason, outcome="failed",
            )
        except Exception:
            logger.exception("Could not persist startup incident")

    def _publish_runtime_start(self) -> None:
        if self._runtime_records is not None and self._attempt is not None:
            try:
                self._runtime_records.start(
                    self._attempt, PROCESS_IDENTITY,
                    boot_id=PROCESS_BOOT_ID, pid=os.getpid(),
                )
                self._progress = ProgressRecorder(
                    self._runtime_records.path.with_name('worker-heartbeat.json'),
                    attempt=self._attempt, account_id=self._verified_account(),
                    boot_id=PROCESS_BOOT_ID, pid=os.getpid(),
                )
            except Exception as exc:
                raise RunnerError(f"runtime startup record unavailable: {exc}", 503) from exc

    def start(self, *, operator: bool = True) -> dict[str, Any]:
        """Connect, build a bot, spawn the worker. Raises RunnerError.

        The device connection happens HERE rather than at launch, which is
        the whole difference: a dead emulator becomes a 503 and a BotError on
        the feed, not exit 1 from a process that never served anything.
        """
        with self._lock:
            if self._attempt is not None and self._runtime_records is not None:
                from fleet.worker_intent import read_intent, write_intent
                intent_root = self._runtime_records.path.parent
                intent = read_intent(intent_root)
                if intent is not None:
                    scope = {key: getattr(self._attempt, key) for key in (
                        "worker_id", "endpoint", "lease_id", "attempt_id")}
                    if any(intent.get(key) != value for key, value in scope.items()):
                        raise RunnerError("bot operator intent identity changed", 409)
                    if intent["desired_state"] == "stopped" and not operator:
                        raise RunnerError("bot stopped by operator", 409)
                if operator:
                    write_intent(intent_root, self._attempt, self._verified_account(), "running")
            if self._running_locked():
                raise RunnerError("the bot is already running", 409)
            # Reap a finished thread before reusing the slot, so a bot that
            # ended on its own does not block the next start.
            self._reap_locked()

            published_start = False
            if self._attempt is not None and self._attempt_started:
                previous = self._attempt
                next_attempt = Attempt.new(
                    previous.worker_id, previous.endpoint,
                    previous.lease_id, previous.attempt_id,
                )
                if self._runtime_records is not None and self._host_adapter is not None:
                    try:
                        runtime_root = self._runtime_records.path.parent
                        if (runtime_root / "fleet-registration.json").exists():
                            from fleet.worker_generation import rotate_registered_attempt

                            def publish_attempt(attempt: Attempt, binding: Path) -> None:
                                self._attempt = attempt
                                self._binding_path = binding
                                self._publish_runtime_start()

                            next_attempt, self._binding_path = rotate_registered_attempt(
                                runtime_root, previous, next_attempt, publish_attempt)
                            published_start = True
                        else:
                            InputLease(runtime_root / "input-lease.json").rotate(
                                previous.generation, next_attempt.generation)
                    except InputLeaseExpired as exc:
                        raise RunnerError("input generation revoked", 409) from exc
                self._attempt = next_attempt
                if self._binding_path is not None:
                    self._binding_path = (
                        self._binding_path.parent / f"{self._attempt.generation}.json"
                    )

            if not published_start:
                self._publish_runtime_start()

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
                    expected_account = self._verified_account()
                    if self._standalone_expected_account is not None:
                        if expected_account not in (None, self._standalone_expected_account):
                            raise RunnerError("standalone saved account mismatch", 409)
                        expected_account = self._standalone_expected_account
                        self.account_state.invalidate_scope("standalone_verification_required")
                    self._supervisor = DeviceSupervisor(
                        path=self._supervisor_path, endpoint=self._attempt.endpoint,
                        connect=connect,
                        expected_account=expected_account,
                        game_package=self._game_package,
                        quarantine_on_exhaustion=self._host_adapter is not None,
                        exhaustion_cooldown=(HOST_RECOVERY_COOLDOWN
                                             if self._host_adapter is not None else None),
                        progress=self._progress,
                        input_lease=(InputLease(self._runtime_records.path.parent /
                                                "input-lease.json")
                                     if self._runtime_records is not None
                                     and self._host_adapter is not None else None),
                        identity_invalidated=self.account_state.invalidate_scope,
                        input_generation=(self._attempt.generation
                                          if self._runtime_records is not None
                                          and self._host_adapter is not None else None),
                    )
                    self._supervisor.recover()
                    if self._supervisor.device is None:
                        failure = self._supervisor.status()
                        if failure.state is RecoveryState.QUARANTINED:
                            raise IdentityError(f"identity incident: {failure.reason}")
                        raise EmulatorError(failure.reason)
                    if self._reroll_progress is not None or self._standalone_expected_account is not None:
                        if self._unknown_dir is None or expected_account is None:
                            raise RecoveryBlocked("reroll account evidence unavailable")
                        self._verify_account_walk(expected_account)
                    device = GuardedDevice(self._supervisor)
                else:
                    device = self._device_factory()
            except RunnerError as exc:
                self._record_start_failure(exc)
                self._error = str(exc)
                self._bus.publish(events.IdentityIncident(message=str(exc)))
                self._bus.publish(events.BotError(message=str(exc)))
                raise
            except EmulatorError as exc:
                self._record_start_failure(exc)
                self._error = str(exc)
                # Published outside the lock would be tidier, but publish()
                # never blocks (see events.EventBus) so holding it is safe.
                if isinstance(exc, IdentityError):
                    self._bus.publish(events.IdentityIncident(message=str(exc)))
                self._bus.publish(events.BotError(message=str(exc)))
                raise RunnerError(str(exc), 503) from None
            except Exception as exc:  # noqa: BLE001 - anything else is still fatal to a start
                self._record_start_failure(exc)
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
                    self._record_start_failure(IdentityError(message))
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
            bot_kwargs = dict(
                device=device,
                templates=self._templates,
                bus=self._bus,
                controls=self._controls,
                checks=self._checks,
                shopping=self._shopping,
                frames=self._frames,
                first_run_id=self._next_run_id,
                best_wave=self._best_wave,
                tier_best_waves=dict(self._tier_best_waves),
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
            if self._progress is not None:
                bot_kwargs['progress'] = self._progress
            recovery = self._build_recovery_locked()
            if recovery is not None:
                bot_kwargs['recovery'] = recovery
            if self._reroll_progress is not None:
                bot_kwargs["reroll_progress"] = self._reroll_progress
            if (self._reroll_progress is not None or self._standalone_expected_account is not None) and self._supervisor is not None:
                bot_kwargs["identity_reverifier"] = self.reverify_identity
            bot = self._bot_factory(**bot_kwargs)

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
        receipts = None
        try:
            if self._runtime_records is not None and self.account_state.safety_path is not None:
                from mission_receipts import MissionReceiptReconciler
                receipts = MissionReceiptReconciler(self._runtime_records.path.parent, self.account_state)
                receipts.start()
            bot.run_forever()
        except Exception as exc:  # noqa: BLE001 - a crashed loop must not be silent
            logger.exception("the scan loop stopped with an error")
            self._bus.publish(
                events.BotError(message=str(exc), traceback=traceback.format_exc())
            )
            with self._lock:
                self._error = str(exc)
        finally:
            if receipts is not None:
                receipts.stop()
            recovery = getattr(bot, "recovery", None)
            if recovery is not None:
                # On the scan thread, after the loop exited: the coordinator
                # is scan-thread affine. close() never joins.
                try:
                    recovery.close()
                except Exception:  # noqa: BLE001
                    logger.exception("recovery coordinator close failed")
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

    def _recovery_fleet_root(self) -> Path | None:
        if self._recovery_root is not None:
            return self._recovery_root
        if self._runtime_records is None:
            return None
        worker_root = self._runtime_records.path.parent
        if not (worker_root / "fleet-registration.json").exists():
            return None
        return worker_root.parent.parent  # <fleet>/workers/<worker>

    def _build_recovery_locked(self) -> Any | None:
        previous = self._recovery
        if previous is not None:
            if not previous.shutdown_complete:
                message = ("recovery_shutdown_pending: the previous recovery coordinator has "
                           "not confirmed shutdown; recovery stays off for this bot")
                logger.error(message)
                self._bus.publish(events.BotError(message=message))
                return None
            self._recovery = None
        root = self._recovery_fleet_root()
        if (root is None or self._attempt is None or self._supervisor is None
                or self._progress is None or self._runtime_records is None):
            return None
        try:
            from recovery_coordinator import RecoveryCoordinator
            kwargs: dict[str, Any] = {}
            if self._recovery_service_factory is not None:
                kwargs["service_factory"] = self._recovery_service_factory
            self._recovery = RecoveryCoordinator(
                root, worker=self._attempt.worker_id,
                status_path=self._runtime_records.path.parent / "recovery-status.json",
                key_loader=self._recovery_key_loader, **kwargs)
        except Exception:  # noqa: BLE001 - recovery must never block a start
            logger.exception("recovery coordinator unavailable; recovery stays off")
            return None
        return self._recovery

    def recovery_shutdown(self) -> dict[str, bool] | None:
        """Retained coordinator's shutdown state, surfaced for operators/tests."""
        with self._lock:
            recovery = self._recovery
        if recovery is None:
            return None
        complete = recovery.shutdown_complete
        failed = recovery.status().get("blocker") == "shutdown_failed"
        return {"complete": complete, "failed": failed}

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
        try:
            harvested_tiers = dict(bot._tier_best_wave)
        except (AttributeError, TypeError):
            harvested_tiers = {}
        for tier, wave in harvested_tiers.items():
            if wave > self._tier_best_waves.get(tier, 0):
                self._tier_best_waves[tier] = wave

    def _reap_locked(self) -> None:
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._bot = None
            self._since = None

    def stop(self, timeout: float = 7.0, *, operator: bool = True) -> dict[str, Any]:
        """End the current bot. Process cleanup preserves operator intent.

        Explicit stops persist intent; ``operator=False`` is for server cleanup.

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
            if operator and self._attempt is not None and self._runtime_records is not None:
                from fleet.worker_intent import write_intent
                try:
                    account = self._verified_account()
                except RunnerError:
                    account = None  # A damaged audit must not prevent the stop.
                write_intent(self._runtime_records.path.parent, self._attempt,
                             account, "stopped")
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
