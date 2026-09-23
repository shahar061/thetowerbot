"""The thing the browser's Start button reaches.

Every test here injects a fake device factory: the runner's job is
lifecycle, and requiring a real emulator to test lifecycle would mean it
never got tested. The bot itself is faked too where the test is about
start/stop rather than about scanning.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

import config
import digits
import vision
from control import Controls
from events import EventBus
from events import IdentityIncident
from runner import BotRunner, RunnerError
from supervisor import GuardedDevice, RecoveryBlocked, RecoveryState
from shopping import ShoppingSession
from sinks.state import BotState
from strategy import ActionRule, Shopping, ShoppingRule, Strategy


class FakeBot:
    """Stands in for TowerBot: spins until stopped, and counts its runs.

    Keeps every construction kwarg. The runner builds each bot from the
    strategy that is active at Start - that is the whole mechanism behind
    "a strategy change applies on the next Start" - and a fake that quietly
    swallowed those kwargs would let the runner stop passing them with no
    test noticing.
    """

    def __init__(
        self, first_run_id: int = 1, best_wave: int | None = None, **kwargs: Any
    ) -> None:
        self.kwargs: dict[str, Any] = {
            "first_run_id": first_run_id, "best_wave": best_wave, **kwargs,
        }
        self.runs = type("R", (), {"next_id": first_run_id, "completed": 0})()
        # Mirrors runs.next_id: a plain attribute the test can mutate to
        # simulate what this bot "learned" during its lifetime, then let
        # _harvest_locked carry forward on stop().
        self._best_wave = best_wave
        self._stopping = threading.Event()
        self.scans = 0

    def run_forever(self, **kwargs) -> None:
        while not self._stopping.wait(0.001):
            self.scans += 1

    def stop(self, *_: object) -> None:
        self._stopping.set()


def a_strategy(**overrides) -> Strategy:
    base = dict(
        name="test",
        actions=(ActionRule(name="Damage", template="upgrade_damage.png"),),
    )
    return Strategy(**{**base, **overrides})


@pytest.fixture
def runner_parts():
    """A runner over fakes, plus the pieces a test needs to inspect."""
    made: list[FakeBot] = []
    devices: list[object] = []

    def device_factory():
        device = object()
        devices.append(device)
        return device

    def bot_factory(**kwargs):
        # Everything through, nothing dropped: see FakeBot's docstring.
        bot = FakeBot(**kwargs)
        made.append(bot)
        return bot

    bus = EventBus()
    seen: list = []
    bus.subscribe(type("R", (), {"offer": lambda self, e: seen.append(e) or True})())
    state = BotState()
    runner = BotRunner(
        bus=bus,
        controls=Controls(strategy=a_strategy()),
        state=state,
        templates=object(),
        device_factory=device_factory,
        checks={"brightness": object(), "digits": None},
        bot_factory=bot_factory,
    )
    return runner, made, devices, seen, state


def test_a_fresh_runner_is_not_running(runner_parts) -> None:
    runner, _, _, _, _ = runner_parts
    status = runner.status()
    assert status["running"] is False
    assert status["since"] is None
    assert status["error"] is None


def test_runner_identity_uses_the_connected_device_without_an_extra_lookup(runner_parts) -> None:
    runner, _, devices, _, _ = runner_parts
    device = type("Device", (), {"serial": "emulator-5554"})()
    runner._device_factory = lambda: devices.append(device) or device

    assert runner.identity() == {"serial": None, "game_version": None}
    runner.start()
    try:
        assert runner.identity() == {"serial": "emulator-5554", "game_version": None}
        assert devices == [device]
    finally:
        runner.stop()


def test_identity_failure_emits_incident_before_start(runner_parts) -> None:
    from device import IdentityError

    runner, made, _, seen, _ = runner_parts
    runner._device_factory = lambda: (_ for _ in ()).throw(IdentityError("missing endpoint"))
    with pytest.raises(RunnerError):
        runner.start()
    assert made == []
    assert any(isinstance(event, IdentityIncident) for event in seen)


@pytest.mark.parametrize("serials", [
    ["emulator-5556", "127.0.0.1:5557"],
    ["127.0.0.1:5555", "127.0.0.1:5555"],
])
def test_missing_or_duplicate_transport_blocks_runner_and_emits_incident(
    runner_parts, monkeypatch, serials
) -> None:
    import device
    from unittest.mock import MagicMock

    client = MagicMock()
    client.device_list.return_value = [type("Attached", (), {"serial": serial})() for serial in serials]
    monkeypatch.setattr(device, "AdbClient", lambda **_: client)
    runner, made, _, seen, _ = runner_parts
    runner._device_factory = lambda: device.connect_device(host="127.0.0.1", port=5555)

    with pytest.raises(RunnerError):
        runner.start()
    assert made == []
    assert any(isinstance(event, IdentityIncident) for event in seen)


def test_runner_persists_attempt_only_after_observed_account_evidence(runner_parts, tmp_path) -> None:
    from fleet.identity import Attempt, IdentityEvidence

    runner, _, _, _, _ = runner_parts
    binding = tmp_path / "attempt.json"
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5555"})()
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = binding
    assert not binding.exists()
    with pytest.raises(RunnerError):
        runner.record_identity_evidence(IdentityEvidence("account-a", runner._attempt.created_at + 1, "frame://one"))
    runner.start()
    try:
        assert not binding.exists()
        runner.record_identity_evidence(IdentityEvidence("account-a", runner._attempt.created_at + 1, "frame://one"))
        assert binding.exists()
    finally:
        runner.stop()


def test_restart_rotates_attempt_generation_and_binding_path(runner_parts, tmp_path) -> None:
    from fleet.identity import Attempt

    runner, _, _, _, _ = runner_parts
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5555"})()
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"

    runner.start()
    first_generation = runner._attempt.generation
    runner.stop()
    runner.start()
    try:
        assert runner._attempt.generation != first_generation
        assert runner._binding_path == tmp_path / f"{runner._attempt.generation}.json"
    finally:
        runner.stop()


def test_runner_guards_taps_until_fresh_verified_account_evidence(runner_parts, tmp_path) -> None:
    from fleet.identity import Attempt, IdentityEvidence

    runner, made, _, _, _ = runner_parts
    raw = type("Device", (), {"serial": "127.0.0.1:5555", "taps": [],
                              "click": lambda self, x, y: self.taps.append((x, y))})()
    runner._device_factory = lambda: raw
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    runner.start()
    try:
        guarded = made[0].kwargs["device"]
        assert isinstance(guarded, GuardedDevice)
        assert runner.status()["recovery"]["reason"] == "fresh_evidence_required"
        with pytest.raises(RecoveryBlocked):
            guarded.click(1, 2)
        evidence = IdentityEvidence("account-a", time.time(), "frame://identity")
        runner.record_identity_evidence(evidence)
        assert guarded.supervisor.observe(
            frame_digest="fresh", observed_at=time.time(),
            screen="MAIN_MENU", account_id="account-a",
        ) is RecoveryState.READY
        guarded.click(1, 2)
        assert raw.taps == [(1, 2)]
    finally:
        runner.stop()


def test_runner_refuses_ambiguous_saved_identity_before_start(runner_parts, tmp_path) -> None:
    from fleet.identity import Attempt

    runner, made, _, _, _ = runner_parts
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5555"})()
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    (tmp_path / ("f" * 32 + ".json")).write_text("{", encoding="utf-8")

    with pytest.raises(RunnerError, match="identity"):
        runner.start()
    assert made == []


def test_runner_does_not_trust_a_binding_without_evidence(runner_parts, tmp_path) -> None:
    import json
    from fleet.identity import Attempt

    runner, made, _, _, _ = runner_parts
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5555"})()
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    payload = {key: getattr(runner._attempt, key)
               for key in ("worker_id", "endpoint", "lease_id", "attempt_id")}
    payload["account_id"] = "account-a"
    (tmp_path / ("e" * 32 + ".json")).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunnerError, match="identity"):
        runner.start()
    assert made == []


def test_failed_binding_write_never_authorizes_a_tap(runner_parts, tmp_path, monkeypatch) -> None:
    from fleet.identity import Attempt, IdentityEvidence

    runner, made, _, _, _ = runner_parts
    raw = type("Device", (), {"serial": "127.0.0.1:5555",
                              "click": lambda self, x, y: pytest.fail("unexpected tap")})()
    runner._device_factory = lambda: raw
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    runner.start()
    try:
        monkeypatch.setattr(Attempt, "persist", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            runner.record_identity_evidence(
                IdentityEvidence("account-a", time.time(), "frame://identity"))
        guarded = made[0].kwargs["device"]
        assert guarded.supervisor.current_account is None
        with pytest.raises(RecoveryBlocked):
            guarded.click(1, 2)
    finally:
        runner.stop()


def test_runner_relaunches_configured_game_without_tapping(tmp_path, runner_parts) -> None:
    from fleet.identity import Attempt

    runner, made, _, _, _ = runner_parts
    launched: list[str] = []
    device = type("Device", (), {
        "serial": "127.0.0.1:5555",
        "app_current": lambda self: type("App", (), {"package": "launcher"})(),
        "app_start": lambda self, package: launched.append(package),
        "click": lambda self, x, y: pytest.fail("unexpected tap"),
    })()
    runner._device_factory = lambda: device
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    runner._game_package = "com.example.tower"

    runner.start()
    try:
        assert launched == ["com.example.tower"]
        assert runner.status()["recovery"]["reason"] == "game_relaunched"
        with pytest.raises(RecoveryBlocked):
            made[0].kwargs["device"].click(1, 2)
    finally:
        runner.stop()


def test_runner_binds_named_host_before_connect_and_quarantines_its_failure(tmp_path, runner_parts) -> None:
    from bluestacks import BlueStacksAdapter, HostInstance
    from fleet.identity import Attempt

    class Host:
        def inventory(self) -> list[HostInstance]:
            return [HostInstance("alpha", "127.0.0.1:5555", "lease-a", "running")]

        def start(self, name: str) -> None:
            pytest.fail("unexpected host start")

        def stop(self, name: str) -> None:
            pytest.fail("unexpected host stop")

    runner, made, _, _, _ = runner_parts
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"
    runner._host_adapter = BlueStacksAdapter(Host(), staging_root=tmp_path)
    runner._host_instance = "alpha"
    popup_checks: list[str] = []
    runner._host_popup_checker = lambda instance: popup_checks.append(instance) or "closed"
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5555"})()
    runner.start()
    try:
        assert made[0].kwargs["device"].serial == "127.0.0.1:5555"
        assert popup_checks == ["alpha"]
        # A lost transport on a live host rests and retries, not quarantines.
        assert runner._supervisor.exhaustion_cooldown is not None
    finally:
        runner.stop()

    runner._host_instance = "beta"
    with pytest.raises(RunnerError):
        runner.start()
    assert popup_checks == ["alpha"]
    assert runner.status()["recovery"]["state"] is RecoveryState.QUARANTINED


def test_supervised_wrong_device_reports_identity_incident(tmp_path, runner_parts) -> None:
    from fleet.identity import Attempt

    runner, made, _, seen, _ = runner_parts
    runner._device_factory = lambda: type("Device", (), {"serial": "127.0.0.1:5557"})()
    runner._attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "attempt-a")
    runner._binding_path = tmp_path / f"{runner._attempt.generation}.json"
    runner._supervisor_path = tmp_path / "supervisor.json"

    with pytest.raises(RunnerError) as caught:
        runner.start()
    assert caught.value.status_code == 503
    assert made == []
    assert any(isinstance(event, IdentityIncident) for event in seen)
    assert runner.status()["recovery"]["state"] is RecoveryState.QUARANTINED


def test_start_connects_a_device_and_spawns_a_bot(runner_parts) -> None:
    runner, made, devices, _, _ = runner_parts
    status = runner.start()
    try:
        assert status["running"] is True
        assert status["since"] is not None
        assert len(devices) == 1
        assert len(made) == 1
    finally:
        runner.stop()


def test_start_resets_a_shopping_session_left_mid_visit(runner_parts) -> None:
    """The restart half of the critical fix: stop the bot mid-visit, and the
    ShoppingSession's `_step` is still non-IDLE. Without this, the NEXT
    bot's very first run_once() would see `shopping.active` True and call
    advance() directly - skipping begin() entirely, and with it the enabled
    check, the cadence, the run cap and the MAIN_MENU precondition - while
    resuming against a stale `_categories` list from the previous policy.
    reset() at Start closes that regardless of what the live policy says now.
    """
    runner, made, devices, seen, state = runner_parts
    shopping = ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=EventBus(),
        reader=digits.NumberReader(),
    )
    policy = Shopping(
        enabled=True, armed=False,
        workshop=(ShoppingRule(name="Damage", category="ATTACK"),),
    )
    shopping.begin(policy, run_count=1)
    assert shopping.active, "the fixture must actually be mid-visit to test anything"

    runner._shopping = shopping
    runner.start()
    try:
        assert shopping.active is False
    finally:
        runner.stop()


def test_stop_ends_the_worker(runner_parts) -> None:
    runner, made, _, _, _ = runner_parts
    runner.start()
    status = runner.stop()
    assert status["running"] is False
    # The thread is genuinely gone, not merely flagged as stopped.
    assert not runner._thread.is_alive()


def test_starting_twice_is_refused_rather_than_silently_ignored(runner_parts) -> None:
    """The browser asked for something that did not happen. Saying so beats
    a 200 that means nothing."""
    runner, made, _, _, _ = runner_parts
    runner.start()
    try:
        with pytest.raises(RunnerError) as caught:
            runner.start()
        assert caught.value.status_code == 409
        assert len(made) == 1
    finally:
        runner.stop()


def test_stopping_a_stopped_runner_is_harmless(runner_parts) -> None:
    """Idempotent on purpose: a double-click on Stop, or a Stop racing the
    bot hitting max_runs, must not raise.

    Starts first, deliberately. Two stops on a runner that was never started
    both take the `bot is None` early return and prove nothing about the
    real double-stop path, which is the interesting one: the second call
    calls stop() on an already-stopped bot and joins a thread that is
    already dead.
    """
    runner, _, _, _, _ = runner_parts
    runner.start()
    assert runner.stop()["running"] is False
    assert runner.stop()["running"] is False


def test_a_stop_the_loop_ignores_is_reported_as_still_running(runner_parts) -> None:
    """The contradiction this exists to prevent: stop() answering "stopped"
    after a join it just watched time out. status() derives running from
    thread liveness, so a hardcoded False here meant POST /api/bot/stop said
    stopped, the next /api/status poll said running, the next Start was
    refused with 409 - and the bot was still tapping the game throughout.
    Entirely reachable: a scan pass stuck on an ADB read outlasts the
    timeout, and a stalled ADB read is exactly when someone presses Stop.
    """
    runner, _, _, _, _ = runner_parts

    class DeafBot(FakeBot):
        def stop(self, *_: object) -> None:
            pass  # ignores the request, exactly like a wedged scan pass

    runner._bot_factory = lambda **kwargs: DeafBot(**kwargs)
    runner.start()
    try:
        status = runner.stop(timeout=0.05)
        assert status["running"] is True
        assert status["since"] is not None
        # The two answers agree, which is the entire point.
        assert runner.status()["running"] is True
    finally:
        # Release it for real, or the daemon thread spins for the session.
        runner._bot._stopping.set()
        runner._thread.join(timeout=5)


def test_each_bot_is_built_from_the_strategy_active_at_start(runner_parts) -> None:
    """"Applies on the next Start" is not a doc claim, it is these two
    kwargs: the runner reads controls at start() and hands the values to the
    new bot, which holds them for its whole life. Nothing else covers the
    mechanism, and a runner that quietly stopped passing them would look
    fine everywhere else.
    """
    runner, made, _, _, _ = runner_parts
    runner._controls.replace(
        a_strategy(screen_confirmations=4, navigation_cooldown=9.0)
    )
    runner.start()
    try:
        assert made[0].kwargs["screen_confirmations"] == 4
        assert made[0].kwargs["navigation_cooldown"] == 9.0
    finally:
        runner.stop()


def test_a_crashing_loop_is_published_and_leaves_the_runner_stopped(
    runner_parts,
) -> None:
    """A bot that raises out of run_forever() must end like any other: the
    error on the feed and in status(), the thread gone, and the runner ready
    to start another. Silence here would leave the dashboard showing a
    running bot that is not there.
    """
    runner, _, _, seen, _ = runner_parts

    def explodes(**kwargs):
        bot = FakeBot(**kwargs)

        def boom(**_: object) -> None:
            raise RuntimeError("matchTemplate blew up")

        bot.run_forever = boom
        return bot

    runner._bot_factory = explodes
    runner.start()
    for _ in range(200):
        if not runner.status()["running"]:
            break
        time.sleep(0.01)

    assert runner.status()["running"] is False
    assert runner.status()["error"] == "matchTemplate blew up"
    assert any(
        e.type == "BotError" and "matchTemplate" in e.message for e in seen
    )


def test_run_ids_carry_across_a_restart(runner_parts) -> None:
    """The failure this exists to stop: session 2's run 2 overwriting
    session 1's run 2 in the runs table."""
    runner, made, _, _, _ = runner_parts
    runner.start()
    made[0].runs.next_id = 12  # the first bot completed some runs
    runner.stop()

    runner.start()
    try:
        assert made[1].runs.next_id == 12
    finally:
        runner.stop()


