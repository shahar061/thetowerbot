"""Read Workshop upgrades and walk the game's between-run shopping pages.

Armed Workshop purchases require a live OCR price and balance, a positive
per-visit coin budget, the configured reserve and permission for unlock tiles.
A tap creates a pending purchase: another frame must show the changed value,
price or unlock transition before a Purchased event reports success. An
inconclusive acknowledgement ends the visit without another purchase attempt.

Rows are addressed by semantic name and category. Missing rows trigger bounded
scrolling, never an inference that the upgrade is locked. Every navigation,
buy or swipe uses one frame and counts against the visit's operation budget;
aborting allows one recovery tap back to Battle. Unarmed rehearsals perform
no device actions and identify their hypothetical purchases with dry_run.
"""

from __future__ import annotations

from account_state import AccountState
from currencies import CommitmentError, CurrencyRepository

import logging
import hashlib
import math
import time
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

import config
import events
import jitter
import ocr
import pages
import tiles
import transactions
import upgrades
import vision
from device import Image, tap
from digits import NumberReader
from perception import Observation, ObservedUpgrade, observe_frame, price_number
from strategy import Shopping, Strategy
from supervisor import RecoveryPreflightBlocked

if TYPE_CHECKING:
    from autopilot import AutopilotState

logger = logging.getLogger("tower_bot.shopping")

# Labels that mean "I have read this" and nothing else. Deliberately not
# CONFIRM, YES, BUY or CLAIM: those answer a question, and a bot that cannot
# read the question must not answer it.
_MODAL_ACKNOWLEDGEMENTS: frozenset[str] = frozenset({"OK"})


class Step(Enum):
    IDLE = auto()
    OPEN_WORKSHOP = auto()
    OPEN_TAB = auto()
    BUY_ROWS = auto()
    OPEN_CARDS = auto()
    BUY_CARDS = auto()
    RETURN = auto()


def header_numbers(
    screen: Image, page: str, top_left: tuple[int, int] | None
) -> tuple[int | None, int | None]:
    """Coins and gems off the menu header, or (None, None) off a page that
    has no header regions (MISSIONS, or a page that failed to classify).

    Read with OCR rather than the glyph atlas. The atlas could only ever
    read a balance built from glyphs a past play session happened to push
    through it - templates/atlas/header/ has never held 2, 3, 5 or 9 - and
    an unreadable balance aborts the visit, so the bot could not shop until
    somebody harvested them by hand. OCR needs no such session, which is
    why build_shopping() now gates on the engine instead of on the atlas.

    Each region is read off its OWN padded crop rather than by filtering one
    whole-frame read down to the two rects. The whole-frame read loses short
    isolated numbers outright: a gem balance of "0" produces no box anywhere
    in the frame, at any confidence floor, so the balance came back None and
    took the visit with it. Cropped and padded, the same pixels read at 0.92.

    Reading twice is also the cheaper half of the trade, which is the
    opposite of what it looks like - the detection stage scales with the
    pixels it is handed, so two small crops measure ~139ms against ~222ms
    for one whole frame.
    """
    regions = config.HEADER_REGIONS.get(page)
    if regions is None or top_left is None:
        return None, None
    coins_region, gems_region = regions
    return (
        _balance_at(screen, _absolute(coins_region, top_left)),
        _balance_at(screen, _absolute(gems_region, top_left)),
    )


def _balance_at(screen: Image, region: config.Rect) -> int | None:
    """A single high-confidence balance, read off that region's own crop.

    Still "exactly one number or nothing": the coin and gem balances sit in
    one header row, and a crop that caught both of them is a crop measured
    wrong. Refusing is safe; picking the first would spend against the wrong
    balance.
    """
    values = [value for box in ocr.read_region(screen, region)
              if box.confidence >= .9
              and (value := price_number(box.text)) is not None]
    return values[0] if len(values) == 1 else None


def _absolute(region: config.Region, top_left: tuple[int, int]) -> config.Rect:
    """A Region is anchor-relative by design (see config.Region); ocr works
    in frame coordinates. This is the one place the two meet."""
    return config.Rect(top_left[0] + region.dx, top_left[1] + region.dy, region.w, region.h)


def _heading_names(screen: Image, category: str) -> bool:
    """Does this page's heading say it is `category`'s tab?

    Compared with everything but the letters stripped out: OCR renders the
    heading as "ATTACKUPGRADES" on some frames and "ATTACK UPGRADES" on
    others, and neither spelling should decide whether the bot can shop.
    Matching the whole heading rather than searching for the category name
    keeps a row called "Unlock Range Upgrades" from reading as one.
    """
    wanted = _letters(f"{category}UPGRADES")
    return any(_letters(box.text) == wanted for box in ocr.read(screen))


def _letters(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalpha())


def _unlimited(shopping: Shopping) -> bool:
    """Is this visit spending without a per-visit cap?

    `coin_budget` is None for "no limit" and 0 for "spend nothing" - two
    opposite meanings that a falsy check would collapse into one.

    A share of the wallet is a cap too, so a visit holding one is not
    unlimited even with no absolute figure set. Reading only coin_budget
    here would send the rotation back to re-buy a row the share had
    already spent its allowance on.
    """
    return (shopping.armed and shopping.coin_budget is None
            and shopping.coin_budget_pct is None)


def _row_named(name: str, rows: tuple[ObservedUpgrade, ...],
               category: str | None = None) -> ObservedUpgrade | None:
    """Match only an unambiguous legacy identity on the expected Workshop tab."""
    entry = upgrades.resolve(name, category)
    if entry is None:
        return None
    matches = [row for row in rows if row.upgrade_id == entry.id
               and row.category == entry.category and row.context == "workshop"
               and upgrades.resolve(row.name, row.category) == entry]
    return matches[0] if len(matches) == 1 else None


def _target_reached(upgrade_id: str, value: float, target: float) -> bool:
    if upgrades.by_id(upgrade_id) is not None:
        return upgrades.target_reached(upgrade_id, value, target)
    return False


@dataclass
class PendingPurchase:
    row: ObservedUpgrade
    coins: int
    visible_ids: frozenset[str]
    frames: int = 0
    info_dismissed: bool = False
    # The durable row this pending answers for, when a journal is attached.
    key: str | None = None


@dataclass
class PendingCard:
    """A card tap waiting for the gem balance to answer for it.

    Separate from PendingPurchase, which is typed to a workshop row and is
    confirmed by that row changing. A card batch leaves no row behind - the
    button looks identical before and after - so the only evidence a card
    was actually opened is that the gems fell by what it cost.
    """

    item: str
    price: int
    gems_before: int
    frames: int = 0
    # The durable row this pending answers for, when a journal is attached.
    key: str | None = None


@dataclass
class RowSearch:
    name: str
    down: bool = False
    scrolls: int = 0
    fingerprint: tuple[str, ...] | None = None


