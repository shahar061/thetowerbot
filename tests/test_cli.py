from __future__ import annotations

import dataclasses
import json
import logging
import signal
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import digits
import tower_bot
from affordability import BrightnessAffordability, DigitAffordability
from strategy import Strategy
from tests.test_digits import build_synthetic_atlas
from tower_bot import parse_args


def test_reroll_registration_port_is_checked_before_worker_host_start(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.identity import Attempt, IdentityEvidence
    from fleet.runtime import WorkerRuntime

    runtime = WorkerRuntime.for_worker(tmp_path / "workers", "Tiramisu64_20", 10020)
    runtime.ensure_directories()
    attempt = Attempt.new("Tiramisu64_20", "127.0.0.1:5755", "lease", "job")
    binding = runtime.checkpoint_root / f"{attempt.generation}.json"
    attempt.persist(binding, IdentityEvidence("DD76C448F3FCFB82", time.time(), "capture"))
    (runtime.root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": "Tiramisu64_20", "endpoint": attempt.endpoint,
        "lease_id": "lease", "account_id": "DD76C448F3FCFB82",
        "web_port": 10020, "binding": str(binding), "job_id": "job",
    }))
    pool = tmp_path / "reroll-pool.json"
    pool.write_text(json.dumps([{
        "name": "Tiramisu64_20", "endpoint": attempt.endpoint, "lease_id": "lease"}]))
    args = parse_args(["--worker-id", "Tiramisu64_20", "--runtime-root",
                       str(runtime.root.parent), "--bluestacks-instance", "Tiramisu64_20",
                       "--reroll-pool", str(pool), "--host", "127.0.0.1", "--port", "5755",
                       "--lease-id", "lease", "--attempt-id", "job", "--web-port", "10020",
                       "--web", "--game-package", "com.TechTreeGames.TheTower"])
    import fleet.bluestacks_air as air

    class ReachedHost(Exception):
        pass

    monkeypatch.setattr(air, "BlueStacksAirInventory", lambda *a, **k: (_ for _ in ()).throw(ReachedHost))
    with pytest.raises(ReachedHost):
        tower_bot._main(args, runtime)


def test_auto_navigate_defaults_off() -> None:
    """A bot that launches battles the moment it starts is surprising - but
    that default now lives in Strategy, not the CLI flag. Unset here means
    "let the loaded strategy decide" (see apply_cli_overrides), and
    Strategy.from_config().auto_navigate is what is actually False."""
    assert parse_args([]).auto_navigate is None
    assert Strategy.from_config().auto_navigate is False


def test_auto_navigate_can_be_enabled() -> None:
    assert parse_args(["--auto-navigate"]).auto_navigate is True


def test_auto_navigate_can_be_turned_back_off() -> None:
    """--auto-navigate persists into the strategy (see apply_cli_overrides),
    so without a counterpart it was a one-way switch: on from the command
    line, off only from the dashboard. False and None must stay distinct -
    False is "turn it off and save that", None is still "don't touch it"."""
    assert parse_args(["--no-auto-navigate"]).auto_navigate is False


def test_tui_defaults_off() -> None:
    assert parse_args([]).tui is False


def test_max_runs_defaults_to_unlimited() -> None:
    assert parse_args([]).max_runs is None


def test_max_runs_parses_an_integer() -> None:
    assert parse_args(["--max-runs", "5"]).max_runs == 5


def test_run_forever_stops_at_max_runs(monkeypatch) -> None:
    """--max-runs is meaningless unless the loop actually honours it."""
    import cv2
    import events
    import vision
    from tower_bot import TowerBot

    fixtures = Path(__file__).parent / "fixtures"
    bot = TowerBot(
        device=MagicMock(),
        templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
        bus=events.EventBus(),
    )
    bot._screen = cv2.imread(str(fixtures / "main_menu.png"), cv2.IMREAD_COLOR)
    monkeypatch.setattr(bot, "refresh_screen", lambda: bot._screen)
    monkeypatch.setattr("time.sleep", lambda _: None)
    bot.runs.completed = 3

    # RULING 1: the check must happen before run_once() is ever called, not
    # merely before the *second* call. Spy on run_once so a check placed
    # after the scan (which would still "return immediately" after exactly
    # one extra scan) cannot slip past this test.
    scans: list[bool] = []
    monkeypatch.setattr(bot, "run_once", lambda: scans.append(True))

    bot.run_forever(interval=0.0, max_runs=3)  # must return immediately

    assert scans == []


