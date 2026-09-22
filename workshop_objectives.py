"""A build's weight list, as objectives.Objective milestones the director can rank.

`objectives.GRAPH` is authored: forty-two hand-written objectives whose ids,
predicates and prices a human typed once. A Workshop build cannot be authored
the same way and the reason is arithmetic, not taste. The turtle build weights
`defense_absolute` with no cap and `thorns` up to 51; writing one objective per
level would be hundreds of near-identical rows whose only difference is a
number, and every one of them would carry a price that is wrong the moment
anything is bought. So this module GENERATES the objectives per call, from the
committed build plus whatever the caller has actually observed, and emits one
objective per meaningful milestone rather than one per level.

Why generated is the only shape that can carry a price
------------------------------------------------------
`director._price_and_currency()` reads the price out of `Action.params`, and
that is deliberate (see its own docstring): for a tier unlock or a UW slot the
cost is a fixed constant the graph's author wrote down, not a wiki value the
ranker would have to trust unverified. A Workshop row has no such constant.
The price of Defense Absolute level 21 is not the price of level 20 - it
climbs with every purchase - so ANY number baked into a static objective is
wrong the first time the bot buys, and worse than wrong: it is a confident
wrong number the affordability horizon will happily divide by, producing a
plan that recommends a spend the account cannot make.

The resolution keeps `director.plan()`'s contract untouched. Because these
objectives are built fresh on every call, the price in `Action.params` can be
the OBSERVED one the caller just read off the Workshop tab, passed in through
`prices`. It is a constant for exactly as long as the objective it sits on
exists, which is one call - the property `_price_and_currency` needs, without
a static constant anywhere.

And when `prices` does not name an upgrade, this module emits NO cost param at
all. Not a guess, not an estimate, not last call's number carried forward:
`_price_and_currency` then returns `(None, None)`, the horizon is `None`
rather than a fiction, and the decision layer answers `observe_price` - go
read the tab - exactly as `fleet/reroll_planner.py:choose_next` already does
for a missing price. An invented price would skip that step and start ranking
purchases against a number nobody ever saw.

Three milestone kinds, and the open question among them
--------------------------------------------------------
Every weighted upgrade produces exactly one objective. Which kind is decided
by the upgrade and the build, in this order:

1. `Upgrade.unlock` - a one-shot tile that vanishes from the tab once bought.
   One objective, "own this unlock". Takes precedence even if the build also
   names a target for it, because a tile you buy once has no ladder to climb.
2. A target in `build.targets` - "reach the displayed value this build stops
   at". Note DISPLAYED VALUE, not level: `builds.Build.targets` is documented
   as "stop buying this upgrade once its displayed VALUE reaches the target"
   (turtle's `thorns: 51.0` is 51 thorn damage), and displayed values are what
   `AccountRevision.workshop_stats` records. Comparing a target against a
   level would silently never fire.
3. Neither - "advance this at least once more", anchored at the value observed
   in the revision this call was given.

Kind 3 is the design decision this module had to make rather than inherit. An
untargeted weighted upgrade has no end state at all - the build buys Damage
forever - so there is no milestone in the game to name, and the three obvious
candidates all fail:

* "satisfied once it is owned at all" makes every untargeted upgrade `done`
  the first time the tab is read (every Workshop row has a displayed value
  from level one), so the build's whole untargeted majority - eleven of the
  opening build's twelve rows - vanishes from the plan and the director
  recommends nothing.
* "never satisfiable" (the shape `claim.*` uses in `objectives.py`) is
  honest about the open-endedness, but `director._permanently_unresolvable_reason`
  HOLDS a never-satisfiable objective out of the top pick unconditionally, so
  the plan would again recommend nothing.
* one objective per level is the hundreds-of-rows explosion above.

So the milestone chosen is RELATIVE: "the displayed value is past where it
stood when this plan was made". It is the smallest milestone that is real -
one purchase landed - it is decidable from a single revision, it can never be
spuriously satisfied (strictly past, not "at least"), and it is false by
construction at the moment of generation, which is the correct answer: nothing
has been bought yet in this snapshot. The price it carries is likewise the
price of exactly that next purchase, which is the only price anybody has
actually observed.

The direction of "past" is taken from `upgrades.target_reached`, never from a
`>` written here, because Shockwave Frequency and Wall Rebuild are displayed
as intervals and IMPROVE DOWNWARD. A hand-written `value > baseline` would
mark those two as "advanced" only when the upgrade got worse, and would report
a genuine purchase as no progress - forever, silently. `target_reached` owns
that direction table (`upgrades._DECREASING_TARGET_IDS`); re-deriving it here
would be a second copy of it, free to drift.

A relative milestone needs a baseline, and an unread baseline is not zero
------------------------------------------------------------------------
If the revision carries no reading for an untargeted upgrade, there is no
anchor, and "has it moved past an unknown number" is unanswerable from ANY
revision - not just this one. That objective therefore declares
`never_satisfiable=True`, which is the literal truth about it and which
`director` reads to hold it out of the top pick. The consequence is
deliberate and worth stating plainly: on a Workshop that has never been read,
every untargeted objective is held, the plan recommends nothing, and the only
sensible next action is to go look at the tab. That is the correct first move,
and it is the same answer the missing-price path gives.

satisfied_by, three-valued, over workshop_stats and nothing else
-----------------------------------------------------------------
`objectives.py`'s docstring states the rule this module obeys: True is
provably satisfied, False is provably not, None is "the facts needed to decide
are absent", and None BLOCKS. An unread level must never be read as level 0 -
that is the one mistake that would make every objective in the build look
actionable and point the bot at a screen it cannot see.

The section read is `AccountRevision.workshop_stats`, alone. Not
`workshop_levels`: nothing in this repository WRITES it (grep finds the field
declaration, the repository's own JSON decode, and readers - no producer
anywhere), so it is `None` on every real revision and a predicate reading it
would return `None` forever.
Not `unlocks` either, for the same reason `objectives.py` gives - on the live
account every section but `workshop_stats` is null.

Unlock objectives answer True or None, never False, and that is not an
oversight. `AccountRevision.workshop_stats` can only ever hold rows that
carried a readable number (`AccountState._facts` drops a row whose value is
`None`), and an unlock tile shows a price, not a value - so no `unlocks.*`
fact reaches a revision at all. What DOES reach one is proof by grant:
`shopping.py:_already_unlocked` establishes that "a granted row cannot appear
on the tab until its unlock is bought, so seeing one is proof rather than an
inference". Seeing `stats.thorns` proves Unlock Thorns was bought. NOT seeing
it proves nothing whatsoever - the panel shows a handful of rows at a time and
the alternative reading of a missing row is "OCR lost it" - so the honest
answer there is None, and None blocks whatever the unlock gates.

requires: real objective ids, and the edge this module will not fake
---------------------------------------------------------------------
`builds.prerequisites()` is game structure - Thorns is unbuyable before Unlock
Thorns on every build there will ever be - and it is keyed by upgrade id.
`objectives.classify()` walks `requires` against OBJECTIVE ids, so each edge is
translated. For the turtle build that yields the real chain the planner needs:
`workshop.thorns` -> `workshop.unlock_thorns` -> `workshop.unlock_defense_upgrades`.

An edge is kept only when the prerequisite is itself weighted by this build.
Turtle weights `cash_bonus` but not `unlock_cash_bonuses` (the opening build
buys that, in an earlier stage of the same account), so there is no objective
here for the edge to point AT. Pointing it at an id no objective carries would
not be caught by anything - `classify()` does a subset test, so a dangling
requirement silently pins the objective to `blocked` forever, which is the
exact silent-no-op `builds.py`'s module docstring exists to prevent. Inventing
an objective for the unweighted prerequisite is no better: `Objective.value`
would have to be a number this build never expressed a preference with. So the
edge is dropped from `requires` and written into the action's `precondition`
instead, where a human reading the plan can see the gate that is not modelled.
Phase 5 is where a cross-build or stage-aware graph could close it.

Ids: one objective per upgrade, and the milestone is not in the id
--------------------------------------------------------------------
`workshop.<upgrade_id>` - the catalog id, unabridged (`workshop.unlock_thorns`,
not `workshop.thorns` with the prefix stripped, which would collide with the
Thorns row itself). The milestone kind and the target number are deliberately
NOT in the id: a build that later gains a target for an upgrade would
otherwise rename that upgrade's objective, breaking every `requires` edge and
every stored reference to it, to say something the objective's own action
already says. One upgrade, one id, is also what makes the `requires`
translation above a lookup rather than a search.

The build id is likewise absent. These graphs are generated one build at a
time, and holding the id stable across a build switch means an account fact
that did not change - Unlock Thorns is bought - keeps the same name when the
strategy moves from opening to turtle.

Knowledge citations: none, honestly
-------------------------------------
`Objective.knowledge_refs` cites wiki facts, checked against
`knowledge.KNOWLEDGE.by_id`; a build's provenance is `builds.v1.json`'s own
`build_sources`, which is a different corpus on purpose (see `builds.py`).
There is no `priority.workshop.*` fact in the committed pack to cite, and
citing an unrelated one to fill the field would hand `director._held_reasons`
a footnote the objective does not actually rest on. Empty is the truthful
value. It has a consequence worth naming: `director`'s "unverified citation
behind a real spend" hold reads `knowledge_refs`, so it cannot see that these
objectives rest on a build whose own `rule_verified` is False. Closing that is
wiring, and wiring is Phase 5's.

Purity
------
No I/O, no clock, no device, no database, and nothing from `fleet/`. The
imports are the standard library, `builds`, `objectives`, `upgrades` and
`account_state` - enforced as an allowlist over this module's own import
statements by `test_workshop_objectives_imports_nothing_that_touches_a_device`,
the same discipline `objectives.py`, `director.py` and
`affordability_horizon.py` apply to themselves, and for the same narrow
reason: `import workshop_objectives` still pulls `cv2`/`numpy` in through
`account_state`, but nothing in that chain performs I/O at import time and no
emulator is ever contacted, so the whole module is exercised against synthetic
revisions.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import builds
import objectives
import upgrades
from account_state import AccountRevision

# Every id this module mints starts here, so a consumer merging this graph
# with `objectives.GRAPH` can tell the generated rows from the authored ones
# without a lookup table.
ID_PREFIX = "workshop."

# The `Action.params` key `director._COST_PARAM_CURRENCIES` maps to "coins".
# Named once, here, rather than spelled inline at the one site that writes it:
# a typo'd key does not raise, it makes `_price_and_currency` return
# `(None, None)` - a priced objective that looks unpriced forever.
COIN_COST_PARAM = "coin_cost"

# No such entry point exists yet - the purchase machinery lives inside
# `shopping.ShoppingLoop._buy_rows`, which is a loop over a scanned tab, not a
# callable that takes an upgrade id. The string is a name for the executor
# Phase 5 has to build, exactly as `objectives.py`'s own "labs.research" and
# "cards.buy_slot" are; `Action.executor` is a string precisely so naming one
# costs this module no import (see `objectives.Action`).
EXECUTOR = "shopping.buy_upgrade"

# Coins are refundable through the Workshop Respec lab, which is the mechanic
# `objectives.py`'s own definition of "reversible" names (`labs.workshop-respec`).
# Nothing bought on the Workshop tab is a permanent commitment the way an
# Ultimate Weapon pick is.
_RISK: objectives.RiskClass = "reversible"


def _number(value: Any) -> float | None:
    """A Fact value as a finite float, or None when it is not one.

    `bool` is rejected explicitly because it is a subclass of `int` in
    Python, so an unguarded `isinstance` check would read a `True` that some
    future reader wrote into a stats Fact as a displayed value of 1.0 - a
    level-1 reading nobody observed. The same guard `builds._number` keeps,
    for the same reason.
    """
    if type(value) is bool or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _observed_value(revision: AccountRevision, concept_id: str) -> float | None:
    """The displayed value `workshop_stats` records for `concept_id`, or None.

    None means "no readable fact", which covers the section never having been
    read (`()` on a fresh `AccountRevision`, `None` if a stored revision had
    it null), the row never having been scanned, and the row having been
    scanned into something unreadable. All three are the same answer to the
    only question a predicate asks - "do I know this number?" - and none of
    them is zero.
    """
    for fact in revision.workshop_stats or ():
        if fact.concept_id == concept_id:
            return _number(fact.value)
    return None


def _objective_id(upgrade_id: str) -> str:
    """The stable id for an upgrade's objective. See the docstring's "Ids"."""
    return f"{ID_PREFIX}{upgrade_id}"


def _unlock_owned(revision: AccountRevision,
                  granted_concept_ids: tuple[str, ...]) -> bool | None:
    """True once any row this unlock grants has been read; otherwise None.

    Never False: see the module docstring's "satisfied_by" section. A missing
    granted row is not evidence of a missing unlock, and claiming otherwise
    would be the one thing the three-valued predicate exists to forbid.
    """
    for concept_id in granted_concept_ids:
        if _observed_value(revision, concept_id) is not None:
            return True
    return None


def _target_met(revision: AccountRevision, upgrade_id: str, concept_id: str,
                target: float) -> bool | None:
    """Whether the displayed value has reached the build's stopping point.

    Direction comes from `upgrades.target_reached`, which knows the two
    upgrades whose targets are floors rather than ceilings.
    """
    value = _observed_value(revision, concept_id)
    if value is None:
        return None
    return upgrades.target_reached(upgrade_id, value, target)


def _advanced_past(revision: AccountRevision, upgrade_id: str, concept_id: str,
                   baseline: float | None) -> bool | None:
    """Whether at least one more purchase has landed since `baseline`.

    `baseline is None` (nobody had read this row when the objective was
    generated) answers None for every revision, not just the generating one:
    "further along than an unknown number" has no true answer. The objective
    declares `never_satisfiable` for that case rather than leaving a
    permanently-None predicate to look like work in progress.

    Strictly past, expressed as `target_reached(...) and value != baseline`
    rather than `value > baseline`, so the two downward-improving upgrades
    advance in their own direction - see the module docstring.
    """
    if baseline is None:
        return None
    value = _observed_value(revision, concept_id)
    if value is None:
        return None
    return value != baseline and upgrades.target_reached(upgrade_id, value, baseline)


def _observed_price(prices: Mapping[str, int] | None, upgrade_id: str) -> int | None:
    """The price the caller actually read for `upgrade_id`, or None.

    Anything that is not a non-negative plain integer - absent, a bool, a
    float, a negative sentinel meaning "unreadable" (the reading
    `fleet/reroll_planner.py:choose_next` already gives a negative price) -
    is None, and None means no cost param is emitted at all. Coercing a
    doubtful value into a number here would put a price nobody observed in
    front of the ranker, which is the single failure this module's price
    handling exists to prevent.
    """
    if prices is None:
        return None
    price = prices.get(upgrade_id)
    if type(price) is bool or not isinstance(price, int) or price < 0:
        return None
    return price


def _params(upgrade_id: str, price: int | None) -> tuple[tuple[str, Any], ...]:
    """The action's params: what to buy, and what it costs if that is known.

    `upgrade_id` rather than the concept id, because the flat catalog id is
    the execution contract (`upgrades.Upgrade.concept_id`'s own docstring
    says so) and the buyer resolves rows by it. The cost entry is present
    only when a price was observed - its ABSENCE is the signal, and it is
    the whole reason this tuple is assembled rather than written literally.
    """
    params: tuple[tuple[str, Any], ...] = (("upgrade", upgrade_id),)
    if price is not None:
        params += ((COIN_COST_PARAM, price),)
    return params


def _precondition(upgrade_id: str, prerequisite: str | None, tracked: bool) -> str | None:
    """What must hold before the action may run, including the untracked gate.

    An unweighted prerequisite is dropped from `requires` (it has no
    objective to point at) but it is still a real game gate, so it is said
    out loud here rather than disappearing - see the module docstring's
    "requires" section.
    """
    if prerequisite is None:
        return None
    if tracked:
        return f"{_objective_id(prerequisite)} satisfied"
    return (f"{prerequisite} must already be owned - this build does not weight it, "
            f"so no objective in this graph tracks it and nothing here blocks "
            f"{_objective_id(upgrade_id)} on it")


def workshop_objectives(build: builds.Build, revision: AccountRevision, *,
                        prices: Mapping[str, int] | None = None,
                        ) -> tuple[objectives.Objective, ...]:
    """`build`'s weight list as one objective per milestone, in weight order.

    Deterministic: the same build, revision and prices produce the same
    objectives, in the same order, every time. The order is `build.weights`'
    own, which is load-bearing - `builds.Build.weights` is a tuple of pairs
    rather than a mapping precisely because the position breaks ties between
    equal effective weights, and a consumer that re-sorted this tuple would
    throw that tiebreak away (the predicates are fresh closures per call, so
    two calls produce equal FIELDS, not equal objects - compare what the
    objectives say, not their identity).

    `prices` maps an `upgrades.CATALOG` id to the coin price the caller just
    read off the Workshop tab. An id it does not name gets no cost param, and
    therefore no price at all downstream; nothing is estimated or carried
    over. `revision` is read only through `workshop_stats`.
    """
    prerequisite_of = builds.prerequisites()
    weighted = {upgrade_id for upgrade_id, _ in build.weights}

    emitted: list[objectives.Objective] = []
    for upgrade_id, weight in build.weights:
        upgrade = upgrades.by_id(upgrade_id)
        if upgrade is None:
            # Unreachable against a loaded pack: `builds._known_upgrade`
            # rejects an unknown weight id at import. Raised rather than
            # skipped anyway, because skipping is how a build quietly plans
            # nothing at all (see `builds.py`'s module docstring).
            raise ValueError(
                f"build {build.id!r} weights unknown upgrade id {upgrade_id!r}")
        concept_id = upgrade.concept_id

        prerequisite = prerequisite_of.get(upgrade_id)
        tracked = prerequisite in weighted
        requires = (_objective_id(prerequisite),) if prerequisite and tracked else ()

        target = build.targets.get(upgrade_id)
        if upgrade.unlock:
            granted = tuple(child.concept_id for child in
                            (upgrades.by_id(c) for c in upgrade.unlocks) if child is not None)
            predicate: objectives.Predicate = (
                lambda r, g=granted: _unlock_owned(r, g))
            # An unlock tile that grants nothing leaves no observable trace
            # anywhere in an AccountRevision, so no revision can ever satisfy
            # it. Declared, not left for a consumer to discover: the whole
            # point of `never_satisfiable` living on the objective.
            never_satisfiable = not granted
            postcondition = (f"workshop_stats reports one of {list(granted)}"
                             if granted else "no observable postcondition exists")
            concept_ids = (concept_id, *granted)
        elif target is not None:
            predicate = (lambda r, u=upgrade_id, c=concept_id, t=target:
                         _target_met(r, u, c, t))
            never_satisfiable = False
            postcondition = f"workshop_stats reports {concept_id} at the build target {target:g}"
            concept_ids = (concept_id,)
        else:
            baseline = _observed_value(revision, concept_id)
            predicate = (lambda r, u=upgrade_id, c=concept_id, b=baseline:
                         _advanced_past(r, u, c, b))
            never_satisfiable = baseline is None
            postcondition = (f"workshop_stats reports {concept_id} past {baseline:g}"
                             if baseline is not None
                             else f"{concept_id} was never read, so no milestone could be anchored")
            concept_ids = (concept_id,)

        price = _observed_price(prices, upgrade_id)
        emitted.append(objectives.Objective(
            id=_objective_id(upgrade_id),
            requires=requires,
            # Nothing in this repository reads `Objective.grants` - the only
            # edge `classify()` walks is `requires`. An invented token here
            # would advertise an edge no consumer follows.
            grants=(),
            satisfied_by=predicate,
            actions=(objectives.Action(
                executor=EXECUTOR,
                params=_params(upgrade_id, price),
                precondition=_precondition(upgrade_id, prerequisite, tracked),
                postcondition=postcondition),),
            # `build.weight_of(upgrade_id)` by construction - read straight off
            # the row being iterated rather than looked up again, so the value
            # and the position that breaks its ties cannot come from two
            # different rows if the same id were ever weighted twice.
            value=weight,
            risk=_RISK,
            # Empty on purpose - a build's provenance is not a wiki fact.
            # See the module docstring's "Knowledge citations".
            knowledge_refs=(),
            concept_ids=concept_ids,
            never_satisfiable=never_satisfiable,
        ))
    return tuple(emitted)