def test_best_wave_reaches_the_bot_it_starts(runner_parts) -> None:
    """Without this, a bot started through the dashboard would always begin
    with best_wave=None regardless of what the database holds, and a
    milestones claim already owed would sit unoffered until that bot's own
    first RunEnded."""
    runner, made, _, _, _ = runner_parts
    runner._best_wave = 137  # what prepare_store() would have seeded

    runner.start()
    try:
        assert made[0].kwargs["best_wave"] == 137
    finally:
        runner.stop()


def test_best_wave_carries_across_a_restart(runner_parts) -> None:
    """The same carry-forward test_run_ids_carry_across_a_restart proves for
    run ids, for the account's best wave: a bot that discovers a new best
    during its lifetime must hand it to the runner, so the NEXT bot this
    runner starts already knows it rather than starting blind."""
    runner, made, _, _, _ = runner_parts
    runner.start()
    made[0]._best_wave = 250  # the first bot's own RunEnded raised this
    runner.stop()

    runner.start()
    try:
        assert made[1].kwargs["best_wave"] == 250
    finally:
        runner.stop()


def test_best_wave_harvest_never_lowers_or_clears_what_is_already_known(
    runner_parts,
) -> None:
    """Max-forward only, and a None guard - the same monotonic rule
    TowerBot itself applies to a RunEnded's wave. A bot that ends with a
    lower reading, or with nothing read at all (an abandoned run, or a
    session that never finished one), must not erase what an earlier bot in
    this same runner already established."""
    runner, made, _, _, _ = runner_parts
    runner.start()
    made[0]._best_wave = 250
    runner.stop()

    runner.start()
    made[1]._best_wave = 100  # lower than the runner already knows
    runner.stop()

    runner.start()
    try:
        assert made[2].kwargs["best_wave"] == 250
    finally:
        runner.stop()

    runner.start()
    made[3]._best_wave = None  # nothing read this session
    runner.stop()

    runner.start()
    try:
        assert made[4].kwargs["best_wave"] == 250
    finally:
        runner.stop()