def test_run_forever_stop_interrupts_a_long_interval_wait(monkeypatch) -> None:
    """stop() must end the between-scan wait immediately, not sleep it out.

    Regression: worker.join()'s timeout used to be sized off the interval
    directly, and the wait itself was a plain time.sleep() that stop() could
    not interrupt. With the browser dial at MAX_INTERVAL (3600s, a value
    Controls explicitly allows) that turned Ctrl+C into an hour-long hang.
    """
    import threading
    import cv2
    import events
    import vision
    from tower_bot import TowerBot

    fixtures = Path(__file__).parent / "fixtures"
    bot = TowerBot(
        device=MagicMock(),
        templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
        bus=events.EventBus(),
    )
    bot._screen = cv2.imread(str(fixtures / "main_menu.png"), cv2.IMREAD_COLOR)
    monkeypatch.setattr(bot, "refresh_screen", lambda: bot._screen)
    bot.controls.apply({"interval": 3600.0})  # the largest the dial allows

    finished = threading.Event()

    def _run() -> None:
        bot.run_forever()  # interval=None: reads the 3600s from controls
        finished.set()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=0.2)  # let it reach the between-scan wait
    assert not finished.is_set()  # sanity: it is actually waiting, not done

    bot.stop()

    assert finished.wait(timeout=1.0), "stop() did not interrupt the wait"
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_once_settles_the_tracker(monkeypatch) -> None:
    """RULING 2: --once must run enough scans for the debounced tracker to
    confirm a state, or it can never report the real screen (a single scan
    always leaves ScreenTracker at UNKNOWN - confirmed against a live
    emulator, which read GAME_OVER at 0.998 while --once printed UNKNOWN)."""
    import cv2
    import screens
    import tower_bot

    fixtures = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(fixtures / "game_over.png"), cv2.IMREAD_COLOR)

    captured: dict[str, tower_bot.TowerBot] = {}
    real_init = tower_bot.TowerBot.__init__

    def spy_init(self, *args, **kwargs) -> None:
        real_init(self, *args, **kwargs)
        captured["bot"] = self

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())
    monkeypatch.setattr(tower_bot, "capture_screen", lambda device: frame)
    monkeypatch.setattr(tower_bot.TowerBot, "__init__", spy_init)

    exit_code = tower_bot.main(["--once"])

    assert exit_code == 0
    assert captured["bot"].screen_state is screens.ScreenState.GAME_OVER
    assert captured["bot"].screen_state is not screens.ScreenState.UNKNOWN


def test_debug_scores_prints_a_table_and_exits_without_scanning(
    monkeypatch, capsys
) -> None:
    """RULING 3: --debug-scores is a one-shot diagnostic - one capture, one
    table of every action and screen-anchor score, no scan loop, no taps."""
    import cv2
    import config
    import tower_bot

    fixtures = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(fixtures / "main_menu.png"), cv2.IMREAD_COLOR)

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())
    monkeypatch.setattr(tower_bot, "capture_screen", lambda device: frame)

    def _boom(*args, **kwargs):
        raise AssertionError("TowerBot must not be constructed in --debug-scores mode")

    monkeypatch.setattr(tower_bot, "TowerBot", _boom)

    exit_code = tower_bot.main(["--debug-scores"])

    assert exit_code == 0
    out = capsys.readouterr().out
    for action in config.ACTIONS:
        assert action.template in out
    for name in config.SCREEN_ANCHORS:
        assert name in out


def test_web_debug_scores_still_connects_eagerly_and_exits_cleanly(
    monkeypatch, capsys
) -> None:
    """Regression: --debug-scores captures a frame and exits before anything
    ever serves, with or without --web - so it must always get an eagerly
    connected device, not the web path's lazy device_factory. Before this
    fix, `serving = args.web and not args.once` left `device = None` for
    `--web --debug-scores`, and capture_screen(None) blew up with a raw
    AttributeError instead of degrading into the same one-shot diagnostic
    plain --debug-scores gives.

    Deliberately does NOT stub tower_bot.capture_screen the way the plain
    --debug-scores test above does: that stub ignores its `device` argument
    entirely, so it would happily "succeed" even if `device` were None and
    mask exactly the regression this test exists to catch. Instead this
    stubs only connect_device, and lets the real capture_screen() run
    against a fake AdbDevice - the same fake-screenshot pattern
    tests/test_device.py uses - so a None device would fail for real, the
    way it did before the fix.
    """
    import config
    import tower_bot
    from PIL import Image as PILImage

    fixtures = Path(__file__).parent / "fixtures"
    fake_device = MagicMock()
    fake_device.screenshot.return_value = PILImage.open(fixtures / "main_menu.png")

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: fake_device)

    def _boom(*args, **kwargs):
        raise AssertionError("TowerBot must not be constructed in --debug-scores mode")

    monkeypatch.setattr(tower_bot, "TowerBot", _boom)

    exit_code = tower_bot.main(["--web", "--debug-scores"])

    assert exit_code == 0
    out = capsys.readouterr().out
    for action in config.ACTIONS:
        assert action.template in out
    for name in config.SCREEN_ANCHORS:
        assert name in out