class ShoppingSession:
    """Walks the Workshop and Cards pages, buying what the policy allows.

    Every method that decides anything is handed the live policy explicitly
    - the same way the rest of the scan loop always reads Shopping fresh
    rather than trusting a copy made when the session was built. There is
    deliberately no `self.policy` here to go stale.
    """

    def __init__(
        self,
        templates: vision.TemplateCache,
        bus: Any,
        reader: NumberReader,
        threshold: float = 0.8,
        disabled_reason: str | None = None,
        journal: transactions.TransactionJournal | None = None,
    ) -> None:
        # The durable half of every spend. Optional because the visit logic
        # is fully testable without a database, and a session built without
        # one simply keeps no crash-proof record - it does not behave
        # differently while the process lives.
        self.journal = journal
        self.currencies = CurrencyRepository(journal.path) if journal is not None else None
        self.account_state: AccountState | None = None
        # Optional reroll planner observation. The buyer still owns every
        # authorization and transaction; this only reports fresh row facts.
        self.reroll_observe_price: Any | None = None
        self._templates = templates
        self._bus = bus
        self._reader = reader
        self._threshold = threshold
        # Set by each advance() from the caller's snapshot. None until then,
        # and None means "no jitter" - _exit_to_battle can tap via _abort on
        # a path that never reached advance().
        self._tuning: Strategy | None = None
        # Set once by build_shopping when the OCR engine cannot be loaded.
        # An unavailable reader must never start a visit that could spend.
        self.disabled_reason = disabled_reason
        # begin() publishes ShoppingUnavailable the first time it declines
        # for disabled_reason, and never again - a disabled session declines
        # every scan forever, and a per-scan publish would flood the feed
        # with the same fact. See begin().
        self._announced_disabled = False

        self._step: Step = Step.IDLE
        self._visit = 0
        self._categories: list[str] = []
        self._taps = 0
        self._bought = 0
        # Three-valued like every spend: a proven total, or None once any
        # attempt this visit resolved without proving what it cost.
        self._spent: int | None = 0
        self._cards_bought = 0
        # Every named rule is evaluated at most once per visit. A tap marks
        # it attempted immediately; acknowledgement decides whether it was
        # actually bought without issuing a duplicate purchase.
        self._exhausted: set[str] = set()
        self._last_run_count: int | None = None
        # The last page seen this visit, or None before the first frame.
        # Compared every advance() against the fresh classification so a
        # real transition (MAIN_MENU -> WORKSHOP -> CARDS -> MAIN_MENU)
        # publishes PageChanged - see advance(). Reset in begin() so a new
        # visit's first frame never diffs against the previous visit's last
        # page.
        self._last_page: str | None = None
        # Consecutive scans with no progress: either the page did not
        # classify at all, or a positioning step's target template could
        # not be found. Reset to 0 the instant anything makes progress; see
        # _register_progress and _miss.
        self._off_page_streak = 0
        # Consecutive scans on which the reader saw nothing at all - see
        # _buy_rows. Separate from _off_page_streak because _dispatch resets
        # that one optimistically before every BUY_ROWS call.
        self._blind_streak = 0
        self.observations: AutopilotState | None = None
        self._pending: PendingPurchase | None = None
        self._pending_card: PendingCard | None = None
        self._search: RowSearch | None = None
        self._coin_spent: int | None = 0
        # Coins on the visit's first readable frame - the base a percentage
        # budget is taken from. Read once rather than per frame so a visit
        # cannot spend a share of a balance its own purchases shrank.
        self._visit_coins: int | None = None
        # Permanent unlock evidence outlives an individual shopping visit.
        self._completed_unlocks: set[str] = set()
        self._recovery_sample: tuple[str, transactions.RecoveryEvidence] | None = None
        self._recovered_keys: set[str] = set()

    @property
    def active(self) -> bool:
        return (self._step is not Step.IDLE or self._unanswered_transaction() is not None
                or bool(self.journal and self.journal.recovered_visit()))

    @property
    def reconciliation_pending(self) -> bool:
        """A dead process's unanswered action owns the device before every other actor."""
        return self._unanswered_transaction() is not None

    def remaining_categories(self) -> list[str]:
        return list(self._categories)

    # -- starting a visit ---------------------------------------------------

    def due(self, shopping: Shopping, run_count: int) -> bool:
        """Whether begin() would start a visit for this run, starting none.

        Exists because the decision has to be made one screen EARLIER than
        begin() can make it. begin() is only ever offered a MAIN_MENU frame,
        but whether the bot ever reaches MAIN_MENU is settled back on
        GAME_OVER, where the navigator picks RETRY or HOME - so that pick
        needs the same answer before there is a menu to ask on.

        Deliberately the same gate as begin(), extracted rather than
        restated: if the two ever drift, the bot detours home and then
        declines to shop, paying for the trip every single run. Silent about
        disabled_reason - the announcement is begin()'s, published once, and
        a predicate that publishes would fire it from the navigator's path
        too.
        """
        if self.disabled_reason or not shopping.enabled:
            return False
        if not shopping.categories_in_priority_order() and not shopping.cards.enabled:
            return False
        return not (
            self._last_run_count is not None
            and run_count - self._last_run_count < shopping.visit_every_n_runs
        )

    def begin(self, shopping: Shopping, run_count: int) -> bool:
        """Start a visit, or decline with a reason.

        Declines when shopping is off, when nothing is enabled to buy, and
        when the cadence says this run is not a visiting run. Returns a bool
        rather than raising because "not this run" is the normal case, not
        an error.

        Also declines when `disabled_reason` is set because the OCR engine
        could not be loaded. The first such decline publishes
        ShoppingUnavailable so the dashboard says why nothing is happening,
        rather than a feature that looks armed and does nothing forever -
        every decline after that stays silent, since the reason never
        changes once the process has started.
        """
        if self.disabled_reason:
            # bus is None in the one caller that builds a session purely to
            # inspect disabled_reason without wiring the rest of the bot
            # (see tests/test_shopping_loop.py) - nothing else here ever
            # runs for a permanently-disabled session, so that stays a
            # legal, bus-less way to ask "why is this disabled".
            if not self._announced_disabled and self._bus is not None:
                self._announced_disabled = True
                self._bus.publish(events.ShoppingUnavailable(reason=self.disabled_reason))
            return False
        if not self.due(shopping, run_count):
            return False

        categories = list(shopping.categories_in_priority_order())
        self._visit += 1
        self._categories = categories
        self._taps = 0
        self._bought = 0
        self._spent = 0
        self._coin_spent = 0
        self._visit_coins = None
        self._pending = None
        self._pending_card = None
        self._recovery_sample = None
        self._recovered_keys.clear()
        # Neither beginning nor ending a visit clears a dead process's row.
        # Only fresh recovery evidence can release that durable action gate.
        self._search = None
        self._cards_bought = 0
        self._exhausted = set()
        self._off_page_streak = 0
        self._blind_streak = 0
        self._last_page = None
        self._last_run_count = run_count
        self._step = Step.OPEN_WORKSHOP if categories else Step.OPEN_CARDS

        self._bus.publish(events.ShoppingStarted(
            visit=self._visit, dry_run=not shopping.armed,
        ))
        return True

    def reset(self) -> None:
        """Return to idle without ending a visit through the normal path.

        Cadence memory (_last_run_count) is deliberately NOT cleared here -
        it has to survive across visits to know how long it has been.
        """
        if self.account_state is not None:
            self.account_state.reset_confirmation()
        self._step = Step.IDLE
        self._categories = []
        self._answer_pending()
        self._recovery_sample = None
        self._recovered_keys.clear()
        self._search = None

    # -- one step ------------------------------------------------------------

    def advance(
        self,
        screen: Image,
        device: Any,
        shopping: Shopping,
        tuning: Strategy | None = None,
    ) -> None:
        """One step. At most one tap, and only when shopping.armed.

        `tuning` carries the live jitter policy for this pass. Stashed on
        the session rather than threaded through the eight `_try_tap` call
        sites between here and `_tap`: those all sit on the step-machine's
        own paths, and giving each an extra parameter to forward would be
        eight chances to forget one. `None` leaves taps un-jittered, which
        is how every direct caller behaved before jitter existed.

        Every path through here must either make progress, publish a skip,
        or end the visit. A step that silently does nothing is how a session
        wedges, which is what _off_page_streak exists to catch.
        """
        self._tuning = tuning
        if not self.active:
            return

        try:
            reading = pages.classify_page(screen, self._templates)
            if self._recover_transaction(reading, screen):
                return
            if not shopping.enabled:
                # Shopping was switched off mid-visit - or this is a stale
                # visit that survived a restart (see BotRunner.start()'s
                # reset() call, which now also catches this case before it
                # gets here). "I turned it off" has to mean the taps stop
                # NOW, not once the visit happens to wind down on its own:
                # this is the one tap path that spends currency, and it is
                # exactly the panic gesture someone reaches for mid-visit.
                # Routed through _abort rather than a bare `self._step =
                # Step.IDLE` so it gets the same recovery tap and the same
                # honest ShoppingEnded any other abort gets, instead of
                # leaving the bot silently stranded on a menu page.
                self._abort(device, shopping, screen, "shopping disabled")
                return
            if reading.page == pages.UNKNOWN:
                self._off_page_streak += 1
                if self._off_page_streak >= 2:
                    self._abort(device, shopping, screen, "unexpected page")
                return
            if self._acknowledge_modal(screen, device, shopping):
                return
            if reading.page != self._last_page:
                # Skip the very first frame of a visit: `_last_page` starts
                # None so there is nothing to have changed FROM, and
                # ShoppingStarted already marks the beginning on the feed.
                if self._last_page is not None:
                    self._bus.publish(events.PageChanged(
                        prev_page=self._last_page, curr_page=reading.page,
                        confidence=reading.confidence,
                    ))
                self._last_page = reading.page
            # Deliberately NOT reset here just because the page classified:
            # _register_progress (called from inside the handlers) is what
            # clears the streak, so a positioning step's own target-miss
            # (recognised page, wrong or absent button) still accumulates
            # across calls instead of being wiped before it can reach 2.
            self._dispatch(reading, screen, device, shopping)
        except Exception as exc:  # noqa: BLE001 - a shopping step must never
            # crash the scan loop; give the coins back to the user's control
            # instead by ending the visit and saying why.
            logger.exception("shopping step raised; ending the visit")
            try:
                self._abort(device, shopping, screen, f"error: {exc}")
            except Exception:  # noqa: BLE001 - the recovery path touches the
                # SAME screen that may have just caused the first exception
                # (_exit_to_battle re-runs vision.locate_template against
                # it), so it can fail too. This module's one promise is that
                # advance() never raises; a bad frame is not licensed to
                # break that promise twice. No ShoppingEnded may have been
                # published if _abort failed before reaching it, so force
                # idle directly rather than leaving the visit stuck retrying
                # the same crash forever.
                logger.exception(
                    "shopping recovery also raised; forcing idle without a return tap"
                )
                self._step = Step.IDLE
                self._categories = []

    def _acknowledge_modal(
        self, screen: Image, device: Any, shopping: Shopping
    ) -> bool:
        """Tap a one-time explainer dialog's OK, if one is up. True if tapped.

        These are queued by the game when a feature is unlocked and appear
        the next time the page is opened - a live visit met "ULTIMATE
        WEAPONS ... [OK]" the moment it reached the Workshop. A modal
        swallows every tap outside itself, so positioning kept tapping a tab
        that could never arrive and the visit spent its whole budget without
        reading a row. It is not the info panel _buy_rows handles: that one
        closes on any tap outside it, this one only on its own button.

        Checked on every step of a visit rather than only where it bit,
        because "a dialog is up" is not a property of any one step. That
        costs one OCR pass per scan; _buy_rows already pays for one, and the
        alternative is a shopping feature that stays dead until a human taps
        OK.

        Only an acknowledgement label counts. Anything offering a choice is
        left alone - this must never be the thing that answers a question
        the bot did not understand.
        """
        for box in ocr.read(screen):
            if box.text.strip().upper() in _MODAL_ACKNOWLEDGEMENTS:
                self._register_progress()
                self._try_tap(
                    box.rect.x + box.rect.w // 2,
                    box.rect.y + box.rect.h // 2,
                    device, shopping, screen,
                )
                return True
        return False

    def _register_progress(self) -> None:
        """Something real happened this call - a tap, an arrival, or a buy
        decision. Clears the no-progress streak so it only ever measures
        CONSECUTIVE stalls, not a lifetime total."""
        self._off_page_streak = 0

    def _miss(self, device: Any, shopping: Shopping, screen: Image, target: str) -> bool:
        """A positioning step could not find `target` this scan.

        Counts toward the same streak an unrecognized page does. One miss is
        an animation still settling; two in a row means the template is
        mis-cut or the game moved the button, and the visit ends rather than
        spinning silently - see the module docstring. Always returns False
        so callers can `return self._miss(...)`.
        """
        self._off_page_streak += 1
        if self._off_page_streak >= 2:
            self._abort(device, shopping, screen, f"{target} not found twice in a row")
        return False

    def _dispatch(self, reading, screen: Image, device: Any, shopping: Shopping) -> None:
        """Cascade through pure positioning transitions, stop at real work.

        A positioning handler returns True when it made a bookkeeping-only
        transition (nothing tapped, nothing published) - safe to keep
        going on the very same frame. BUY_ROWS and BUY_CARDS never cascade:
        each call does exactly one unit of buying work and stops, tapped or
        not, so that re-reading the header always reflects this call's own
        frame.
        """
        while True:
            step = self._step
            if step is Step.OPEN_WORKSHOP:
                if self._open_workshop(reading, screen, device, shopping):
                    continue
                return
            if step is Step.OPEN_TAB:
                if self._open_tab(reading, screen, device, shopping):
                    continue
                return
            if step is Step.BUY_ROWS:
                # A buy step always does something - tap, skip, or a
                # category handoff - so reaching it is progress in itself.
                # This resets the streak OPTIMISTICALLY, before _buy_rows
                # has actually run, rather than only on a confirmed tap or
                # publish. That is safe only because _buy_rows is itself
                # unconditionally self-terminating (every branch taps,
                # publishes, or hands off to the next step) - if a future
                # edit ever adds an early `return` that does none of those,
                # this reset would silently defeat the wedge protection for
                # that branch. Keep that invariant in mind before adding one.
                self._register_progress()
                self._buy_rows(reading, screen, device, shopping)
                return
            if step is Step.OPEN_CARDS:
                if self._open_cards(reading, screen, device, shopping):
                    continue
                return
            if step is Step.BUY_CARDS:
                # See the BUY_ROWS branch above - same optimistic-reset /
                # self-terminating coupling applies to _buy_cards.
                self._register_progress()
                self._buy_cards(reading, screen, device, shopping)
                return
            if step is Step.RETURN:
                if self._return(reading, screen, device, shopping):
                    continue
                return
            return  # IDLE, or reached mid-loop by an end_visit call

    # -- positioning ----------------------------------------------------------

    def _next_after_categories(self, shopping: Shopping) -> Step:
        return Step.OPEN_CARDS if shopping.cards.enabled else Step.RETURN

    def _open_workshop(self, reading, screen: Image, device: Any, shopping: Shopping) -> bool:
        # Defensive only: begin() sets _step to OPEN_WORKSHOP exclusively
        # when categories is non-empty, and nothing else ever routes back
        # here, so this branch is not known to be reachable today. Kept
        # rather than asserted against, so a future caller that DOES reach
        # this step with nothing queued degrades gracefully instead of
        # raising.
        if not self._categories:
            self._step = self._next_after_categories(shopping)
            self._register_progress()
            return True
        if reading.page == "WORKSHOP":
            self._step = Step.OPEN_TAB
            self._register_progress()
            return True
        match = vision.locate_template(
            screen, self._templates.get(config.NAV_TARGETS["WORKSHOP"]), self._threshold
        )
        if match is None:
            return self._miss(device, shopping, screen, "WORKSHOP nav button")
        x, y = match.center
        self._register_progress()
        self._try_tap(x, y, device, shopping, screen)
        return False

    def _open_tab(self, reading, screen: Image, device: Any, shopping: Shopping) -> bool:
        # Defensive only, same as OPEN_WORKSHOP's identical guard above:
        # _step becomes OPEN_TAB only via a cascade or a BUY_ROWS handoff
        # that both already confirmed _categories is non-empty. Not known
        # to be reachable today.
        if not self._categories:
            self._step = self._next_after_categories(shopping)
            self._register_progress()
            return True
        if reading.page != "WORKSHOP":
            # Navigation should already have landed us on WORKSHOP by the
            # time OPEN_TAB runs (see OPEN_WORKSHOP) - if it has not, that is
            # not a transient "still loading" state, it is a wedge.
            return self._miss(device, shopping, screen, "workshop page")

        category = self._categories[0]
        rows = [r for r in shopping.rows_for(category) if r.name not in self._exhausted]
        if not rows:
            # Nothing left to look for on this tab - BUY_ROWS will pop it.
            self._step = Step.BUY_ROWS
            self._register_progress()
            return True
        if _heading_names(screen, category):
            # Arrival is judged by the page's own heading - "UTILITY
            # UPGRADES" - which names the tab outright.
            #
            # Not by the tab button's look: every tab template still matches
            # its own selected page well above threshold (measured 0.955 on
            # the tab it is standing on), so its absence cannot signal
            # arrival. And no longer by locating one of this category's row
            # templates either: that answers "no" forever once the row is
            # bought and gone, and a live visit spent its entire budget
            # tapping the UTILITY tab it was already standing on for exactly
            # that reason. The heading is there whatever is left to buy.
            self._step = Step.BUY_ROWS
            self._register_progress()
            return True

        tab_template = config.WORKSHOP_TABS[category]
        # These four buttons share a bright border. On the first Workshop
        # visit the tutorial arrow can make Defense's border match the UW
        # button more strongly than Defense itself. Search only its column.
        lane = tuple(config.WORKSHOP_TABS).index(category)
        lane_width = screen.shape[1] // 4
        left = lane * lane_width
        right = (lane + 1) * lane_width
        template = self._templates.get(tab_template)
        # The tutorial arrow overlaps the lower half of Defense's button.
        # Its top border and icon remain unobscured in the live frame.
        match = vision.locate_template(
            screen[:, left:right], template[:template.shape[0] // 2], self._threshold,
        )
        if match is None:
            return self._miss(device, shopping, screen, f"{category} tab button")
        x = match.top_left[0] + left + template.shape[1] // 2
        y = match.top_left[1] + template.shape[0] // 2
        self._register_progress()
        self._try_tap(x, y, device, shopping, screen)
        return False

    def _open_cards(self, reading, screen: Image, device: Any, shopping: Shopping) -> bool:
        # First-visit popups (see config.NAV_DISMISS) sit between the Cards
        # tab and the page itself. Tried in order, same as navigate.py would.
        # Their absence is normal (most visits see no popup at all), so it
        # does not count as a miss.
        for dismiss_path in config.NAV_DISMISS:
            match = vision.locate_template(
                screen, self._templates.get(dismiss_path), self._threshold
            )
            if match is not None:
                x, y = match.center
                self._register_progress()
                self._try_tap(x, y, device, shopping, screen)
                return False
        if reading.page == "CARDS":
            self._step = Step.BUY_CARDS
            self._register_progress()
            return True
        match = vision.locate_template(
            screen, self._templates.get(config.NAV_TARGETS["CARDS"]), self._threshold
        )
        if match is None:
            return self._miss(device, shopping, screen, "CARDS nav button")
        x, y = match.center
        self._register_progress()
        self._try_tap(x, y, device, shopping, screen)
        return False

    def _return(self, reading, screen: Image, device: Any, shopping: Shopping) -> bool:
        if reading.page == "MAIN_MENU":
            self._end_visit(device, shopping, screen, aborted=False)
            return False
        match = vision.locate_template(
            screen, self._templates.get(config.NAV_TARGETS["BATTLE_TAB"]), self._threshold
        )
        if match is None:
            return self._miss(device, shopping, screen, "BATTLE_TAB nav button")
        x, y = match.center
        self._register_progress()
        self._try_tap(x, y, device, shopping, screen)
        return False

    # -- buying ----------------------------------------------------------------

    def _buy_rows(self, reading: Any, screen: Image, device: Any, shopping: Shopping) -> None:
        """Read, decide, then wait for another frame to acknowledge a purchase."""
        if self._recover_transaction(reading, screen):
            return
        if not self._categories:
            self._step = self._next_after_categories(shopping)
            return
        category = self._categories[0]
        observation = observe_frame(screen, "workshop")
        if self.account_state is not None:
            self.account_state.observe_account(observation)
        if self.observations is not None:
            self.observations.observe(observation)
        coins, _gems = header_numbers(screen, reading.page, reading.top_left)
        if self._pending is not None:
            self._confirm_purchase(observation, coins, device, shopping, screen)
            return
        rules = [r for r in shopping.rows_for(category) if r.name not in self._exhausted]
        if not rules:
            self._categories.pop(0)
            self._search = None
            self._step = Step.OPEN_TAB if self._categories else self._next_after_categories(shopping)
            return
        rule = rules[0]
        entry = upgrades.resolve(rule.name, category)
        if entry is None:
            self._exhausted.add(rule.name)
            self._bus.publish(events.PurchaseSkipped(
                item=rule.name, reason="unknown_identity",
                detail="no executable Workshop identity", coins_before=coins,
            ))
            return
        rule_id = entry.id
        if rule_id in self._completed_unlocks:
            self._exhausted.add(rule.name)
            self._bus.publish(events.PurchaseSkipped(item=rule.name, reason="already_unlocked"))
            return
        if coins is None:
            self._abort(device, shopping, screen, "unreadable balance")
            return
        if self._visit_coins is None:
            self._visit_coins = coins
        visible = observation.rows
        if not visible:
            self._blind_streak += 1
            if self._blind_streak >= 2:
                self._abort(device, shopping, screen, "nothing readable on the page")
                return
            self._try_tap(*config.PANEL_DISMISS_POINT, device, shopping, screen)
            self._bus.publish(events.PurchaseSkipped(item=rule.name, reason="unreadable", detail="screen"))
            return
        self._blind_streak = 0
        if observation.category != category:
            self._abort(device, shopping, screen, f"{category} heading not confirmed")
            return
        seen = _row_named(rule.name, visible, category)
        if self.reroll_observe_price is not None:
            self.reroll_observe_price(rule_id, coins,
                                      seen.price if seen is not None and seen.status == "available" else None)
        if seen is None and self._already_unlocked(entry, observation):
            self._retire_unlock(rule.name, rule_id, coins)
            return
        if seen is None:
            self._find_row(rule.name, observation, device, shopping, screen, coins)
            return
        self._search = None
        is_unlock = entry.unlock
        reason = None
        detail = ""
        if is_unlock and not shopping.allow_unlocks:
            reason, detail = "disabled", "unlock permission is off"
        elif seen.status != "available" or seen.price is None or seen.tap is None:
            reason, detail = "unreadable", "price" if seen.price is None else seen.status
        elif rule.target is not None and seen.value is None:
            reason, detail = "unreadable", "target requires a readable current value"
        elif rule.target is not None and _target_reached(seen.upgrade_id, seen.value, rule.target):
            reason, detail = "target_reached", f"current value {seen.value} meets target {rule.target}"
        elif seen.price > coins:
            reason = "unaffordable"
        elif coins - seen.price < shopping.coin_reserve:
            reason, detail = "reserve", "purchase would cross the coin reserve"
        elif shopping.armed and (budget := self._visit_budget(shopping)) is not None and (
                budget == 0 or self._coin_spent is None
                or self._coin_spent + seen.price > budget):
            # An unknown total cannot be shown to fit under a ceiling, so it
            # stops a bounded visit rather than being counted as nothing.
            reason, detail = "budget", (
                "this visit has already spent an unproven amount"
                if self._coin_spent is None
                else "purchase would exceed the Workshop visit budget")
        if reason is not None:
            self._bus.publish(events.PurchaseSkipped(item=rule.name, reason=reason,
                                                    detail=detail, coins_before=coins))
            self._exhausted.add(rule.name)
            return
        try:
            intent = self._open_intent(
                item=seen.name, category=seen.category, currency="coins",
                price=seen.price, wallet_before=coins, armed=shopping.armed,
                before={"upgrade_id": seen.upgrade_id, "value": seen.value,
                        "price": seen.price, "status": seen.status,
                        "confidence": seen.confidence, "observed_at": observation.observed_at,
                        "frame_digest": observation.frame_digest,
                        "frame_width": observation.frame_width, "frame_height": observation.frame_height},
            )
        except CommitmentError:
            self._bus.publish(events.PurchaseSkipped(item=rule.name, reason="reserve",
                                                    detail="funds committed to another plan", coins_before=coins))
            self._exhausted.add(rule.name)
            return
        try:
            sent = self._try_tap(*seen.tap, device, shopping, screen)
        except RecoveryPreflightBlocked:
            self._abandon_intent(intent, "input refused before tap")
            return
        if not sent:
            self._abandon_intent(intent, "the tap was never sent")
            return
        self._mark_acted(intent)
        if not _unlimited(shopping):
            # An unlimited visit re-buys: leaving the row un-exhausted sends
            # the next frame back through this same decision, now reading the
            # price the purchase just raised. What ends the rotation is the
            # wallet, not a counter - "unaffordable", "reserve", "maxed" and
            # "target_reached" all exhaust the row on their own way through.
            #
            # Only when armed. A rehearsal spends nothing, so the price it
            # re-reads is the price it just "paid": it would buy row one
            # forever and abort the visit on the tap budget instead of
            # reporting the list.
            self._exhausted.add(rule.name)
        if shopping.armed:
            self._pending = PendingPurchase(
                seen, coins, frozenset(r.upgrade_id for r in visible),
                key=intent.key if intent is not None else None,
            )
            if self.observations is not None:
                self.observations.decision("verifying", f"Confirming Workshop purchase: {seen.name}", seen.upgrade_id)
        else:
            self._record_purchase(seen, coins, dry_run=True)

    def _visit_budget(self, shopping: Shopping) -> int | None:
        """Coins this visit may spend in total, or None for no limit.

        Both limits bind when both are set, and the tighter one decides:
        a share of the wallet and an absolute ceiling answer different
        questions, so letting either silently outrank the other would make
        a configured number mean nothing.
        """
        limits = [limit for limit in (
            shopping.coin_budget,
            None if shopping.coin_budget_pct is None or self._visit_coins is None
            else int(shopping.coin_budget_pct * self._visit_coins),
        ) if limit is not None]
        return min(limits) if limits else None

    def _already_unlocked(self, entry: upgrades.Upgrade, observation: Observation) -> bool:
        """Is this unlock's row missing because the account already bought it?

        A granted row cannot appear on the tab until its unlock is bought, so
        seeing one is proof rather than an inference - which is the whole
        point. The alternative reading of a missing unlock row is "OCR lost
        it", and that one costs a full top-to-bottom scan of the tab, every
        visit, forever, for a row that is never coming back.

        Any ONE granted row settles it. They are revealed together, but the
        panel shows a handful of rows at a time, so requiring all of them
        would make the proof depend on where the tab happens to be scrolled.
        """
        if not entry.unlock or not entry.unlocks:
            return False
        return any(row.upgrade_id in entry.unlocks for row in observation.rows)

    def _retire_unlock(self, name: str, upgrade_id: str, coins: int | None) -> None:
        """Stop asking for an unlock this account has demonstrably bought.

        _completed_unlocks outlives the visit, so the proof is read once and
        every later visit skips the row before it costs a frame.
        """
        self._exhausted.add(name)
        self._completed_unlocks.add(upgrade_id)
        self._bus.publish(events.PurchaseSkipped(
            item=name, reason="already_unlocked",
            detail="the rows it grants are on the tab", coins_before=coins))

    def _record_purchase(self, row: ObservedUpgrade, coins: int, *, dry_run: bool,
                         verified: ObservedUpgrade | None = None,
                         outcome: transactions.Outcome | None = None) -> None:
        """Publish one purchase. A real one carries the journal's outcome.

        A rehearsal tallies the price it read, since that is all it is
        rehearsing. A real purchase tallies only what the outcome proved.
        """
        self._bought += 1
        spent = row.price if outcome is None else outcome.spent
        self._spend(spent, coins=True)
        self._bus.publish(events.Purchased(
            item=row.name, category=row.category, price=row.price,
            coins_before=coins, dry_run=dry_run,
            verdict=None if outcome is None else outcome.verdict.value,
            spent=None if outcome is None else outcome.spent,
        ))
        if not dry_run and self.observations is not None:
            self.observations.verified(verified or row)
            self.observations.decision("workshop", f"Verified Workshop purchase: {row.name}")

    def _confirm_purchase(self, observation: Observation, coins: int | None, device: Any,
                          shopping: Shopping, screen: Image) -> None:
        pending = self._pending
        # Buying an unlock can move the newly granted tiles under the same
        # touch. The game sometimes opens a child's info panel as the layout
        # changes. That overlay hides the receipt, so clear it once from the
        # Workshop title before counting confirmation frames.
        if not pending.info_dismissed and not observation.rows and self._info_panel_visible(screen):
            if self._try_tap(*config.PANEL_DISMISS_POINT, device, shopping, screen):
                pending.info_dismissed = True
            return
        before = pending.row
        after = next((r for r in observation.rows if r.upgrade_id == before.upgrade_id), None)
        same_category = observation.category == before.category
        changed = same_category and after is not None and (
            after.status == "maxed"
            or (after.price is not None and before.price is not None and after.price > before.price)
            or (after.value is not None and before.value is not None
                and after.value != before.value
                and _target_reached(before.upgrade_id, after.value, before.value))
        )
        entry = upgrades.resolve(before.name, before.category)
        is_unlock = (entry is not None and entry.unlock) or tiles.normalise(before.name).startswith("unlock")
        # A missing tile alone can be an OCR miss. Require newly visible rows
        # and the matching coin debit as evidence of an unlock transition.
        unlocked = (same_category and is_unlock and after is None and coins is not None
                    and coins <= pending.coins - before.price
                    and any(r.upgrade_id not in pending.visible_ids for r in observation.rows))
        if changed or unlocked:
            confirmed = after if changed else replace(before, status="unlocked", price=None,
                                                     observed_at=observation.observed_at)
            if unlocked and self.observations is not None:
                self.observations.observe(replace(observation, rows=(*observation.rows, confirmed)))
            if unlocked:
                self._completed_unlocks.add(before.upgrade_id)
            outcome = self._close(pending.key, price=before.price, wallet_before=pending.coins,
                                  wallet_after=coins, effect_changed=True)
            self._record_purchase(before, pending.coins, dry_run=False, verified=confirmed,
                                  outcome=outcome)
            self._pending = None
            return
        pending.frames += 1
        if pending.frames >= 3:
            # The row did not change within the window. Whether the coins
            # moved anyway is exactly what is not known, so the journal
            # records that and claims nothing further.
            self._pending = None
            self._spend(self._close(pending.key, price=before.price, wallet_before=pending.coins,
                                    wallet_after=coins, effect_changed=None).spent, coins=True)
            self._bus.publish(events.PurchaseSkipped(item=before.name, reason="unconfirmed",
                                                    detail="purchase did not produce a readable change"))
            if self.observations is not None:
                self.observations.decision("blocked", f"Workshop purchase of {before.name} was not confirmed")
            self._abort(device, shopping, screen, "purchase acknowledgement was inconclusive")
        elif self.observations is not None:
            self.observations.decision("verifying", f"Waiting for {before.name} to change", before.upgrade_id)

    @staticmethod
    def _info_panel_visible(screen: Image) -> bool:
        labels = {box.text.strip().lower() for box in ocr.read(screen)
                  if box.confidence >= .9}
        return "current level" in labels and "max level" in labels

    def _find_row(self, name: str, observation: Observation, device: Any,
                  shopping: Shopping, screen: Image, coins: int) -> None:
        """Find a hidden row with a bounded top-to-bottom scan of this category."""
        if self._search is None or self._search.name != name:
            self._search = RowSearch(name)
        search = self._search
        fingerprint = tuple(r.upgrade_id for r in observation.rows)
        limit = self._tuning.autopilot.max_scrolls if self._tuning is not None else 8
        at_end = fingerprint == search.fingerprint or search.scrolls >= limit
        if at_end and not search.down:
            search.down, search.scrolls = True, 0
        elif at_end or observation.heading_y is None or not shopping.armed:
            self._bus.publish(events.RowUnmatched(item=name, read=tuple(r.name for r in observation.rows)))
            self._bus.publish(events.PurchaseSkipped(item=name, reason="no_match", coins_before=coins))
            self._exhausted.add(name)
            entry = upgrades.resolve(name, observation.category)
            if self.observations is not None:
                if entry is not None:
                    self.observations.unknown(entry, "workshop", observation.observed_at)
                self.observations.decision("workshop", f"{name} was not found; availability is unknown")
            self._search = None
            return
        if self._taps >= shopping.max_taps_per_visit:
            self._abort(device, shopping, screen, "tap budget exhausted")
            return
        from autopilot import scroll_panel
        self._taps += 1
        scroll_panel(device, screen, observation.heading_y, down=search.down)
        search.scrolls += 1
        search.fingerprint = fingerprint
        if self.observations is not None:
            self.observations.decision("discovering", f"Scanning Workshop for {name}")

    def _buy_cards(self, reading, screen: Image, device: Any, shopping: Shopping) -> None:
        """Buy the configured batch, repeatedly, up to the per-visit cap.

        Unlike a workshop row, a card batch is not "used up" by one
        purchase - the game lets the same button be bought again - so this
        does not add to _exhausted after a successful buy. _cards_bought and
        max_per_visit are what bound it instead.
        """
        if self._recover_transaction(reading, screen):
            return
        cards = shopping.cards
        if not cards.enabled:
            self._step = Step.RETURN
            return
        if self._cards_bought >= cards.max_per_visit:
            self._step = Step.RETURN
            return

        # Coins are irrelevant to a card purchase (it spends gems) - the
        # header is always read as a pair, so the unused half is discarded
        # rather than stored on an attribute nothing ever reads back.
        _coins, gems = header_numbers(screen, reading.page, reading.top_left)
        if gems is None:
            self._abort(device, shopping, screen, "unreadable balance")
            return

        # A tap already went out and has not been answered for. Nothing else
        # in this step may run until it is - buying again while the first
        # purchase is unproven is how one tap becomes two.
        if self._pending_card is not None:
            self._confirm_card(gems, device, shopping, screen)
            return

        item = cards.batch
        template_path = config.CARD_BUTTONS[item]
        match = vision.locate_template(screen, self._templates.get(template_path), self._threshold)
        if match is None:
            self._bus.publish(
                events.PurchaseSkipped(
                    item=item, reason="no_match", gems_before=gems,
                )
            )
            self._step = Step.RETURN
            return

        price = self._reader.read(screen, config.CARD_PRICE_REGION, match.top_left, "menu")
        if price is None:
            self._bus.publish(
                events.PurchaseSkipped(item=item, reason="unreadable", detail="price",
                    gems_before=gems,
                )
            )
            self._step = Step.RETURN
            return

        if price > gems:
            self._bus.publish(
                events.PurchaseSkipped(item=item, reason="unaffordable",
                    gems_before=gems,
                )
            )
            self._step = Step.RETURN
            return

        if gems - price < cards.gem_floor:
            self._bus.publish(
                events.PurchaseSkipped(
                    item=item, reason="capped", detail="gem floor",
                    gems_before=gems,
                )
            )
            self._step = Step.RETURN
            return

        x, y = match.center
        # Written BEFORE the tap, which is the only ordering that helps: a
        # journal entry made afterwards is lost by exactly the crash it
        # exists to survive.
        try:
            intent = self._open_intent(
                item=item, category="CARDS", currency="gems", price=price,
                wallet_before=gems, armed=shopping.armed,
            )
        except CommitmentError:
            self._bus.publish(events.PurchaseSkipped(
                item=item, reason="reserve", detail="funds committed to another plan",
                gems_before=gems))
            self._step = Step.RETURN
            return
        try:
            sent = self._try_tap(x, y, device, shopping, screen)
        except RecoveryPreflightBlocked:
            self._abandon_intent(intent, "input refused before tap")
            return
        if not sent:
            self._abandon_intent(intent, "the tap was never sent")
            return
        self._mark_acted(intent)

        if shopping.armed:
            # The tap is now a question, not a result. _confirm_card answers
            # it off the next frame's gem balance.
            self._pending_card = PendingCard(
                item=item, price=price, gems_before=gems,
                key=intent.key if intent is not None else None,
            )
        else:
            # A rehearsal never tapped, so no balance will ever move and
            # there is nothing to confirm. Mirrors the workshop path, which
            # records an unarmed purchase immediately for the same reason.
            self._record_card(item, price, gems, dry_run=True)

    # -- the durable journal ------------------------------------------------

    def _unanswered_transaction(self) -> transactions.Transaction | None:
        """An attempt on disk that this process cannot account for.

        In a living session every attempt is opened, acted on and resolved
        inside one step, so the only way one is found still open here is
        that the process which made it died before it could be answered.
        """
        if self.journal is None:
            return None
        still_open = self.journal.open_transactions()
        pending_keys = {pending.key for pending in (self._pending, self._pending_card) if pending is not None}
        return next((txn for txn in still_open if txn.key not in pending_keys), None)

    def _recover_transaction(self, reading: Any, screen: Image) -> bool:
        """A recovery scan consumes the step and can never send a device action."""
        if self._restore_recovered_visit():
            return True
        txn = self._unanswered_transaction()
        if txn is None:
            return False
        now = time.time()
        digest = hashlib.sha256(screen.tobytes()).hexdigest()
        category, currency, wallet, changed, value = None, None, None, None, None
        observed_at = now
        page = reading.page.upper()
        if txn.category == "CARDS" and page == "CARDS":
            category, currency = "CARDS", "gems"
            _, wallet = header_numbers(screen, reading.page, reading.top_left)
            changed = (txn.price is not None and txn.price > 0 and wallet is not None
                       and txn.wallet_before is not None and txn.wallet_before - wallet == txn.price)
        elif txn.currency == "coins" and page == "WORKSHOP":
            observation = observe_frame(screen, "workshop")
            now = time.time()
            before = txn.before
            row = _row_named(txn.item, observation.rows, txn.category)
            valid = (
                observation.context == "workshop" and observation.category == txn.category
                and observation.frame_digest == digest
                and observation.frame_width == screen.shape[1] == before.get("frame_width")
                and observation.frame_height == screen.shape[0] == before.get("frame_height")
                and txn.acted_at is not None and txn.acted_at < observation.observed_at <= now
                and now - observation.observed_at <= 30
                and bool(before.get("frame_digest")) and before.get("confidence", 0) >= .9
                and before.get("observed_at") is not None
                and 0 <= txn.acted_at - before["observed_at"] <= 30
                and row is not None and row.upgrade_id == before.get("upgrade_id")
                and math.isfinite(row.confidence) and .9 <= row.confidence <= 1
                and row.observed_at == observation.observed_at
                and row.status in ("available", "maxed")
            )
            if valid:
                category, currency = observation.category, "coins"
                observed_at = observation.observed_at
                wallet, _ = header_numbers(screen, reading.page, reading.top_left)
                value = row.value
                changed = (
                    row.status == "maxed" and before.get("status") != "maxed"
                    or row.value is not None and math.isfinite(row.value)
                    and before.get("value") is not None and row.value != before["value"]
                    and _target_reached(row.upgrade_id, row.value, before["value"])
                )
                if self.account_state is not None:
                    self.account_state.observe_account(observation)
                    if self.account_state.snapshot()["error"]:
                        changed = None
                if self.observations is not None:
                    self.observations.observe(observation)
        evidence = transactions.RecoveryEvidence(
            category=category, currency=currency, wallet_after=wallet,
            effect_changed=changed, observed_at=observed_at, frame_digest=digest,
            effect_value=value,
        )
        previous = self._recovery_sample
        consistent = (
            previous is not None and previous[0] == txn.key
            and 0 < observed_at - previous[1].observed_at <= 30
            and replace(previous[1], observed_at=observed_at, frame_digest=digest) == evidence
        )
        self._recovery_sample = (txn.key, evidence)
        outcome = self.journal.reconcile(
            txn.key, evidence if consistent else replace(evidence, effect_changed=None), now=time.time(),
        )
        if outcome.verdict == transactions.Verdict.UNPROVEN:
            self._spent = None
            if txn.currency == "coins":
                self._coin_spent = None
            self._bus.publish(events.PurchaseSkipped(item=txn.item, reason="unreconciled", detail=outcome.reason))
            if self.observations is not None:
                self.observations.decision("blocked", outcome.reason)
            return True
        if self.currencies is not None:
            self.currencies.release(f"purchase:{txn.key}", txn.currency)
        self._restore_recovered_visit()
        self._recovery_sample = None
        return True

    def _restore_recovered_visit(self) -> bool:
        """Recovery finishes the interrupted visit; receipts survive until it ends."""
        receipts = self.journal.recovered_visit() if self.journal else ()
        if not receipts or all(txn.key in self._recovered_keys for txn, _ in receipts):
            return False
        self._spent, self._coin_spent, self._bought, self._cards_bought = 0, 0, 0, 0
        for txn, outcome in receipts:
            self._spend(outcome.spent, coins=txn.currency == "coins")
            self._bought += 1
            if txn.currency == "coins":
                if self._visit_coins is None:
                    self._visit_coins = txn.wallet_before
                self._exhausted.add(txn.item)
            else:
                self._cards_bought += 1
            if txn.key not in self._recovered_keys:
                self._bus.publish(self.journal.recovery_event(txn, outcome))
                self._recovered_keys.add(txn.key)
        self._categories = []
        self._step = Step.RETURN
        return True

    def _open_intent(
        self, *, item: str, category: str, currency: str, price: int,
        wallet_before: int, armed: bool, before: dict[str, Any] | None = None,
    ) -> transactions.Transaction | None:
        """Record what is about to be attempted. None when nothing will be.

        A rehearsal writes nothing: it sends no tap, so there is no window
        for a crash to fall into and nothing for a later process to
        reconcile.
        """
        if self.journal is None or not armed:
            return None
        request = transactions.Intent(
            item=item, category=category, currency=currency, price=price,
            wallet_before=wallet_before, ts=time.time(),
            before=before or {},
        )
        owner = f"purchase:{request.key}"
        if self.currencies is not None and not self.currencies.reserve(
                owner, currency, price, wallet=wallet_before):
            raise CommitmentError("wallet cannot cover this purchase and its commitments")
        try:
            return self.journal.open(request)
        except Exception:
            if self.currencies is not None:
                self.currencies.release(owner, currency)
            raise

    def _mark_acted(self, intent: transactions.Transaction | None) -> None:
        if self.journal is not None and intent is not None:
            self.journal.record_action(intent.key, at=time.time())

    def _abandon_intent(
        self, intent: transactions.Transaction | None, reason: str
    ) -> None:
        """Close an intent whose device action was never sent.

        Nothing moved, so this resolves cleanly rather than being left for
        a restart to puzzle over.
        """
        if self.journal is not None and intent is not None:
            self.journal.resolve(
                intent.key, wallet_after=intent.wallet_before,
                effect_changed=False, ts=time.time(),
            )
            if self.currencies is not None:
                self.currencies.release(f"purchase:{intent.key}", intent.currency)
            logger.debug("intent for %s abandoned: %s", intent.item, reason)

    def _close(
        self, key: str | None, *, price: int | None, wallet_before: int | None,
        wallet_after: int | None, effect_changed: bool | None,
    ) -> transactions.Outcome:
        """Answer one attempt with the evidence that followed.

        Always judged, journal or not: the verdict is what decides what
        this attempt may be tallied and recorded as having cost, and a
        session without a journal must not fall back to the read price.
        """
        if self.journal is not None and key is not None:
            currency = self.journal._require(key).currency
            outcome = self.journal.resolve(
                key, wallet_after=wallet_after, effect_changed=effect_changed,
                ts=time.time(),
            )
            if (self.currencies is not None and currency is not None
                    and outcome.verdict != transactions.Verdict.UNPROVEN):
                self.currencies.release(f"purchase:{key}", currency)
            return outcome
        return transactions.judge(
            key or "", price=price, wallet_before=wallet_before,
            wallet_after=wallet_after, effect_changed=effect_changed,
        )

    def _spend(self, amount: int | None, *, coins: bool) -> None:
        """Add one attempt's cost to the visit, keeping unknown unknown."""
        self._spent = None if self._spent is None or amount is None else self._spent + amount
        if coins:
            self._coin_spent = (None if self._coin_spent is None or amount is None
                                else self._coin_spent + amount)

    def _answer_pending(self) -> None:
        """Close the attempts this visit tapped and never saw answered.

        No frame is left to judge them by, so each resolves UNPROVEN and
        the visit's spend becomes unknown - the tap went out, and nothing
        says what it cost. Rows a dead process left open stay blocked until
        fresh evidence reconciles them.
        """
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self._spend(self._close(
                pending.key, price=pending.row.price, wallet_before=pending.coins,
                wallet_after=None, effect_changed=None).spent, coins=True)
        if self._pending_card is not None:
            card, self._pending_card = self._pending_card, None
            self._spend(self._close(
                card.key, price=card.price, wallet_before=card.gems_before,
                wallet_after=None, effect_changed=None).spent, coins=False)

    def _record_card(self, item: str, price: int, gems: int, *, dry_run: bool,
                     outcome: transactions.Outcome | None = None) -> None:
        self._cards_bought += 1
        self._bought += 1
        self._spend(price if outcome is None else outcome.spent, coins=False)
        self._bus.publish(events.Purchased(
            # A card purchase spends gems, not coins - gems_before is the
            # honest field for it. coins_before stays at its default None
            # here rather than being reused for the wrong currency (see
            # events.Purchased's own docstring).
            item=item, category="CARDS", price=price, gems_before=gems,
            dry_run=dry_run,
            verdict=None if outcome is None else outcome.verdict.value,
            spent=None if outcome is None else outcome.spent,
        ))

    def _confirm_card(self, gems: int, device: Any, shopping: Shopping,
                      screen: Image) -> None:
        """Answer an outstanding card tap from the gem balance.

        The evidence is a fall of exactly the price. A smaller fall, no
        fall, or a rise all leave it unproven: gems arrive from missions
        and rewards at any moment, so "the balance is lower" on its own
        does not name this purchase as the cause.
        """
        pending = self._pending_card
        if gems == pending.gems_before - pending.price:
            self._pending_card = None
            outcome = self._close(pending.key, price=pending.price,
                                  wallet_before=pending.gems_before,
                                  wallet_after=gems, effect_changed=True)
            self._record_card(pending.item, pending.price, pending.gems_before,
                              dry_run=False, outcome=outcome)
            return

        pending.frames += 1
        if pending.frames >= 3:
            self._pending_card = None
            # The gems did not move as predicted and the frames ran out.
            # Whether they moved for some other reason is exactly what is
            # not known, so the journal records that and nothing more.
            self._spend(self._close(
                pending.key, price=pending.price, wallet_before=pending.gems_before,
                wallet_after=gems, effect_changed=None).spent, coins=False)
            self._bus.publish(events.PurchaseSkipped(
                item=pending.item, reason="unconfirmed",
                detail="card purchase did not move the gem balance",
                gems_before=gems,
            ))
            # Aborting rather than moving on, exactly as an unconfirmed
            # workshop purchase does: the gems may in fact be gone, and a
            # visit that cannot tell has no business tapping again.
            self._abort(device, shopping, screen,
                        "card purchase acknowledgement was inconclusive")

    # -- tapping and ending ------------------------------------------------

    def _tap(self, device: Any, x: int, y: int, shopping: Shopping) -> None:
        """The ONLY place device.tap is called anywhere in this module.

        Counts the attempt regardless of armed, so a rehearsal hits the same
        tap-budget ceiling a real run would - the whole point of a rehearsal
        is that it behaves exactly like the real thing except for this one
        line.

        Being the only tap site is also what makes jitter here cover every
        workshop tab, workshop row, card buy and exit-to-battle tap at once.
        The offset and the pause are applied AFTER the armed check, so a
        rehearsal stays instant instead of paying a per-tap pause for taps
        it is not going to send.
        """
        self._taps += 1
        if not shopping.armed:
            return
        if self._tuning is not None:
            x, y = jitter.point(x, y, self._tuning.tap_jitter_px)
            jitter.pause(self._tuning.tap_delay, self._tuning.timing_jitter)
        tap(device, x, y)

    def _try_tap(
        self, x: int, y: int, device: Any, shopping: Shopping, screen: Image
    ) -> bool:
        """Tap if the budget allows it; abort the visit if it does not.

        Checked before tapping, not after: incrementing first and checking
        next call would let one call slip a tap past the cap.
        """
        if self._taps >= shopping.max_taps_per_visit:
            self._abort(device, shopping, screen, "tap budget exhausted")
            return False
        self._tap(device, x, y, shopping)
        return True

    def _exit_to_battle(self, device: Any, shopping: Shopping, screen: Image) -> None:
        """Best-effort return tap used by every abort path.

        Deliberately NOT gated by the tap budget (round-2 fix: the first
        cut of this method WAS gated, which meant a budget-exhaustion abort
        - the most common abort there is, since the budget is the primary
        safety valve - could never make this tap. The result was worse than
        untidy: with _step forced to IDLE, advance() no-ops forever;
        navigate.Navigator cannot rescue it either, since it only acts on
        GAME_OVER and MAIN_MENU and a menu page classifies UNKNOWN to the
        screen tracker by design. The bot would sit on the Workshop or
        Cards page indefinitely, tapping nothing, while unknown-screen
        snapshotting quietly filled up with pictures of it.

        The cap exists to stop an errand from tapping indefinitely, not to
        strand the bot somewhere it cannot leave once the cap trips. This is
        the one tap explicitly licensed past the ceiling - one tap over a
        40-tap budget is not the risk the ceiling guards against.
        """
        match = vision.locate_template(
            screen, self._templates.get(config.NAV_TARGETS["BATTLE_TAB"]), self._threshold
        )
        if match is not None:
            x, y = match.center
            self._tap(device, x, y, shopping)

    def _abort(self, device: Any, shopping: Shopping, screen: Image, reason: str) -> None:
        self._end_visit(device, shopping, screen, aborted=True, reason=reason)

    def _end_visit(
        self, device: Any, shopping: Shopping, screen: Image, *, aborted: bool, reason: str = ""
    ) -> None:
        stale = self._unanswered_transaction()
        if stale is not None:
            self._spent = None
            if stale.currency == "coins":
                self._coin_spent = None
        if aborted and stale is None:
            self._exit_to_battle(device, shopping, screen)
        # Before the total is published, so a tap still awaiting its answer
        # makes the total unknown rather than silently absent from it.
        self._answer_pending()
        self._bus.publish(events.ShoppingEnded(
            visit=self._visit, bought=self._bought, spent=self._spent,
            aborted=aborted, reason=reason,
        ))
        if self.journal is not None:
            self.journal.finish_recovered_visit(self._recovered_keys)
        if self.account_state is not None:
            self.account_state.reset_confirmation()
        self._step = Step.IDLE
        self._categories = []
        self._pending = None
        self._pending_card = None
        self._recovery_sample = None
        self._search = None