def test_start_resets_bot_state_but_not_the_bus(runner_parts) -> None:
    import events as ev

    runner, _, _, seen, state = runner_parts
    state.apply(ev.ScanCompleted(seq=1, ts=time.time(), screen="IN_RUN", duration_ms=1.0))
    before_seq = runner._bus.publish(ev.Navigated(target="BATTLE")).seq

    runner.start()
    try:
        assert state.snapshot()["scans"] == 0
        # The bus is deliberately NOT reset: seq must keep climbing so a
        # reconnecting browser's Last-Event-ID stays meaningful across a
        # restart, which is exactly when you are watching.
        assert runner._bus.publish(ev.Navigated(target="BATTLE")).seq > before_seq
    finally:
        runner.stop()


def test_a_dead_emulator_is_reported_not_raised_out_of_the_process(runner_parts) -> None:
    """Today a bad device returns exit 1 from main() before anything serves.
    In idle mode it must not take the dashboard down with it."""
    runner, _, _, seen, _ = runner_parts

    def broken():
        from device import EmulatorError

        raise EmulatorError("no emulator at 127.0.0.1:5555")

    runner._device_factory = broken
    with pytest.raises(RunnerError) as caught:
        runner.start()
    assert caught.value.status_code == 503
    assert "no emulator" in str(caught.value)

    assert runner.status()["running"] is False
    assert runner.status()["error"] is not None
    # And it lands in the feed and on the errors page like every other failure.
    assert any(e.type == "BotError" for e in seen)


