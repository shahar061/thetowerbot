"""The shopping state machine, with no device and no emulator.

Frames come from the committed fixtures, so this exercises the real matcher
and the real digit reader against real pixels - only the tapping is faked.

Every dry-run test asserts the fake device recorded ZERO taps. That is the
property the whole rehearsal rests on, so it is asserted directly rather than
inferred from the events.
"""

from pathlib import Path
from types import SimpleNamespace

import cv2
import dataclasses
import hashlib
import pytest

import config
import db
import digits
import ocr
import tiles
import events
import shopping as shopping_mod
import transactions
import vision
from supervisor import RecoveryPreflightBlocked, RecoveryBlocked
from perception import Observation, ObservedUpgrade
from strategy import CardPolicy, Shopping, ShoppingRule

FIXTURES = Path(__file__).parent / "fixtures"


class _NoRowTemplates(vision.TemplateCache):
    """A template cache with the workshop ROW templates removed.

    Deleted-file behaviour without deleting a file: anything under
    workshop/row_ or workshop/unlock_ raises, everything else (nav buttons,
    tab pictograms, page anchors) loads normally.
    """

    def get(self, name: str):
        if name.startswith("workshop/row_") or name.startswith("workshop/unlock_"):
            raise AssertionError(f"the buy path still reads a row template: {name}")
        return super().get(name)


class FakeDevice:
    """Records taps instead of sending them."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple] = []

    def swipe(self, *args: float) -> None:
        self.swipes.append(args)


@pytest.mark.parametrize("name,upgrade_id,expected_taps", [
    ("Unfamiliar Power", "discovered:unfamiliarpower", 0),
    ("Golden Tower", "discovered:goldentower", 0),
    ("Damage", "damage", 1),
])
def test_only_legacy_identity_can_purchase_an_affordable_observed_row(
    session, monkeypatch, fake_header, name: str, upgrade_id: str, expected_taps: int,
) -> None:
    row = ObservedUpgrade(upgrade_id, name, "ATTACK", "workshop", 1, 5,
                          "available", 1, config.Rect(0, 0, 100, 100), (50, 80))
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", (row,), {}, None, 1, 270))
    policy = a_policy(armed=True, coin_budget=100, workshop=(
        ShoppingRule(name=name, category="ATTACK", target=2),
    ))
    device = FakeDevice()
    session.begin(policy, run_count=1)
    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)
    assert len(device.taps) == expected_taps
    if expected_taps == 0:
        assert any(e.reason == "unknown_identity" for e in session._bus.of_type("PurchaseSkipped"))
        assert row.payload()["upgrade_id"] == upgrade_id


def test_unknown_targets_and_mismatched_observation_identity_fail_closed() -> None:
    row = ObservedUpgrade("discovered:damage", "Damage", "ATTACK", "workshop", 1, 5,
                          "available", 1, config.Rect(0, 0, 100, 100), (50, 80))
    assert shopping_mod._row_named("Damage", (row,), "ATTACK") is None
    assert shopping_mod._target_reached("discovered:damage", 10, 2) is False


@pytest.fixture(autouse=True)
def capture_taps(monkeypatch):
    """device.tap is module-level in shopping.py, so patch it there."""
    def _tap(device, x, y):
        device.taps.append((x, y))

    monkeypatch.setattr(shopping_mod, "tap", _tap)


@pytest.fixture
def fake_header(monkeypatch):
    """Force what the session believes the balances are.

    Patched at module scope in shopping.py, not by setting session state: the
    session re-reads the header every step, so an attribute poked before
    advance() is overwritten before anything reads it. A test that sets
    _coins and then asserts on affordability is asserting nothing.
    """
    values = {"coins": 1770, "gems": 40}

    def _header(screen, page, top_left):
        return values["coins"], values["gems"]

    monkeypatch.setattr(shopping_mod, "header_numbers", _header)
    return values


class Recorder:
    """A bus that keeps what it was given."""

    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event):
        self.published.append(event)
        return event

    def of_type(self, name: str):
        return [e for e in self.published if e.type == name]


def frame(name: str):
    img = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    assert img is not None, f"missing fixture: {name}.png"
    return img


def a_policy(**over) -> Shopping:
    base = dict(
        enabled=True,
        armed=False,
        coin_budget=10_000,
        allow_unlocks=True,
        workshop=(
            ShoppingRule(name="Unlock Cash Bonuses", category="UTILITY"),
            ShoppingRule(name="Health", category="DEFENSE"),
            ShoppingRule(name="Damage", category="ATTACK"),
        ),
    )
    return Shopping(**{**base, **over})


@pytest.fixture
def session():
    return shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
    )


# -- starting a visit ------------------------------------------------------
def test_a_disabled_policy_never_starts_a_visit(session) -> None:
    assert session.begin(a_policy(enabled=False), run_count=1) is False
    assert session.active is False


def test_an_enabled_policy_starts_a_visit(session) -> None:
    assert session.begin(a_policy(), run_count=1) is True
    assert session.active is True


def test_the_cadence_skips_runs_between_visits(session) -> None:
    policy = a_policy(visit_every_n_runs=3)
    assert session.begin(policy, run_count=1) is True
    session.reset()
    assert session.begin(policy, run_count=2) is False
    assert session.begin(policy, run_count=3) is False
    assert session.begin(policy, run_count=4) is True


def test_a_policy_with_no_enabled_rows_and_no_cards_starts_nothing(session) -> None:
    """A visit that would buy nothing is a minute of tab-tapping for free."""
    policy = a_policy(workshop=(), cards=CardPolicy(enabled=False))
    assert session.begin(policy, run_count=1) is False


def test_begin_opens_cards_directly_when_only_cards_are_enabled(session) -> None:
    """The untested `else` of begin()'s step assignment: no workshop rows to
    visit, so the visit should go straight to the Cards page rather than
    stopping at Workshop for nothing."""
    policy = a_policy(workshop=(), cards=CardPolicy(enabled=True))
    assert session.begin(policy, run_count=1) is True
    assert session._step is shopping_mod.Step.OPEN_CARDS


def test_a_permanently_disabled_session_announces_it_only_once(session) -> None:
    """begin() must say why nothing is happening the first time it declines
    for disabled_reason, and stay silent every time after - the reason never
    changes once the process has started, so a publish per scan would just
    flood the feed with the same fact forever."""
    session.disabled_reason = "header atlas is missing 2, 3, 5, 6, 9"
    for _ in range(5):
        assert session.begin(a_policy(), run_count=1) is False
    unavailable = session._bus.of_type("ShoppingUnavailable")
    assert len(unavailable) == 1
    assert unavailable[0].reason == session.disabled_reason


# -- turning shopping off mid-visit -----------------------------------------
def test_disabling_shopping_mid_visit_ends_it_instead_of_continuing(session) -> None:
    """The critical bug this closes: shopping.enabled was checked only in
    begin(), never in advance(), so unticking "Shop between runs" while a
    visit was under way left the bot tapping through it to completion. The
    fix routes a disabled policy through the same abort path any other wedge
    takes - a best-effort return tap and an honest ShoppingEnded - rather
    than a silent `_step = IDLE`.
    """
    device = FakeDevice()
    policy = a_policy(enabled=True, armed=False)
    session.begin(policy, run_count=1)
    assert session.active is True

    turned_off = a_policy(enabled=False, armed=False)
    session.advance(frame("menu_workshop_utility"), device, turned_off)

    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted
    assert ended[-1].reason == "shopping disabled"
    assert session.active is False
    assert device.taps == [], "unarmed - even the recovery tap must not reach the device"


# -- reading the header ----------------------------------------------------
def test_the_header_reads_coins_and_gems_off_a_workshop_frame() -> None:
    """Real engine, real fixture. The two balances sit on one header row, so
    this is also what proves each region keeps to its own number."""
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    screen = frame("menu_workshop_attack")
    _, top_left = vision.best_score(screen, cache.get(config.PAGE_ANCHORS["WORKSHOP"]))
    coins, gems = shopping_mod.header_numbers(screen, "WORKSHOP", top_left)
    assert coins == 1770
    assert gems == 40


def test_the_header_reads_a_balance_the_glyph_atlas_could_not(monkeypatch) -> None:
    """2, 3 and 5 are glyphs templates/atlas/header/ has never held, so a
    balance containing them read as None through the atlas and aborted the
    visit. Reading the header with OCR is what retires that failure - and
    with it the harvesting session build_shopping() used to demand.
    """
    top_left = (32, 244)
    coins_region, _gems_region = config.HEADER_REGIONS["WORKSHOP"]
    coins_rect = shopping_mod._absolute(coins_region, top_left)

    def _read_region(screen, region, **kwargs):
        if region != coins_rect:
            return ()
        return (ocr.TextBox(text="2.35K", confidence=0.99, rect=config.Rect(20, 20, 130, 46)),)

    monkeypatch.setattr(shopping_mod.ocr, "read_region", _read_region)
    coins, gems = shopping_mod.header_numbers(None, "WORKSHOP", top_left)
    assert coins == 2350
    assert gems is None, "nothing was read in the gem region"


def test_a_single_digit_gem_balance_is_read() -> None:
    """The regression. A whole-frame read returns no box at all for a lone
    "0" - not a low-confidence one, none - so gems came back None and took
    the visit with it. Real engine, real fixture, no monkeypatching."""
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    screen = frame("menu_main")
    _, top_left = vision.best_score(screen, cache.get(config.PAGE_ANCHORS["MAIN_MENU"]))

    coins, gems = shopping_mod.header_numbers(screen, "MAIN_MENU", top_left)

    assert coins == 78
    assert gems == 0, "a zero balance is a balance, not an unreadable header"


def test_a_region_holding_two_numbers_still_refuses(monkeypatch) -> None:
    """Per-region crops did not relax the one-number rule. A crop that
    catches both balances is a crop measured wrong, and spending against the
    wrong one is worse than not shopping."""
    def _read_region(screen, region, **kwargs):
        return (
            ocr.TextBox(text="1770", confidence=0.99, rect=config.Rect(20, 20, 90, 46)),
            ocr.TextBox(text="40", confidence=0.99, rect=config.Rect(140, 20, 50, 46)),
        )

    monkeypatch.setattr(shopping_mod.ocr, "read_region", _read_region)
    assert shopping_mod.header_numbers(None, "WORKSHOP", (32, 244)) == (None, None)


def test_the_header_reads_nothing_off_a_page_that_has_no_header() -> None:
    """MISSIONS, or a frame that failed to classify. Returning a pair of
    Nones rather than raising is what lets the caller treat "no header here"
    and "unreadable header" as the same refusal."""
    assert shopping_mod.header_numbers(None, "MISSIONS", (32, 244)) == (None, None)


# -- page transitions -------------------------------------------------------
def test_a_page_transition_publishes_page_changed(session) -> None:
    """events.PageChanged is defined and store-tested but was never actually
    published anywhere - this is that wiring, on the transition a real
    visit makes crossing from the main menu onto the Workshop page."""
    device = FakeDevice()
    policy = a_policy()
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)  # taps into WORKSHOP
    session.advance(frame("menu_workshop_utility"), device, policy)

    changed = session._bus.of_type("PageChanged")
    assert len(changed) == 1
    assert changed[0].prev_page == "MAIN_MENU"
    assert changed[0].curr_page == "WORKSHOP"


def test_the_first_frame_of_a_visit_publishes_no_transition(session) -> None:
    """There is nothing to have changed FROM on the very first frame -
    ShoppingStarted already marks the beginning, so this must not also fire
    a PageChanged from some leftover state."""
    device = FakeDevice()
    policy = a_policy()
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    assert session._bus.of_type("PageChanged") == []


# -- the dry run taps nothing ---------------------------------------------
def test_a_whole_unarmed_visit_taps_nothing(session) -> None:
    """The property the rehearsal exists for, asserted directly."""
    device = FakeDevice()
    policy = a_policy(armed=False)
    session.begin(policy, run_count=1)
    for name in ("menu_main", "menu_workshop_utility", "menu_workshop_defense",
                 "menu_workshop_attack", "menu_cards", "menu_main"):
        for _ in range(4):
            session.advance(frame(name), device, policy)
    assert device.taps == []


def test_an_unarmed_visit_still_reports_what_it_would_buy(session) -> None:
    device = FakeDevice()
    policy = a_policy(armed=False)
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_utility"), device, policy)
    session.advance(frame("menu_workshop_utility"), device, policy)
    purchases = session._bus.of_type("Purchased")
    assert purchases, "a rehearsal that reports nothing proves nothing"
    assert all(p.dry_run for p in purchases)
    assert purchases[0].item == "Unlock Cash Bonuses"
    assert purchases[0].price == 40


# -- the buying rule -------------------------------------------------------
def test_the_highest_priority_affordable_row_wins_not_the_cheapest(session) -> None:
    """Order is the whole policy. Critical Chance costs 50 and Damage 30;
    with Critical Chance listed first it must be the one chosen."""
    device = FakeDevice()
    policy = a_policy(workshop=(
        ShoppingRule(name="Critical Chance", category="ATTACK"),
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)
    bought = session._bus.of_type("Purchased")
    assert bought[0].item == "Critical Chance"


def test_a_row_is_bought_without_its_template_file(session, fake_header) -> None:
    """Nothing in the workshop buy path reads a row template any more.

    The rule no longer even has a `template` field to carry, so this swaps
    in a cache that raises if the buy path asks for one anyway: if any code
    path still loads a row template, the purchase cannot happen. Tab and nav
    templates are untouched and still load - only the ROW templates are gone.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    session._templates = _NoRowTemplates(config.TEMPLATE_DIR)
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)
    assert not session._bus.of_type("Purchased"), "a tap is not yet a purchase"
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)

    bought = session._bus.of_type("Purchased")
    assert bought and bought[0].item == "Damage"
    assert bought[0].price == 30