def test_resolution_guard_warns_without_crashing_when_capture_fails(
    monkeypatch, caplog
) -> None:
    """The guard must degrade to a warning, never abort startup - a device
    mock (or a real capture hiccup) must not take down the whole bot."""
    import tower_bot

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())
    monkeypatch.setattr(tower_bot.TowerBot, "run_once", lambda self: False)

    exit_code = tower_bot.main(["--once"])

    assert exit_code == 0


def test_auto_navigate_does_not_start_a_run_past_the_cap(monkeypatch) -> None:
    """run_forever breaks at the TOP of the next iteration, but the scan that
    completed run N has already tapped RETRY on the way out - so `--max-runs 5`
    used to exit with run 6 live in the emulator."""
    import cv2
    import events
    import vision
    from control import Controls
    from tower_bot import TowerBot

    fixtures = Path(__file__).parent / "fixtures"
    bot = TowerBot(
        device=MagicMock(),
        templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
        bus=events.EventBus(),
        controls=Controls(strategy=dataclasses.replace(Strategy.from_config(), auto_navigate=True)),
    )
    bot._screen = cv2.imread(str(fixtures / "game_over.png"), cv2.IMREAD_COLOR)
    monkeypatch.setattr(bot, "refresh_screen", lambda: bot._screen)
    bot.run_once()
    bot.run_once()  # settled on GAME_OVER

    navigated: list[bool] = []
    monkeypatch.setattr(
        bot.navigator, "maybe_navigate", lambda *a, **k: navigated.append(True)
    )
    bot.runs.completed = 2

    bot.run_once(max_runs=2)
    assert navigated == []  # cap reached: do not tap RETRY into run 3

    bot.run_once(max_runs=None)
    assert navigated == [True]  # control: uncapped, it still navigates


def test_tui_keeps_stdlib_logging_off_the_terminal() -> None:
    """rich's Live draws on stdout while logging writes to stderr, so INFO
    lines shred the panel. With BotError on the bus, nothing is lost."""
    import logging

    import tower_bot

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers.clear()
        tower_bot.configure_logging(tui=True)
        assert root.handlers
        assert all(isinstance(h, logging.NullHandler) for h in root.handlers)

        root.handlers.clear()
        tower_bot.configure_logging(tui=False)
        streams = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.NullHandler)
        ]
        assert streams
        # The logger NAME must reach the line. The Phase 1 OCR rehearsal
        # selects its evidence with `grep tower_bot.ocr_ab`, and a format
        # without %(name)s makes "no disagreements" and "nothing was ever
        # logged" the same empty grep.
        record = logging.LogRecord(
            "tower_bot.ocr_ab", logging.WARNING, __file__, 1, "hello", None, None
        )
        assert "tower_bot.ocr_ab" in streams[0].format(record)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_affordability_defaults_to_digits() -> None:
    """As with auto_navigate above: unset on the CLI now means "let the
    strategy decide", and Strategy.from_config() is where "digits" actually
    lives as the default."""
    assert parse_args([]).affordability is None
    assert Strategy.from_config().affordability == "digits"


def test_affordability_can_be_forced_to_brightness() -> None:
    args = parse_args(["--affordability", "brightness"])
    assert args.affordability == "brightness"