def test_the_error_clears_on_a_successful_start(runner_parts) -> None:
    runner, _, _, _, _ = runner_parts

    def broken():
        from device import EmulatorError

        raise EmulatorError("nope")

    original = runner._device_factory
    runner._device_factory = broken
    with pytest.raises(RunnerError):
        runner.start()
    assert runner.status()["error"] is not None

    runner._device_factory = original
    runner.start()
    try:
        assert runner.status()["error"] is None
    finally:
        runner.stop()


def test_a_bot_that_ends_on_its_own_leaves_the_runner_stopped(runner_parts) -> None:
    """max_runs reached. The server keeps serving; only the bot ended."""
    runner, _, _, _, _ = runner_parts

    def ends_immediately(*, device, first_run_id, **kwargs):
        bot = FakeBot(first_run_id=first_run_id)
        bot.run_forever = lambda **k: None
        return bot

    runner._bot_factory = ends_immediately
    runner.start()
    for _ in range(200):
        if not runner.status()["running"]:
            break
        time.sleep(0.01)
    assert runner.status()["running"] is False


def test_concurrent_starts_spawn_exactly_one_bot(runner_parts) -> None:
    runner, made, _, _, _ = runner_parts
    refused: list[RunnerError] = []

    def racer() -> None:
        try:
            runner.start()
        except RunnerError as exc:
            refused.append(exc)

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(made) == 1
        assert len(refused) == 7
    finally:
        runner.stop()