def test_a_row_costing_more_than_the_balance_is_skipped_as_unaffordable(
    session, fake_header
) -> None:
    """Poking session._coins directly would assert nothing: BUY_ROWS's
    re-read-every-step rule overwrites it before anything sees it.
    fake_header is what actually makes the session believe the balance is 10.

    A single ATTACK-only policy (rather than the brief's three-category
    default) is used so the two advance() calls land the session in
    BUY_ROWS on the very frame this test controls, instead of stalling on a
    tab-switch toward a different, unvisited category first.
    """
    device = FakeDevice()
    policy = a_policy(workshop=(
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    fake_header["coins"] = 10
    session.advance(frame("menu_workshop_attack"), device, policy)
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "unaffordable" for s in skips)


def test_an_unaffordable_skip_reports_the_balance_that_refused_it(
    session, fake_header
) -> None:
    """The skip IS the balance reading. Most rows on this account are
    unaffordable, so a visit that buys nothing is the common one - and
    without this the ledger would learn a balance only on the rare visit
    that bought something."""
    device = FakeDevice()
    policy = a_policy(workshop=(
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    fake_header["coins"] = 10
    session.advance(frame("menu_workshop_attack"), device, policy)

    skips = session._bus.of_type("PurchaseSkipped")
    unaffordable = [s for s in skips if s.reason == "unaffordable"]
    assert unaffordable
    for skip in unaffordable:
        assert skip.coins_before == 10
        # The currency it did not spend stays None rather than being reused,
        # because the ledger reads the currency off whichever field is set.
        assert skip.gems_before is None


def test_an_unreadable_balance_stops_the_visit_rather_than_guessing(
    session, fake_header
) -> None:
    """See the comment on
    test_a_row_costing_more_than_the_balance_is_skipped_as_unaffordable for
    why fake_header replaces a direct session._coins poke, and why this
    uses a single-category policy.

    There is no brightness fallback on a menu page, and guessing is the
    failure mode that costs coins.
    """
    device = FakeDevice()
    policy = a_policy(workshop=(
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    fake_header["coins"] = None
    session.begin(policy, run_count=1)
    session.advance(frame("menu_workshop_attack"), device, policy)
    ended = session._bus.of_type("ShoppingEnded")
    skipped = session._bus.of_type("PurchaseSkipped")
    assert ended or any(s.reason == "unreadable" for s in skipped)
    assert device.taps == []


def test_a_row_whose_price_cannot_be_read_is_skipped_rather_than_guessed(
    session, monkeypatch
) -> None:
    """The row's own unreadable-PRICE branch, distinct from an unreadable
    balance: coins read fine (real header, not faked), the row is found, and
    its price is not.

    This used to be reached by declaring the wrong `layout` on a rule, which
    made the template reader crop the price from garbage pixels. Addressing
    rows by name off OCR deletes that route - the price comes from the tile
    the name was found in, so there is no offset left to get wrong (spec §7:
    the mode stops existing when tiles are detected rather than assumed).
    The branch itself still matters: a tile whose price box is missing or
    unparseable yields Row.price None, and a refused read must never become
    a guessed purchase.
    """
    device = FakeDevice()
    priceless = ObservedUpgrade("unlock_cash_bonuses", "Unlock Cash Bonuses", "UTILITY",
                                "workshop", None, None, "unreadable", 1,
                                tiles.Rect(30, 500, 1020, 196), None)
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("UTILITY", (priceless,), {}, None, 1, 270))
    policy = a_policy(workshop=(
        ShoppingRule(name="Unlock Cash Bonuses",
                     category="UTILITY"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_workshop_utility"), device, policy)
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "unreadable" and s.detail == "price" for s in skips)
    assert device.taps == []


# -- category order --------------------------------------------------------
def test_tabs_are_visited_in_the_order_the_rows_imply(session) -> None:
    policy = a_policy()
    session.begin(policy, run_count=1)
    assert session.remaining_categories() == ["UTILITY", "DEFENSE", "ATTACK"]


def test_tutorial_arrow_does_not_route_defense_tap_to_uw_tab(session) -> None:
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Health", category="DEFENSE", target=2),
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.OPEN_TAB
    device = FakeDevice()
    screen = frame("menu_workshop_utility_tutorial_arrow")

    session._open_tab(SimpleNamespace(page="WORKSHOP"), screen, device, policy)

    assert device.taps == [(405, 2146)]


# -- bailing out -----------------------------------------------------------
def test_the_tap_budget_ends_the_visit(session) -> None:
    """Exactly 2 (the tab-switch attempts that spend the budget) plus 1 (the
    recovery tap on the way out, which is deliberately NOT bound by the same
    cap). A loose `<= 2` bound would also pass if the code made zero taps,
    which hides a too-few-taps bug more serious than a too-many-taps one.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, max_taps_per_visit=2)
    session.begin(policy, run_count=1)
    for _ in range(20):
        session.advance(frame("menu_workshop_attack"), device, policy)
    assert len(device.taps) == 3
    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted


def test_a_budget_exhausted_abort_still_attempts_the_return_tap(session) -> None:
    """Round-2 fix (Critical 1): without this, a budget-exhaustion abort -
    the most common abort there is, since the tap budget is the primary
    safety valve - left the bot stranded IDLE on a menu page forever: the
    scan loop no-ops on IDLE, and navigate.Navigator cannot rescue it either
    (it only acts on GAME_OVER and MAIN_MENU, and a menu page classifies
    UNKNOWN to the screen tracker by design).
    """
    device = FakeDevice()
    policy = a_policy(armed=True, max_taps_per_visit=1)
    session.begin(policy, run_count=1)

    session.advance(frame("menu_workshop_attack"), device, policy)
    assert len(device.taps) == 1, "the single tap the budget allows"

    session.advance(frame("menu_workshop_attack"), device, policy)
    assert len(device.taps) == 2, "the recovery tap, one over the cap of 1"
    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted
    assert session.active is False


def test_a_double_failure_during_abort_recovery_does_not_crash_the_scan_loop(
    session, monkeypatch
) -> None:
    """Round-2 fix (Critical 2): advance()'s except handler calls _abort,
    which re-runs vision.locate_template against the SAME screen that may
    have just caused the original exception (see _exit_to_battle). If that
    lookup also raises, the second exception used to propagate straight out
    of advance() - breaking the module docstring's own promise that a
    shopping step never crashes the scan loop.
    """
    device = FakeDevice()
    policy = a_policy()
    session.begin(policy, run_count=1)

    def _raise_classify(screen, cache, threshold=None):
        raise RuntimeError("boom: classify")

    def _raise_locate(screen, template, threshold):
        raise RuntimeError("boom: locate")

    monkeypatch.setattr(shopping_mod.pages, "classify_page", _raise_classify)
    monkeypatch.setattr(shopping_mod.vision, "locate_template", _raise_locate)

    session.advance(frame("menu_main"), device, policy)  # must not raise

    assert session.active is False
    assert device.taps == []


def test_an_unexpected_page_twice_running_ends_the_visit(session) -> None:
    """Once is an animation. Twice is lost."""
    device = FakeDevice()
    policy = a_policy()
    session.begin(policy, run_count=1)
    session.advance(frame("in_run_lit"), device, policy)
    assert session.active is True, "one odd frame is not enough to give up"
    session.advance(frame("in_run_lit"), device, policy)
    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted
    assert session.active is False


def test_a_missing_nav_target_ends_the_visit_after_two_misses(session) -> None:
    """A positioning step that cannot find its target must not spin
    silently forever with the tap budget untouched and the event feed
    empty - it is a leg of an errand supposed to be making progress, not an
    opportunistic Navigator tap.

    menu_missions has no bottom tab bar at all, so NAV_TARGETS["BATTLE_TAB"]
    never matches there (measured: 0.31, nowhere near the 0.8 threshold) -
    exactly the "wrong crop, or the game moved the button" scenario this
    guards against.
    """
    device = FakeDevice()
    policy = a_policy()
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.RETURN

    session.advance(frame("menu_missions"), device, policy)
    assert session.active is True, "one miss is not enough to give up"
    assert device.taps == []

    session.advance(frame("menu_missions"), device, policy)
    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted
    assert "BATTLE_TAB" in ended[-1].reason
    assert session.active is False
    assert device.taps == []


def test_a_finished_visit_returns_to_idle(session) -> None:
    device = FakeDevice()
    policy = a_policy(cards=CardPolicy(enabled=False))
    session.begin(policy, run_count=1)
    for name in ("menu_main", "menu_workshop_utility", "menu_workshop_defense",
                 "menu_workshop_attack", "menu_main"):
        for _ in range(6):
            session.advance(frame(name), device, policy)
    assert session.active is False


# -- cards -----------------------------------------------------------------
def test_cards_are_not_bought_when_the_policy_is_off(session) -> None:
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(enabled=False))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    session.advance(frame("menu_cards"), device, policy)
    assert device.taps == []


def test_a_dismiss_popup_is_tapped_before_reading_the_cards_page(
    session, monkeypatch
) -> None:
    """OPEN_CARDS's popup-dismiss branch, over config.NAV_DISMISS.

    No committed fixture happens to show a first-visit popup mid-flight
    (measured directly: the highest any NAV_DISMISS template scores against
    any committed fixture is 0.624, nowhere near the 0.8 threshold), so the
    popup MATCH is faked here - the same way fake_header fakes a balance no
    fixture happens to show - while the real state machine and the real tap
    path are exercised on a real frame.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(enabled=True))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.OPEN_CARDS

    real_locate = shopping_mod.vision.locate_template
    dismiss_template = session._templates.get(config.NAV_DISMISS[0])

    def _locate(screen, template, threshold):
        if template is dismiss_template:
            return vision.Match(center=(500, 600), score=1.0, top_left=(400, 550))
        return real_locate(screen, template, threshold)

    monkeypatch.setattr(shopping_mod.vision, "locate_template", _locate)

    session.advance(frame("menu_cards"), device, policy)
    assert device.taps == [(500, 600)]
    assert session._step is shopping_mod.Step.OPEN_CARDS, (
        "a dismiss tap is not arrival - the next scan re-checks the page"
    )


def test_a_missing_card_button_is_skipped_as_no_match(session) -> None:
    """Cards' no_match branch: the configured batch's button template is
    simply not on screen (a real frame with no card buttons at all, rather
    than a faked miss)."""
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(enabled=True))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    session.advance(frame("menu_workshop_attack"), device, policy)
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "no_match" for s in skips)
    assert device.taps == []


def test_a_card_batch_costing_more_than_the_balance_is_skipped_as_unaffordable(
    session, fake_header
) -> None:
    """Cards' plain unaffordable branch, distinct from the gem-floor
    ("capped") case: the batch costs more than the whole balance, not just
    more than the balance minus the floor."""
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(enabled=True, gem_floor=0))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 10  # x1 costs 20
    session.advance(frame("menu_cards"), device, policy)
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "unaffordable" for s in skips)
    assert device.taps == []


def test_unreadable_gems_stop_the_visit_rather_than_guessing(session, fake_header) -> None:
    """Cards' own unreadable-balance branch - the gem analogue of
    test_an_unreadable_balance_stops_the_visit_rather_than_guessing.

    Unarmed (as that test is): an abort still attempts the recovery tap
    (round-2 fix, Critical 1), and with armed=True that tap would actually
    reach the fake device - this test is about the abort firing, not about
    the recovery tap, so it stays unarmed like its coin counterpart.
    """
    device = FakeDevice()
    policy = a_policy(cards=CardPolicy(enabled=True))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = None
    session.advance(frame("menu_cards"), device, policy)
    ended = session._bus.of_type("ShoppingEnded")
    assert ended and ended[-1].aborted
    assert device.taps == []


def test_the_gem_floor_stops_card_buying(session, fake_header) -> None:
    """40 gems, a 20-gem card and a floor of 40: buying would breach it.

    fake_header replaces a direct session._gems poke, for the same reason
    as the workshop tests above.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(enabled=True, gem_floor=40))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 40
    session.advance(frame("menu_cards"), device, policy)
    assert device.taps == []
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "capped" for s in skips)


def test_card_buying_stops_at_the_per_visit_cap(session, fake_header) -> None:
    """fake_header replaces a direct session._gems poke.

    The balance now has to actually fall for the first buy to count - a
    frozen 400 gems used to be enough, back when a tap was its own proof.
    It is dropped once, by the price, and then held: the cap, not a missing
    confirmation, is what has to stop the rest.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    session.advance(frame("menu_cards"), device, policy)  # the tap
    fake_header["gems"] = 380  # the x1 batch costs 20
    for _ in range(4):
        session.advance(frame("menu_cards"), device, policy)
    bought = [e for e in session._bus.of_type("Purchased") if e.category == "CARDS"]
    assert len(bought) == 1


def test_a_card_tap_is_not_a_purchase_until_the_gems_move(
    session, fake_header
) -> None:
    """A card tap buys nothing on its own.

    The workshop path has always required a second frame before it called a
    tap a purchase; the card path published `Purchased` on the same frame
    it tapped, on the strength of "the button was where the template said
    it would be". A tap a modal swallowed was recorded as a gem debit that
    never happened.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400

    session.advance(frame("menu_cards"), device, policy)

    assert device.taps, "400 gems against a 20-gem batch: it should have tapped"
    assert [e for e in session._bus.of_type("Purchased") if e.category == "CARDS"] == [], (
        "the gems have not been re-read yet, so nothing proves a card was opened"
    )


def test_a_card_purchase_is_recorded_once_the_gems_fall_by_its_price(
    session, fake_header
) -> None:
    """The other half: real evidence does confirm it, exactly once."""
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    session.advance(frame("menu_cards"), device, policy)

    fake_header["gems"] = 380  # the x1 batch costs 20
    session.advance(frame("menu_cards"), device, policy)

    bought = [e for e in session._bus.of_type("Purchased") if e.category == "CARDS"]
    assert len(bought) == 1
    assert bought[0].price == 20


def test_a_card_tap_left_unanswered_cannot_be_confirmed_by_a_later_visit(
    session, fake_header
) -> None:
    """A pending tap belongs to the visit that made it.

    Gems move between visits for reasons that have nothing to do with a
    card - missions, rewards, the player. A stale pending that survived
    would eventually meet a balance matching its arithmetic and record a
    purchase for a debit that came from somewhere else entirely.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    session.advance(frame("menu_cards"), device, policy)  # tapped, never answered
    session.reset()

    assert session.begin(policy, run_count=2) is True
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 380  # exactly what the abandoned pending was waiting for
    session.advance(frame("menu_cards"), device, policy)

    bought = [e for e in session._bus.of_type("Purchased") if e.category == "CARDS"]
    assert bought == [], (
        "a balance read in a new visit cannot answer for the old visit's tap"
    )


def test_a_card_tap_is_journalled_before_it_is_sent(tmp_path, fake_header) -> None:
    """The durable half. `_pending_card` dies with the process; the journal
    row is what a restart can still read."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    session = shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
        journal=journal,
    )
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400

    session.advance(frame("menu_cards"), device, policy)

    still_open = journal.open_transactions()
    assert [t.item for t in still_open] == ["x1"]
    assert still_open[0].stage == transactions.Stage.ACTED
    assert still_open[0].price == 20


def test_card_preflight_denial_closes_unsent_intent(tmp_path, monkeypatch,
                                                   fake_header) -> None:
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    session = shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(), reader=digits.NumberReader(), journal=journal,
    )
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1,
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    monkeypatch.setattr(shopping_mod, "tap", lambda *_: (_ for _ in ()).throw(
        RecoveryPreflightBlocked("fresh_evidence")))

    session._buy_cards(SimpleNamespace(page="CARDS", top_left=None),
                       frame("menu_cards"), FakeDevice(), policy)

    assert journal.open_transactions() == ()
    assert session._pending_card is None


def test_a_tap_a_dead_process_left_open_is_not_sent_again(tmp_path, fake_header) -> None:
    """The acceptance gate, end to end.

    A previous process tapped and died. The gems may or may not be gone;
    from here that is unknowable. Tapping again risks paying twice, so this
    visit does not tap at all.
    """
    path = tmp_path / "bot.db"
    dead = transactions.TransactionJournal(path)
    txn = dead.open(transactions.Intent(
        item="x1", category="CARDS", currency="gems", price=20,
        wallet_before=400, ts=1.0,
    ))
    dead.record_action(txn.key, at=1.0)

    session = shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
        journal=transactions.TransactionJournal(path),
    )
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400

    session.advance(frame("menu_cards"), device, policy)

    assert device.taps == [], (
        "an unreconciled attempt may already have spent these gems"
    )
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "unreconciled" for s in skips)


def test_a_visit_that_ended_cleanly_does_not_block_the_next_one(
    tmp_path, fake_header
) -> None:
    """The counterweight to the refusal above.

    A crash leaves an attempt open because nothing got the chance to close
    it, and that is what blocks the next process. A visit that ends in an
    orderly way did get that chance, so it takes it - otherwise one
    unconfirmed card would stop the bot from ever shopping again.
    """
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    session = shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
        journal=journal,
    )
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    session.advance(frame("menu_cards"), device, policy)  # tapped, unanswered
    session.reset()

    assert journal.open_transactions() == (), (
        "an orderly end answers what it opened, even if the answer is "
        "'nobody knows'"
    )

    assert session.begin(policy, run_count=2) is True
    session._step = shopping_mod.Step.BUY_CARDS
    session.advance(frame("menu_cards"), device, policy)
    assert len(device.taps) == 2


def _journalled_workshop_session(path, monkeypatch):
    """A session over a real journal, with one affordable ATTACK row."""
    row = ObservedUpgrade("damage", "Damage", "ATTACK", "workshop", 1, 5,
                          "available", 1, config.Rect(0, 0, 100, 100), (50, 80))
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", (row,), {}, None, 1, 270))
    return shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
        journal=transactions.TransactionJournal(path),
    )


def _workshop_policy():
    return a_policy(armed=True, coin_budget=100, workshop=(
        ShoppingRule(name="Damage", category="ATTACK", target=2),
    ))


def test_a_workshop_tap_is_journalled_before_it_is_sent(tmp_path, monkeypatch,
                                                        fake_header) -> None:
    """The workshop row is the bot's main spender; it gets the same durable
    record the card path does."""
    path = tmp_path / "bot.db"
    session = _journalled_workshop_session(path, monkeypatch)
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert len(device.taps) == 1
    still_open = session.journal.open_transactions()
    assert [(t.item, t.stage, t.price) for t in still_open] == [
        ("Damage", transactions.Stage.ACTED, 5)
    ]


def test_workshop_preflight_denial_closes_unsent_intent(tmp_path, monkeypatch,
                                                       fake_header) -> None:
    path = tmp_path / "bot.db"
    session = _journalled_workshop_session(path, monkeypatch)
    policy = _workshop_policy()
    session.begin(policy, run_count=1)
    monkeypatch.setattr(shopping_mod, "tap", lambda *_: (_ for _ in ()).throw(
        RecoveryPreflightBlocked("fresh_evidence")))

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), FakeDevice(), policy)

    assert session.journal.open_transactions() == ()
    assert session._pending is None


def test_workshop_unknown_tap_outcome_keeps_intent(tmp_path, monkeypatch,
                                                    fake_header) -> None:
    path = tmp_path / "bot.db"
    session = _journalled_workshop_session(path, monkeypatch)
    policy = _workshop_policy()
    session.begin(policy, run_count=1)

    def unknown_outcome(*_):
        raise RecoveryBlocked("action outcome unknown")

    monkeypatch.setattr(shopping_mod, "tap", unknown_outcome)
    with pytest.raises(RecoveryBlocked):
        session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                          frame("menu_workshop_attack"), FakeDevice(), policy)

    assert session.journal.open_transactions()[0].stage is transactions.Stage.INTENDED


def test_a_workshop_tap_a_dead_process_left_open_is_not_sent_again(
    tmp_path, monkeypatch, fake_header
) -> None:
    """The acceptance gate on the path that spends coins."""
    path = tmp_path / "bot.db"
    dead = transactions.TransactionJournal(path)
    txn = dead.open(transactions.Intent(
        item="Damage", category="ATTACK", currency="coins", price=5,
        wallet_before=1770, ts=1.0,
    ))
    dead.record_action(txn.key, at=1.0)

    session = _journalled_workshop_session(path, monkeypatch)
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert device.taps == [], (
        "those coins may already be spent by the process that died"
    )


def _row(upgrade_id: str, name: str, price: int) -> ObservedUpgrade:
    return ObservedUpgrade(upgrade_id, name, "ATTACK", "workshop", 1, price,
                           "available", 1, config.Rect(0, 0, 100, 100), (50, 80))


class _Page:
    """A Workshop page whose rows a test can change between frames."""

    def __init__(self, monkeypatch, *rows: ObservedUpgrade) -> None:
        self.rows = rows
        monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                            Observation("ATTACK", self.rows, {}, None, 1, 270))


def _step(session, device, policy) -> None:
    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)


def _restart_workshop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                      fake_header: dict) -> tuple:
    from account_state import AccountRepository, AccountState
    path = tmp_path / "bot.db"
    clock = [10.]
    monkeypatch.setattr(shopping_mod.time, "time", lambda: clock[0])
    page = _Page(monkeypatch, dataclasses.replace(_row("damage", "Damage", 5), confidence=.99))
    screen = frame("menu_workshop_attack")

    def observe(*_: object) -> Observation:
        clock[0] += .01  # OCR completes after the recovery scan starts.
        return Observation("ATTACK", tuple(dataclasses.replace(r, observed_at=clock[0]) for r in page.rows),
                           {}, None, clock[0], 270, context="workshop",
                           frame_digest=hashlib.sha256(screen.tobytes()).hexdigest(),
                           frame_width=screen.shape[1], frame_height=screen.shape[0])

    monkeypatch.setattr(shopping_mod, "observe_frame", observe)
    def session() -> shopping_mod.ShoppingSession:
        value = shopping_mod.ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Recorder(),
                                            digits.NumberReader(), journal=transactions.TransactionJournal(path))
        value.account_state = AccountState(AccountRepository(path))
        return value

    dead = session()
    policy = _workshop_policy()
    dead.begin(policy, 1)
    device = FakeDevice()
    _step(dead, device, policy)
    assert len(device.taps) == 1
    restarted = session()
    restarted.begin(policy, 1)
    clock[0] = 20.
    return restarted, page, clock, path


def test_restart_workshop_proof_updates_account_ledger_and_visit_without_a_tap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_header: dict,
) -> None:
    session, page, clock, path = _restart_workshop(tmp_path, monkeypatch, fake_header)
    page.rows = (dataclasses.replace(page.rows[0], value=2, price=6),)
    fake_header["coins"] = 1765
    device = FakeDevice()
    policy = _workshop_policy()
    _step(session, device, policy)
    clock[0] += 1
    _step(session, device, policy)
    assert device.taps == []
    assert session.journal.open_transactions() == ()
    assert (session._bought, session._spent, session._coin_spent, session._visit_coins) == (1, 5, 5, 1770)
    purchase, = session._bus.of_type("Purchased")
    assert (purchase.verdict, purchase.spent) == ("bought", 5)
    revision = session.account_state.snapshot()["revision"]
    assert revision["workshop_stats"][0]["value"] == 2
    with db.reader(path) as conn:
        assert conn.execute("SELECT delta, balance_after FROM ledger").fetchone()[:] == (-5, 1765)
    _step(session, device, policy)
    assert device.taps == []


@pytest.mark.parametrize("fault", ["missing_wallet", "unchanged", "wrong_identity", "low_confidence", "wrong_context", "stale", "geometry", "income"])
def test_restart_ambiguous_workshop_stays_blocked_across_resets_and_visits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_header: dict, fault: str,
) -> None:
    session, page, clock, path = _restart_workshop(tmp_path, monkeypatch, fake_header)
    page.rows = (dataclasses.replace(page.rows[0], value=2, price=6),)
    fake_header["coins"] = 1765
    if fault == "missing_wallet":
        fake_header["coins"] = None
    elif fault == "unchanged":
        page.rows = (dataclasses.replace(page.rows[0], value=1, price=5),)
    elif fault == "wrong_identity":
        page.rows = (dataclasses.replace(page.rows[0], upgrade_id="health"),)
    elif fault == "low_confidence":
        page.rows = (dataclasses.replace(page.rows[0], confidence=.2),)
    elif fault == "income":
        fake_header["coins"] = 1768
    else:
        observe = shopping_mod.observe_frame
        changes = {"wrong_context": {"context": "battle"}, "stale": {"observed_at": 10.},
                   "geometry": {"frame_width": 1}}[fault]
        monkeypatch.setattr(shopping_mod, "observe_frame", lambda *args: dataclasses.replace(observe(*args), **changes))
    device = FakeDevice()
    policy = _workshop_policy()
    _step(session, device, policy)
    clock[0] += 1
    _step(session, device, policy)
    session.reset()
    assert session.begin(policy, 2)
    # A menu navigation or modal acknowledgement must also be refused.
    session.advance(frame("menu_cards"), device, policy)
    assert device.taps == []
    assert transactions.TransactionJournal(path).open_transactions()[0].stage == transactions.Stage.ACTED
    assert session._spent is None
    assert session._bus.of_type("Purchased") == []


def test_restart_without_its_proof_page_closes_unproven_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_header: dict,
) -> None:
    session, page, clock, path = _restart_workshop(tmp_path, monkeypatch, fake_header)
    device = FakeDevice()
    policy = _workshop_policy()
    off_page = SimpleNamespace(page="main_menu", top_left=None)
    assert session._recover_transaction(off_page, frame("menu_cards"))
    clock[0] += shopping_mod.RECOVERY_TIMEOUT_SECONDS - 1
    assert session._recover_transaction(off_page, frame("menu_cards"))
    assert session.reconciliation_pending
    clock[0] += 1
    assert session._recover_transaction(off_page, frame("menu_cards"))
    assert device.taps == []
    assert not session.reconciliation_pending
    txn, outcome = transactions.TransactionJournal(path).recovered_visit()[0]
    assert (outcome.verdict, outcome.spent) == (transactions.Verdict.UNPROVEN, None)
    assert session._spent is None
    assert "Damage" in session._exhausted
    skip = session._bus.of_type("PurchaseSkipped")[-1]
    assert skip.reason == "unproven"


def test_restart_card_wallet_proof_counts_against_card_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         fake_header: dict) -> None:
    path = tmp_path / "bot.db"
    dead = transactions.TransactionJournal(path)
    txn = dead.open(transactions.Intent(item="x1", category="CARDS", currency="gems", price=20,
                                        wallet_before=400, ts=1.))
    dead.record_action(txn.key, at=1.)
    clock = [20.]
    monkeypatch.setattr(shopping_mod.time, "time", lambda: clock[0])
    session = shopping_mod.ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Recorder(),
        digits.NumberReader(), journal=transactions.TransactionJournal(path))
    policy = a_policy(armed=True, workshop=(), cards=CardPolicy(enabled=True, gem_floor=0, max_per_visit=1))
    session.begin(policy, 1)
    fake_header["gems"] = 380
    device = FakeDevice()
    session.advance(frame("menu_cards"), device, policy)
    clock[0] += 1
    session.advance(frame("menu_cards"), device, policy)
    assert device.taps == []

    assert session.journal.open_transactions() == ()
    assert (session._cards_bought, session._spent) == (1, 20)
    session._buy_cards(SimpleNamespace(page="CARDS", top_left=None), frame("menu_cards"), device, policy)
    assert device.taps == []


def test_restart_reader_failure_cannot_trigger_an_abort_tap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_header: dict,
) -> None:
    session, page, clock, path = _restart_workshop(tmp_path, monkeypatch, fake_header)

    def fail(*_: object) -> Observation:
        raise RuntimeError("reader failed during restart")

    monkeypatch.setattr(shopping_mod, "observe_frame", fail)
    device = FakeDevice()
    session.advance(frame("menu_workshop_attack"), device, _workshop_policy())
    assert device.taps == []
    assert transactions.TransactionJournal(path).open_transactions()[0].stage == transactions.Stage.ACTED


def test_restart_owns_the_frame_after_runner_reset_without_begin(tmp_path: Path, fake_header: dict) -> None:
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(transactions.Intent(item="x1", category="CARDS", currency="gems",
                                          price=20, wallet_before=400, ts=1.))
    journal.record_action(txn.key, at=1.)
    session = shopping_mod.ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Recorder(),
                                          digits.NumberReader(), journal=journal)
    assert session.active
    session.reset()
    assert session.active
    device = FakeDevice()
    session.advance(frame("menu_cards"), device, a_policy(armed=True, enabled=False))
    assert device.taps == []
    assert session._spent is None


def test_scan_loop_restart_cannot_navigate_around_an_unreconciled_tap(
    tmp_path: Path, bot_on_main_menu, fake_header: dict,
) -> None:
    bot = bot_on_main_menu(a_policy(armed=True))
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(transactions.Intent(item="x1", category="CARDS", currency="gems",
                                          price=20, wallet_before=400, ts=1.))
    journal.record_action(txn.key, at=1.)
    bot.shopping.journal = journal
    bot.shopping.reset()
    bot._screen = frame("menu_cards")
    bot.run_once()
    assert bot.device.taps == []
    assert bot.shopping.active
    assert journal.open_transactions()[0].stage == transactions.Stage.ACTED


def test_restart_blocks_other_scan_loop_actions_before_reconciliation(
    tmp_path: Path, bot_in_run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run(a_policy(armed=True))
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(transactions.Intent(item="x1", category="CARDS", currency="gems",
                                          price=20, wallet_before=400, ts=1.))
    journal.record_action(txn.key, at=1.)
    bot.shopping.journal = journal

    def speed_action(*_: object) -> bool:
        bot.device.click(10, 10)
        return True

    monkeypatch.setattr(bot, "_manage_speed", speed_action)
    bot.run_once()
    assert bot.device.taps == []
    assert journal.open_transactions()[0].stage == transactions.Stage.ACTED


def test_second_restart_restores_reconciled_visit_before_later_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_header: dict,
) -> None:
    session, page, clock, path = _restart_workshop(tmp_path, monkeypatch, fake_header)
    page.rows = (dataclasses.replace(page.rows[0], value=2, price=6),)
    fake_header["coins"] = 1765
    _step(session, FakeDevice(), _workshop_policy())
    clock[0] += 1
    _step(session, FakeDevice(), _workshop_policy())
    restarted = shopping_mod.ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Recorder(),
        digits.NumberReader(), journal=transactions.TransactionJournal(path))
    restarted.reset()
    assert restarted.active
    device = FakeDevice()
    restarted.advance(frame("menu_workshop_attack"), device, _workshop_policy())
    assert device.taps == []
    assert (restarted._bought, restarted._spent, restarted._coin_spent) == (1, 5, 5)
    # Ending the recovered visit makes a new process idle, with one durable debit.
    restarted._end_visit(device, _workshop_policy(), frame("menu_workshop_attack"), aborted=False)
    assert not shopping_mod.ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Recorder(),
        digits.NumberReader(), journal=transactions.TransactionJournal(path)).active
    with db.reader(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE kind = 'WORKSHOP_BUY'").fetchone()[0] == 1


def test_a_changed_row_whose_wallet_did_not_prove_the_price_is_no_debit(
    tmp_path, monkeypatch, fake_header
) -> None:
    """The price read before the tap is a prediction, not a debit.

    The row changed, so something was bought - but the coins did not fall
    by the price that was read, and the journal resolved the attempt
    UNPROVEN. The ledger line must not turn that read price into -5.
    """
    import ledger

    session = _journalled_workshop_session(tmp_path / "bot.db", monkeypatch)
    page = _Page(monkeypatch, _row("damage", "Damage", 5))
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)
    _step(session, device, policy)  # the tap, at 1770 coins

    page.rows = (_row("damage", "Damage", 6),)  # the row moved on...
    _step(session, device, policy)  # ...but the wallet still reads 1770

    (bought,) = session._bus.of_type("Purchased")
    assert bought.verdict == "unproven" and bought.spent is None
    (line,) = ledger.classify(bought)
    assert line.delta is None, "an unproven purchase is an unknown movement"
    assert line.price == 5
    assert session._coin_spent is None and session._spent is None


def test_a_proven_workshop_purchase_debits_what_the_wallet_lost(
    tmp_path, monkeypatch, fake_header
) -> None:
    import ledger

    session = _journalled_workshop_session(tmp_path / "bot.db", monkeypatch)
    page = _Page(monkeypatch, _row("damage", "Damage", 5))
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)
    _step(session, device, policy)

    page.rows = (_row("damage", "Damage", 6),)
    fake_header["coins"] = 1765
    _step(session, device, policy)

    (bought,) = session._bus.of_type("Purchased")
    assert (bought.verdict, bought.spent, bought.price) == ("bought", 5, 5)
    assert ledger.classify(bought)[0].delta == -5
    assert (session._coin_spent, session._spent) == (5, 5)
    assert session.journal.open_transactions() == ()


def test_an_unproven_spend_stops_a_bounded_visit_budget(
    tmp_path, monkeypatch, fake_header
) -> None:
    """A budget cannot be honoured against a total nobody knows.

    Treating the unknown amount as zero would let the visit keep buying past
    its ceiling; treating it as the read price would assert a number the
    wallet refused to confirm. The only honest move is to stop spending.
    """
    session = _journalled_workshop_session(tmp_path / "bot.db", monkeypatch)
    page = _Page(monkeypatch, _row("damage", "Damage", 5),
                 _row("attack_speed", "Attack Speed", 5))
    device = FakeDevice()
    policy = a_policy(armed=True, coin_budget=100, workshop=(
        ShoppingRule(name="Damage", category="ATTACK", target=2),
        ShoppingRule(name="Attack Speed", category="ATTACK", target=2),
    ))
    session.begin(policy, run_count=1)
    _step(session, device, policy)  # tap Damage
    page.rows = (_row("damage", "Damage", 6), _row("attack_speed", "Attack Speed", 5))
    _step(session, device, policy)  # confirmed changed, wallet unmoved: unproven
    _step(session, device, policy)  # Attack Speed would fit in 100 coins

    assert len(device.taps) == 1, "a visit whose spend is unknown taps no more"
    skips = [s for s in session._bus.of_type("PurchaseSkipped") if s.item == "Attack Speed"]
    assert [s.reason for s in skips] == ["budget"]


def test_an_unconfirmed_workshop_tap_ends_the_visit_with_an_unknown_spend(
    tmp_path, monkeypatch, fake_header
) -> None:
    """The tap went out and nothing proved or refuted it. Whatever the visit
    reports as spent, it is not the sum of the purchases it could prove."""
    session = _journalled_workshop_session(tmp_path / "bot.db", monkeypatch)
    _Page(monkeypatch, _row("damage", "Damage", 5))
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)
    for _ in range(4):
        _step(session, device, policy)

    (ended,) = session._bus.of_type("ShoppingEnded")
    assert ended.aborted and ended.spent is None
    assert session._bus.of_type("Purchased") == []


def test_ending_a_visit_mid_confirmation_leaves_its_spend_unknown(
    tmp_path, monkeypatch, fake_header
) -> None:
    session = _journalled_workshop_session(tmp_path / "bot.db", monkeypatch)
    _Page(monkeypatch, _row("damage", "Damage", 5))
    device = FakeDevice()
    policy = _workshop_policy()
    session.begin(policy, run_count=1)
    _step(session, device, policy)  # tapped, not yet answered

    session._abort(device, policy, frame("menu_workshop_attack"), "stopped")

    (ended,) = session._bus.of_type("ShoppingEnded")
    assert ended.spent is None
    assert session.journal.open_transactions() == ()


def test_a_confirmed_card_carries_the_journal_verdict(tmp_path, fake_header) -> None:
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    session = shopping_mod.ShoppingSession(
        templates=vision.TemplateCache(config.TEMPLATE_DIR),
        bus=Recorder(),
        reader=digits.NumberReader(),
        journal=journal,
    )
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    session.advance(frame("menu_cards"), device, policy)
    fake_header["gems"] = 380
    session.advance(frame("menu_cards"), device, policy)

    (bought,) = [e for e in session._bus.of_type("Purchased") if e.category == "CARDS"]
    assert (bought.verdict, bought.spent) == ("bought", 20)
    assert session._spent == 20


def test_an_unconfirmed_card_ends_the_visit_with_an_unknown_spend(
    session, fake_header
) -> None:
    device = FakeDevice()
    policy = a_policy(armed=True, cards=CardPolicy(
        enabled=True, gem_floor=0, max_per_visit=1
    ))
    session.begin(policy, run_count=1)
    session._step = shopping_mod.Step.BUY_CARDS
    fake_header["gems"] = 400
    for _ in range(4):
        session.advance(frame("menu_cards"), device, policy)

    (ended,) = session._bus.of_type("ShoppingEnded")
    assert ended.aborted and ended.spent is None


def test_the_bot_never_taps_unlock_new_slot(session) -> None:
    """Slots are out of scope by design - the community gem order puts lab
    slots above them and the bot cannot see labs. Guarded by the fact that no
    slot template exists at all, which this pins."""
    assert not any("slot" in path.lower() for path in config.CARD_BUTTONS.values())


# -- where a purchase actually taps -----------------------------------------
def test_a_row_purchase_taps_the_price_panel_not_the_label(
    session, fake_header
) -> None:
    """The label is not a button. Verified on a live device: the bot ran a
    clean armed visit, published Purchased, and bought nothing - coins,
    stat value and price all unchanged - because match.center lands on the
    words "Attack Speed". A tap on the price strip bought it (coins
    1770 -> 1740, price 30 -> 56) at the point PRICE_REGIONS already
    locates from the same anchor.

    This is the trap config.buy_point() exists to avoid for in-run
    upgrades; a workshop tile turns out to share it.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Damage",
                     category="ATTACK"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)

    assert session._pending is not None, "the buy tap was never attempted"
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)
    assert session._bus.of_type("Purchased"), "the row was never bought"
    # taps[0] is the nav tap that opened the Workshop; the purchase is last.
    # (434, 633) is the centre of the price box OCR read on this frame - the
    # tap is derived from the read, so whatever a row charges is what gets
    # tapped. The label's own centre, which this used to tap, is (130, 558).
    assert device.taps[-1] == (434, 633)
    assert (130, 558) not in device.taps, "tapped the label, which buys nothing"


