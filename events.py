"""Typed events and the bus that fans them out to sinks.

The bus is the seam between the scan loop and everything that observes it.
Its one hard invariant: publish() never blocks. Sinks offer into bounded
queues and drop on overflow, so a slow browser or a locked database can
never stall the bot.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event. The bus stamps seq and ts on publish."""

    seq: int = 0
    ts: float = 0.0

    @property
    def type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, kw_only=True)
class IdentityIncident(Event):
    message: str


@dataclass(frozen=True, kw_only=True)
class ScreenChanged(Event):
    prev: str
    curr: str
    confidence: float
    scores: dict[str, float]


@dataclass(frozen=True, kw_only=True)
class ScanCompleted(Event):
    screen: str
    duration_ms: float
    wallet: int | None = None


@dataclass(frozen=True, kw_only=True)
class Tapped(Event):
    action: str
    x: int
    y: int
    score: float
    price: int | None = None
    wallet: int | None = None


@dataclass(frozen=True, kw_only=True)
class BattlePurchased(Event):
    item: str
    upgrade_id: str
    price: int | None
    value: float | None


@dataclass(frozen=True, kw_only=True)
class AutopilotDecided(Event):
    """The battle autopilot changed what it is doing, and why.

    Published only on a change, so a run that stops buying says which state
    it stopped in - saving, blocked, waiting - instead of going quiet.
    """
    phase: str
    reason: str
    upgrade_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class Skipped(Event):
    action: str
    reason: str  # paused | screen_gated | dimmed | unaffordable | cooldown
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    run_id: int
    purpose: str = "farm"


@dataclass(frozen=True, kw_only=True)
class RunEnded(Event):
    run_id: int
    duration: float
    wave: int | None = None
    coins: int | None = None
    tier: int | None = None
    abandoned: bool = False


@dataclass(frozen=True, kw_only=True)
class Navigated(Event):
    target: str  # RETRY | BATTLE


@dataclass(frozen=True, kw_only=True)
class PageChanged(Event):
    """Which MENU page is showing. Separate from ScreenChanged, which tracks
    the run lifecycle - see pages.py for why the two never merge.

    Fields named `prev_page` and `curr_page` rather than `prev` and `curr`.
    This is not cosmetic: sinks/store.py has a special case that maps
    `ScreenChanged.curr` to the `screen` column (the run-lifecycle screen).
    A `PageChanged` with a field called `curr` would hit that same branch,
    writing menu pages (WORKSHOP, CARDS) into the `screen` column. The
    database schema groups events by `screen` for the Stats dashboard's
    "events by screen" chart, and menu pages mixed in would silently change
    what a chart the user already relies on means. With the fields renamed,
    they fall through to the JSON `detail` blob and the `screen` column keeps
    meaning exactly one thing: the run-lifecycle screen. Renaming is the
    smaller and more durable fix than special-casing store.to_row.
    """

    prev_page: str
    curr_page: str
    confidence: float


@dataclass(frozen=True, kw_only=True)
class ShoppingStarted(Event):
    """A visit begins. No `coins`/`gems` fields here on purpose: begin()
    has not read a frame yet when this publishes, so any balance here would
    always be None - a field that can only ever hold one value is not
    reporting anything. The first real balance shows up on the first
    Purchased or PurchaseSkipped of the visit instead."""

    visit: int
    dry_run: bool = True


@dataclass(frozen=True, kw_only=True)
class ShoppingUnavailable(Event):
    """Published once, the first time ShoppingSession.begin() declines
    because this machine's header atlas cannot support a balance read - not
    on every scan that follows, which would flood the feed with the same
    fact forever. See build_shopping() and ShoppingSession.begin()."""

    reason: str