def test_account_state_survives_runner_restart(runner_parts: tuple) -> None:
    runner, made, _, _, _ = runner_parts
    shared = runner.account_state
    runner.start()
    assert made[-1].kwargs['account_state'] is shared
    runner.stop()
    runner.start()
    assert made[-1].kwargs['account_state'] is shared
    runner.stop()


def test_screen_reading_lifecycle_retains_history_without_stale_current(runner_parts: tuple) -> None:
    from account_screens import ScreenReading
    runner, _, _, _, _ = runner_parts
    state = runner.account_state.screen_readings
    reading = ScreenReading('account.settings', 123., 1080, 2400, 'a' * 64, (), ())
    state.observe(reading)
    runner.start()
    try:
        assert state.snapshot()['current_screen_id'] is None
        state.observe(reading)
    finally:
        runner.stop()
    snapshot = state.snapshot()
    assert snapshot['current_screen_id'] is None
    assert snapshot['readings'][0]['observed_at'] == 123.


# -- Collect stats transaction ---------------------------------------------
class _MenuBot(FakeBot):
    """A FakeBot that reports a screen, the way request_stats_collection asks."""

    screen: str = "MAIN_MENU"

    @property
    def screen_state(self) -> Any:
        return type("S", (), {"value": self.screen})()


@pytest.fixture
def menu_runner(runner_parts) -> tuple[BotRunner, list]:
    runner, made, _, _, _ = runner_parts
    runner._bot_factory = lambda **kwargs: made.append(_MenuBot(**kwargs)) or made[-1]
    return runner, made


