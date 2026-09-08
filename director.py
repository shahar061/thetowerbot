"""The director: one pure function from an account snapshot to a ranked plan.

``plan(revision, *, knowledge, graph, rates, strategy) -> Plan`` arms
nothing, taps nothing, spends nothing. It reads a revision, a knowledge
pack, an objective graph and a measured currency rate, and produces an
ordered, explained list of candidates plus (at most) one ``top`` pick. A
human reads this daily; nothing downstream of it in this phase acts on it.

Score, in one line
-------------------
``score = value / max(hours_to_afford, HORIZON_FLOOR_HOURS)`` - payoff per
hour of waiting. An objective whose horizon is ``None`` ("we cannot say how
long") gets ``score=None`` and sorts dead last, never first: a naive
``None`` sorting first (Python's default for a lot of homemade sort keys)
would make the least-knowable objective look the most urgent. An objective
whose horizon is ``math.inf`` (a *measured* dead end - see
``affordability_horizon.py``) still gets a real, if tiny, score: dividing by
infinity is finite arithmetic (0.0), never an exception and never ``nan``.
``None`` and ``math.inf`` therefore behave differently on purpose, exactly
as they must stay distinct in ``CurrencyRates`` and ``hours_to_afford``
below them: unknown blocks ranking outright, infinite ranks (very) low
without erasing the fact that it was actually measured.

Readiness is inherited, not re-decided
---------------------------------------
``status`` for a candidate is exactly ``objectives.classify(graph,
revision)[objective.id]`` - "done" / "ready" / "blocked". This module does
not re-derive the None-blocks rule; ``objectives.py`` already owns it (see
its own module docstring and ``test_objectives.py``'s coverage) and
``next_task.py``'s set-inclusion readiness test is the same algebra
``classify()`` already implements. Re-deciding it here would be a second
place for that rule to drift from the first.

``status`` alone, though, cannot tell a reader whether a "ready" objective's
OWN predicate came back known-False ("we checked - not done yet") or
unresolved-None ("we have never actually looked") - ``classify()``
deliberately renders both the same status, and that collapse is the
correct one for graph readiness (see its own docstring). It is not the
correct one for a human reading this dashboard, so ``Candidate.observed``
carries the raw ``satisfied_by(revision)`` tri-state alongside ``status``,
and ``_why`` renders it distinctly ("ready to attempt - never observed")
rather than folding it back into "ready to attempt" - see
``Candidate``'s own docstring.

``held_by``: the plan's own gate, separate from readiness
-----------------------------------------------------------
A candidate can be "ready" per the graph and still be HELD - flagged so it
can never become ``top`` - for reasons the graph itself does not encode:

1. A permanently-unresolvable predicate. ``cards.unlock.*`` returns ``None``
   forever (no ``AccountRevision`` shape expresses per-card unlock state)
   and ``claim.*`` returns ``False`` forever (the cadence needs a clock
   ``satisfied_by``'s one-argument signature cannot take) - see
   ``objectives.py``'s own family-4/5 notes. Both are therefore permanently
   "ready" and can never become "done". Held unconditionally, keyed off
   ``Objective.never_satisfiable`` - a field DECLARED on the graph
   (``objectives.py``), not matched here by id prefix. An id-prefix list in
   this module would be exactly the "a future author has to remember to
   update it" shape this phase keeps correcting elsewhere (underscore ids,
   opt-in concept-ref validation, a domain-seeded prefix set); a third
   never-satisfiable family added under a new prefix would have been
   silently ranked as normal. Declaring it on the objective instead, with
   ``objectives.py``'s own
   ``test_every_objective_is_satisfiable_or_declares_itself_never_satisfiable``
   proving every *other* objective is reachable from some synthetic
   revision, turns a forgotten declaration into a failing test in the
   graph's own suite - not a silent gap here.
2. A conflicted knowledge citation. Any objective whose ``knowledge_refs``
   touches a fact tainted by an unresolved wiki conflict is held, quoting
   the conflict's own ``statement`` and ``quote`` - independent of whether
   the objective has a price, because a contested fact is a live editorial
   dispute regardless of what it is footnoting.
3. An unverified citation behind a real spend. Every fact in the committed
   knowledge pack has ``rule_verified=False`` today (see ``knowledge.py``'s
   module docstring), so ``KNOWLEDGE.authorises(fact)`` is ``False`` for
   everything. Gating readiness itself on that would recommend nothing at
   all, which ``objectives.py`` explicitly declined to do (knowledge informs
   graph *shape*, not predicate truth). This module makes a narrower,
   different choice: an unverified citation only holds an objective that
   would actually SPEND currency (has a ``price``) on the strength of that
   citation - "recommending a spend it cannot justify" is the concrete harm,
   not "recommending research whose priority order might be wrong."
4. A one-way risk with no matching pre-approval. ``risk == "one_way"``
   (Ultimate Weapon picks, the Black Hole Damage lab) is permanent by
   ``objectives.py``'s own design. Held unless the ``strategy`` argument's
   arming ladder already names the action - which, for every strategy file
   committed to this repo today, it does not (no mapping from an
   ``Action.executor`` to a ``Strategy`` action name/template exists yet;
   that link is Phase 2's job). ``strategy`` is read for exactly this,
   nothing more: the whole point of taking it as a parameter is to explain
   what WOULD gate the action, not to gate the action itself - this module
   arms nothing.

Purity
------
No I/O, no clock, no device, no database RUNS when ``plan()`` is called -
``Plan.observed_at`` is ``revision.created_at`` - the revision's own
timestamp - never ``time.time()``: "now" is not a concept this module is
allowed to have on its own. ``tests/test_director.py``'s
``test_director_module_imports_nothing_that_touches_a_device`` enforces
THIS module's own, direct import statements as an allowlist - not the
transitive closure reachable through them. It cannot be the transitive
closure: this module imports ``objectives``, which imports
``account_state``, which imports ``ultimate_weapons``, which imports
``ocr`` and ``device``, which import ``cv2``/``numpy``/``adbutils`` - so
``import director`` genuinely needs those packages installed (outside this
repo's own venv, it raises ``ModuleNotFoundError: No module named 'cv2'``,
not a clean import). No I/O runs at import time anywhere in that chain
(``device.py`` only constructs a logger), and no emulator connection is
ever made - that is the real, narrower guarantee this allowlist and
``objectives.py``'s own enforce, the same way ``objectives.py`` and
``affordability_horizon.py`` enforce their own; ``test_the_plan_is_pure``
and ``test_the_plan_arms_nothing`` pin determinism and the forbidden-import
substring check directly on ``plan()``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import knowledge
import objectives
from account_state import AccountRevision
from affordability_horizon import hours_to_afford
from progression import CurrencyRates
from strategy import Strategy

# Below this, "sooner" is noise: a scan interval measured in seconds and a
# run measured in minutes cannot resolve a 30-second difference, and
# pretending to rank one would be false precision.
HORIZON_FLOOR_HOURS = .25

# The only Action.params keys this graph (or a future one) would use to
# name a real currency spend, mapped to the `currencies.<name>` concept id
# family catalog/concepts.v1.json defines. Only "stone_cost" is populated by
# any objective in the graph today (the nine uw.slot.* objectives); the
# rest are named for when a coin- or gem-priced objective exists to use
# them, so a future author extends one dict instead of re-deriving this
# mapping from scratch.
_COST_PARAM_CURRENCIES: dict[str, str] = {
    "coin_cost": "coins",
    "gem_cost": "gems",
    "stone_cost": "stones",
    "cash_cost": "cash",
}

# AccountRevision.inventory carries no writer yet anywhere in this repo
# (Phase 0b's job) so there is no established Fact.concept_id convention to
# reuse. This module's own choice, made explicit rather than left to be
# reverse-engineered later: the catalog's own `currencies.<name>` ids
# (catalog/concepts.v1.json's `currencies` domain - currencies.coins,
# currencies.gems, currencies.stones, ...) are the ids a balance-observing
# reader should write, and the ids this module reads.
_CURRENCY_CONCEPT_PREFIX = "currencies."


@dataclass(frozen=True)
class Candidate:
    """One objective, ranked and explained.

    `score` and `hours_to_afford` are `None` together only when the horizon
    is unknowable (no price modeled, or price/balance/rate never read) -
    never merely because the objective is blocked or held; a blocked or
    held objective still reports what its score WOULD be, so a human can
    see what unblocking or clearing the hold would buy.

    `value` mirrors `Objective.value` and exists on the candidate (not just
    reachable through the graph) so `rank` can use it directly as a
    tiebreak for the common case where `score` is `None` for both
    candidates being compared - see `rank`'s own docstring for why that
    case is the common one, not the rare one. Defaults to `0.0` for the
    synthetic candidates this module's own test suite constructs directly
    (bypassing `plan()`, which always supplies the real objective's value).

    `observed` is the tri-state `objective.satisfied_by(revision)` result
    that decided `status` - not re-derived from `status`, which collapses
    it. `status == "done"` implies `observed is True`; `status in
    ("ready", "blocked")` never has `observed is True` (that would have
    made it "done"), but does NOT by itself tell you whether the predicate
    returned `False` (known: not satisfied) or `None` (unknown: never
    observed) - exactly the distinction `classify()` deliberately does not
    carry past itself (see `objectives.classify`'s own docstring: a root
    objective's unresolved own-state still reads "ready", by design, and
    that design is correct for graph readiness). A human reading the
    dashboard needs the distinction anyway - "ready, and we know it isn't
    done yet" reads very differently from "ready, but we have never
    actually looked" - so it is carried here, alongside `status`, rather
    than folded into it.
    """

    objective_id: str
    status: objectives.Status
    score: float | None
    hours_to_afford: float | None
    price: int | float | None
    currency: str | None
    blocked_by: tuple[str, ...]
    held_by: tuple[str, ...]
    why: str
    knowledge_refs: tuple[str, ...]
    value: float = 0.0
    observed: bool | None = None


@dataclass(frozen=True)
class Plan:
    """The whole recommendation, plus the one instant it was computed for."""

    observed_at: float | None
    revision_id: int | None
    candidates: tuple[Candidate, ...]
    top: Candidate | None
    reason: str


def score(value: float, hours: float | None) -> float | None:
    """Payoff per hour of waiting, or None when the wait is unknowable.

    `hours == math.inf` is deliberately NOT special-cased here: dividing a
    finite `value` by `math.inf` is ordinary, exception-free float
    arithmetic that yields `0.0` - never `math.nan`, never `math.inf`
    itself. Special-casing it anyway would risk exactly the collapse this
    phase's governing rule forbids (treating "measured, never" the same as
    "unmeasured") by tempting a future edit to route both through the same
    branch. Only `hours is None` gets its own branch, because only that
    case must produce `score=None` rather than a number.
    """
    if hours is None:
        return None
    return value / max(hours, HORIZON_FLOOR_HOURS)


def rank(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Ready before blocked, unheld before held, scored before unscoreable,
    then by score descending, then by declared value descending, then by id
    so the order is total.

    The sort key is explicit about None rather than relying on a default:
    Python cannot compare None to a float, and a naive `key=lambda c: c.score`
    would either crash or - with a `or 0` - rank an unmeasurable objective
    above a poor but measurable one.

    The value tiebreak matters because most of the graph is unscoreable: 33
    of 42 objectives carry no price at all (see `_price_and_currency` -
    only the nine `uw.slot.*` objectives have one, in stones), so `score`
    and `hours_to_afford` are `None` for the rest and the `-(c.score or
    0.)` term above is `-0.` for every one of them - a tie. Without this
    line the next key was `c.objective_id`, and Python's `sorted` is
    stable but the KEY itself has no more information left to break the
    tie with except the id string - so every unpriced candidate, including
    the ones a human most needs ranked correctly (`tier.unlock.2`, value
    1.8 - the wave-100 gate itself), sorted alphabetically instead of by
    how much they are worth. `Objective.value` is exactly the number this
    module has for "how much is this worth" when a price/horizon cannot be
    computed at all; using it here, before the id, means the id is only
    ever consulted as the LAST, purely-cosmetic tiebreak among candidates
    that are equal in every way that matters - never a proxy for priority.
    """
    return tuple(sorted(candidates, key=lambda c: (
        c.status != "ready",
        bool(c.held_by),
        c.score is None,
        -(c.score or 0.),
        -c.value,
        c.objective_id,
    )))