@dataclass(frozen=True, kw_only=True)
class Purchased(Event):
    """One thing bought, or - when dry_run - one thing that would have been.

    `price`, `coins_before` and `gems_before` are None rather than 0 when
    they could not be read. Zero is a free upgrade; None is "we did not
    know", and collapsing the two would make an unreadable price look like a
    bargain in the log.

    `coins_before` and `gems_before` are separate fields, not one balance
    reused for whichever currency this purchase spent: a card purchase
    spends gems, and a `Purchased(category="CARDS")` row whose `coins_before`
    silently held the gem balance would be exactly the kind of dishonesty
    this event exists to prevent. A workshop row purchase spends coins and
    reports `coins_before`; a card purchase spends gems and reports
    `gems_before`; the other field stays None for that purchase rather than
    being reused for the wrong currency.

    `price` is what was READ before the tap. `verdict` and `spent` are what
    the transaction journal proved afterwards, and only `spent` may become a
    debit: a proven amount, 0 when nothing provably left the wallet, None
    when the evidence could not say. `verdict` is None for a rehearsal and
    for events written before purchases were journalled.
    """

    item: str
    category: str
    price: int | None = None
    coins_before: int | None = None
    gems_before: int | None = None
    dry_run: bool = True
    verdict: str | None = None
    spent: int | None = None
    transaction_key: str | None = None


@dataclass(frozen=True, kw_only=True)
class PurchaseSkipped(Event):
    """One thing not bought, and why.

    `coins_before` and `gems_before` follow the same rule Purchased sets out:
    the currency this row WOULD have spent carries the balance, the other
    stays None rather than being reused for the wrong currency. The ledger
    infers a skip's currency from which of the two is set, so filling both
    would make it report the wrong one.

    They are here because a skip is a balance reading. Most rows on this
    account are unaffordable, so a visit that buys nothing is the common
    case - and without these fields it would tell the ledger nothing about
    where the balances actually stand.
    """

    item: str
    # What refused the row, in the vocabulary shopping.py actually emits:
    #   unaffordable | reserve | budget - the wallet, the floor under it,
    #     or this visit's allowance said no.
    #   already_unlocked - the tab is showing the rows this unlock grants,
    #     so it was bought before and is never coming back.
    #   no_match | unreadable | unknown_identity - the row, its price or
    #     its identity could not be read.
    #   target_reached | capped - the row is finished with.
    #   disabled - an unlock row with unlock permission off. Note that a
    #     row disabled in the STRATEGY never reaches here: rows_for()
    #     filters it out before BUY_ROWS sees it.
    #   unconfirmed | unreconciled - a tap whose effect nothing could
    #     confirm, this visit or a dead process's.
    reason: str
    detail: str = ""
    coins_before: int | None = None
    gems_before: int | None = None


@dataclass(frozen=True, kw_only=True)
class RowUnmatched(Event):
    """A configured row was not among the rows OCR read off the page.

    Two very different things look identical without this: a row that has
    been bought and is gone from the page (normal, permanent), and a row
    whose name OCR garbles on every scan (a defect that would otherwise
    present as a row that mysteriously never buys). `read` carries the raw
    strings so the difference is visible in the feed rather than guessed at.

    Names are published verbatim - NOT normalised. Normalisation is what
    hid the difference; `'Damage / Meter C'` is the whole point.
    """

    item: str
    read: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ShoppingEnded(Event):
    visit: int
    bought: int
    # None when any attempt this visit made resolved without proving what it
    # cost: the visit's total is then unknown, not the sum of what was read.
    spent: int | None
    aborted: bool = False
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class UnknownScreen(Event):
    snapshot_path: str
    best_anchor: str
    best_score: float


@dataclass(frozen=True, kw_only=True)
class BotError(Event):
    message: str
    traceback: str = ""


@dataclass(frozen=True, kw_only=True)
class ControlChanged(Event):
    """A live setting changed. `changed` holds only the fields that moved.

    Persisted through the events table's JSON `detail` blob, so it needs no
    schema migration - there is no `changed` column and there does not need
    to be one.
    """

    changed: dict[str, Any]
    source: str = "web"


class Sink(Protocol):
    """A consumer of events. offer() MUST NOT block."""

    def offer(self, event: Event) -> bool:
        """Accept the event, or return False if it was dropped."""
        ...