def test_digits_without_an_atlas_degrades_to_brightness(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unlabelled atlas must not stop the bot - phase 2 behaviour is the
    floor, not an error."""
    with caplog.at_level(logging.WARNING):
        check = tower_bot.build_affordability("digits", atlas_root=tmp_path)
    assert isinstance(check, BrightnessAffordability)
    assert "atlas" in caplog.text.lower()


def test_digits_with_an_atlas_uses_digits(tmp_path: Path) -> None:
    for size_class in digits.SIZE_CLASSES:
        build_synthetic_atlas(tmp_path / size_class)
    check = tower_bot.build_affordability("digits", atlas_root=tmp_path)
    assert isinstance(check, DigitAffordability)


def test_a_partial_atlas_still_degrades(tmp_path: Path) -> None:
    """One size class built is not enough: the price is what gates a purchase,
    and a bot reading the wallet but never the price would gate on nothing."""
    build_synthetic_atlas(tmp_path / "wallet")
    check = tower_bot.build_affordability("digits", atlas_root=tmp_path)
    assert isinstance(check, BrightnessAffordability)


def test_brightness_is_used_even_when_an_atlas_exists(tmp_path: Path) -> None:
    for size_class in digits.SIZE_CLASSES:
        build_synthetic_atlas(tmp_path / size_class)
    check = tower_bot.build_affordability("brightness", atlas_root=tmp_path)
    assert isinstance(check, BrightnessAffordability)


def test_web_defaults_off() -> None:
    assert parse_args([]).web is False


def test_web_binds_loopback_by_default() -> None:
    """The dashboard serves session screenshots and has no auth."""
    args = parse_args(["--web"])
    assert args.web is True
    assert args.web_host == "127.0.0.1"


def test_fleet_requires_explicit_local_idle_configuration(tmp_path) -> None:
    assert tower_bot.main(["--web", "--idle", "--fleet-capacity", "5"]) == 1
    assert tower_bot.main(["--web", "--idle", "--fleet-capacity", "5",
                           "--fleet-name-prefix", "Tiramisu64_", "--fleet-root",
                           str(tmp_path), "--web-host", "0.0.0.0"]) == 1
    assert tower_bot.main(["--web", "--idle", "--fleet-capacity", "0",
                           "--fleet-name-prefix", "Tiramisu64_", "--fleet-root",
                           str(tmp_path)]) == 1


def test_the_store_is_on_by_default_and_can_be_turned_off() -> None:
    assert parse_args([]).store is True
    assert parse_args(["--no-store"]).store is False


def test_web_and_no_store_can_be_combined() -> None:
    """--web --no-store is a supported mode: a dashboard with no history."""
    args = parse_args(["--web", "--no-store"])
    assert args.web is True
    assert args.store is False


def test_the_database_path_can_be_overridden() -> None:
    assert parse_args(["--db", "/tmp/other.db"]).db == "/tmp/other.db"


def test_prepare_store_seeds_both_counters_and_prunes(tmp_path) -> None:
    """A restart must continue the sequence, not collide with it."""
    import time

    import db

    path = tmp_path / "bot.db"
    conn = db.connect(path)
    db.start_run(conn, 5, started_at=1.0)
    db.insert_event(conn, {
        "seq": 41, "run_id": 5, "ts": time.time(), "type": "Navigated",
        "screen": None, "action": None, "reason": None, "score": None,
        "price": None, "wallet": None, "detail": None,
    })
    db.insert_event(conn, {
        "seq": 42, "run_id": 5, "ts": time.time() - 90 * 86400, "type": "Navigated",
        "screen": None, "action": None, "reason": None, "score": None,
        "price": None, "wallet": None, "detail": None,
    })
    conn.close()

    seed_seq, last_run, seed_best_wave = tower_bot.prepare_store(path)

    assert (seed_seq, last_run, seed_best_wave) == (42, 5, None)
    with db.reader(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_a_seeded_bus_does_not_reissue_a_stored_seq() -> None:
    import events

    bus = events.EventBus(start_seq=42)

    assert bus.publish(events.Navigated(target="RETRY")).seq == 43


def test_prepare_store_closes_out_a_run_a_killed_process_left_live(tmp_path) -> None:
    """M4: no RunEnded ever fires for a killed (not stopped) bot, so the run
    keeps ended_at IS NULL - and the dashboard renders it as `live` forever.
    prepare_store is the one place that legitimately holds the writable
    connection outside the sink's own thread, so it is where this gets
    closed out."""
    import db

    path = tmp_path / "bot.db"
    conn = db.connect(path)
    db.start_run(conn, 1, started_at=100.0)  # never finished - the crash case
    db.finish_run(
        conn, 2, started_at=50.0, ended_at=90.0, wave=1, coins=1, tier=1,
        abandoned=False, scan_count=1, tap_count=1,
    )
    conn.close()

    tower_bot.prepare_store(path)

    with db.reader(path) as conn:
        runs = {r["id"]: r for r in db.list_runs(conn)}
    assert (runs[1]["ended_at"], runs[1]["abandoned"]) == (100.0, 1)
    # A run that already ended cleanly must not be touched.
    assert (runs[2]["ended_at"], runs[2]["abandoned"]) == (90.0, 0)


def test_prepare_store_logs_nothing_when_there_is_nothing_to_close(tmp_path, caplog) -> None:
    import db

    path = tmp_path / "bot.db"
    db.connect(path).close()

    with caplog.at_level(logging.INFO):
        tower_bot.prepare_store(path)

    assert "Closed" not in caplog.text


def test_prepare_store_backfills_the_ledger_before_pruning(tmp_path) -> None:
    """Ordering is the whole point. An event already past the retention
    window is deleted by the prune, so if backfill ran second the permanent
    history would be born already missing everything older than 30 days."""
    import db

    path = tmp_path / "bot.db"
    conn = db.connect(path)
    conn.execute(
        """INSERT INTO events (seq, run_id, ts, type, screen, action, reason,
                               score, price, wallet, detail)
           VALUES (1, NULL, 0.0, 'Purchased', NULL, NULL, NULL, NULL, 75, NULL,
                   '{"item": "Health", "category": "DEFENSE",
                     "coins_before": 1770, "gems_before": null,
                     "dry_run": false}')"""
    )
    conn.commit()
    conn.close()

    tower_bot.prepare_store(path, retention_days=30)

    with db.reader(path) as reader:
        # The event itself aged out...
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        # ...but the ledger kept it, which is why the ledger exists.
        page = db.ledger_page(reader)
        assert len(page) == 1
        assert page[0]["kind"] == "WORKSHOP_BUY"