def test_a_price_the_glyph_atlas_cannot_read_is_bought_at_the_ocr_price(
    session, fake_header
) -> None:
    """The case the whole cut-over rests on, in real pixels.

    menu_workshop_attack_escalated.png is a live capture taken after a
    purchase pushed Attack Speed from 30 to 56. The `menu` atlas has never
    held a 6, so the template reader returns None on that price and the row
    was refused as "unreadable" - a row the bot could see, afford and never
    buy. OCR reads 56 off the same pixels.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Attack Speed",
                     category="ATTACK"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)

    assert not session._bus.of_type("Purchased"), "a tap must await acknowledgement"
    pending = session._pending
    assert pending is not None, "the row was refused - see PurchaseSkipped"
    assert pending.row.name == "Attack Speed"
    assert pending.row.price == 56
    # The price strip inside the buy panel, read off this very frame.
    assert device.taps[-1] == (952, 634)


# -- a screen the reader cannot see -----------------------------------------
def _two_attack_rows():
    return (
        ShoppingRule(name="Damage",
                     category="ATTACK"),
        ShoppingRule(name="Attack Speed",
                     category="ATTACK"),
    )


def test_a_blinded_screen_does_not_write_the_row_off(session, fake_header) -> None:
    """"I cannot see" is not "it is not here".

    A live armed visit tapped a row's label, which opened an info panel over
    the grid, and then skipped every remaining row as no_match - exhausting
    four rows that were sitting right there, unbought, for the rest of the
    visit. The row must survive an unreadable frame.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=_two_attack_rows())
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)   # buys Damage
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)
    session.advance(frame("menu_workshop_info_panel"), device, policy)

    assert "Attack Speed" not in session._exhausted, (
        "a row nobody could see was written off for the visit"
    )
    skips = session._bus.of_type("PurchaseSkipped")
    assert any(s.reason == "unreadable" and s.detail == "screen" for s in skips)
    assert not any(s.reason == "no_match" for s in skips), (
        "an unreadable screen was reported as the row being absent"
    )


