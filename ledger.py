"""Turn events into ledger lines - this account's non-battle history.

Two halves, split by statefulness, because they fail differently. classify()
is pure: it owns the catalog of which event becomes which kind of line and
nothing else, so every judgment in it is testable with no database.
LedgerWriter (next task) owns the running balances and the reconciliation
rule, because remembering the last observed balance is inherently stateful.

The ledger excludes in-run SPENDING. In-run upgrades are bought with
per-run cash that resets at the start of the next run - the `wallet` on
Tapped and ScanCompleted - which is a different currency from `coins` and
not account history in any sense.

The test is the currency, not the screen the tap happened on, so a battle
contributes two kinds of line rather than one. Its payout, and any floating
gem collected off the ring: those are account gems, identical to the ones a
mission claim pays, and leaving them out would silently understate the
balance the reconciler is trying to keep honest.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import db
import events
import upgrades
from fleet import workshop_prices

COINS = "coins"
GEMS = "gems"

# The currencies the writer keeps a running balance for. Anything else that
# reaches the ledger - stones paid by a milestone, say - is stored with
# balance_after NULL on every line. The page has to be able to tell that
# apart from a balance that simply has not been read yet: "not tracked" and
# "unknown" are different answers, and showing either as an empty wallet
# would be a third, wrong one.
BALANCED_CURRENCIES: tuple[str, ...] = (COINS, GEMS)

# Every kind a line can carry. The last five are RESERVED - the parts of the
# economy the bot cannot see (card slots, modules, relics, ultimate weapons)
# plus hand-entered lines. They are named here so
# adding one later is a branch in classify(), not a schema change.
KINDS: tuple[str, ...] = (
    "RUN_PAYOUT",
    "WORKSHOP_BUY",
    "LAB",
    "CARD_BUY",
    "MISSION_CLAIM",
    "MAIL_CLAIM",
    "MILESTONE_CLAIM",
    "GEM_CLAIM",
    "CLAIM_SKIPPED",
    "CLAIM_UNCERTAIN",
    "BUY_SKIPPED",
    "VISIT_START",
    "VISIT_END",
    "SHOP_UNAVAILABLE",
    "POLICY_CHANGED",
    "UNEXPLAINED",
    "ROUNDING",
    "CARD_SLOT",
    "MODULE",
    "RELIC",
    "UW",
    "MANUAL",
)


@dataclass(frozen=True, kw_only=True)
class LedgerLine:
    """One row of the account's history.

    `delta` and `price` are separate on purpose, and the difference is what
    makes a dry-run rehearsal safe to record: `price` is what it cost or
    would have cost, `delta` is what ACTUALLY moved.

    `delta` itself distinguishes two things a single None would collapse:

    * 0    - provably moved nothing. A skip, a rehearsal.
    * None - the amount that moved could not be determined, whatever the
             source: an unreadable price, an unreadable payout, an unreadable
             claim reward, a claim tapped but never confirmed. For a line
             that NAMES A CURRENCY, this is what leaves a hole in the running
             balance until the next reading closes it, as an explicit
             UNEXPLAINED line. A currency-less delta=None (an uncertain
             claim, say) never reaches the reconciler at all - see
             _reconcile - so it never leaves that kind of hole.
    """

    kind: str
    ts: float
    seq: int | None = None
    item: str | None = None
    category: str | None = None
    currency: str | None = None
    delta: int | None = None
    price: int | None = None
    balance_after: int | None = None
    observed: int | None = None
    dry_run: bool = False
    run_id: int | None = None
    visit: int | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Flatten into a row for db.insert_ledger.

        Here rather than in db.py deliberately: db.py "knows rows, not
        events", and importing this module into it would invert that.
        Mirrors sinks/store.py's to_row for the events table.
        """
        row = dataclasses.asdict(self)
        detail = row.pop("detail")
        row["dry_run"] = int(self.dry_run)
        row["detail"] = json.dumps(detail, default=str) if detail else None
        return row