def _price_and_currency(objective: objectives.Objective) -> tuple[int | float | None, str | None]:
    """A spend, if the objective's own action names one.

    Reads only `Action.params` - never a knowledge `Fact_.value` at
    runtime, for the same reason `objectives.py`'s predicates never do (see
    its module docstring's "Knowledge citations" section): a params entry
    is data the objective's own author wrote onto the graph, not a wiki
    value this module would otherwise have to trust unverified.
    """
    for action in objective.actions:
        for key, value in action.params:
            currency = _COST_PARAM_CURRENCIES.get(key)
            if currency is not None:
                return value, currency
    return None, None


def _balance(revision: AccountRevision, currency: str | None) -> int | float | None:
    """The account's current balance of `currency`, or None if unread.

    None propagates from either half of the question being unanswered:
    `currency` itself unknown (no price modeled) or `revision.inventory`
    never read at all. Phase 0b pays off here or not at all - see this
    module's docstring for the concept-id convention this reads.
    """
    if currency is None or revision.inventory is None:
        return None
    concept_id = f"{_CURRENCY_CONCEPT_PREFIX}{currency}"
    for fact in revision.inventory:
        if fact.concept_id == concept_id:
            return fact.value  # type: ignore[return-value]
    return None


def _rate_for(currency: str | None, rates: CurrencyRates) -> float | None:
    """`rates` measures one currency (coins, per `progression.rates`'s farm
    runs). Any other currency has no measured rate at all - not zero, not
    unmeasured-and-therefore-assumed-zero, just genuinely absent - so this
    returns None rather than reusing `rates.coins_per_hour` for a currency
    it was never measured against."""
    if currency == "coins":
        return rates.coins_per_hour
    return None