class EventBus:
    """Thread-safe fan-out. publish() never blocks."""

    def __init__(self, start_seq: int = 0) -> None:
        self._counter = itertools.count(start_seq + 1)
        self._lock = threading.Lock()
        self._sinks: list[Sink] = []
        self.dropped = 0

    def subscribe(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def publish(self, event: Event) -> Event:
        with self._lock:
            stamped = replace(event, seq=next(self._counter), ts=time.time())
            sinks = list(self._sinks)

        for sink in sinks:
            if not sink.offer(stamped):
                with self._lock:
                    self.dropped += 1
        return stamped


@dataclass(frozen=True, kw_only=True)
class SpeedAdjusted(Event):
    """One arrow tap on the in-battle speed widget.

    Carries the reading it decided from, not just the direction: "tapped up"
    alone cannot be checked against anything later, whereas "read x1.0,
    wanted x2.0, tapped up" says whether the tap was the right call even if
    the next frame shows it did nothing.
    """

    direction: str  # up | down
    source: str = "policy"  # policy | web
    # Both None for a manual nudge from the dashboard: that path deliberately
    # does not read the widget first, because a button press means "one step
    # from wherever it is now", not "move toward a value".
    reading: float | None = None
    target: float | None = None


@dataclass(frozen=True, kw_only=True)
class ClaimStarted(Event):
    """One claim walk armed. `target` is which page it walks."""

    target: str


@dataclass(frozen=True, kw_only=True)
class MissionClaimed(Event):
    """One mission reward taken, with the evidence that it landed.

    `completed_before`/`completed_after` are the daily counter either side of
    the tap and are what PROVE the claim happened: the counter moves the
    moment a reward is taken, and the card itself disappears.

    `coins` and `gems` are what the card offered, read from its reward
    column, and are None rather than 0 when they could not be read - zero is
    a reward of nothing, None is "we did not know".

    `gems_before` is meant to be the gem balance before the claim, and exact
    rather than an abbreviation - unlike `coins`, the missions page never
    rounds gems. There is deliberately no `coins_before`: the page abbreviates
    coins ("6.08K"), and a field holding a rounded balance would be read as a
    real one by the reconciler.

    No producer in this slice sets `gems_before`: `MissionsReading` carries no
    wallet balance, only `ClaimTarget`'s reward amounts, and `claim_evidence`
    carries no balance either. Every `MissionClaimed` this slice publishes
    therefore has `gems_before=None`; reading a real wallet balance here is
    later work, not something a `None` here should be mistaken for.
    """

    mission: str
    mission_id: str | None = None
    coins: int | None = None
    gems: int | None = None
    completed_before: int | None = None
    completed_after: int | None = None
    gems_before: int | None = None


@dataclass(frozen=True, kw_only=True)
class ClaimSkipped(Event):
    """A claim the walk refused to make, and why it refused."""

    target: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class MilestoneClaimed(Event):
    """One milestone reward taken, as the reward modal itself named it.

    `reward_text` is the modal's own line ("25 COINS"), which is what makes
    this exact: the ladder's slots are told apart by icon art nothing here
    reads, but the ceremony states its reward in words.

    `currency` and `amount` are None together when the reward moved no
    currency - `Unlock Lab` is a real reward on the recorded ladder - which the
    ledger encodes as delta=0. There is deliberately no balance field: the
    header abbreviates coins ("6.08K"), and this reader parses no wallet.

    There is no per-claim counter on this page. The evidence that a claim
    landed is the modal closing and, for the walk as a whole, `Claim All`
    disappearing. Weaker than the missions counter, and stated rather than
    implied.
    """

    reward_text: str | None = None
    currency: str | None = None
    amount: int | None = None
    tier: int | None = None


@dataclass(frozen=True, kw_only=True)
class FloatingGemClaimed(Event):
    """The free gem that orbits the tower mid-battle, taken and PROVEN taken.

    Published only when the HUD gem counter actually rose across the tap.
    The gem orbits, so a tap can land where the sprite was rather than where
    it is, and a tap alone is not evidence - an unconfirmed one publishes
    ClaimUncertain instead, exactly as a menu claim does.

    `delta` is therefore always a real subtraction of two readings, never
    the two gems the wiki says a pickup pays. That number is what the game
    is documented to give, not what this bot watched it give.
    """

    point: tuple[int, int]
    gems_before: int
    gems_after: int
    delta: int
    run_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class ClaimUncertain(Event):
    """A claim that was TAPPED and never confirmed. It may have landed.

    Distinct from ClaimSkipped, which is a claim the walk refused to make and
    which therefore provably moved nothing. This one moved an unknown amount,
    which is the fact LedgerLine reserves delta=None for.
    """

    target: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class ClaimEnded(Event):
    """How a claim walk ended. `claimed` is how many rewards it took."""

    target: str
    claimed: int
    reason: str = ""
    aborted: bool = False