def classify(event: events.Event) -> tuple[LedgerLine, ...]:
    """The catalog. Returns () for every event that is not account history.

    An empty tuple is the honest answer for an unrecognised event, not a
    guess: a new event type gets a branch here when someone decides what it
    means, and until then it simply is not in the ledger.

    A tuple rather than one line because a single event can move two
    balances - a mission claim pays coins AND gems - and LedgerLine carries
    one currency, because that is what the reconciler works in.
    """
    base: dict[str, Any] = {"ts": event.ts, "seq": event.seq}

    match event:
        case events.LabSlotUnlocked():
            return (LedgerLine(
                kind="LAB", item=f"Lab slot {event.slot}", category="SLOT",
                currency=GEMS, delta=-event.price, price=event.price,
                observed=event.gems_before,
                detail={"slot": event.slot, "gems_after": event.gems_after}, **base,
            ),)
        case events.LabResearchStarted():
            return (LedgerLine(
                kind="LAB", item=("Game Speed" if event.concept_id == "labs.game-speed"
                                  else event.concept_id), category="RESEARCH",
                currency=COINS, delta=-event.price, price=event.price,
                observed=event.coins_before,
                detail={"slot": 1, "concept_id": event.concept_id,
                        "coins_after": event.coins_after,
                        "completes_at": event.completes_at}, **base,
            ),)
        case events.RunEnded():
            # The only thing a battle contributes. RunEnded.coins is read off
            # the game-over modal's Coins caption - coins EARNED that run, not
            # a running total - so it is a credit, not a balance reading.
            return (LedgerLine(
                kind="RUN_PAYOUT",
                currency=COINS,
                delta=event.coins,
                run_id=event.run_id,
                reason="abandoned" if event.abandoned else None,
                **base,
            ),)

        case events.Purchased():
            cards = event.category == "CARDS"
            price = event.price
            if event.dry_run:
                delta: int | None = 0
            elif event.verdict is not None:
                # The journal answered this attempt, so what it proved is
                # the movement. The read price stays on the line as what it
                # would have cost; it is never promoted to a debit the
                # wallet refused to confirm.
                delta = None if event.spent is None else -event.spent
            elif price is None:
                delta = None
            else:
                delta = -price
            return (LedgerLine(
                kind="CARD_BUY" if cards else "WORKSHOP_BUY",
                item=event.item,
                category=event.category,
                currency=GEMS if cards else COINS,
                delta=delta,
                price=price,
                observed=event.gems_before if cards else event.coins_before,
                dry_run=event.dry_run,
                detail={**({"verdict": event.verdict} if event.verdict else {}),
                        **({"transaction_key": event.transaction_key} if event.transaction_key else {}),
                        **({"reason": event.reason} if event.reason else {})},
                **base,
            ),)

        case events.PurchaseSkipped():
            # The currency comes from WHICH balance the event reported.
            # PurchaseSkipped carries the balance for the currency the skip
            # would have spent and leaves the other None - the same rule
            # Purchased documents - so exactly one of these is ever set.
            if event.gems_before is not None:
                currency, observed = GEMS, event.gems_before
            elif event.coins_before is not None:
                currency, observed = COINS, event.coins_before
            else:
                # A row backfilled from before those fields existed. Still a
                # real line; it just reconciles nothing.
                currency, observed = None, None
            return (LedgerLine(
                kind="BUY_SKIPPED",
                item=event.item,
                currency=currency,
                # A skip provably moved nothing, which is not the same fact
                # as an unreadable price.
                delta=0,
                observed=observed,
                reason=event.reason,
                detail={"detail": event.detail} if event.detail else {},
                **base,
            ),)

        case events.MailClaimed():
            return tuple(LedgerLine(
                kind='MAIL_CLAIM', category='MAIL', currency=currency,
                delta=amount, detail={'confirmation': event.confirmation}, **base,
            ) for currency, amount in ((COINS, event.coins), (GEMS, event.gems)))

        case events.MissionClaimed():
            # Two currencies move on one claim and a LedgerLine carries one.
            # This is the event that made classify return a tuple.
            #
            # Coins are never `observed`. The page abbreviates them ("6.08K"),
            # and an abbreviated balance handed to the reconciler contradicts
            # the running total and manufactures an UNEXPLAINED line on every
            # single claim. Gems read exactly (60 -> 63 across one claim) and
            # are safe to anchor on.
            return (
                LedgerLine(kind="MISSION_CLAIM", item=event.mission,
                           category="MISSIONS", currency=COINS, delta=event.coins,
                           detail={"mission_id": event.mission_id,
                                   "completed_after": event.completed_after},
                           **base),
                LedgerLine(kind="MISSION_CLAIM", item=event.mission,
                           category="MISSIONS", currency=GEMS, delta=event.gems,
                           observed=event.gems_before, **base),
            )

        case events.ClaimStarted():
            return (LedgerLine(kind="VISIT_START", reason=event.target, **base),)

        case events.ClaimEnded():
            return (LedgerLine(
                kind="VISIT_END",
                reason=event.reason or ("aborted" if event.aborted else None),
                detail={"target": event.target, "claimed": event.claimed,
                        "aborted": event.aborted},
                **base),)

        case events.ClaimSkipped():
            return (LedgerLine(
                kind="CLAIM_SKIPPED",
                # A refusal provably moved nothing, which is not the same
                # fact as a reward whose amount could not be read.
                delta=0,
                reason=event.reason,
                detail={"target": event.target, "detail": event.detail}
                if event.detail else {"target": event.target},
                **base),)

        case events.MilestoneClaimed():
            # One line, not two: a milestone reward is a single currency, or
            # none at all. Coins are never `observed` for the same reason
            # MissionClaimed's are not - the header abbreviates them.
            return (LedgerLine(
                kind="MILESTONE_CLAIM",
                item=event.reward_text,
                category="MILESTONES",
                currency=event.currency,
                # Three states, not two. reward_text is None when the reward
                # line was never read - an unknown amount, delta=None, same
                # as any other unreadable amount. reward_text present with no
                # currency (`Unlock Lab`) provably moved nothing, delta=0.
                # reward_text present with a currency is the ordinary case.
                delta=(None if event.reward_text is None
                       else event.amount if event.currency is not None
                       else 0),
                detail={"tier": event.tier, "reward_text": event.reward_text},
                **base),)

        case events.FloatingGemClaimed():
            # The one thing a battle contributes besides its payout. It is
            # in-run by position and account history by nature: the gems it
            # pays are the same gems a mission claim pays, not the per-run
            # cash the module docstring excludes.
            #
            # Both balances are real readings taken either side of the tap -
            # the event is only published when the counter actually rose -
            # so this is safe to anchor the running total on, unlike the
            # abbreviated coin balances elsewhere in this catalog.
            return (LedgerLine(
                kind="GEM_CLAIM",
                item="floating gem",
                category="BATTLE",
                currency=GEMS,
                delta=event.delta,
                # No price. A gem picked up off the ring cost nothing, and
                # price=0 would read as "bought for free" - a different
                # claim about the world than "was given".
                balance_after=event.gems_after,
                observed=event.gems_before,
                run_id=event.run_id,
                detail={"point": list(event.point)},
                **base),)

        case events.ClaimUncertain():
            return (LedgerLine(
                kind="CLAIM_UNCERTAIN",
                # None, not 0: a tapped CLAIM that never confirmed may well
                # have taken a reward. delta=0 would assert it did not.
                delta=None,
                reason=event.reason,
                detail={"target": event.target, "detail": event.detail}
                if event.detail else {"target": event.target},
                **base),)

        case events.ShoppingStarted():
            return (LedgerLine(
                kind="VISIT_START", visit=event.visit, dry_run=event.dry_run, **base
            ),)

        case events.ShoppingEnded():
            return (LedgerLine(
                kind="VISIT_END",
                visit=event.visit,
                reason=event.reason or ("aborted" if event.aborted else None),
                detail={"bought": event.bought, "spent": event.spent,
                        "aborted": event.aborted},
                **base,
            ),)

        case events.ShoppingUnavailable():
            return (LedgerLine(kind="SHOP_UNAVAILABLE", reason=event.reason, **base),)

        case events.ControlChanged():
            # Why the spending policy changed, which is often the answer to
            # "why did it stop buying that".
            return (LedgerLine(
                kind="POLICY_CHANGED",
                reason=event.source,
                detail={"changed": event.changed},
                **base,
            ),)

    return ()