def test_a_blinded_screen_is_tapped_clear(session, fake_header) -> None:
    """The panel closes on a tap anywhere outside it. The dismiss point sits
    on the page title, ABOVE the tile grid - measured there rather than in
    the empty space below it, because that space fills up as rows unlock and
    a tap that lands on a price box would buy something nobody asked for.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=_two_attack_rows())
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)
    session.advance(frame("menu_workshop_info_panel"), device, policy)

    assert device.taps[-1] == config.PANEL_DISMISS_POINT


def test_info_panel_after_unlock_is_dismissed_while_purchase_stays_pending(
    session, fake_header,
) -> None:
    device = FakeDevice()
    fake_header["coins"] = 163
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Unlock Coin Bonuses", category="UTILITY"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_utility_restocked"), device, policy)
    assert session._pending is not None
    assert session._pending.row.name == "Unlock Coin Bonuses"

    # An unlock can rearrange the tiles under the tap and open a child row's
    # info panel. It must be cleared before the pending debit is judged.
    session.advance(frame("menu_workshop_info_panel"), device, policy)
    assert device.taps[-1] == config.PANEL_DISMISS_POINT
    assert session._pending is not None
    assert session._bus.of_type("PurchaseSkipped") == []


def test_a_screen_that_stays_blind_ends_the_visit(session, fake_header) -> None:
    """The dismiss tap is one attempt, not a loop. If the page is still
    unreadable on the next scan it is something this code does not
    understand, and spinning on it silently is the failure mode
    _off_page_streak exists to prevent everywhere else.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=_two_attack_rows())
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_attack"), device, policy)
    session.advance(frame("menu_workshop_attack_escalated"), device, policy)
    session.advance(frame("menu_workshop_info_panel"), device, policy)
    session.advance(frame("menu_workshop_info_panel"), device, policy)

    assert session._bus.of_type("ShoppingEnded"), "the visit spun instead of ending"
    assert session.active is False