def _permanently_unresolvable_reason(objective: objectives.Objective) -> str | None:
    """Item 1 of the four handed-down findings.

    Reads `objective.never_satisfiable` - a field declared ON the graph
    (objectives.py) - rather than matching an id prefix here. An id-prefix
    list in a *consumer* module is exactly the shape this phase keeps
    tripping over (underscore ids, opt-in concept-ref validation, a
    domain-seeded prefix set, and formerly this list): a fact a future
    author has to remember to update in a second place. Declaring it on
    the objective, with
    `test_every_objective_is_satisfiable_or_declares_itself_never_satisfiable`
    enforcing that every OTHER objective is provably satisfiable by some
    synthetic revision, means a third never-satisfiable family added later
    under a new id prefix fails the objectives suite if its author forgets
    the declaration - not silently ranked as a normal candidate here.
    """
    if objective.never_satisfiable:
        return (
            "this objective's predicate can never return True from any "
            "observed account revision (declared never_satisfiable=True on "
            "the objective - see objectives.py) - it is permanently "
            "'ready' and is held out of the single top pick so the plan "
            "never recommends work that can never be marked done"
        )
    return None


def _has_pre_approval(objective: objectives.Objective, strategy: Strategy) -> bool:
    """Whether the strategy's arming ladder already names this action.

    There is no mapping yet from an Objective's `Action.executor` (e.g.
    "ultimate_weapons.pick") to a Strategy `ActionRule`'s on-screen
    `name`/`template` - building that link is Phase 2's job, once an
    executor exists that could act on it at all. This checks the only cheap
    signal available today: an *enabled* ActionRule literally naming the
    objective's id, its action's executor, or one of the concept ids its
    predicate cites. Against every strategy committed to this repo today
    that is False for every one-way objective - the honest state of the
    world before Phase 2 exists, not a bug in this check.
    """
    names = {objective.id, *objective.concept_ids, *(a.executor for a in objective.actions)}
    named_in_ladder = {rule.name.strip().lower() for rule in strategy.actions if rule.enabled}
    named_in_ladder |= {rule.template.strip().lower() for rule in strategy.actions if rule.enabled}
    return any(name.lower() in named_in_ladder for name in names)


