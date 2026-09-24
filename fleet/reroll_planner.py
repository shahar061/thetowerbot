"""The reroll goal, expressed as an adapter onto the general purchase planner.

This module used to BE the planner: two hardcoded weight tables, a
prerequisite map, a target map, a focus map, and a `weight / (1 + purchases)`
ranking loop, all of it reachable only through `fleet/`. Everything below the
reroll ladder now lives in modules that know nothing about rerolling -
`builds.py` holds the recipes, `build_selection.py` picks one,
`workshop_objectives.py` turns it into a graph, `value_propagation.py` ranks
it, `director.py` explains the ranking and `decision.py` turns it into one of
five moves - and what is left here is the translation between a reroll
worker's evidence and that pipeline's vocabulary.

What stayed here, and why it is not a planning decision
-------------------------------------------------------
Two things, both reroll POLICY rather than planning:

1. The Tier 1 Wave 60 branch. Once the account has verifiably cleared wave
   60 it has stones to spend on its first Ultimate Weapon, and that pick is
   `objectives.py`'s only `one_way` risk class - irreversible, and not the
   bot's to make. It returns `needs_operator` before any build is selected,
   because selecting a build would imply the next move is a Workshop
   purchase when it is a human conversation.
2. The goal sentences ("Reach Tier 1 Wave 20"). They name what the reroll
   operator is trying to do with this account, which is a fleet concern; a
   build is a recipe, not a milestone, and `builds.v1.json` deliberately
   records no wave numbers.

Everything else is delegated: which build, in what order, whether a
prerequisite is met, whether the price was read, whether the balance covers
it, whether the spend limit allows it, and what sentence to say about all of
that.

Why the numbers are gone from this file
----------------------------------------
`_OPENING`, `_TURTLE`, `_PREREQUISITES`, `_TARGETS` and `_FOCUS` are deleted,
not re-exported. A re-export would have kept every consumer working and
quietly left two copies of the weights in the repository - one in
`knowledge/builds.v1.json`, one here - free to drift the first time somebody
tuned a build by editing the JSON, because nothing would have complained.
`builds.py` carries a sha256 of this file's old contents in its
`build_sources` for exactly that reason: the port is checkable, and now
unduplicated.

`_TARGETS["defense_percent"] = 50.0` was NOT carried into the pack, and is
not reintroduced here. It was dead: `defense_percent` is weighted by neither
build, and the old ranking loop consulted `_TARGETS` only for ids already in
the candidate list, so the entry was never once evaluated. `builds.py`
rejects a target for an unweighted upgrade outright, which is the same
finding turned into an import-time error.

The coordinate this adapter writes into workshop_stats
-------------------------------------------------------
`AccountRevision.workshop_stats` records DISPLAYED VALUES, and it is the only
section a live account populates. A reroll worker's strongest evidence is not
a displayed value though - it is `RerollFacts.purchases`, a count per upgrade
rebuilt from the verified ledger (`fleet/reroll_progress.py:_history` counts
only `WORKSHOP_BUY` rows whose verdict was "bought" or "free", plus unlocks
proven by `already_unlocked`). So this module writes two revisions, in one
deliberate coordinate:

* the ANCHOR revision, every readable row at 0 - the account as it stood the
  moment it was rerolled - which `workshop_objectives` uses to anchor its
  relative "advance this at least once more" milestones; and
* the CURRENT revision, every readable row at its verified purchase count,
  which `director.plan` classifies against.

The pair is what makes "this row has been bought since the reroll" decidable
at all: generated and classified against the SAME revision, a relative
milestone is false by construction (that is `workshop_objectives`'s own
documented behaviour), and the plan would recommend the same row forever.

A displayed READING (`RerollFacts.values`, which the worker takes from real
`stats.*` facts) is used for exactly one thing: an upgrade the selected build
declares a `targets` entry for, because a target is stated in displayed units
("thorns 51" is 51 thorn damage, not 51 purchases). Mixing the two
coordinates anywhere else would be the bug this split exists to prevent: a
fresh account displays Damage 27 before anything is bought, and a reading of
27 against an anchor of 0 would mark Damage permanently done and never buy
it again. Where a targeted row has no reading yet, the count stands in, which
can only ever UNDERSTATE progress and so can only ever keep buying - the same
answer the old `facts.values.get(upgrade_id, 0) >= target` gave for a row
nobody had read.

A row is written at all only when it is on the tab: an upgrade the catalog
gates behind an unlock tile appears only once that tile is owned. That is
what lets an unlock objective resolve, since `workshop_objectives` proves an
unlock by the rows it GRANTS being readable (`shopping.py:_already_unlocked`'s
rule), never by an `unlocks.*` fact, which no reader ever writes.

What changed about the bot's behaviour, stated plainly
--------------------------------------------------------
The old ranking was `weight / (1 + purchases)`: a diminishing-return rotation
that came back to Damage every few buys and never finished anything. The
pipeline has no such term, and two committed decisions replace it:

* `value_propagation` gives an enabler a discounted share of the best thing
  it leads to, so `unlock_defense_upgrades` (10.5 on its own, 20.22 once it
  inherits from the thorns chain) would outrank the old `damage` (12). The
  opening build is now weighted as a strict priority instead - see its note
  in `knowledge/builds.v1.json` - and paces attack with `level_caps`.
* a milestone is either met or not, so a weighted row is bought until its
  milestone is met and then dropped, rather than revisited forever.

Both are the point of the phases that introduced them, and both are visible
in `tests/test_reroll_planner.py`, which asserts the new order.

Purity
------
`choose_next` and `project_next` take every input as a parameter and reach no
clock, device, screen or database - the same rule `objectives.py`,
`director.py` and `decision.py` hold themselves to. The one file read in the
whole dependency closure is each pack's import-time load, which happens once
and never during a decision.

A variant arrives as an id on `RerollFacts`; the file that stores it is
read by `fleet/reroll_progress.py`, not here.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping

import build_selection
import builds
import decision as decision_module
import director
import knowledge
import upgrades
import workshop_objectives
from account_state import AccountRevision, Evidence, Fact
from progression import CurrencyRates
from strategy import Strategy
from workshop_objectives import ID_PREFIX


# The draw: instead of always taking the top-ranked ready upgrade, pick one
# of the top DRAW_POOL with probability value**sharpness / sum(value**sharpness).
# At 6, a fresh opening account (Damage 100, Attack Speed 90, Unlock Defense
# Upgrades 84.4) keeps the top pick ~53% of the time, takes the runner-up
# ~28% and the third ~19%. It varies accounts without undoing the priority:
# blocked rows and rows past their cap are never in the pool, and an
# attack row that is drawn early simply reaches its allowance sooner.
# Seeded by account and verified purchase count, so a decision can be
# replayed and stays the same between the unpriced and priced calls.
DRAW_SHARPNESS = 6.
DRAW_POOL = 3


@dataclass(frozen=True)
class RerollFacts:
    account_id: str
    best_tier_1_wave: int | None = None
    purchases: Mapping[str, int] = field(default_factory=dict)
    values: Mapping[str, float] = field(default_factory=dict)
    wallet_coins: int | None = None
    lifetime_coins: int | None = None
    prices: Mapping[str, int] = field(default_factory=dict)
    spend_fraction: float | None = None
    # None ranks deterministically: always the top pick.
    draw_sharpness: float | None = DRAW_SHARPNESS
    # The id stored in the worker's reroll-variant.json, or None. Resolved
    # against the selected build, so it only acts while that build lists it.
    variant: str | None = None
    # Confirmed Workshop coin debits on utility; None means the ledger cannot
    # prove the amount, so the bounded opening allocation is not activated.
    utility_spent_coins: int | None = None


@dataclass(frozen=True)
class RerollDecision:
    account_id: str
    stage: str
    goal: str
    state: str
    upgrade_id: str | None
    item: str | None
    category: str | None
    price: int | None
    wallet_coins: int | None
    lifetime_coins: int | None
    reason: str
    filler: bool = False
    starter: bool = False


@dataclass(frozen=True)
class PlannedPurchase:
    account_id: str
    position: int
    upgrade_id: str
    item: str
    category: str
    unlock: bool
    focus: str


# The wave at which this account stops being a Workshop problem. Not a build
# threshold (build_selection owns that one, at wave 20) and deliberately not
# in the pack: it is the reroll operator's finish line, and the pack records
# recipes rather than goals.
STONES_WAVE = 60

# What the operator is trying to reach while running each build. Keyed by
# build id because that is what `build_selection.Selection.build_id` returns
# and what `RerollDecision.stage` has always carried; a build the pack gains
# later gets the neutral sentence rather than a wave number invented here.
_GOALS: Mapping[str, str] = {
    "opening": "Reach Tier 1 Wave 20",
    "turtle": "Reach Tier 1 Wave 60",
}
_DEFAULT_GOAL = "Advance this reroll account"

# Said when the build declares no focus line for an upgrade. The pack carries
# one per weighted row today, so this is the shape of a future build's gap,
# not a live default.
_DEFAULT_FOCUS = "Advance the reroll account"
_ECONOMY_FOCUS = {
    "unlock_cash_bonuses": "Open Cash/Wave for faster in-run upgrades",
    "cash_per_wave": "Earn more cash each wave",
    "unlock_coin_bonuses": "Open Coins/Kill for Workshop income",
    "coins_per_kill_bonus": "Earn more permanent coins from kills",
    "cash_bonus": "Use the remaining early allocation for cash income",
}

UTILITY_TARGET_COINS = 350
UTILITY_CEILING_COINS = 400
_ECONOMY_WEIGHTS = (
    ("unlock_cash_bonuses", 100.),
    ("cash_per_wave", 200.),
    ("unlock_coin_bonuses", 80.),
    ("coins_per_kill_bonus", 150.),
    ("cash_bonus", 60.),
)
_FILLER_CAPS = {
    "cash_per_wave": 5, "coins_per_kill_bonus": 5,
    "cash_bonus": 5, "damage": 3, "attack_speed": 3,
}
FILLER_SHARE = .2
STARTER_MAX_PRICE = 75
STARTER_UPGRADES = ("damage", "attack_speed", "health",
                    "unlock_defense_upgrades", "defense_absolute")

# Which unlock tile gates each row, read off the catalog's own `unlocks`
# lists rather than off `builds.prerequisites()`. The two agree today, but
# they answer different questions: the pack's map is the planning graph a
# build walks, while this one is the game fact "this row is not on the tab
# yet", which is what decides whether a reading could exist at all.
_GATED_BY: Mapping[str, str] = {
    child: upgrade.id
    for upgrade in upgrades.CATALOG if upgrade.unlock
    for child in upgrade.unlocks
}

# `director.plan` ranks on payoff-per-hour where a price, a balance and an
# income RATE are all known. A reroll worker measures no income - RerollFacts
# carries no coins-per-hour and never has - so the honest rate is "no
# measurement", which makes every horizon None and every score None, and the
# ranking falls through to propagated value exactly as the old weight
# comparison did. A fabricated rate here would invent an ordering nobody
# measured.
_NO_MEASURED_INCOME = CurrencyRates(
    None, None, 0,
    "a reroll worker measures no coin income, so no horizon is claimed")

# `director._has_pre_approval` is consulted only for `one_way` objectives,
# and every objective `workshop_objectives` emits is `reversible` (the
# Workshop Respec lab refunds coins). So this strategy exists to satisfy the
# signature, and it names nothing: a strategy that pre-approved something
# would be a permission this module has no business granting, and the buyer
# still validates account, screen, row, balance, price and transaction.
_NO_PRE_APPROVALS = Strategy.from_dict({
    "name": "reroll", "actions": [{"name": "none", "template": "none",
                                   "enabled": False}]})

# Every synthesized Fact cites this instead of an OCR rectangle. It is not a
# reading and must not be mistaken for one by anything that later learns to
# render evidence: the number beside it came from the ledger, not the screen.
_LEDGER_EVIDENCE = Evidence(
    observed_at=0., confidence=1., raw_name="reroll ledger",
    raw_value=None, rect=(0, 0, 0, 0), frame_width=0, frame_height=0,
    frame_digest="reroll-ledger")


def _readable_rows(facts: RerollFacts) -> tuple[str, ...]:
    """Every non-unlock upgrade whose row the account can currently see.

    A row with no gating unlock is on the tab from the first visit; a gated
    one appears only once its tile is bought, which the ledger records as a
    purchase of the tile itself (`_history` counts an `already_unlocked`
    skip as proof, too). Emitting a Fact for a row the account cannot see
    would be the one mistake `workshop_objectives`' three-valued predicates
    exist to prevent: it would prove an unlock nobody bought, because an
    unlock is proven by its granted rows being readable.

    Unlock tiles themselves are excluded. Their concept ids are `unlocks.*`,
    `workshop_stats` holds `stats.*`, and no reader in this repository ever
    writes an `unlocks.*` fact - a tile shows a price, not a value.
    """
    owned = {upgrade_id for upgrade_id, count in facts.purchases.items() if count > 0}
    return tuple(
        upgrade.id for upgrade in upgrades.CATALOG
        if not upgrade.unlock and _GATED_BY.get(upgrade.id, None) in (None, *owned))


def _stats(facts: RerollFacts, *, targeted: frozenset[str],
           anchored: bool) -> tuple[Fact, ...]:
    """The readable rows as `workshop_stats` Facts. See the module docstring.

    `anchored` writes every row at 0 - the account as it was rerolled - which
    is the baseline `workshop_objectives` anchors its relative milestones to.
    Without it the milestone is generated and judged against the same number
    and is false forever, and the plan repeats one row until the end of time.

    `targeted` names the upgrades the build stops at a displayed value, and
    is the only place a real reading is allowed to enter: everywhere else the
    coordinate is the verified purchase count, so that a base stat the game
    displays before anything is bought cannot be read as progress.
    """
    stats: list[Fact] = []
    for upgrade_id in _readable_rows(facts):
        reading = facts.values.get(upgrade_id) if upgrade_id in targeted else None
        value = 0. if anchored else float(
            reading if reading is not None else facts.purchases.get(upgrade_id, 0))
        upgrade = upgrades.by_id(upgrade_id)
        assert upgrade is not None  # _readable_rows iterates the catalog itself
        stats.append(Fact(upgrade.concept_id, value, "verified", _LEDGER_EVIDENCE))
    return tuple(stats)


def _revision(facts: RerollFacts, *, targeted: frozenset[str] = frozenset(),
              anchored: bool = False) -> AccountRevision:
    """`facts` as the account snapshot every general module reads.

    Only `workshop_stats` is populated, which is also all a live account
    carries. `inventory` in particular is left unread rather than filled from
    `wallet_coins`: `decision.decide` takes the balance as its own argument
    and says the shortfall in coins, whereas `director`'s use of inventory is
    to compute an hours-to-afford horizon against an income rate this worker
    has never measured. Writing the balance there would produce a horizon of
    infinity dressed up as a measurement.
    """
    return AccountRevision(account_id=facts.account_id,
                           workshop_stats=_stats(facts, targeted=targeted,
                                                 anchored=anchored))


def _build_for(facts: RerollFacts) -> builds.Build:
    """The committed build this account should be running.

    Replaces the line this file used to carry,
    `stage = "turtle" if (facts.best_tier_1_wave or 0) >= 20 else "opening"`,
    with the ladder `build_selection` grew around it. `best_tier_1_wave` is
    passed explicitly because it is not a field on `AccountRevision` - it is
    computed from battle history by `fleet/reroll_metrics.py` and arrives
    here on `RerollFacts`, and inventing a `stats.*` concept id for it would
    be a string nothing resolves.

    `requested` and `incumbent` are both None. A reroll worker has no
    operator build override to honour (nothing on `RerollFacts` carries one)
    and no incumbent to defend, and passing a fabricated incumbent would turn
    hysteresis - a guard against paying for a respec twice - into a lock on
    whatever this call happened to compute last.
    """
    selection = build_selection.select_build(
        _revision(facts), best_tier_1_wave=facts.best_tier_1_wave)
    build = builds.by_id(selection.build_id)
    # Unreachable: `build_selection` only ever returns an id it read out of
    # the same registry, and validates its fallback at import. Raised rather
    # than allowed to become `None`, because a None here would fall through
    # as "no supported purchase" - a planner that quietly stops planning.
    if build is None:
        raise ValueError(
            f"build_selection chose {selection.build_id!r}, which is not in the pack")
    variant = build.variant(facts.variant)
    return build if variant is None else replace(build, level_caps=variant.level_caps)


def _upgrade_id(objective_id: str | None) -> str | None:
    """A `workshop.<upgrade_id>` objective id back to the catalog id.

    Returns None for anything else, which today is unreachable - the only
    graph this module ever plans over is the one `workshop_objectives`
    generates - and is still checked, because the alternative is handing
    `fleet/reroll_progress.py` an id it will put into a `ShoppingRule` and
    then fail to find a row for.
    """
    if objective_id is None or not objective_id.startswith(ID_PREFIX):
        return None
    upgrade_id = objective_id.removeprefix(ID_PREFIX)
    return upgrade_id if upgrades.by_id(upgrade_id) is not None else None


def _price_share(price: int | None, lifetime_coins: int | None) -> str:
    """The clause that puts a price next to what the account has ever earned.

    Kept verbatim from the old `choose_next`, and kept HERE rather than asked
    of `decision.py`: lifetime coins are a reroll measurement (read off the
    account stats summary by `fleet/reroll_progress.py`) and mean nothing to
    a general planner that has no notion of an account's whole history.
    """
    if price is None or lifetime_coins is None or lifetime_coins <= 0:
        return ""
    return f" Its price is {price / lifetime_coins:.0%} of verified lifetime coins."


def _variant_label(build: builds.Build, facts: RerollFacts) -> str:
    """` (Income first)` when this account plays a variant of this build."""
    variant = build.variant(facts.variant)
    return f" ({variant.name})" if variant is not None else ""


def _as_int_price(price: int | float | None) -> int | None:
    """`decision.Decision.price` as the `int | None` this worker publishes.

    `RerollDecision.price` is serialised into `reroll-plan.json` and compared
    against a wallet, and `Candidate.price` is typed `int | float | None`
    because the authored graph can carry a float. Every price that can reach
    this module came through `RerollFacts.prices` as an int, so the float
    branch is a type-level possibility rather than a live one - rounded up so
    that a price it ever does carry is never published as less than it is.
    """
    if price is None:
        return None
    return int(price) if float(price).is_integer() else int(price) + 1


def _with_level_caps(build: builds.Build, purchases: Mapping[str, int]) -> builds.Build:
    """`build` with each level cap turned into a target for this call.

    A cap is counted in verified purchases, and a capped row is not in
    `build.targets`, so `_stats` already writes it as its purchase count -
    the coordinate the cap is stated in. The allowance moves with every
    purchase it counts, which is why it is recomputed per call rather than
    stored.
    """
    if not build.level_caps:
        return build
    return replace(build, targets=MappingProxyType({
        **build.targets,
        **{row: cap.allowance(purchases) for row, cap in build.level_caps.items()},
    }))


def _economy_build(build: builds.Build, facts: RerollFacts) -> builds.Build:
    """Prioritize early utility while verified spend fits the hard ceiling."""
    spent = facts.utility_spent_coins
    if spent is None or spent >= UTILITY_TARGET_COINS:
        return build
    remaining = UTILITY_CEILING_COINS - spent
    affordable_ids = {upgrade_id for upgrade_id, _ in _ECONOMY_WEIGHTS
                      if facts.prices.get(upgrade_id, 0) <= remaining}
    owned = {upgrade_id for upgrade_id, count in facts.purchases.items() if count > 0}
    prerequisite = builds.prerequisites()
    weights = tuple((upgrade_id, weight) for upgrade_id, weight in _ECONOMY_WEIGHTS
                    if upgrade_id in affordable_ids and
                    prerequisite.get(upgrade_id) in (None, *owned, *affordable_ids))
    if not weights:
        return build
    return replace(build, weights=weights, targets=MappingProxyType({}),
                   level_caps=MappingProxyType({
                       "cash_per_wave": builds.LevelCap(2),
                       "coins_per_kill_bonus": builds.LevelCap(3),
                   }))


def _cheap_filler(facts: RerollFacts, main: RerollDecision) -> RerollDecision:
    """Look for one small purchase while saving for an unaffordable goal."""
    wallet = facts.wallet_coins
    if (facts.utility_spent_coins is None or main.state != "save_coins"
            or wallet is None or main.price is None or main.price <= wallet):
        return main
    owned = {upgrade_id for upgrade_id, count in facts.purchases.items() if count > 0}
    ceiling = int(wallet * FILLER_SHARE)
    for upgrade_id, cap in _FILLER_CAPS.items():
        if upgrade_id == main.upgrade_id or facts.purchases.get(upgrade_id, 0) >= cap:
            continue
        gate = _GATED_BY.get(upgrade_id)
        if gate is not None and gate not in owned:
            continue
        upgrade = upgrades.by_id(upgrade_id)
        assert upgrade is not None
        price = facts.prices.get(upgrade_id)
        if price is None or price < 0 or price > ceiling:
            continue
        state = "buy"
        reason = (f"Saving for {main.item} ({main.price} coins); {upgrade.name} "
                  f"costs {price}, within the {ceiling}-coin filler allowance.")
        return RerollDecision(facts.account_id, main.stage, main.goal, state,
                              upgrade_id, upgrade.name, upgrade.category, price,
                              wallet, facts.lifetime_coins, reason, filler=True)
    return main


def _survival_starter(facts: RerollFacts) -> RerollDecision | None:
    """One cheap level per starter row before the bounded utility allocation."""
    if (facts.best_tier_1_wave or 0) >= 20 or facts.utility_spent_coins is None:
        return None
    for uid in STARTER_UPGRADES:
        if facts.purchases.get(uid, 0) > 0:
            continue
        gate = _GATED_BY.get(uid)
        if gate and facts.purchases.get(gate, 0) == 0:
            continue
        price = facts.prices.get(uid)
        if price is not None and price > STARTER_MAX_PRICE:
            continue
        upgrade = upgrades.by_id(uid)
        assert upgrade is not None
        state = ("observe_price" if price is None else "observe_balance" if facts.wallet_coins is None
                 else "buy" if facts.wallet_coins >= price else "save_coins")
        return RerollDecision(facts.account_id, "opening", _GOALS["opening"], state,
                              uid, upgrade.name, upgrade.category, price,
                              facts.wallet_coins, facts.lifetime_coins,
                              f"Survival starter: one {upgrade.name} upgrade, up to {STARTER_MAX_PRICE} coins, "
                              "before the early utility allocation." + _variant_label(_build_for(facts), facts), starter=True)
    return None


def _draw(plan: director.Plan, facts: RerollFacts) -> tuple[director.Plan, str]:
    """Replace `plan.top` with a weighted draw from the top ready candidates.

    Returns the plan and a sentence for the reason, empty when the draw kept
    the top pick. See DRAW_SHARPNESS.
    """
    sharpness = facts.draw_sharpness
    if sharpness is None or plan.top is None:
        return plan, ""
    pool = [c for c in plan.candidates
            if c.status == "ready" and not c.held_by][:DRAW_POOL]
    odds = [c.value ** sharpness for c in pool]
    seed = f"{facts.account_id}:{sum(facts.purchases.values())}"
    roll = random.Random(hashlib.sha256(seed.encode()).digest()).random() * sum(odds)
    picked = pool[-1]
    for candidate, weight in zip(pool, odds):
        roll -= weight
        if roll < 0:
            picked = candidate
            break
    if picked is plan.top:
        return plan, ""
    share = odds[pool.index(picked)] / sum(odds)
    top = upgrades.by_id(_upgrade_id(plan.top.objective_id) or "")
    top_name = top.name if top is not None else plan.top.objective_id
    return (replace(plan, top=picked),
            f" This was drawn at {share:.0%} odds over the top pick, {top_name}.")


def choose_next(facts: RerollFacts) -> RerollDecision:
    """The one next move for this reroll account, with its reasoning attached.

    An adapter, not a planner: it keeps the reroll ladder (wave 60 stops
    Workshop planning), translates the worker's evidence into the account
    snapshot the general modules read, and translates their answer back into
    the field names, `state` vocabulary and `goal`/`stage` strings
    `fleet/reroll_progress.py` and the fleet dashboard already consume.

    Authorises nothing. `state == "buy"` is a recommendation; the buyer still
    validates the account, the screen, the row, the balance, the price and
    the transaction, exactly as it did when the weights lived in this file.
    """
    if not facts.account_id:
        raise ValueError("reroll account identity required")
    if facts.draw_sharpness is not None and facts.draw_sharpness <= 0:
        raise ValueError(f"draw sharpness must be > 0, got {facts.draw_sharpness}")
    if facts.best_tier_1_wave is not None and facts.best_tier_1_wave >= STONES_WAVE:
        # Reroll policy, not planning, and so it runs before a build is
        # chosen: the account's next move is a one-way Ultimate Weapon pick,
        # and recommending a Workshop purchase here would bury it.
        return RerollDecision(
            facts.account_id, "stones", "Earn stones for the first Ultimate Weapon",
            "needs_operator", None, None, None, None, facts.wallet_coins,
            facts.lifetime_coins,
            "Tier 1 Wave 60 was verified; Ultimate Weapon choice stays with the operator.")

    starter = _survival_starter(facts)
    if starter is not None:
        return starter
    original_build = _build_for(facts)
    build = _economy_build(original_build, facts)
    targeted = frozenset(build.targets)
    graph = workshop_objectives.workshop_objectives(
        _with_level_caps(build, facts.purchases),
        _revision(facts, targeted=targeted, anchored=True),
        prices=facts.prices)
    plan = director.plan(
        _revision(facts, targeted=targeted), knowledge=knowledge.KNOWLEDGE,
        graph=graph, rates=_NO_MEASURED_INCOME, strategy=_NO_PRE_APPROVALS)
    if plan.top is None and build is not original_build:
        build = original_build
        targeted = frozenset(build.targets)
        graph = workshop_objectives.workshop_objectives(
            _with_level_caps(build, facts.purchases),
            _revision(facts, targeted=targeted, anchored=True), prices=facts.prices)
        plan = director.plan(
            _revision(facts, targeted=targeted), knowledge=knowledge.KNOWLEDGE,
            graph=graph, rates=_NO_MEASURED_INCOME, strategy=_NO_PRE_APPROVALS)
    plan, drawn = _draw(plan, replace(facts, draw_sharpness=None)
                         if build is not original_build else facts)
    outcome = decision_module.decide(plan, wallet=facts.wallet_coins,
                                     spend_fraction=facts.spend_fraction)

    upgrade_id = _upgrade_id(outcome.objective_id)
    upgrade = upgrades.by_id(upgrade_id) if upgrade_id is not None else None
    if upgrade is None:
        # Every state but this one names a purchase, so an unnamed one is an
        # account a human has to look at - the same answer, and the same
        # goal sentence, the old ranking gave when nothing had its
        # prerequisites met.
        return RerollDecision(
            facts.account_id, build.id, "Review account progression",
            "needs_operator", None, None, None, None, facts.wallet_coins,
            facts.lifetime_coins, outcome.reason)

    price = _as_int_price(outcome.price)
    economy_reason = (f"Early utility allocation: {facts.utility_spent_coins} "
                      f"of about {UTILITY_TARGET_COINS} verified coins invested "
                      f"(ceiling {UTILITY_CEILING_COINS}). "
                      if build is not original_build else "")
    result = RerollDecision(
        facts.account_id, build.id, _GOALS.get(build.id, _DEFAULT_GOAL),
        outcome.state, upgrade.id, upgrade.name, upgrade.category, price,
        facts.wallet_coins, facts.lifetime_coins,
        economy_reason + outcome.reason + drawn + _price_share(price, facts.lifetime_coins)
        + _variant_label(build, facts))
    return _cheap_filler(facts, result)


def project_next(facts: RerollFacts, *, limit: int = 10) -> tuple[PlannedPurchase, ...]:
    """Show an ordered projection; only choose_next may authorize the first buy.

    Future prices and balances are unknown, so this simulates nothing but
    progress: each step re-runs the whole pipeline against the account plus
    the milestones the steps before it reached. The executable shopping path
    still reads live evidence.

    One step is one MILESTONE, not one purchase, and that is the one place
    this projection models more than it observes. A row the build stops at a
    displayed value (`thorns` at 51) takes an unknown number of purchases to
    get there - unknown because it depends on a price curve and a value curve
    nobody in this repository has measured - so the step records that the
    target was reached and moves on. Counting it as a single purchase instead
    would leave the projection stuck on that one row for every remaining
    position, which tells an operator nothing about the route; claiming a
    number of purchases would be a measurement nobody made.

    `limit` is a ceiling, not a promise: the projection stops early when the
    build runs out of milestones, which is a real end state now that a
    milestone can be met - the old `weight / (1 + purchases)` rotation could
    never reach one.
    """
    counts = dict(facts.purchases)
    values = dict(facts.values)
    planned: list[PlannedPurchase] = []
    for position in range(1, max(0, limit) + 1):
        step = replace(facts, purchases=counts, values=values,
                       wallet_coins=None, prices={})
        decision = choose_next(step)
        if decision.upgrade_id is None:
            break
        upgrade = upgrades.by_id(decision.upgrade_id)
        if upgrade is None:
            break
        # `RerollDecision.stage` IS the build id this step planned against,
        # so the focus line and the target below come from the same build
        # that chose the upgrade - never from a second selection call that
        # could answer differently.
        build = builds.by_id(decision.stage)
        focus = (_ECONOMY_FOCUS.get(upgrade.id)
                 or (build.focus.get(upgrade.id) if build is not None else None)
                 or _DEFAULT_FOCUS)
        planned.append(PlannedPurchase(facts.account_id, position, upgrade.id,
                                       upgrade.name, upgrade.category,
                                       upgrade.unlock, focus))
        counts[upgrade.id] = counts.get(upgrade.id, 0) + 1
        target = build.targets.get(upgrade.id) if build is not None else None
        if target is not None:
            values[upgrade.id] = target
    return tuple(planned)