def test_an_explainer_modal_is_acknowledged_rather_than_tapped_around(
    session, fake_header
) -> None:
    """One-time explainer modals swallow every tap until their OK is pressed.

    menu_workshop_explainer_modal.png is a live capture: unlocking a feature
    queued an "ULTIMATE WEAPONS ... [OK]" dialog that was already up when the
    Workshop opened. The page classifies as WORKSHOP, so positioning carried
    on tapping a tab that could never arrive, and the visit spent its whole
    budget without reading a single row. Nothing here reaches _buy_rows, so
    the blind handling there cannot help - and a tap anywhere outside this
    one does not close it, unlike the info panel.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=_two_attack_rows())
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    session.advance(frame("menu_workshop_explainer_modal"), device, policy)

    # The OK button, read off that very frame.
    assert device.taps[-1] == (541, 1578)


def test_arrival_is_judged_by_the_page_heading_not_a_row_template(
    session, fake_header
) -> None:
    """A tab whose configured row has been bought must still be recognised.

    Arrival used to be "can I locate one of this category's row templates?".
    That answers "no" forever once the row is bought and gone from the page,
    so the session taps the tab until its budget dies - a live visit spent a
    whole visit doing exactly that on UTILITY after an earlier run bought
    Unlock Cash Bonuses.

    menu_workshop_utility_restocked.png is that page: the configured row's
    template no longer matches anything, and three other rows are sitting
    there. The heading says which tab this is, and OCR can read it.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Unlock Cash Bonuses",
                     category="UTILITY"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    for _ in range(3):
        session.advance(frame("menu_workshop_utility_restocked"), device, policy)

    skips = session._bus.of_type("PurchaseSkipped")
    assert skips, (
        "never reached the rows - still waiting to arrive on a tab it is on"
    )
    assert "Unlock Cash Bonuses" in session._exhausted, (
        "a row genuinely gone from the page must be given up on, once"
    )