def _held_reasons(objective: objectives.Objective, pack: knowledge.Pack,
                   strategy: Strategy, price: int | float | None) -> tuple[str, ...]:
    """One string per reason `objective` may never become `top` right now.

    Order: the permanently-unresolvable check first (cheapest, and applies
    regardless of knowledge), then per-citation conflicts (independent of
    price), then the price-gated unverified-spend check, then the one-way
    risk check. Every reason is a full sentence, not a code, because a
    human reads this - not just whether `held_by` is truthy.
    """
    reasons: list[str] = []

    permanently_unresolvable = _permanently_unresolvable_reason(objective)
    if permanently_unresolvable is not None:
        reasons.append(permanently_unresolvable)

    spend_rests_on_unverified_fact = False
    for ref in objective.knowledge_refs:
        fact = pack.by_id(ref)
        if fact is None:
            # Unreachable against the committed pack today (test_objectives.py
            # and test_knowledge.py both validate every citation resolves) -
            # kept as an explicit, honest branch rather than an unchecked
            # KeyError, per this phase's own governing rule.
            reasons.append(f"knowledge citation {ref!r} does not exist in the committed pack")
            continue
        conflicts = pack.conflicts_for(ref)
        if conflicts:
            for conflict in conflicts:
                reasons.append(
                    f"{ref} is contested ({conflict.id}): {conflict.statement} "
                    f"- quoting the source: {conflict.quote!r}"
                )
        elif not pack.authorises(ref):
            # `conflicts` is already empty and `fact` already exists at this
            # point (both handled above), so authorises() can only still be
            # False here for one reason: rule_verified is not True. Calling
            # it directly - rather than re-deriving that same
            # "rule_verified is True and not tainted" boolean inline -
            # keeps this module referring to the ONE place that invariant
            # is defined (knowledge.Pack.authorises), rather than a second
            # definition of it that could drift from the first.
            spend_rests_on_unverified_fact = True

    if price is not None and spend_rests_on_unverified_fact:
        reasons.append(
            "this objective would spend real currency resting on a wiki "
            "citation that is not yet rule_verified - knowledge.authorises() "
            "cannot justify the spend, so it is held rather than ranked as "
            "actionable"
        )

    if objective.risk == "one_way" and not _has_pre_approval(objective, strategy):
        reasons.append(
            "this is a one-way, irreversible choice and the strategy's "
            "arming ladder declares no matching pre-approval for it"
        )

    return tuple(reasons)