@pytest.mark.parametrize(
    "host, should_warn",
    [
        ("127.0.0.1", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("192.168.1.50", True),
    ],
)
def test_warn_if_web_host_exposed(host, should_warn, caplog) -> None:
    """M5: the reflex for "reach it from my laptop" is --web-host 0.0.0.0,
    and the page it exposes serves live screenshots with no auth."""
    with caplog.at_level(logging.WARNING):
        tower_bot.warn_if_web_host_exposed(host)

    assert ("not loopback" in caplog.text) is should_warn


def test_once_with_web_does_not_advertise_a_dashboard_that_never_starts(
    monkeypatch, capsys
) -> None:
    """M1: --once wins over --web, so a bot that never starts the dashboard
    must not print its URL - the print used to run before the once/web
    branch and fire unconditionally."""
    import tower_bot

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())
    monkeypatch.setattr(tower_bot.TowerBot, "run_once", lambda self: False)

    exit_code = tower_bot.main(["--once", "--web"])

    assert exit_code == 0
    assert "Dashboard on" not in capsys.readouterr().out


def test_web_alone_still_prints_the_dashboard_url(monkeypatch, capsys, tmp_path) -> None:
    """The other half of the guard above: --web without --once must still
    advertise the dashboard - printed before sink.start() (task 8, minor 6:
    under --tui that call hands the terminal to rich's Live, which would
    swallow a line printed any later), and `not args.once` must not silence
    this case too."""
    import tower_bot

    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())
    # No `interval` parameter: fails loudly (missing argument) if serve_web
    # ever goes back to forwarding one.
    monkeypatch.setattr(
        tower_bot.TowerBot,
        "run_forever",
        lambda self, max_runs=None: None,
    )
    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    exit_code = tower_bot.main(["--web", "--db", str(tmp_path / "bot.db")])

    assert exit_code == 0
    assert "Dashboard on http://127.0.0.1:8765" in capsys.readouterr().out


def test_idle_through_main_never_starts_the_bot(monkeypatch, capsys, tmp_path) -> None:
    """--idle's headline behaviour, glued together through main() rather than
    tested piecemeal: parse_args's --idle and serve_web's
    start_immediately=False are each unit-tested on their own, but nothing
    before this proved main() actually wires
    start_immediately=not args.idle through. Asserts on the strongest signal
    available without standing up a real uvicorn: connect_device (the
    runner's device_factory) must never be called, because runner.start()
    must never run under --idle."""
    import tower_bot

    connect_calls: list[tuple[str, int]] = []

    def _connect(host: str, port: int):
        connect_calls.append((host, port))
        return MagicMock()

    monkeypatch.setattr(tower_bot, "connect_device", _connect)
    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    exit_code = tower_bot.main(
        ["--web", "--idle", "--db", str(tmp_path / "bot.db")]
    )

    assert exit_code == 0
    # The whole point: serve_web must never call runner.start() under
    # --idle, so the lazy device_factory (connect_device) is never invoked.
    assert connect_calls == []
    assert "no bot running, press Start" in capsys.readouterr().out