def test_a_row_ocr_could_not_match_is_published_with_what_was_read(
    session, fake_header
) -> None:
    """A garbled name must be loud, not merely unbought.

    Live evidence that this is real and not defensive: OCR read
    'Damage / Meter C' off the ATTACK tab, the coin glyph having joined the
    row name. A row spelled that way in a strategy would never match, and
    without this event the feed would show nothing at all - the row would
    simply never be bought, forever, for no visible reason.

    menu_workshop_utility_restocked.png is the honest version of the same
    shape: the configured row is not on the page, and three others are.

    Coins / Wave rather than an unlock row on purpose. An unlock missing
    from a page that shows what it grants is not unmatched at all - it is
    spent, and _already_unlocked retires it without this event. What is
    left for RowUnmatched is the genuine case: a row nothing on the page
    can account for.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Coins / Wave",
                     category="UTILITY"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    for _ in range(3):
        session.advance(frame("menu_workshop_utility_restocked"), device, policy)

    unmatched = session._bus.of_type("RowUnmatched")
    assert unmatched, "nothing said why the row was never bought"
    assert unmatched[0].item == "Coins / Wave"
    assert unmatched[0].read == (
        "Cash Bonus", "Cash / Wave", "Unlock Coin Bonuses",
    )


# -- an unlimited coin budget ----------------------------------------------
def _escalating_row(session, monkeypatch, prices, *, coins=1770):
    """One Damage row whose price, level and the wallet all move with each tap.

    Keyed on session._taps rather than device.taps so a rehearsal - which
    counts its taps but sends none - reads exactly the same board a live
    visit does. The wallet debits what the taps sent so far cost, which is
    what makes coin_reserve and affordability real here instead of frozen.
    """
    def _index():
        return min(session._taps, len(prices) - 1)

    def _row():
        return ObservedUpgrade("damage", "Damage", "ATTACK", "workshop",
                               1 + _index(), prices[_index()], "available", 1,
                               config.Rect(0, 0, 100, 100), (50, 80))

    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", (_row(),), {}, None, 1, 270))
    monkeypatch.setattr(shopping_mod, "header_numbers", lambda *_:
                        (coins - sum(prices[:min(session._taps, len(prices))]), 40))


def _keep_buying(session, device, policy, ticks=14):
    for _ in range(ticks):
        session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                          frame("menu_workshop_attack"), device, policy)


def test_an_unlimited_budget_re_buys_one_row_until_it_is_unaffordable(
    session, monkeypatch
) -> None:
    """The point of the setting: a row is not "used up" by one purchase, so
    the visit keeps buying it at its new, higher price until the wallet -
    not a configured cap - is what stops it."""
    device = FakeDevice()
    _escalating_row(session, monkeypatch, [5, 10, 20, 5_000])
    policy = a_policy(armed=True, coin_budget=None, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    session.begin(policy, run_count=1)

    _keep_buying(session, device, policy)

    assert len(session._bus.of_type("Purchased")) == 3
    assert [e.reason for e in session._bus.of_type("PurchaseSkipped")] == ["unaffordable"]


def test_an_unlimited_budget_still_stops_at_the_coin_reserve(
    session, monkeypatch
) -> None:
    """"As long as you have the coins" means down to the reserve, not past
    it. The reserve is the floor the unlimited budget spends towards."""
    device = FakeDevice()
    _escalating_row(session, monkeypatch, [5, 10, 20, 40])
    policy = a_policy(armed=True, coin_budget=None, coin_reserve=1_740, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    session.begin(policy, run_count=1)

    _keep_buying(session, device, policy)

    assert len(session._bus.of_type("Purchased")) == 2
    assert [e.reason for e in session._bus.of_type("PurchaseSkipped")] == ["reserve"]


def test_an_unlimited_budget_still_stops_a_row_at_its_target(
    session, monkeypatch
) -> None:
    device = FakeDevice()
    _escalating_row(session, monkeypatch, [5, 10, 20, 40])
    policy = a_policy(armed=True, coin_budget=None, workshop=(
        ShoppingRule(name="Damage", category="ATTACK", target=3),
    ))
    session.begin(policy, run_count=1)

    _keep_buying(session, device, policy)

    assert len(session._bus.of_type("Purchased")) == 2
    assert [e.reason for e in session._bus.of_type("PurchaseSkipped")] == ["target_reached"]


def test_an_unlimited_rehearsal_still_walks_each_row_once(
    session, monkeypatch
) -> None:
    """Nothing leaves the wallet unarmed, so the price never rises and a
    repeat-buying rehearsal would spin on row one until the tap budget
    aborted the visit. A rehearsal reports the whole list instead."""
    device = FakeDevice()
    _escalating_row(session, monkeypatch, [5])
    policy = a_policy(armed=False, coin_budget=None, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    session.begin(policy, run_count=1)

    _keep_buying(session, device, policy)

    assert device.taps == []
    assert len(session._bus.of_type("Purchased")) == 1


def test_a_finite_budget_still_buys_each_row_at_most_once_a_visit(
    session, monkeypatch
) -> None:
    """The pin on every profile already on disk: naming a number keeps the
    behaviour it has always had."""
    device = FakeDevice()
    _escalating_row(session, monkeypatch, [5, 10, 20, 40])
    policy = a_policy(armed=True, coin_budget=10_000, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    session.begin(policy, run_count=1)

    _keep_buying(session, device, policy)

    assert len(session._bus.of_type("Purchased")) == 1


# -- unlocks the account has already bought ---------------------------------
def _tab_row(upgrade_id: str, name: str, category: str = "ATTACK") -> ObservedUpgrade:
    return ObservedUpgrade(upgrade_id, name, category, "workshop", 1, 100,
                           "available", 1, config.Rect(0, 0, 100, 100), (50, 80))


def test_an_unlock_is_retired_by_the_rows_it_granted_being_on_the_tab(
    session, monkeypatch, fake_header,
) -> None:
    """Attack Range cannot be on the tab until Unlock Range Upgrades is bought.

    The live bot scanned this exact tab top-to-bottom on every visit for a
    row that was spent days earlier, and reported its availability unknown.
    """
    rows = (_tab_row("damage", "Damage"), _tab_row("range", "Attack Range"),
            _tab_row("damage_per_meter", "Damage / Meter"))
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", rows, {}, None, 1, 270))
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Unlock Range Upgrades", category="ATTACK"),
    ))
    device = FakeDevice()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert [s.reason for s in session._bus.of_type("PurchaseSkipped")] == ["already_unlocked"]
    assert device.swipes == [] and device.taps == []
    assert session._bus.of_type("RowUnmatched") == []


def test_a_spent_unlock_is_retired_off_the_real_page_that_replaced_it(
    session, fake_header,
) -> None:
    """The same live shape, in real pixels rather than a stubbed reading.

    menu_workshop_utility_restocked.png is the UTILITY tab after Unlock Cash
    Bonuses was bought: Cash Bonus and Cash / Wave - the two rows it grants -
    are sitting on the page, and the unlock itself is gone. The page is the
    receipt, so nothing here should be scrolling in search of it.
    """
    device = FakeDevice()
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Unlock Cash Bonuses", category="UTILITY"),
    ))
    session.begin(policy, run_count=1)
    session.advance(frame("menu_main"), device, policy)
    for _ in range(3):
        session.advance(frame("menu_workshop_utility_restocked"), device, policy)

    skips = session._bus.of_type("PurchaseSkipped")
    assert [s.reason for s in skips] == ["already_unlocked"]
    assert session._bus.of_type("RowUnmatched") == []
    assert device.swipes == []


def test_the_search_for_a_spent_unlock_stops_when_its_rows_scroll_into_view(
    session, monkeypatch, fake_header,
) -> None:
    """A tab scrolled past the proof still costs one swipe, not a full scan.

    The evidence test runs on every frame, including the frames the row
    search itself produces, so the scan ends as soon as a granted row comes
    into view. That is what makes remembering spent unlocks across restarts
    unnecessary: the page re-proves it, cheaply, whenever it is read.
    """
    device = FakeDevice()
    scrolled = [
        Observation("ATTACK", (_tab_row("damage", "Damage"),), {}, None, 1, 270),
        Observation("ATTACK", (_tab_row("damage_per_meter", "Damage / Meter"),),
                    {}, None, 1, 270),
    ]
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        scrolled[min(len(device.swipes), 1)])
    policy = a_policy(armed=True, workshop=(
        ShoppingRule(name="Unlock Range Upgrades", category="ATTACK"),
    ))
    session.begin(policy, run_count=1)

    for _ in range(2):
        session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                          frame("menu_workshop_attack"), device, policy)

    assert len(device.swipes) == 1
    assert [s.reason for s in session._bus.of_type("PurchaseSkipped")] == ["already_unlocked"]
    assert session._bus.of_type("RowUnmatched") == []


# -- a budget that keeps pace with the prices --------------------------------
def _priced_row(session, monkeypatch, price: int) -> None:
    row = _tab_row("damage", "Damage")
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", (dataclasses.replace(row, price=price),),
                                    {}, None, 1, 270))


@pytest.mark.parametrize("coins,taps", [(1000, 0), (4000, 1)])
def test_the_visit_budget_keeps_pace_with_the_wallet(
    session, monkeypatch, fake_header, coins: int, taps: int,
) -> None:
    """One policy, two wallets, one 300-coin row: refused, then bought.

    A fixed coin_budget is outgrown by its own purchases. Workshop prices
    climb with every level bought - the live bot walked Health from 55 to
    234 in an afternoon - so a constant eventually sits below every price
    on the page and refuses everything, silently, while the wallet fills
    up. A share of the wallet cannot go stale that way.
    """
    fake_header["coins"] = coins
    _priced_row(session, monkeypatch, price=300)
    policy = a_policy(armed=True, coin_budget=None, coin_budget_pct=.25, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    device = FakeDevice()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert len(device.taps) == taps
    assert [e.reason for e in session._bus.of_type("PurchaseSkipped")] == (
        [] if taps else ["budget"])


def test_reroll_planner_receives_verified_row_price_before_purchase(
    session, monkeypatch, fake_header,
) -> None:
    fake_header["coins"] = 1000
    _priced_row(session, monkeypatch, price=300)
    seen: list[tuple[str, int | None, int | None]] = []
    session.reroll_observe_price = lambda upgrade, balance, price: seen.append(
        (upgrade, balance, price))
    policy = a_policy(armed=True, coin_budget_pct=.5, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),))
    session.begin(policy, run_count=1)
    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), FakeDevice(), policy)
    assert seen == [("damage", 1000, 300)]


def test_reroll_observes_neighbor_prices_but_not_pending_receipts(
    session, monkeypatch, fake_header,
) -> None:
    fake_header["coins"] = 1000
    rows = (
        dataclasses.replace(_tab_row("damage", "Damage"), price=300),
        dataclasses.replace(_tab_row("attack_speed", "Attack Speed"), price=30),
    )
    monkeypatch.setattr(shopping_mod, "observe_frame", lambda *_:
                        Observation("ATTACK", rows, {}, None, 1, 270))
    seen = []
    session.reroll_observe_prices = lambda prices, coins: seen.append((prices, coins))
    policy = a_policy(armed=True, coin_budget=500, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),))
    session.begin(policy, run_count=1)
    device = FakeDevice()
    for _ in range(2):
        session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                          frame("menu_workshop_attack"), device, policy)
    assert seen == [({"damage": 300, "attack_speed": 30}, 1000)]
    assert len(device.taps) == 1


def test_a_wallet_share_and_a_coin_ceiling_both_bind(
    session, monkeypatch, fake_header,
) -> None:
    """Set both and the tighter one decides - neither silently outranks the
    other. Here a quarter of 1770 is 442, and the ceiling is 250."""
    _priced_row(session, monkeypatch, price=300)
    policy = a_policy(armed=True, coin_budget=250, coin_budget_pct=.25, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    device = FakeDevice()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert device.taps == []
    assert [e.reason for e in session._bus.of_type("PurchaseSkipped")] == ["budget"]


def test_a_wallet_share_is_a_cap_so_a_bought_row_is_not_re_bought(
    session, monkeypatch, fake_header,
) -> None:
    """A share budget makes a visit capped, not unlimited.

    An uncapped visit deliberately leaves the row un-exhausted and buys it
    again at its new price, letting the wallet end the rotation. A visit
    holding a share of that same wallet must not: it has a cap, and reading
    only coin_budget to decide would call it unlimited and spend past it.
    """
    _priced_row(session, monkeypatch, price=300)
    policy = a_policy(armed=True, coin_budget=None, coin_budget_pct=.25, workshop=(
        ShoppingRule(name="Damage", category="ATTACK"),
    ))
    device = FakeDevice()
    session.begin(policy, run_count=1)

    session._buy_rows(SimpleNamespace(page="workshop", top_left=None),
                      frame("menu_workshop_attack"), device, policy)

    assert len(device.taps) == 1
    assert "Damage" in session._exhausted


# -- input refused by the device guard ------------------------------------

def _refuse_taps(monkeypatch, refusals: int) -> list[tuple[int, int]]:
    """Refuse the first `refusals` taps the way the guard does, then send."""
    sent: list[tuple[int, int]] = []
    left = [refusals]

    def guarded(_device, x, y):
        if left[0]:
            left[0] -= 1
            raise RecoveryPreflightBlocked("fresh_evidence")
        sent.append((x, y))

    monkeypatch.setattr(shopping_mod, "tap", guarded)
    return sent


def test_a_refused_tap_is_retried_on_the_next_frame(session, monkeypatch) -> None:
    """A loaded host makes the guard refuse input on frames it thinks are
    stale. Nothing was sent, so the visit waits instead of ending."""
    sent = _refuse_taps(monkeypatch, 2)
    policy = a_policy(armed=True)
    session.begin(policy, run_count=1)
    for _ in range(2):
        session.advance(frame("menu_main"), FakeDevice(), policy)
        assert session.active is True
        assert session._taps == 0
    session.advance(frame("menu_main"), FakeDevice(), policy)
    assert len(sent) == 1 and session._taps == 1
    assert session._bus.of_type("ShoppingEnded") == []


def test_a_guard_that_keeps_refusing_still_ends_the_visit(session, monkeypatch) -> None:
    sent = _refuse_taps(monkeypatch, 1_000)
    policy = a_policy(armed=True)
    session.begin(policy, run_count=1)
    for _ in range(shopping_mod.MAX_REFUSED_STEPS):
        session.advance(frame("menu_main"), FakeDevice(), policy)
    assert session.active is False
    assert sent == []
    ended = session._bus.of_type("ShoppingEnded")
    assert len(ended) == 1 and ended[0].aborted
    assert "input refused" in ended[0].reason


def test_zero_budget_price_probe_reads_expensive_reference_and_every_row(
    session, monkeypatch, fake_header,
) -> None:
    fake_header['coins'] = 90
    rows = (
        dataclasses.replace(_tab_row('damage', 'Damage'), price=100),
        dataclasses.replace(_tab_row('attack_speed', 'Attack Speed'), price=40),
        dataclasses.replace(_tab_row('critical_chance', 'Critical Chance'), price=20),
    )
    monkeypatch.setattr(shopping_mod, 'observe_frame', lambda *_:
        Observation('ATTACK', rows, {}, None, 1, 270))
    observed: list[tuple[str, int | None]] = []
    neighbors: list[dict[str, int]] = []
    session.reroll_observe_price = lambda uid, _balance, price: observed.append((uid, price))
    session.reroll_observe_prices = lambda prices, _balance: neighbors.append(prices)
    policy = a_policy(armed=True, coin_budget=0, allow_unlocks=False,
        cards=CardPolicy(enabled=False), workshop=tuple(
            ShoppingRule(name=row.name, category='ATTACK') for row in rows))
    device = FakeDevice()
    session.begin(policy, run_count=1)
    for _ in range(4):
        session._buy_rows(SimpleNamespace(page='workshop', top_left=None),
            frame('menu_workshop_attack'), device, policy)
    assert observed == [('damage',100),('attack_speed',40),('critical_chance',20)]
    assert neighbors[0] == {'damage':100,'attack_speed':40,'critical_chance':20}
    assert device.taps == [] and session._pending is None
    assert session._categories == []