def _why(objective_id: str, status: objectives.Status, blocked_by: tuple[str, ...],
         held_by: tuple[str, ...], price: int | float | None, currency: str | None,
         hours: float | None, scored: float | None, rates: CurrencyRates,
         observed: bool | None) -> str:
    """A sentence a human reads - never just a score.

    `observed is None` on a "ready" objective means `satisfied_by(revision)`
    itself returned `None` - not "known, and not yet done" but "we have
    never actually looked" (every `AccountRevision` section but
    `workshop_stats` is null on the live account today, so this is the
    ordinary case there, not an edge case). `classify()` deliberately
    renders both the same `Status` - see its own docstring - because that
    collapse is safe for graph readiness; it is NOT safe for a human
    reading this sentence, who would otherwise read "ready" as "confirmed
    not done yet" every time, even when nothing has actually confirmed
    that. Said only for "ready": "blocked" already has its own sentence
    (the prerequisite, not this objective's own predicate, is what is
    unresolved), and "done" means the predicate returned `True`, never
    `None`.
    """
    parts: list[str] = []

    if status == "blocked":
        parts.append(f"Blocked on: {', '.join(blocked_by)}.")
    elif status == "done":
        parts.append(f"{objective_id} is already satisfied on this revision.")
    elif observed is None:
        parts.append(
            f"{objective_id} is ready to attempt - never observed: no account "
            "data has ever confirmed or denied this on its own, only its "
            "prerequisites are known to be met."
        )
    else:
        parts.append(f"{objective_id} is ready to attempt.")

    if held_by:
        parts.append("Held: " + " | ".join(held_by) + ".")

    if price is None:
        parts.append("No price is modeled for this objective, so it cannot be ranked by payoff.")
    elif hours is None:
        if currency == "coins":
            parts.append(f"Costs {price} {currency}; horizon unknown - {rates.reason}")
        else:
            parts.append(
                f"Costs {price} {currency}; horizon unknown - no income rate "
                f"has ever been measured for {currency}."
            )
    elif math.isinf(hours):
        # Deliberately never says "unknown": this IS a measurement (a
        # tier that earns nothing at the measured rate), not a missing one.
        # See CurrencyRates.coins_per_hour's own contract - collapsing this
        # into the "horizon unknown" branch above is exactly the mistake
        # this phase exists to prevent.
        parts.append(
            f"Costs {price} {currency}; at the measured rate this will "
            "never be affordable - a measured dead end, not a missing one."
        )
    elif scored is None:
        parts.append(f"Costs {price} {currency}; about {hours:.2f}h to afford.")
    else:
        parts.append(f"Costs {price} {currency}; about {hours:.2f}h to afford (score {scored:.3f}).")

    return " ".join(parts)


