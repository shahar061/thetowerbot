"""Pytest configuration for thetowerbot tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# Add the repo root to the path so tests can import modules from it
repo_root = Path(__file__).parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import cv2  # noqa: E402 - after the sys.path fix-up above
import pytest  # noqa: E402 - after the sys.path fix-up above

import config  # noqa: E402 - after the sys.path fix-up above
import digits  # noqa: E402 - after the sys.path fix-up above
import events  # noqa: E402 - after the sys.path fix-up above
import ocr  # noqa: E402 - after the sys.path fix-up above
import screens  # noqa: E402 - after the sys.path fix-up above
import vision  # noqa: E402 - after the sys.path fix-up above
from control import Controls  # noqa: E402 - after the sys.path fix-up above
from device import Image  # noqa: E402 - after the sys.path fix-up above
from shopping import ShoppingSession  # noqa: E402 - after the sys.path fix-up above
from strategy import ActionRule, Claims, Shopping, Strategy  # noqa: E402 - after the fix-up above
from tower_bot import TowerBot  # noqa: E402 - after the sys.path fix-up above

# Captured at import time, before the autouse fixture below (or anything
# else) can ever repoint config.STRATEGY_DIR. Exactly one test needs the
# real, committed directory rather than the session's fenced stand-in - see
# test_strategy_store.py::test_the_committed_default_matches_config.
REAL_STRATEGY_DIR = config.STRATEGY_DIR


def seed_template_dir(templates_dir: Path) -> None:
    """Create empty stand-ins for every template Strategy.from_config()'s
    default now references, so a test that points config.TEMPLATE_DIR at
    `templates_dir` and then calls save()/ensure_seeded() (which run
    validated()) does not fail on a template that has nothing to do with
    what the test is actually checking.

    Covers config.ACTIONS (the in-run rows) only. A workshop row is found by
    matching its name against what tiles.read_rows sees on the page (spec
    §7's AMENDMENT), not by a template, so validated() no longer has a
    shopping-side template to check and there is nothing to seed for it.
    """
    for action in config.ACTIONS:
        (templates_dir / action.template).write_bytes(b"")


@pytest.fixture(scope="session", autouse=True)
def fenced_strategy_dir(tmp_path_factory: pytest.TempPathFactory):
    """Point every default-constructed StrategyStore at a throwaway
    directory for the whole test session.

    strategy.StrategyStore(), built with no directory - which is what
    tower_bot.main() does, and what any future call site defaulting one
    will do too - resolves to config.STRATEGY_DIR: the repo's real, tracked
    strategies/. tower_bot.apply_cli_overrides() PERSISTS by design (see its
    own docstring), so any test that drives main() with one of the
    overlapping flags (--interval, --auto-navigate, --max-runs,
    --affordability) writes straight into the committed profile. No test
    happens to pass one of those flags to main() today, which is why this
    has not been *observed* doing damage - but "no test happens to" is luck,
    not a guarantee, and a suite that CAN rewrite a tracked file will
    eventually do so as more tests are added. Fencing the whole session off
    the real directory turns that into a structural guarantee instead of a
    habit to remember.

    Session-scoped and autouse so it holds for every test - present and
    future - without each one opting in. The function-scoped `monkeypatch`
    fixture cannot be used at session scope, so this uses
    pytest.MonkeyPatch's own context manager instead, and stays open for the
    whole session via `yield` inside the `with`.
    """
    fenced = tmp_path_factory.mktemp("strategy_dir_fence")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "STRATEGY_DIR", fenced)
        yield fenced


@pytest.fixture(autouse=True)
def no_ocr_reuse_between_tests():
    """ocr keeps the last menu full read at module level (spec P3). A test
    that replaces ocr.read, or feeds the same frame different boxes, must
    never be answered from another test's read."""
    ocr._last_full = None
    yield
    ocr._last_full = None


# --------------------------------------------------------------------------
# Shopping-loop bot fixtures (tests/test_shopping_loop.py)
# --------------------------------------------------------------------------
_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _frame(name: str) -> Image:
    path = _FIXTURES_DIR / f"{name}.png"
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None, f"missing fixture: {name}.png"
    return image


class _FakeDevice:
    """Records taps as (x, y) pairs.

    navigate.Navigator, shopping.ShoppingSession and
    TowerBot.find_and_click_image all end up calling device.tap(device, x, y),
    which is nothing but device.click(x, y) - so a bare click() recorder
    observes every tap path at once, with no need to monkeypatch each
    module's own `tap` import separately.
    """

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class _RecordingBus:
    """Keeps every event handed to it, unstamped.

    Real events.EventBus.publish() stamps seq/ts via dataclasses.replace -
    unnecessary ceremony here, since these tests only ever inspect an
    event's own fields (its `.type` and whatever it carries).
    """

    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event: events.Event) -> events.Event:
        self.published.append(event)
        return event