def _rounded_since_reading(conn: sqlite3.Connection) -> dict[str, int]:
    """Run payouts written since each balanced currency's last reading.

    Read back from the table so a restarted writer allows the same rounding
    as the one that wrote them.
    """
    return {currency: conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE currency = ? AND dry_run = 0 AND kind = 'RUN_PAYOUT' "
        "AND id > COALESCE((SELECT MAX(id) FROM ledger WHERE currency = ? AND dry_run = 0 "
        "AND observed IS NOT NULL), 0)", (currency, currency)).fetchone()[0]
        for currency in BALANCED_CURRENCIES}


class LedgerWriter:
    """Turns events into ledger lines, carrying the running balances.

    Stateful, unlike classify(), because reconciliation needs to remember
    what the last observed balance was. Seeded from the table rather than
    from zero: a restart that started from zero would read the next real
    balance as an enormous unexplained gain.

    Two pieces of state per currency, not one, and the difference matters:

    * `_known`  - the last balance actually READ off the screen, plus every
                  movement since that was itself known.
    * `_stale`  - whether an unknown movement (an unreadable price) has
                  happened since. A stale chain can no longer compute a
                  balance, but it can still reconcile: the next reading is
                  compared against `_known`, and the gap - which includes
                  whatever the unreadable purchase cost - becomes one
                  UNEXPLAINED line.

    Collapsing the two by setting `_known` to None on a hole was tried and
    is wrong: it silently discards the last certain balance, so the reading
    that should have closed the hole instead starts a fresh chain and the
    missing coins are never accounted for anywhere.

    Not thread-safe, and does not need to be - the store sink's consumer
    thread is the only caller, and it is also the database's only writer.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        self._known: dict[str, int | None] = db.last_balances(conn)
        # A seeded writer assumes an intact chain: last_balances only returns
        # a non-NULL balance_after, which is by definition a line that closed
        # cleanly. If the process died mid-hole, the first reading after the
        # restart still produces the right UNEXPLAINED - only a non-observing
        # event landing in between would be computed off a stale base.
        self._stale: dict[str, bool] = {COINS: False, GEMS: False}
        # Rounded amounts (run payouts) added since each currency's last
        # reading: how far that reading may land from the running total.
        self._rounded: dict[str, int] = _rounded_since_reading(conn)

    def lines_for(self, event: events.Event) -> list[LedgerLine]:
        """Every line this event produces, in the order they must be written.

        One event can carry more than one currency, and each is reconciled
        against its own running balance - so an unreadable coin price cannot
        stall the gem chain, and vice versa.
        """
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self._data_version:
            # Recovery writes synchronously, even if its queued event is lost.
            self._known = db.last_balances(self._conn)
            for currency in BALANCED_CURRENCIES:
                row = self._conn.execute(
                    "SELECT balance_after FROM ledger WHERE currency = ? AND dry_run = 0 "
                    "ORDER BY id DESC LIMIT 1", (currency,),
                ).fetchone()
                self._stale[currency] = row is not None and row[0] is None
            self._rounded = _rounded_since_reading(self._conn)
            self._data_version = version
        if isinstance(event, events.Purchased) and event.transaction_key:
            if self._conn.execute(
                "SELECT 1 FROM ledger WHERE json_extract(detail, '$.transaction_key') = ? LIMIT 1",
                (event.transaction_key,),
            ).fetchone():
                return []
        out: list[LedgerLine] = []
        for line in classify(event):
            out.extend(self._reconcile(line))
        return out

    def _reconcile(self, line: LedgerLine) -> list[LedgerLine]:
        """One line against its currency's running balance.

        Usually one line back. Two when the balance it reports contradicts
        the running total, in which case the UNEXPLAINED line comes FIRST:
        the gap happened before the event that revealed it.
        """
        from transactions import reading_tolerance

        currency = line.currency
        if currency is None:
            return [line]

        out: list[LedgerLine] = []
        known = self._known.get(currency)
        stale = self._stale.get(currency, False)

        if line.observed is not None:
            if known is not None and line.observed != known:
                # A gap a rounded payout or an abbreviated header can explain
                # is not money that moved; only a bigger one is unexplained.
                rounding = abs(line.observed - known) <= reading_tolerance(
                    known, line.observed, rounded_amounts=self._rounded.get(currency, 0))
                out.append(
                    LedgerLine(
                        kind="ROUNDING" if rounding else "UNEXPLAINED",
                        ts=line.ts,
                        currency=currency,
                        delta=line.observed - known,
                        balance_after=line.observed,
                        observed=line.observed,
                        reason=("within reading and rounding tolerance" if rounding
                                else "balance moved outside the bot"),
                    )
                )
            self._rounded[currency] = 0
            # The game is the source of truth. Whatever the running total
            # said, the number on screen is what the balance actually is -
            # and reading it closes any hole that was open.
            known, stale = line.observed, False

        if known is None or stale or line.delta is None:
            balance = None
        else:
            balance = known + line.delta

        out.append(dataclasses.replace(line, balance_after=balance))
        if line.kind == "RUN_PAYOUT" and line.delta is not None:
            self._rounded[currency] = self._rounded.get(currency, 0) + 1

        # Commit the reading unconditionally. An UNEXPLAINED line above has
        # already priced any gap it revealed, so the anchor has to move to it
        # even when this line's own movement is unknown - otherwise the next
        # reading prices the same gap a second time.
        if line.delta is None:
            # Moved by an amount nobody read. The anchor stays where the last
            # reading put it and the chain stops deriving balances, but it
            # keeps reconciling: the next reading prices the whole gap,
            # this purchase's cost included.
            self._known[currency] = known
            self._stale[currency] = True
        else:
            # A known movement counts against the anchor even while stale.
            # Dropping it here is what made a stale chain report a payout as
            # an unexplained GAIN.
            self._known[currency] = None if known is None else known + line.delta
            self._stale[currency] = stale
        return out


# The events the ledger cares about, by the `type` string stored in the
# events table. Anything else is skipped without being rebuilt at all.
_REPLAYABLE: dict[str, type[events.Event]] = {
    "RunEnded": events.RunEnded,
    "LabResearchStarted": events.LabResearchStarted,
    "LabSlotUnlocked": events.LabSlotUnlocked,
    "Purchased": events.Purchased,
    "PurchaseSkipped": events.PurchaseSkipped,
    "ShoppingStarted": events.ShoppingStarted,
    "ShoppingEnded": events.ShoppingEnded,
    "ShoppingUnavailable": events.ShoppingUnavailable,
    "ControlChanged": events.ControlChanged,
    "ClaimStarted": events.ClaimStarted,
    "MissionClaimed": events.MissionClaimed,
    "MailClaimed": events.MailClaimed,
    "ClaimSkipped": events.ClaimSkipped,
    "ClaimEnded": events.ClaimEnded,
    "MilestoneClaimed": events.MilestoneClaimed,
    "ClaimUncertain": events.ClaimUncertain,
    "FloatingGemClaimed": events.FloatingGemClaimed,
}


def _rebuild(row: dict[str, Any]) -> events.Event | None:
    """Reconstruct a stored row back into the event it came from.

    Best-effort by design. A row written by an older build can be missing
    fields this event now requires, and a ledger line is not worth crashing
    a launch over - such a row is skipped and the history simply starts
    later.
    """
    cls = _REPLAYABLE.get(row["type"])
    if cls is None:
        return None

    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {"seq": row["seq"], "ts": row["ts"]}
    if "run_id" in fields and row.get("run_id") is not None:
        kwargs["run_id"] = row["run_id"]
    if "price" in fields and row.get("price") is not None:
        kwargs["price"] = row["price"]
    if "reason" in fields and row.get("reason") is not None:
        kwargs["reason"] = row["reason"]
    kwargs.update({k: v for k, v in (row.get("detail") or {}).items() if k in fields})

    try:
        return cls(**kwargs)
    except TypeError:
        return None


def backfill(conn: sqlite3.Connection) -> int:
    """Replay the events table into the ledger. Returns lines written.

    A ONE-TIME migration, not a repair tool, and it says so by refusing to
    run at all once the ledger holds anything. Derived UNEXPLAINED lines
    have no seq, so the unique index that dedupes replayed source events
    cannot dedupe them; a second unguarded replay would duplicate every one.

    Called from prepare_store BEFORE the prune, so events that are already
    past the retention window still make it into the permanent history.
    """
    if conn.execute("SELECT 1 FROM ledger LIMIT 1").fetchone() is not None:
        return 0

    rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    writer = LedgerWriter(conn)
    written = 0
    for raw in rows:
        event = _rebuild(_decode_event(raw))
        if event is None:
            continue
        for line in writer.lines_for(event):
            db.insert_ledger(conn, line.as_row())
            written += 1
    return written


def repair_unproven_buys(conn: sqlite3.Connection) -> int:
    """Put the price back on bought lines the old journal rule left at '?'.

    The journal once demanded a wallet drop of exactly the price, and the
    header abbreviates balances ("2.61K"), so real buys were left with no
    amount - and the next reading's UNEXPLAINED line swallowed their cost.
    The price is proven after all when the attributed catalog lists it for
    the item, or when that next reading lands within the header's
    abbreviation of before-minus-price. It then moves onto the buy, the
    balances between are derived again, and the UNEXPLAINED line keeps only
    the residual. Its balance_after is a reading, so nothing after it
    changes. Idempotent: a repaired line no longer has a NULL delta.
    """
    from transactions import reading_tolerance

    buys = conn.execute(
        "SELECT id, item, category, currency, price, observed FROM ledger "
        "WHERE kind = 'WORKSHOP_BUY' AND delta IS NULL AND dry_run = 0 "
        "AND price > 0 AND observed IS NOT NULL "
        "AND json_extract(detail, '$.verdict') = 'bought' ORDER BY id"
    ).fetchall()
    repaired = 0
    with conn:
        for buy_id, item, category, currency, price, observed in buys:
            upgrade = upgrades.resolve(item, category)
            in_catalog = upgrade is not None and workshop_prices.lists_price(upgrade.id, price)
            balance = observed - price
            running: int | None = balance
            derived: list[tuple[int, int | None]] = []
            for line_id, kind, delta, reading in conn.execute(
                "SELECT id, kind, delta, observed FROM ledger "
                "WHERE currency = ? AND dry_run = 0 AND id > ? ORDER BY id",
                (currency, buy_id),
            ).fetchall():
                if reading is not None:
                    break
                running = None if running is None or delta is None else running + delta
                derived.append((line_id, running))
            else:
                # No reading since: only the catalog can vouch for the price.
                if not in_catalog:
                    continue
                kind = None
            if kind is not None:
                if kind != "UNEXPLAINED":
                    # A reading the stale chain matched exactly: with the
                    # price applied it would need a line that does not exist.
                    continue
                close = running is not None and abs(reading - running) <= reading_tolerance(observed, reading)
                if not (in_catalog or close):
                    continue
                residual = delta + price
                if residual:
                    conn.execute("UPDATE ledger SET delta = ? WHERE id = ?", (residual, line_id))
                else:
                    conn.execute("DELETE FROM ledger WHERE id = ?", (line_id,))
            conn.execute("UPDATE ledger SET delta = ?, balance_after = ? WHERE id = ?",
                         (-price, balance, buy_id))
            conn.executemany("UPDATE ledger SET balance_after = ? WHERE id = ?",
                             [(value, line_id) for line_id, value in derived])
            repaired += 1
    return repaired


def _decode_event(row: Any) -> dict[str, Any]:
    data = dict(row)
    raw = data.get("detail")
    data["detail"] = json.loads(raw) if raw else {}
    return data