def _census(ranked: tuple[Candidate, ...], rates: CurrencyRates) -> str:
    """The counted state of the whole graph, in one sentence - the census
    and income state a human needs on the page every day, not only on the
    days there is no `top` pick.

    Counted, never summed or averaged: an infinite horizon mixed into an
    arithmetic mean would make the mean infinite too, silently erasing
    every finite horizon sitting next to it. Kept as separate, comparable
    counts instead - the same "never collapse" discipline `hours_to_afford`
    and `CurrencyRates` apply to the value itself.

    `rates.reason` is quoted here unconditionally - not only when a
    coins-priced candidate happens to exist. `_why` above quotes it too,
    but only for a coins-priced objective's own sentence, and the
    committed graph has none (every price in it is `stone_cost` - see
    `_price_and_currency`'s own comment); without this line a reader of
    the real graph's plan could never tell "income has never been
    measured" from "measured and irrelevant to what's priced today".
    """
    done = sum(1 for c in ranked if c.status == "done")
    ready = sum(1 for c in ranked if c.status == "ready")
    blocked = sum(1 for c in ranked if c.status == "blocked")
    held = sum(1 for c in ranked if c.held_by)
    unknown_horizon = sum(1 for c in ranked if c.price is not None and c.hours_to_afford is None)
    infinite_horizon = sum(
        1 for c in ranked
        if c.hours_to_afford is not None and math.isinf(c.hours_to_afford)
    )
    return (
        f"{done} done, {ready} ready, {blocked} blocked, {held} held. Of "
        f"the priced objectives, {unknown_horizon} have an unknown horizon "
        f"and {infinite_horizon} have a measured-infinite one. {rates.reason}"
    )