def test_collect_stats_is_refused_until_a_running_unpaused_bot_is_on_the_menu(
    menu_runner: tuple[BotRunner, list],
) -> None:
    runner, made = menu_runner
    with pytest.raises(RunnerError) as stopped:
        runner.request_stats_collection()
    assert stopped.value.status_code == 409
    runner.start()
    try:
        made[-1].screen = "IN_RUN"
        with pytest.raises(RunnerError) as battling:
            runner.request_stats_collection()
        assert battling.value.status_code == 409
        made[-1].screen = "MAIN_MENU"
        runner._controls.apply({"paused": True})
        with pytest.raises(RunnerError) as held:
            runner.request_stats_collection()
        assert held.value.status_code == 409
        assert not runner.collection.active
        runner._controls.apply({"paused": False})
        assert runner.request_stats_collection()["status"] == "running"
        # Never queued: a second request is refused, not banked behind the first.
        with pytest.raises(RunnerError) as busy:
            runner.request_stats_collection()
        assert busy.value.status_code == 409
        assert runner.collection.active
    finally:
        runner.stop()


def test_collect_stats_is_unavailable_when_the_reader_could_not_load(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """Unavailable is 503 and distinct from a refusal: nothing is armed."""
    runner, _ = menu_runner
    runner._shopping = ShoppingSession(object(), None, digits.NumberReader(),
                                       disabled_reason="the OCR engine could not be built")
    runner.start()
    try:
        with pytest.raises(RunnerError) as unavailable:
            runner.request_stats_collection()
        assert unavailable.value.status_code == 503
        assert "OCR engine" in str(unavailable.value)
        assert not runner.collection.active
        assert runner.collection.snapshot()["status"] == "idle"
    finally:
        runner.stop()


def test_a_restart_cancels_a_half_walked_transaction_and_lets_a_retry_start(
    menu_runner: tuple[BotRunner, list],
) -> None:
    runner, _ = menu_runner
    runner.start()
    runner.request_stats_collection()
    runner.stop()
    ended = runner.collection.snapshot()
    assert ended["status"] == "failed" and ended["result"]["reason"] == "bot_stopped"
    runner.start()
    try:
        # The failure is not resumed, and it does not block the next attempt.
        assert not runner.collection.active
        assert runner.request_stats_collection()["status"] == "running"
    finally:
        runner.stop()


def test_every_bot_is_handed_the_runner_s_own_transaction(
    menu_runner: tuple[BotRunner, list],
) -> None:
    runner, made = menu_runner
    runner.start()
    runner.stop()
    runner.start()
    runner.stop()
    assert [bot.kwargs["collection"] for bot in made] == [runner.collection] * 2


# -- Missions visit ---------------------------------------------------------
def test_a_missions_visit_is_refused_until_a_running_unpaused_bot_is_on_the_menu(
    menu_runner: tuple[BotRunner, list],
) -> None:
    runner, made = menu_runner
    with pytest.raises(RunnerError) as stopped:
        runner.request_missions_visit()
    assert stopped.value.status_code == 409
    runner.start()
    try:
        made[-1].screen = "IN_RUN"
        with pytest.raises(RunnerError) as battling:
            runner.request_missions_visit()
        assert battling.value.status_code == 409
        made[-1].screen = "MAIN_MENU"
        runner._controls.apply({"paused": True})
        with pytest.raises(RunnerError) as held:
            runner.request_missions_visit()
        assert held.value.status_code == 409
        assert not runner.visit.active
        runner._controls.apply({"paused": False})
        assert runner.request_missions_visit()["status"] == "running"
        with pytest.raises(RunnerError) as busy:
            runner.request_missions_visit()
        assert busy.value.status_code == 409
        assert runner.visit.active
    finally:
        runner.stop()


def test_two_transactions_are_never_armed_at_once_in_either_order(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """Both walk out of the same main menu. A second one armed behind the
    first would read the first one's screen as its own arrival evidence."""
    runner, _ = menu_runner
    runner.start()
    try:
        runner.request_stats_collection()
        with pytest.raises(RunnerError) as during_collection:
            runner.request_missions_visit()
        assert during_collection.value.status_code == 409
        assert not runner.visit.active
        runner.collection.cancel("test", "cleared for the other direction")
        runner.request_missions_visit()
        with pytest.raises(RunnerError) as during_visit:
            runner.request_stats_collection()
        assert during_visit.value.status_code == 409
        assert not runner.collection.active
    finally:
        runner.stop()


def test_a_restart_cancels_a_half_walked_visit_and_lets_a_retry_start(
    menu_runner: tuple[BotRunner, list],
) -> None:
    runner, _ = menu_runner
    runner.start()
    runner.request_missions_visit()
    runner.stop()
    ended = runner.visit.snapshot()
    assert ended["status"] == "failed" and ended["result"]["reason"] == "bot_stopped"
    runner.start()
    try:
        assert not runner.visit.active
        assert runner.request_missions_visit()["status"] == "running"
    finally:
        runner.stop()


# -- Missions claim -----------------------------------------------------------
def test_a_restart_cancels_a_half_walked_claim_and_lets_a_retry_start(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """A claim TAPS things that change the account, so it must not outlive
    the bot that was walking it any more than collect_stats or visit do -
    the same stop-site cancel (`_run`'s finally block), the same reason.

    Named for the restart, but despite the name its assertions are actually
    satisfied by the STOP-site cancel: `stop()` already ends the claim via
    `_run`'s finally block before this test's second `start()` ever runs, so
    that `start()` only ever finds an already-idle object and confirms it
    stays that way. It is not proof of `start()`'s OWN `bot_restarted`
    cancel - the sibling `collection`/`visit` restart tests share this same
    naming weakness. `test_a_restart_cancels_a_claim_left_active_with_no_bot_walking_it`,
    just below, isolates that other cancel site instead."""
    runner, _ = menu_runner
    runner.start()
    runner.request_missions_claim()
    runner.stop()
    ended = runner.claim.snapshot()
    assert ended["status"] == "failed" and ended["result"]["reason"] == "bot_stopped"
    runner.start()
    try:
        assert not runner.claim.active
        assert runner.request_missions_claim()["status"] == "running"
    finally:
        runner.stop()


def test_a_restart_cancels_a_claim_left_active_with_no_bot_walking_it(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """Isolates the OTHER cancel site: `start()`'s own `bot_restarted` cancel,
    not the stop-site one the test above already exercises via `_run`'s
    finally block (which would otherwise have already cleared it before this
    `start()` ever ran). Arms `self.claim` directly, the way a stale owner
    from a scenario `stop()` never observed would leave it, so the ONLY thing
    that can end it here is `start()`'s own restart cancel."""
    runner, _ = menu_runner
    runner.start()
    runner.stop()
    assert runner.claim.request() is True
    assert runner.claim.active, "the fixture must actually be armed to test anything"
    runner.start()
    try:
        assert not runner.claim.active
    finally:
        runner.stop()


def test_a_claim_and_the_other_two_transactions_are_never_armed_at_once_in_either_order(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """The same mutual exclusion as
    test_two_transactions_are_never_armed_at_once_in_either_order, extended
    to the third transaction: all three walk out of the same main menu, so a
    second one armed behind any of the others would read that one's screen
    as its own arrival evidence."""
    runner, _ = menu_runner
    runner.start()
    try:
        runner.request_missions_claim()
        with pytest.raises(RunnerError) as during_claim_collection:
            runner.request_stats_collection()
        assert during_claim_collection.value.status_code == 409
        assert not runner.collection.active
        with pytest.raises(RunnerError) as during_claim_visit:
            runner.request_missions_visit()
        assert during_claim_visit.value.status_code == 409
        assert not runner.visit.active
        runner.claim.cancel("test", "cleared for the other direction")

        runner.request_stats_collection()
        with pytest.raises(RunnerError) as during_collection_claim:
            runner.request_missions_claim()
        assert during_collection_claim.value.status_code == 409
        assert not runner.claim.active
        runner.collection.cancel("test", "cleared for the other direction")

        runner.request_missions_visit()
        with pytest.raises(RunnerError) as during_visit_claim:
            runner.request_missions_claim()
        assert during_visit_claim.value.status_code == 409
        assert not runner.claim.active
    finally:
        runner.stop()


# --- the milestones claim walk -------------------------------------------

def test_a_milestones_claim_refuses_while_each_sibling_walks(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """Mutual exclusion IS the safety property here: two transactions walking
    the same menus from different remembered steps is the hazard the design
    exists to prevent. Three directions, each isolated."""
    runner, _ = menu_runner
    runner.start()
    try:
        for arm, cancel in ((runner.request_stats_collection, runner.collection),
                            (runner.request_missions_visit, runner.visit),
                            (runner.request_missions_claim, runner.claim)):
            arm()
            with pytest.raises(RunnerError) as busy:
                runner.request_milestones_claim()
            assert busy.value.status_code == 409
            assert not runner.milestones_claim.active
            cancel.cancel("test", "cleared for the next direction")
    finally:
        runner.stop()


def test_each_sibling_refuses_while_a_milestones_claim_walks(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """The other three directions. A one-directional guard would let two
    transactions walk the same menus from different steps."""
    runner, _ = menu_runner
    runner.start()
    try:
        runner.request_milestones_claim()
        for arm, other in ((runner.request_stats_collection, runner.collection),
                           (runner.request_missions_visit, runner.visit),
                           (runner.request_missions_claim, runner.claim)):
            with pytest.raises(RunnerError) as busy:
                arm()
            assert busy.value.status_code == 409
            assert not other.active
    finally:
        runner.stop()


def test_a_stop_cancels_a_half_walked_milestones_claim(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """The stop/finally site. Slice 2 shipped a claim walk whose own docstring
    said a restart cancelled it while only the pause path did."""
    runner, _ = menu_runner
    runner.start()
    runner.request_milestones_claim()
    runner.stop()
    ended = runner.milestones_claim.snapshot()
    assert ended["status"] == "failed"
    assert ended["result"]["reason"] == "bot_stopped"


def test_a_restart_cancels_a_half_walked_milestones_claim(
    menu_runner: tuple[BotRunner, list],
) -> None:
    """The RESTART site specifically, isolated from the stop site: arm the walk
    after a completed stop(), so the only cancel that can fire is the one in
    start(). Slice 2's first attempt at this could not tell the two apart."""
    runner, _ = menu_runner
    runner.start()
    runner.stop()
    assert runner.milestones_claim.request(now=0.) is True
    runner.start()
    try:
        ended = runner.milestones_claim.snapshot()
        assert ended["result"]["reason"] == "bot_restarted"
    finally:
        runner.stop()