class _RecordingSnapshotWriter:
    """Stands in for snapshots.SnapshotWriter: records instead of touching
    disk, and never rate-limits - a test drives exactly as many scans as it
    wants to observe and must see every one of them land (or not)."""

    def __init__(self) -> None:
        self.written: list[Image] = []

    def maybe_write(self, image: Image, now: float | None = None) -> Path:
        self.written.append(image)
        return _FIXTURES_DIR / f"fake-unknown-{len(self.written)}.png"


def _shopping_bot(
    frame_name: str, *, state: screens.ScreenState, policy: Shopping, auto_navigate: bool,
    claims: Claims = Claims(),
) -> TowerBot:
    """One TowerBot, frozen on one frame, with its tracker pre-confirmed.

    Pre-setting the tracker directly - rather than warming it up through a
    couple of run_once() calls the way test_bot_reporting.py's settled_bot
    does - is deliberate: warming up through run_once() would run the very
    begin()/advance() logic these tests exist to exercise, before the test
    gets a chance to control it.

    The shopping session is built directly, never through
    tower_bot.build_shopping(): that function's startup gate builds the OCR
    engine to decide whether to disable buying, and these tests are about
    the visit loop, not about whether a wheel imports. A directly-built
    session skips the gate and stays fast.
    """
    device = _FakeDevice()
    bus = _RecordingBus()
    templates = vision.TemplateCache(config.TEMPLATE_DIR)
    session = ShoppingSession(templates, bus, digits.NumberReader())

    bot = TowerBot(
        device=device,
        templates=templates,
        bus=bus,
        controls=Controls(strategy=Strategy(
            # Strategy requires at least one action; disabled, so the
            # IN_RUN fixture never tries to tap it.
            name="t",
            actions=(ActionRule(name="Damage", template="upgrade_damage.png", enabled=False),),
            shopping=policy, auto_navigate=auto_navigate, claims=claims,
        )),
        shopping=session,
        navigation_cooldown=0.0,
    )
    image = _frame(frame_name)
    bot._screen = image
    bot.refresh_screen = lambda: bot._screen  # no real device to capture from
    bot.tracker.state = state
    bot.tracker._confirmed = True
    bot.snapshots = _RecordingSnapshotWriter()
    return bot


@pytest.fixture
def bot_on_main_menu() -> Callable[..., TowerBot]:
    """Factory: a bot already confirmed on MAIN_MENU, one call per policy.

    auto_navigate is on and the navigation cooldown zeroed (see
    _shopping_bot), so a test asserting "BATTLE was/was not tapped" is
    exercising the suppression itself rather than passing because
    auto_navigate defaulted off.

    Seeded with one already-completed run: a bot idling on the main menu has
    naturally just finished one, and
    test_reaching_the_run_cap_does_not_start_a_visit needs that to be true
    for a `max_runs=1` cap to mean something already reached, rather than a
    limit nothing here would otherwise trip.

    `claims` is an optional keyword defaulting to a disabled Claims(), so
    every caller that predates the claim cadence still gets a bot on which
    _offer_claim() never fires. Tests of the claim cadence itself pass
    their own Claims(enabled=True, ...) - see test_autopilot_loop.py's
    "Claim cadence in the loop" section.
    """
    def build(policy: Shopping, claims: Claims | None = None) -> TowerBot:
        bot = _shopping_bot(
            "main_menu", state=screens.ScreenState.MAIN_MENU,
            policy=policy, auto_navigate=True,
            claims=claims if claims is not None else Claims(),
        )
        bot.runs.completed = 1
        return bot

    return build