def _plan_reason(top: Candidate | None, ranked: tuple[Candidate, ...], rates: CurrencyRates) -> str:
    """The plan's own one-paragraph summary - never just the top pick's
    `why`.

    Two things earlier reviews found this function was discarding exactly
    when a reader needed them most: the ready/blocked/held census, and
    `CurrencyRates.reason` (see `_census`'s own docstring on why quoting it
    only inside a coins-priced candidate's `why` is not enough against the
    real graph). Both used to live ONLY in the `top is None` branch below -
    so on any day a `top` pick exists, which is most days, the census and
    income state were computed and then thrown away. `_census` is now
    appended to the top-pick sentence too, rather than only replacing it,
    so the dashboard's income state is legible every day, not only on days
    with no recommendation.
    """
    census = _census(ranked, rates)
    if top is not None:
        return f"Top pick: {top.why} {census}"

    return (
        "No objective is both ready and unheld right now: held pending "
        "independent knowledge verification or a one-way pre-approval, or "
        f"blocked on a prerequisite. {census}"
    )


def plan(revision: AccountRevision, *, knowledge: knowledge.Pack,
         graph: tuple[objectives.Objective, ...], rates: CurrencyRates,
         strategy: Strategy) -> Plan:
    """One pure function: an account snapshot to a ranked, explained plan.

    Arms nothing, taps nothing, spends nothing - see this module's
    docstring for how `held_by` decides what would gate an action without
    ever gating it here. `observed_at` is `revision.created_at`: the only
    notion of "now" this function is allowed, since it takes no clock.
    """
    statuses = objectives.classify(graph, revision)
    done = frozenset(oid for oid, s in statuses.items() if s == "done")

    candidates: list[Candidate] = []
    for objective in graph:
        status = statuses[objective.id]
        blocked_by = (
            tuple(sorted(set(objective.requires) - done)) if status == "blocked" else ()
        )
        # The same predicate call classify() already made internally (via
        # `done`), re-run here rather than threaded out of classify()'s own
        # return value: classify()'s contract stays exactly {"done", "ready",
        # "blocked"} per id (see objectives.classify's own docstring and
        # test coverage), and this module gets the tri-state it additionally
        # needs - see Candidate.observed's docstring - without asking that
        # contract to carry a second, director-only concern. satisfied_by is
        # PURE (objectives.py's own governing rule), so calling it twice is
        # cheap and never risks disagreeing with classify()'s own call.
        observed = objective.satisfied_by(revision)

        price, currency = _price_and_currency(objective)
        balance = _balance(revision, currency)
        rate = _rate_for(currency, rates)
        hours = hours_to_afford(price, balance, rate)
        scored = score(objective.value, hours)

        held_by = _held_reasons(objective, knowledge, strategy, price)
        why = _why(objective.id, status, blocked_by, held_by, price, currency,
                   hours, scored, rates, observed)

        candidates.append(Candidate(
            objective_id=objective.id,
            status=status,
            score=scored,
            hours_to_afford=hours,
            price=price,
            currency=currency,
            blocked_by=blocked_by,
            held_by=held_by,
            why=why,
            knowledge_refs=objective.knowledge_refs,
            value=objective.value,
            observed=observed,
        ))

    ranked = rank(tuple(candidates))
    top = ranked[0] if ranked and ranked[0].status == "ready" and not ranked[0].held_by else None
    reason = _plan_reason(top, ranked, rates)

    return Plan(
        observed_at=revision.created_at,
        revision_id=revision.revision_id,
        candidates=ranked,
        top=top,
        reason=reason,
    )