class _FakeServer:
    """Stands in for uvicorn.Server so the test never binds a real port."""

    instances: list["_FakeServer"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.should_exit = False
        # serve_web's own _Server subclasses whatever uvicorn.Server is, so
        # with this patched in it subclasses THIS - and its handle_exit
        # calls super().handle_exit(). Without a stand-in here that call is
        # an AttributeError and the Ctrl+C path cannot be tested at all.
        self.exits: list[int] = []
        _FakeServer.instances.append(self)

    def run(self) -> None:
        return

    def handle_exit(self, sig, frame) -> None:
        self.exits.append(sig)


class FakeRunner:
    """Stands in for BotRunner: enough to prove serve_web calls start()/stop()
    at the right moments without a real device or scan loop."""

    def __init__(self, *, fail_start: "RunnerError | None" = None) -> None:
        self.started = 0
        self.stopped = 0
        self.fail_start = fail_start

    def start(self) -> dict:
        self.started += 1
        if self.fail_start is not None:
            raise self.fail_start
        return {"running": True, "since": 1.0, "error": None}

    def stop(self) -> dict:
        self.stopped += 1
        return {"running": False, "since": None, "error": None}


def test_serve_web_starts_the_runner_by_default(monkeypatch) -> None:
    """--web without --idle must still mean "serve and scan" - the
    compatibility guarantee, exercised at the serve_web level."""
    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    runner = FakeRunner()
    shutdown = threading.Event()
    tower_bot.serve_web(runner, object(), host="127.0.0.1", port=8123, shutdown=shutdown)

    assert runner.started == 1
    # The fake server's run() returns immediately, so serve_web's own
    # finally is what stops the runner and sets `shutdown` here.
    assert runner.stopped >= 1
    assert shutdown.is_set() is True

    server = _FakeServer.instances[-1]
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8123)


def test_serve_web_does_not_start_the_runner_under_idle(monkeypatch) -> None:
    """--idle's whole point: serve the dashboard, but wait for a browser to
    press Start rather than starting a bot on the way up."""
    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    runner = FakeRunner()
    shutdown = threading.Event()
    tower_bot.serve_web(
        runner, object(), host="127.0.0.1", port=8123, shutdown=shutdown,
        start_immediately=False,
    )

    assert runner.started == 0


def test_serve_web_survives_a_runner_start_failure(monkeypatch, caplog) -> None:
    """The Step 8 pass condition: a dead emulator must not take the
    dashboard down with it. serve_web must log the RunnerError and keep
    serving rather than letting it propagate out of server.run()."""
    from runner import RunnerError

    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    runner = FakeRunner(fail_start=RunnerError("no device", 503))
    shutdown = threading.Event()
    with caplog.at_level(logging.ERROR, logger="tower_bot"):
        tower_bot.serve_web(runner, object(), host="127.0.0.1", port=8123, shutdown=shutdown)

    assert runner.started == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("no device" in message for message in errors)


