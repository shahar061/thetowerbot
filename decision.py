"""The decision layer: one ranked plan in, one thing to do next out.

``director.plan()`` answers "what is worth doing, in what order, and what is
holding the rest". That is a page a human reads. It is not an instruction: it
does not say whether the coins are there, whether anybody has even looked at
the price, or whether spending now would empty a balance the account needs
for something else. ``decide()`` answers that last question and nothing else -
it turns a ranked plan plus two observations (a wallet, a spend limit) into
exactly one of five states.

The five states are ``fleet/reroll_planner.py:choose_next``'s, generalised
------------------------------------------------------------------------------
``choose_next`` has been driving real reroll accounts, and its state machine
is the part of it worth keeping: its inputs are a hard-coded weight table, a
hard-coded prerequisite map and a hard-coded target list, all three of which
now live in ``knowledge/builds.v1.json`` and reach a ranked plan through
``builds`` -> ``workshop_objectives`` -> ``director``. What does NOT change on
the way is the decision:

``observe_price``
    The chosen candidate has no price. This is the ``None`` that
    ``workshop_objectives`` deliberately produces for a Workshop row nobody
    has read - the price of Defense Absolute level 21 is not the price of
    level 20, so no constant can stand in for it and inventing one would rank
    a spend against a number nobody ever saw. It means "go read the row", not
    "give up", and it is the one state whose remedy is a screenshot rather
    than a purchase.
``observe_balance``
    The price is known and the wallet is not. Same shape, other half.
``save_coins``
    Known, and either unaffordable or affordable but outside the spend
    reserve. Reports the exact SHORTFALL - how many coins are missing - not a
    horizon. ``plan()`` speaks in hours because it is ranking; an operator
    asking "can I buy this yet" wants the number that closes the gap.
``buy``
    Affordable, and within the reserve.
``needs_operator``
    Nothing on the plan is both ready and unheld. There is no next purchase
    to name, and the honest answer is that a person has to look.

Why the choice is ``plan.top`` and not a second scan of ``candidates``
------------------------------------------------------------------------
``director.rank`` sorts ready-before-everything and unheld-before-held, so
``ranked[0]`` is the best ready, unheld candidate whenever one exists at all,
and ``top`` is exactly ``ranked[0] if ready and not held``. "The best
candidate that may be acted on" therefore already has one implementation, in
the module that owns the ranking. Re-deriving it here - scanning
``candidates`` for the first ready, unheld one - would be a second copy of the
gate whose entire purpose is that there is only one: the day ``plan()``
learned a new reason to withhold a recommendation, this module would keep
recommending it. So ``plan.top is None`` IS ``needs_operator``, by
definition, and this module never inspects ``held_by`` or ``status`` itself.

That also means propagation cannot leak a purchase through here. A blocked
objective carrying a large inherited value is still blocked, still not
``top``, and therefore never reaches a ``buy`` (see ``director``'s own
docstring, and ``test_director.py``'s
``test_a_blocked_objective_is_never_top_however_much_value_it_holds``).

The spend reserve, ported faithfully
--------------------------------------
``choose_next``'s guard: when ``0 < spend_fraction < 1`` and ``price >
int(wallet * spend_fraction)``, the answer is ``save_coins`` with
``ceil(price / spend_fraction) - wallet`` - the coins needed to make this
purchase fit INSIDE the limit, not merely to afford it. It is the only thing
standing between an autonomous planner and a drained balance, so the
arithmetic is copied rather than re-derived, and
``test_decision.py`` pins both boundaries exactly (at ``price == int(wallet *
fraction)`` it buys; one coin past, it saves).

A ``spend_fraction`` outside ``[0, 1]`` RAISES rather than being ignored.
``choose_next`` simply lets its guard lapse for such a value, which is the
dangerous failure here: an operator who writes ``50`` meaning "50%", or a
negative left by a bad edit, would get no reserve at all and no indication
that the one guard on spending had quietly switched itself off. ``0`` and
``1`` are accepted and both mean "no reserve" - the two legitimate ways to
say it, kept because ``choose_next`` already treats them that way and because
neither is a plausible typo for a real limit. This is the same judgement
``value_propagation._validated_discount`` makes about its own tunable: a
nonsensical setting is a configuration bug, and clamping it silently would
reorder (or here, unlock) every spend while looking like it worked.

A negative price or a negative wallet reads as UNOBSERVED, not as a number.
That is this repository's established sentinel - ``choose_next`` already
receives a negative price for an unreadable row, and
``workshop_objectives._observed_price`` already drops one - so a ``-1``
arriving here answers ``observe_price`` / ``observe_balance`` ("go look
again"), never a shortfall computed from a quantity that cannot exist.

The unmet ``precondition``: surfaced, deliberately not gated
--------------------------------------------------------------
``builds.prerequisites()`` says ``cash_bonus -> unlock_cash_bonuses``, but the
turtle build does not weight ``unlock_cash_bonuses`` (the opening build buys
it, a stage earlier). ``workshop_objectives`` therefore drops that edge from
``requires`` - a dangling id would make ``classify()``'s subset test pin the
objective to ``blocked`` forever - and writes it into the action's
``precondition`` instead. So today nothing blocks ``workshop.cash_bonus`` on
its unlock, and this module is the first place that could.

It does not block on it either, and the reason is evidence rather than
convenience. Reaching ``buy`` or ``save_coins`` at all requires an OBSERVED
price, and a price is read off the Workshop tab's row for that upgrade -
which is the same proof ``shopping.py:_already_unlocked`` already relies on:
"a granted row cannot appear on the tab until its unlock is bought, so seeing
one is proof rather than an inference". An unmet unlock and an observed price
are therefore very nearly mutually exclusive, and in the case where the price
is absent this module already answers ``observe_price`` - "go read the row" -
which is precisely the action that resolves the gate. Blocking as well would
replace a self-correcting loop with a dead end (``needs_operator`` on an
account that only needs someone to look at a screen), and it would do so on
the strength of a string.

That last part is the second half of the reason. The only way to tell a
precondition that ``requires`` already enforces ("workshop.unlock_thorns
satisfied", true by construction whenever the candidate is ready) from one
nothing enforces is to read the prose ``workshop_objectives`` wrote, and a
consumer that branches on another module's wording is a parser for English
that breaks silently the next time somebody improves a sentence. So the
string is carried verbatim onto the ``Decision`` and named in the reason for
every state that would spend, where a human can act on it, and no rule is
keyed off its contents. ``test_decision.py`` pins both halves: an untracked
precondition with an observed price still answers ``buy`` and says the gate
out loud, and the same objective with no price answers ``observe_price``.

Purity
------
No I/O, no clock, no device, no database, and nothing from ``fleet/`` -
``fleet/reroll_planner.py`` is the behaviour this module generalises, never
an import (it carries a `RerollFacts` shape bound to one account's reroll
loop, which is the coupling this phase exists to remove).
``test_decision.py``'s
``test_decision_module_imports_nothing_that_touches_a_device`` enforces that
as an allowlist over this module's own import statements, the same discipline
``objectives.py``, ``director.py``, ``value_propagation.py`` and
``workshop_objectives.py`` each apply to themselves.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import director

State = Literal["observe_price", "observe_balance", "save_coins", "buy", "needs_operator"]

# The five, in the order `decide` can reach them, exported so a consumer (a
# dashboard, a test) can enumerate them without re-typing the strings. A
# `Literal` is not iterable at runtime and `typing.get_args` on it is an
# implementation detail; this is the one list, and `test_decision.py` checks
# it against the annotation so the two cannot drift.
STATES: tuple[State, ...] = (
    "observe_price", "observe_balance", "save_coins", "buy", "needs_operator")


@dataclass(frozen=True)
class Decision:
    """One thing to do next, and the sentence that justifies it.

    Every field is present in every state; the ones that cannot be known
    are `None`, never a zero standing in for an absence. `price is None`
    with `state == "observe_price"` is the whole point of that state, and a
    `0` there would read as free.

    `objective_id` is `None` only for `needs_operator`, where there is no
    chosen candidate to name.

    `shortfall` is the exact number of coins missing, and it is `None` for
    every state but `save_coins`. It is deliberately not a horizon in
    hours: `director` ranks in hours because it is comparing objectives
    against each other, but an operator looking at one purchase asks "how
    much short am I", and answering that with "about 1.3h at the measured
    rate" makes them do the arithmetic the planner already could have.
    Both numbers exist - the horizon is on `director.Candidate` - and each
    is stated in the register of the question it answers.

    `path` is `director.Candidate.path`: the chain of objective ids whose
    inherited value justifies this candidate's rank
    (`value_propagation.chain_to`). One element means it earned its place
    on its own declared value; more means the reason to buy it is what it
    leads to, and `reason` says so in words.

    `price` and `wallet` are the READINGS, after the sentinel check in
    `_observed` - so a `-1` that arrived meaning "OCR could not resolve
    this row" is reported as `None` here, matching the state it produced.
    Echoing the raw `-1` back next to `state="observe_price"` would be a
    record that contradicts itself.

    `preconditions` is carried verbatim from the objective's actions and
    never acted on - see the module docstring's "unmet precondition"
    section for why surfacing beats gating here, and why no rule may be
    keyed off the text.
    """

    state: State
    objective_id: str | None
    price: int | float | None
    currency: str | None
    wallet: int | float | None
    shortfall: int | None
    path: tuple[str, ...]
    preconditions: tuple[str, ...]
    reason: str


def _validated_spend_fraction(spend_fraction: float | None) -> float | None:
    """`spend_fraction` as a usable reserve, or a raised ValueError.

    See the module docstring: a value outside `[0, 1]` cannot be honoured
    and must not be silently ignored, because ignoring it removes the only
    guard on spending while the caller goes on believing one is set. `nan`
    raises through the same comparison (every comparison against `nan` is
    False), rather than disabling the guard the way `nan` silently disables
    an unguarded `>` test.
    """
    if spend_fraction is None:
        return None
    fraction = float(spend_fraction)
    if not (0. <= fraction <= 1.):
        raise ValueError(
            "spend_fraction is the share of the wallet this purchase may use, "
            f"so it must be between 0.0 and 1.0 inclusive, got {spend_fraction!r} "
            "- 0 and 1 both mean 'no reserve'; a percentage (50) or a negative "
            "would disable the only limit on spending")
    return fraction


def _observed(value: int | float | None) -> int | float | None:
    """A reading, or None when what arrived is not one.

    `bool` is rejected before `isinstance(..., int)` can accept it, the
    same guard `workshop_objectives._number` and `builds._number` keep and
    for the same reason: `True` is an `int` in Python, and a `True` that
    reached a price or a balance would be spent as a 1.

    A negative is None rather than a number, because this repository
    already uses a negative as "unreadable" for exactly these two
    quantities (`fleet/reroll_planner.py:choose_next` receives a negative
    price for a row OCR could not resolve, and
    `workshop_objectives._observed_price` drops one). `nan` goes the same
    way: it is not a quantity, and every comparison it takes part in is
    False, so leaving it in would slip past both the affordability test and
    the reserve test and arrive at `buy`.
    """
    if value is None or type(value) is bool:
        return None
    if not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or value < 0:
        return None
    return value


def _amount(value: int | float) -> str:
    """A quantity of currency, written the way a price tag is.

    Not `f"{value:g}"`, which switches to scientific notation above six
    digits: a Workshop price of 1500000 would print as "1.5e+06", and an
    operator cannot count that against a balance. Not bare `str` either,
    which would render a float price as "300.0" next to an integer
    balance of "1200". Integral values print as integers; anything else
    prints in full.
    """
    return str(int(value)) if float(value).is_integer() else str(value)


def _leads_to(path: tuple[str, ...]) -> str:
    """The clause that says what a propagated rank was actually for.

    Empty for a one-element (or empty) path: nothing depends on this
    objective, so it is worth buying for itself and a claim that it "leads
    to" something would be false. `director.plain_name` is shared rather
    than re-implemented so that the plan's sentence and this one name the
    same objective with the same words - the full chain is on
    `Decision.path` for anyone who needs to check it against the graph.
    """
    if len(path) < 2:
        return ""
    # Parenthetical, and always immediately after the objective's name, so
    # it reads as a clause about the thing being bought in every one of the
    # five sentences rather than dangling off whatever word happens to end
    # them ("...before deciding whether to buy or save, which leads to
    # thorns" attaches the chain to the wrong noun).
    return f" (which leads to {director.plain_name(path[-1])})"


def _gate_clause(preconditions: tuple[str, ...]) -> str:
    """The gates the graph states but does not enforce, said out loud.

    Appended only to the two states that lead to spending. Verbatim, and
    including the preconditions that `requires` already enforces: telling
    them apart would mean parsing another module's prose (see the module
    docstring), and a redundant true sentence costs a reader a moment
    while a dropped one costs them the purchase.
    """
    if not preconditions:
        return ""
    return " Check first: " + "; ".join(preconditions) + "."


def decide(plan: director.Plan, *, wallet: int | None = None,
           spend_fraction: float | None = None) -> Decision:
    """The one next move `plan` implies, given what is known about the money.

    `wallet` is the balance of the chosen candidate's OWN currency
    (`Decision.currency`, which the sentence names rather than assuming
    coins) - the caller reads it and passes it, because a `Plan` carries a
    horizon in hours, and an hours-to-afford of `0.0` says "affordable"
    without saying by how much, which is not enough to compute a shortfall
    from. `None` means nobody has read it, which is `observe_balance` and
    not zero: an unread balance treated as zero would answer "save 300
    coins" to an account that already has them.

    `spend_fraction` is the share of that wallet this purchase may use. See
    the module docstring for the reserve's arithmetic and for why a value
    outside `[0, 1]` raises instead of lapsing.

    Pure: same plan, same wallet, same fraction, same Decision, every time.
    Decides nothing about WHEN to act and taps nothing - like `plan()`
    above it, this is a recommendation with its reasoning attached.
    """
    reserve = _validated_spend_fraction(spend_fraction)
    balance = _observed(wallet)
    top = plan.top

    if top is None:
        # Not an error and not a failure: `plan.reason` already explains
        # what is blocked or held, and this module does not restate it (a
        # second summary of someone else's census is a second thing to keep
        # in agreement). It names the one fact it owns - there is nothing
        # to buy - and hands back.
        return Decision(
            state="needs_operator", objective_id=None, price=None, currency=None,
            wallet=balance, shortfall=None, path=(), preconditions=(),
            reason="Nothing on this plan is both ready and unheld, so there is no "
                   "purchase to recommend and the next move is the operator's.")

    price = _observed(top.price)
    name = director.plain_name(top.objective_id)
    leads_to = _leads_to(top.path)
    currency = top.currency or "coins"
    gates = _gate_clause(top.preconditions)

    def decision(state: State, shortfall: int | None, reason: str) -> Decision:
        return Decision(
            state=state, objective_id=top.objective_id, price=price,
            currency=top.currency, wallet=balance, shortfall=shortfall,
            path=top.path, preconditions=top.preconditions, reason=reason)

    if price is None:
        return decision(
            "observe_price", None,
            f"Read the current Workshop price for {name}{leads_to} before "
            "deciding whether to buy or save.")

    if balance is None:
        return decision(
            "observe_balance", None,
            f"Read the current balance of {currency} before spending "
            f"{_amount(price)} on {name}{leads_to}.")

    if balance < price:
        # ceil, not a bare subtraction: a price is an integer everywhere
        # this repository produces one, but `Candidate.price` is typed
        # `int | float | None`, and a float shortfall rounded DOWN would
        # tell an operator to save one coin too few and then refuse the
        # purchase again when they came back.
        missing = math.ceil(price - balance)
        return decision(
            "save_coins", missing,
            f"Save {missing} more {currency} for {name}{leads_to}.{gates}")

    if reserve is not None and 0. < reserve < 1. and price > int(balance * reserve):
        # `ceil(price / reserve) - balance`, verbatim from `choose_next`:
        # the balance at which this purchase fits INSIDE the limit, not the
        # one at which it is merely affordable. Truncating `balance *
        # reserve` with `int` is also verbatim, and is what makes the
        # boundary exact - at `price == int(balance * reserve)` the
        # purchase is allowed.
        needed = math.ceil(math.ceil(price / reserve) - balance)
        return decision(
            "save_coins", needed,
            f"Save {needed} more {currency} to keep {name}{leads_to} "
            f"within the {reserve:.0%} spend limit.{gates}")

    return decision(
        "buy", None,
        f"Buy {name}{leads_to} for {_amount(price)} {currency} - the observed "
        f"balance of {_amount(balance)} covers it.{gates}")