@pytest.fixture
def bot_on_game_over() -> Callable[[Shopping], TowerBot]:
    """Factory: a bot confirmed on GAME_OVER, one call per policy.

    The death screen is where the between-runs detour is decided: RETRY
    starts the next run from here without ever passing through MAIN_MENU,
    which is the only screen a Workshop visit can begin from. Seeded with
    one completed run, like bot_on_main_menu, so a cadence of
    visit_every_n_runs=1 has a run to count.
    """
    def build(policy: Shopping) -> TowerBot:
        bot = _shopping_bot(
            "game_over", state=screens.ScreenState.GAME_OVER,
            policy=policy, auto_navigate=True,
        )
        bot.runs.completed = 1
        return bot

    return build


@pytest.fixture
def bot_on_workshop() -> Callable[[Shopping], TowerBot]:
    """Factory: a bot confirmed UNKNOWN (by design - see pages.py) on a
    workshop tab that does not yet show the policy's ATTACK row.

    "menu_workshop_utility" shows the UTILITY tab selected, so a policy whose
    only category is ATTACK finds its row nowhere on screen and keeps
    tapping the ATTACK tab button every scan instead - the tab button itself
    scores ~1.0 regardless of which tab is selected (see shopping.py's
    module docstring), so this is stable across as many scans as a test
    wants to run, never buying, erroring, or returning.
    """
    def build(policy: Shopping) -> TowerBot:
        return _shopping_bot(
            "menu_workshop_utility", state=screens.ScreenState.UNKNOWN,
            policy=policy, auto_navigate=False,
        )

    return build


@pytest.fixture
def bot_in_run() -> Callable[[Shopping], TowerBot]:
    """Factory: a bot confirmed IN_RUN, one call per policy.

    Local to this file rather than reusing test_bot_reporting.py's own
    `bot_in_run` fixture (which returns `(bot, seen)` and takes no policy) -
    pytest resolves each test file's own fixture of the same name first, so
    the two coexist without conflict.
    """
    def build(policy: Shopping) -> TowerBot:
        return _shopping_bot(
            "in_run_lit", state=screens.ScreenState.IN_RUN,
            policy=policy, auto_navigate=False,
        )

    return build


@pytest.fixture
def bot_in_run_on() -> Callable[[str], TowerBot]:
    """Factory: a bot confirmed IN_RUN on a named in-run frame.

    Which tab the frame shows is the point. `screens/in_run.png` is the
    ATTACK header crop, so only an ATTACK frame carries a panel anchor;
    DEFENSE and UTILITY frames are IN_RUN by their cash counter alone and
    classify with `top_left=None`. A test about what the loop does on each
    tab has to be able to pick the tab.
    """
    def build(frame_name: str) -> TowerBot:
        return _shopping_bot(
            frame_name, state=screens.ScreenState.IN_RUN,
            policy=Shopping(), auto_navigate=False,
        )

    return build


@pytest.fixture
def bot_in_run_paused() -> TowerBot:
    """A bot IN_RUN on a frame whose speed widget reads x0.0 - the game
    stopped dead at the widget's bottom step.

    Not a factory like the fixtures above: nothing about this frame concerns
    shopping, so there is no policy to vary. Captured off a live emulator
    rather than constructed, because the whole point of it is that x0.0 is a
    real reading the bot has to recognise and climb out of.
    """
    return _shopping_bot(
        "in_run_paused", state=screens.ScreenState.IN_RUN,
        policy=Shopping(), auto_navigate=False,
    )


@pytest.fixture
def bot_in_run_fast() -> TowerBot:
    """A bot IN_RUN on a frame whose speed widget reads x1.5 - the ceiling on
    the account these fixtures were captured from. The counterpart to
    bot_in_run_paused: one frame above every legal target, one below.
    """
    return _shopping_bot(
        "in_run_fast", state=screens.ScreenState.IN_RUN,
        policy=Shopping(), auto_navigate=False,
    )