def test_serve_web_watches_shutdown_and_stops_the_runner(monkeypatch) -> None:
    """The route (see web/app.py's shutdown_all()) can only set the flag -
    serve_web's watcher thread is what turns that into runner.stop() and
    server.should_exit. Uses a server whose run() blocks on should_exit, so
    the watcher - not serve_web's own post-run() finally - is what is
    actually under test."""

    class _BlockingServer(_FakeServer):
        def run(self) -> None:
            while not self.should_exit:
                time.sleep(0.01)

    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _BlockingServer)

    runner = FakeRunner()
    shutdown = threading.Event()
    thread = threading.Thread(
        target=tower_bot.serve_web,
        kwargs=dict(runner=runner, app=object(), host="127.0.0.1", port=8123, shutdown=shutdown),
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if runner.started:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("serve_web never started the runner")

    shutdown.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert _FakeServer.instances[-1].should_exit is True
    assert runner.stopped >= 1


def test_ctrl_c_sets_the_shutdown_flag_before_uvicorn_stops(monkeypatch) -> None:
    """The Ctrl+C path, which nothing exercised.

    uvicorn's own handle_exit begins a graceful shutdown that waits for
    in-flight responses, and an SSE feed or an MJPEG stream is in-flight for
    as long as a tab is open. Both generators end themselves only once
    `shutdown` is set, so setting it BEFORE delegating is what lets them
    finish inside the normal path instead of waiting out the
    timeout_graceful_shutdown backstop. The ordering IS the behaviour, so
    that is what this asserts - not merely that both things happened.
    """
    order: list[object] = []

    class _RecordingEvent(threading.Event):
        def set(self) -> None:
            order.append("shutdown")
            super().set()

    _FakeServer.instances.clear()
    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    shutdown = _RecordingEvent()
    tower_bot.serve_web(
        FakeRunner(), object(), host="127.0.0.1", port=8123, shutdown=shutdown
    )

    # serve_web's own finally already set the flag on the way out. This is
    # about handle_exit alone, so start it from a clean slate - and point the
    # server's own record at the same list, so the two halves are ordered
    # against each other rather than merely both present.
    server = _FakeServer.instances[-1]
    shutdown.clear()
    order.clear()
    server.exits = order

    server.handle_exit(signal.SIGINT, None)

    assert shutdown.is_set()
    assert order == ["shutdown", signal.SIGINT]


def test_controls_carry_whatever_strategy_they_are_handed() -> None:
    """Controls' dataclass defaults are a fallback; the Strategy it is
    constructed with is what snapshot() reports.

    Named for what it does: it builds the Strategy itself by hand rather than
    through apply_cli_overrides, so it covers none of the CLI seeding path -
    see test_apply_cli_overrides_persists_only_what_was_passed for that.
    parse_args is still called here to pin the values it hands back.
    """
    from control import Controls
    from tower_bot import parse_args

    args = parse_args(["--interval", "3.0", "--auto-navigate", "--affordability", "brightness"])
    strategy = dataclasses.replace(
        Strategy.from_config(),
        interval=args.interval,
        auto_navigate=args.auto_navigate,
        affordability=args.affordability,
    )
    controls = Controls(strategy=strategy)
    live = controls.snapshot()
    assert live.strategy.interval == 3.0
    assert live.strategy.auto_navigate is True
    assert live.strategy.affordability == "brightness"


def test_a_directory_of_unloadable_strategies_exits_with_a_message(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    """A hand-edited profile with a trailing comma must not end in a
    traceback - the same treatment connect_device's EmulatorError gets."""
    import config

    (tmp_path / "default.json").write_text("{ not json")
    monkeypatch.setattr(config, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(tower_bot, "connect_device", lambda host, port: MagicMock())

    with caplog.at_level(logging.ERROR, logger="tower_bot"):
        assert tower_bot.main(["--once", "--no-store"]) == 1

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and str(tmp_path) in errors[-1]


def test_build_checks_and_controls_downgrades_when_atlas_is_absent(
    tmp_path: Path,
) -> None:
    """The behaviour this whole task exists to protect: requesting digits
    with no atlas built must not let the dashboard claim "digits" while the
    bot actually runs brightness."""
    strategy = dataclasses.replace(Strategy.from_config(), affordability="digits")

    checks, controls = tower_bot.build_checks_and_controls(strategy, atlas_root=tmp_path)

    assert checks["digits"] is None
    assert isinstance(checks["brightness"], BrightnessAffordability)
    affordability = controls.snapshot().strategy.affordability
    assert affordability == "brightness"
    # The invariant a future refactor has to keep true: whatever strategy
    # Controls names, checks must have a real entry for it - never None.
    assert checks[affordability] is not None


def test_build_checks_and_controls_uses_digits_when_atlas_is_present(
    tmp_path: Path,
) -> None:
    for size_class in digits.SIZE_CLASSES:
        build_synthetic_atlas(tmp_path / size_class)
    strategy = dataclasses.replace(Strategy.from_config(), affordability="digits")

    checks, controls = tower_bot.build_checks_and_controls(strategy, atlas_root=tmp_path)

    assert isinstance(checks["digits"], DigitAffordability)
    affordability = controls.snapshot().strategy.affordability
    assert affordability == "digits"
    assert checks[affordability] is not None


def test_idle_and_strategy_flags_parse() -> None:
    from tower_bot import parse_args

    args = parse_args(["--web", "--idle", "--strategy", "crit"])
    assert args.idle is True
    assert args.strategy == "crit"


def test_idle_defaults_off_so_web_still_means_serve_and_scan() -> None:
    """The compatibility guarantee. If this fails, every existing invocation
    changed behaviour."""
    from tower_bot import parse_args

    assert parse_args(["--web"]).idle is False


def test_overlapping_flags_default_to_none_so_unset_is_distinguishable() -> None:
    """A flag that was not passed must not look like a flag set to the
    argparse default - otherwise it would overwrite the loaded strategy with
    a value nobody asked for."""
    from tower_bot import parse_args

    args = parse_args([])
    assert args.interval is None
    assert args.max_runs is None
    assert args.affordability is None
    assert args.auto_navigate is None


def test_apply_cli_overrides_persists_only_what_was_passed(tmp_path, monkeypatch) -> None:
    import config
    from strategy import Strategy, StrategyStore
    from tests.conftest import seed_template_dir
    from tower_bot import apply_cli_overrides, parse_args

    templates = tmp_path / "templates"
    templates.mkdir()
    seed_template_dir(templates)
    monkeypatch.setattr(config, "TEMPLATE_DIR", templates)

    store = StrategyStore(tmp_path / "strategies")
    store.ensure_seeded()

    result = apply_cli_overrides(store, store.load("default"), parse_args(["--interval", "9"]))
    assert result.interval == 9.0
    # Persisted, not merely applied: one truth, always. See the spec's
    # section 10 for why this beats override-without-persist.
    assert store.load("default").interval == 9.0
    # And nothing else moved.
    assert store.load("default").auto_navigate is False


def test_apply_cli_overrides_writes_nothing_when_no_flag_was_passed(
    tmp_path, monkeypatch
) -> None:
    import config
    from strategy import StrategyStore
    from tests.conftest import seed_template_dir
    from tower_bot import apply_cli_overrides, parse_args

    templates = tmp_path / "templates"
    templates.mkdir()
    seed_template_dir(templates)
    monkeypatch.setattr(config, "TEMPLATE_DIR", templates)

    store = StrategyStore(tmp_path / "strategies")
    loaded = store.ensure_seeded()
    before = store.path_for("default").read_text()

    assert apply_cli_overrides(store, loaded, parse_args([])) == loaded
    assert store.path_for("default").read_text() == before


def test_a_default_constructed_store_cannot_reach_the_real_strategies_dir(
    fenced_strategy_dir, tmp_path, monkeypatch
) -> None:
    """The regression test for the anomaly a full-suite run once produced:
    the repo's committed strategies/default.json turning up modified
    (interval rewritten from 2.0 to 5.0) with no test asserting it should
    be.

    tower_bot.main() builds `StrategyStore()` with no directory, which
    resolves to config.STRATEGY_DIR - the repo's real, tracked strategies/
    - and apply_cli_overrides() PERSISTS by design (see its own docstring).
    So any test that ever drives main() with an overlapping flag
    (--interval, --auto-navigate, --max-runs, --affordability) would write
    straight into the committed profile. This proves the fix:
    tests/conftest.py's session-scoped, autouse fenced_strategy_dir fixture
    repoints config.STRATEGY_DIR at a throwaway directory for the whole
    suite, so a default-constructed StrategyStore can no longer reach the
    real one no matter what a test - present or future - passes to main().

    Delete that fixture from conftest.py and this test fails: `store.directory`
    resolves to `tests.conftest.REAL_STRATEGY_DIR` instead of the fixture's
    temp path, and the write below lands in the tracked file this test
    exists to protect.
    """
    import json

    import config
    from strategy import StrategyStore
    from tests.conftest import REAL_STRATEGY_DIR, seed_template_dir
    from tower_bot import apply_cli_overrides, parse_args

    templates = tmp_path / "templates"
    templates.mkdir()
    seed_template_dir(templates)
    monkeypatch.setattr(config, "TEMPLATE_DIR", templates)

    store = StrategyStore()  # no directory - exactly what main() builds
    assert store.directory == fenced_strategy_dir
    assert store.directory != REAL_STRATEGY_DIR

    loaded = store.ensure_seeded()
    apply_cli_overrides(store, loaded, parse_args(["--interval", "9"]))

    # The write landed in the fence...
    assert store.load("default").interval == 9.0
    # ...never in the repo's tracked file.
    real_default = json.loads((REAL_STRATEGY_DIR / "default.json").read_text())
    assert real_default["interval"] != 9.0


def test_run_forever_rescans_promptly_while_a_battle_purchase_is_pending(monkeypatch) -> None:
    """The frame confirming a battle tap also decides the next one, so the
    loop fetches it after BATTLE_FOLLOWUP_SECONDS rather than the full interval."""
    import events
    import vision
    from tower_bot import TowerBot

    bot = TowerBot(
        device=MagicMock(),
        templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
        bus=events.EventBus(),
    )
    bot.controls.apply({"interval": 5.0})
    waits: list[float] = []

    def run_once(max_runs=None) -> bool:
        bot.autopilot.pending = (MagicMock(), 0.0) if not waits else None
        return True

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 2:
            bot.stop()
        return False

    monkeypatch.setattr(bot, "run_once", run_once)
    monkeypatch.setattr(bot._stopping, "wait", wait)
    bot.run_forever()

    assert waits[0] == config.BATTLE_FOLLOWUP_SECONDS
    assert waits[1] > 4.0