def _horizon_payload(hours: float | None) -> dict[str, object]:
    """A JSON-safe projection of an hours-to-afford horizon.

    `math.inf` is not valid JSON: `json.dumps` (and therefore every
    ``JSONResponse`` this repo returns) happily emits the bare token
    ``Infinity`` for it, which is not legal per RFC 8259 and which a strict
    parser - a browser's own `JSON.parse`, notably - rejects outright. That
    makes a naive `{"hours_to_afford": candidate.hours_to_afford}` a ticking
    bomb: it round-trips through Python's own `json` module (which is why a
    test using `json.loads(json.dumps(...))` would not catch it) but breaks
    the moment a real browser fetches the route.

    So this is the one place that gets corrected for the JSON boundary,
    with an explicit `kind` discriminator rather than a sentinel number a
    client would have to know to special-case:
      - `hours is None` ("we cannot say - go gather data") -> `{"kind":
        "unknown"}`.
      - `math.isinf(hours)` ("measured: not at this rate, ever") -> `{"kind":
        "infinite"}`.
      - otherwise, a real, finite wait -> `{"kind": "hours", "hours": ...}`.
    A client that only reads `.hours` when `kind == "hours"` can never
    mistake "unknown" for "infinite" for "12.3", which is exactly the
    collapse this whole phase exists to prevent - see this module's
    docstring and `affordability_horizon.py`.
    """
    if hours is None:
        return {"kind": "unknown"}
    if math.isinf(hours):
        return {"kind": "infinite"}
    return {"kind": "hours", "hours": hours}


def _knowledge_ref_payload(ref: str, knowledge: knowledge.Pack) -> dict[str, object]:
    """One citation, with the source link a human needs to go check it.

    `by_id` returning `None` is unreachable against the committed pack
    today (both `test_objectives.py` and `test_knowledge.py` validate every
    citation resolves - see `_held_reasons` above for the same note) but is
    handled explicitly rather than trusted blindly, per this phase's own
    governing rule.
    """
    fact = knowledge.by_id(ref)
    return {"id": ref, "source_url": fact.source_url if fact is not None else None}


def _candidate_payload(candidate: Candidate, knowledge: knowledge.Pack) -> dict[str, object]:
    """One candidate, JSON-safe, with nothing collapsed.

    `blocked_by`, `held_by` and `knowledge_refs` stay separate lists rather
    than folding into one "why" string a UI would have to re-parse: a human
    reading the dashboard needs "blocked" (a prerequisite), "held" (a
    deliberate gate) and "cited" (why it exists at all) to stay visibly
    distinct, exactly as `objectives.classify` and this module's own
    docstring keep them distinct upstream.
    """
    return {
        "objective_id": candidate.objective_id,
        "status": candidate.status,
        "score": candidate.score,
        "hours_to_afford": _horizon_payload(candidate.hours_to_afford),
        "price": candidate.price,
        "currency": candidate.currency,
        "blocked_by": list(candidate.blocked_by),
        "held_by": list(candidate.held_by),
        "why": candidate.why,
        "knowledge_refs": [_knowledge_ref_payload(ref, knowledge) for ref in candidate.knowledge_refs],
    }


def as_payload(result: Plan, *, knowledge: knowledge.Pack, top_n: int = 5) -> dict[str, object]:
    """A JSON-safe projection of `result`, for the one route that serves it.

    Kept here, not in `web/app.py` - `web/app.py` knows HTTP, not what a
    `Plan` means; the same reasoning `ledger.LedgerLine.as_row` follows
    ("db.py knows rows, not events").

    `candidates` is the ranked list's first `top_n` entries plus every
    remaining candidate whose `held_by` is non-empty, in their original
    rank order: a hold is the most actionable thing on the page - a
    decision waiting on a human - so it is never truncated away, however
    far down the ranking it sits. The two slices are index-disjoint
    (`result.candidates[:top_n]` and `result.candidates[top_n:]`), so no
    candidate can appear twice.
    """
    kept = list(result.candidates[:top_n]) + [c for c in result.candidates[top_n:] if c.held_by]
    return {
        "observed_at": result.observed_at,
        "revision_id": result.revision_id,
        "reason": result.reason,
        "top": _candidate_payload(result.top, knowledge) if result.top is not None else None,
        "candidates": [_candidate_payload(c, knowledge) for c in kept],
    }
