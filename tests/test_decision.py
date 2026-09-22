"""The decision layer: one ranked plan in, one thing to do next out.

Five states, generalised from `fleet/reroll_planner.py:choose_next`, and
three rules these tests exist to protect - in the order they would hurt:

  - The spend reserve is the only thing between an autonomous planner and a
    drained balance. Its boundary is pinned EXACTLY (at `price == int(wallet
    * fraction)` it buys; one coin past, it saves), and a `spend_fraction`
    outside [0, 1] raises rather than silently lapsing.
  - An absent price is `observe_price` - "go read the Workshop row" - never
    a guess and never a refusal. `workshop_objectives` produces that `None`
    on purpose, and a decision layer that treated it as "give up" would
    strand the account in front of a screen nobody has looked at.
  - A blocked objective can never be bought, however much value propagation
    pushes into it. `decide` takes `plan.top`, whose rule is unchanged.
"""
from __future__ import annotations

import dataclasses
import math
import typing

import pytest

import builds
import decision
import director
import knowledge
import objectives
import upgrades
import workshop_objectives
from account_state import AccountRevision, Evidence, Fact
from progression import CurrencyRates
from strategy import Strategy

CHAIN = ("workshop.unlock_defense_upgrades", "workshop.unlock_thorns", "workshop.thorns")


def candidate(**overrides: object) -> director.Candidate:
    """The turtle build's own top pick, as `director.plan` produces it.

    Built directly rather than through `plan()` so that one field at a time
    can be varied - a price, a chain, a precondition - without arranging an
    account revision that happens to produce it. The end-to-end path is
    exercised for real further down, against the committed build pack.
    """
    base = director.Candidate(
        objective_id="workshop.unlock_defense_upgrades", status="ready",
        score=4.1, hours_to_afford=.3, price=300, currency="coins",
        blocked_by=(), held_by=(), why="w", knowledge_refs=(), value=19.72,
        observed=None, path=CHAIN)
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def plan_of(top: director.Candidate | None) -> director.Plan:
    return director.Plan(observed_at=1., revision_id=1,
                         candidates=(top,) if top is not None else (),
                         top=top, reason="r")


def fact(concept_id: str, value: object) -> Fact:
    return Fact(concept_id, value, "verified", Evidence(
        1., .99, concept_id, str(value), (0, 0, 1, 1), 1080, 2400, "abc"))


def revision(**stats: object) -> AccountRevision:
    """A revision whose only populated section is `workshop_stats`.

    Keyword names are `upgrades.CATALOG` ids, bridged to the `stats.*`
    concept ids a real reading carries through `Upgrade.concept_id` - the
    same helper `tests/test_workshop_objectives.py` uses, so a test cannot
    key a fact on an id no reader writes.
    """
    return dataclasses.replace(AccountRevision(), workshop_stats=tuple(
        fact(upgrades.by_id(upgrade_id).concept_id, value)  # type: ignore[union-attr]
        for upgrade_id, value in stats.items()))


def turtle_plan(prices: dict[str, int], rev: AccountRevision | None = None) -> director.Plan:
    """The REAL pipeline: committed build -> generated objectives -> plan."""
    build = builds.by_id("turtle")
    assert build is not None
    account = rev if rev is not None else AccountRevision()
    return director.plan(
        account, knowledge=knowledge.KNOWLEDGE,
        graph=workshop_objectives.workshop_objectives(build, account, prices=prices),
        rates=CurrencyRates(1000., 1, 3, "test"),
        strategy=Strategy.from_config())


# -- the five states -----------------------------------------------------
def test_an_unread_price_asks_for_the_price_rather_than_giving_up() -> None:
    """`workshop_objectives` emits NO cost param for a row nobody has read,
    so `Candidate.price` is `None` - deliberately, because the price of
    Defense Absolute level 21 is not the price of level 20 and no constant
    can stand in. The answer is "go read the row", which is a step
    forwards, not `needs_operator`, which is a dead stop."""
    result = decision.decide(plan_of(candidate(price=None)), wallet=1200)
    assert result.state == "observe_price"
    assert result.price is None
    assert result.shortfall is None


def test_a_known_price_with_an_unread_wallet_asks_for_the_balance() -> None:
    result = decision.decide(plan_of(candidate()), wallet=None)
    assert result.state == "observe_balance"
    assert result.wallet is None


def test_an_unaffordable_price_reports_the_exact_shortfall_in_coins() -> None:
    """A shortfall, not a horizon. `director` ranks in hours because it is
    comparing objectives; an operator holding one purchase asks how much
    they are short, and "about 1.3h at the measured rate" makes them do the
    planner's arithmetic themselves."""
    result = decision.decide(plan_of(candidate(price=300)), wallet=100)
    assert result.state == "save_coins"
    assert result.shortfall == 200
    assert "200" in result.reason


def test_an_affordable_price_within_the_reserve_is_a_buy() -> None:
    result = decision.decide(plan_of(candidate(price=300)), wallet=1200,
                             spend_fraction=.5)
    assert result.state == "buy"
    assert result.shortfall is None


def test_nothing_ready_and_unheld_is_the_operators_move() -> None:
    """`plan.top is None` IS this state: `rank` already puts ready-unheld
    first, so `top` is the one implementation of "may be acted on" and this
    module does not keep a second."""
    result = decision.decide(plan_of(None), wallet=1200)
    assert result.state == "needs_operator"
    assert result.objective_id is None
    assert result.path == ()


def test_every_state_the_module_declares_is_reachable_and_spelled_once() -> None:
    """`STATES` and the `State` annotation are two lists of the same five
    strings; a consumer enumerating one while `decide` returns the other
    would fail on a state it had never heard of."""
    assert set(decision.STATES) == set(typing.get_args(decision.State))
    assert len(decision.STATES) == 5


# -- the reserve, at the boundary ----------------------------------------
def test_a_price_exactly_at_the_reserve_limit_is_still_a_buy() -> None:
    """`price > int(wallet * fraction)` - strictly past, so the limit itself
    is allowed. Ported verbatim from `choose_next`, because an off-by-one in
    either direction here is a purchase made or refused wrongly on every
    call, forever."""
    result = decision.decide(plan_of(candidate(price=500)), wallet=1000,
                             spend_fraction=.5)
    assert result.state == "buy"


def test_one_coin_past_the_reserve_limit_saves_instead() -> None:
    result = decision.decide(plan_of(candidate(price=501)), wallet=1000,
                             spend_fraction=.5)
    assert result.state == "save_coins"


def test_the_reserve_shortfall_is_the_balance_that_brings_it_inside_the_limit() -> None:
    """Not the balance that merely affords it - the account can already
    afford 501 out of 1000. `ceil(price / fraction) - wallet` = 1002 - 1000,
    and at a wallet of 1002 the same purchase is inside the limit: the test
    checks that second half too, because an amount that does not actually
    clear the guard would leave the planner saving forever."""
    result = decision.decide(plan_of(candidate(price=501)), wallet=1000,
                             spend_fraction=.5)
    assert result.shortfall == 2
    assert decision.decide(plan_of(candidate(price=501)), wallet=1002,
                           spend_fraction=.5).state == "buy"


def test_an_exactly_affordable_price_buys_when_no_reserve_is_set() -> None:
    """`wallet == price`: affordable, with nothing left over. The guard that
    matters is affordability, and `<` rather than `<=` is what makes the
    last coin spendable."""
    result = decision.decide(plan_of(candidate(price=300)), wallet=300)
    assert result.state == "buy"


def test_a_spend_fraction_of_none_zero_or_one_all_mean_no_reserve() -> None:
    """All three are legitimate ways to say "no limit" - `choose_next`'s
    guard only engages strictly between 0 and 1 - and none of them may turn
    an affordable purchase into a save."""
    for fraction in (None, 0., 1.):
        result = decision.decide(plan_of(candidate(price=300)), wallet=300,
                                 spend_fraction=fraction)
        assert result.state == "buy", fraction


def test_a_spend_fraction_outside_zero_to_one_raises_rather_than_lapsing() -> None:
    """The dangerous failure this guards: an operator writing `50` meaning
    "50%", or a negative left by a bad edit, would get NO reserve at all and
    no sign that the only limit on spending had switched itself off."""
    for fraction in (50., -.5, 1.5, math.nan):
        with pytest.raises(ValueError):
            decision.decide(plan_of(candidate()), wallet=1000,
                            spend_fraction=fraction)


# -- zeroes and sentinels ------------------------------------------------
def test_a_zero_wallet_saves_the_whole_price_rather_than_reading_as_unknown() -> None:
    """Zero is a reading; `None` is the absence of one. Collapsing them
    would either ask for a balance somebody just read, or invent one they
    never did."""
    result = decision.decide(plan_of(candidate(price=300)), wallet=0)
    assert result.state == "save_coins"
    assert result.shortfall == 300


def test_a_free_objective_is_affordable_on_an_empty_wallet() -> None:
    """Price 0 against wallet 0: nothing is owed, so nothing is saved for.
    A `<=` in the affordability test, or a reserve guard that fired on
    `0 > 0`, would leave the planner saving for something already free."""
    result = decision.decide(plan_of(candidate(price=0)), wallet=0,
                             spend_fraction=.5)
    assert result.state == "buy"


def test_a_negative_price_reads_as_unobserved_rather_than_as_a_refund() -> None:
    """This repository's own sentinel for an unreadable row - `choose_next`
    already receives one, `workshop_objectives._observed_price` already
    drops one. Arithmetic on it would report a negative shortfall, i.e. a
    purchase that pays the account."""
    result = decision.decide(plan_of(candidate(price=-1)), wallet=1000)
    assert result.state == "observe_price"
    assert result.price is None


def test_a_negative_wallet_reads_as_unobserved_rather_than_as_a_debt() -> None:
    result = decision.decide(plan_of(candidate(price=300)), wallet=-1)
    assert result.state == "observe_balance"
    assert result.wallet is None


def test_a_boolean_wallet_is_not_a_balance_of_one() -> None:
    """`True` is an `int` in Python, so an unguarded `isinstance` check
    would spend it as a coin - the same guard `builds._number` and
    `workshop_objectives._number` keep."""
    result = decision.decide(plan_of(candidate(price=300)), wallet=True)  # type: ignore[arg-type]
    assert result.state == "observe_balance"


# -- the sentence --------------------------------------------------------
def test_every_state_explains_itself_to_a_person() -> None:
    """The register of `reroll_planner._FOCUS`: what this means for the
    account, not which branch was taken. A log parser is not the reader."""
    plans = [
        (plan_of(candidate(price=None)), 1200, None),
        (plan_of(candidate()), None, None),
        (plan_of(candidate(price=300)), 100, None),
        (plan_of(candidate(price=300)), 1200, None),
        (plan_of(None), 1200, None),
    ]
    for plan, wallet, fraction in plans:
        result = decision.decide(plan, wallet=wallet, spend_fraction=fraction)
        assert result.reason.strip()
        assert result.reason.endswith(".")
        assert result.reason[0].isupper()
        assert "state" not in result.reason.lower()


def test_a_decision_on_a_propagated_chain_says_what_the_chain_leads_to() -> None:
    """The sentence this whole feature exists to produce. Without it the
    operator is told to buy the cheapest thing on the list with no hint of
    why it outranked the expensive one."""
    result = decision.decide(plan_of(candidate()), wallet=1200)
    assert "unlock defense upgrades" in result.reason
    assert "which leads to thorns" in result.reason
    assert result.path == CHAIN


def test_a_decision_on_a_leaf_claims_no_chain() -> None:
    """A one-element path means nothing depends on this objective; claiming
    it "leads to" itself would be a fabricated justification."""
    lonely = candidate(objective_id="workshop.health", path=("workshop.health",))
    result = decision.decide(plan_of(lonely), wallet=1200)
    assert "which leads to" not in result.reason


def test_a_large_price_is_written_out_rather_than_in_scientific_notation() -> None:
    """A late-game Workshop price runs to seven digits, and `f"{price:g}"`
    renders 1500000 as "1.5e+06" - a number an operator cannot count
    against a balance."""
    result = decision.decide(plan_of(candidate(price=1_500_000)), wallet=2_000_000)
    assert "1500000" in result.reason
    assert "e+" not in result.reason


def test_the_same_inputs_decide_the_same_way_every_time() -> None:
    """Pure: no clock, no I/O, no hidden state. A recommendation that
    changed between two identical calls could not be checked by anyone."""
    plan = plan_of(candidate())
    first = decision.decide(plan, wallet=1000, spend_fraction=.5)
    second = decision.decide(plan, wallet=1000, spend_fraction=.5)
    assert first == second


def test_a_decision_cannot_be_edited_after_it_is_made() -> None:
    result = decision.decide(plan_of(candidate()), wallet=1200)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = "buy"  # type: ignore[misc]


# -- the unmet precondition (finding 1) ----------------------------------
def untracked_gate_candidate(**overrides: object) -> director.Candidate:
    """`workshop.cash_bonus` as the turtle build really emits it.

    `builds.prerequisites()` says `cash_bonus -> unlock_cash_bonuses`, but
    turtle weights no objective for that unlock (the opening build buys it a
    stage earlier), so `workshop_objectives` drops the edge from `requires`
    - a dangling id would pin the objective to `blocked` forever through
    `classify()`'s subset test - and states it in the action's
    `precondition` instead. Nothing in the graph blocks on it.
    """
    gate = ("unlock_cash_bonuses must already be owned - this build does not "
            "weight it, so no objective in this graph tracks it and nothing "
            "here blocks workshop.cash_bonus on it")
    fields: dict[str, object] = {
        "objective_id": "workshop.cash_bonus", "price": 500,
        "path": ("workshop.cash_bonus",), "preconditions": (gate,)}
    fields.update(overrides)
    return candidate(**fields)


def test_an_untracked_precondition_is_named_in_the_sentence_but_does_not_block() -> None:
    """The decision this phase had to make, pinned so it cannot change by
    accident.

    Reaching `buy` at all requires an OBSERVED price, and a price is read
    off the tab's row for that upgrade - the same proof
    `shopping.py:_already_unlocked` relies on ("a granted row cannot appear
    on the tab until its unlock is bought, so seeing one is proof rather
    than an inference"). So an unmet unlock and an observed price are very
    nearly mutually exclusive, and refusing here would replace a
    self-correcting loop with a dead end. The gate is therefore SURFACED -
    verbatim, in the reason and on the Decision - and never branched on: the
    only way to tell it from a precondition `requires` already enforces is
    to parse prose another module wrote.
    """
    result = decision.decide(plan_of(untracked_gate_candidate()), wallet=1200)
    assert result.state == "buy"
    assert "unlock_cash_bonuses must already be owned" in result.reason
    assert result.preconditions and "unlock_cash_bonuses" in result.preconditions[0]


def test_the_same_gated_objective_with_no_price_asks_someone_to_look() -> None:
    """The other half of the reason above: when the price is missing - which
    is the state an account that has not bought the unlock is actually in,
    because the row is not on the tab to read - the answer is already "go
    look at the Workshop", which is exactly the step that settles the gate.
    """
    result = decision.decide(plan_of(untracked_gate_candidate(price=None)), wallet=1200)
    assert result.state == "observe_price"


def test_a_save_also_names_the_gate_it_is_saving_towards() -> None:
    """Saving for a purchase that turns out to be impossible is the same
    wasted wait as making it, so the gate is said on both spending states."""
    result = decision.decide(plan_of(untracked_gate_candidate()), wallet=100)
    assert result.state == "save_coins"
    assert "unlock_cash_bonuses" in result.reason


# -- what propagation still does not do ----------------------------------
def test_a_blocked_objective_is_never_bought_however_much_value_it_inherits() -> None:
    """Propagation fixes the numerator, not the gate. `workshop.thorns` is
    the turtle build's goal and it is blocked on its unlock; a decision
    layer that ranked on effective value itself - rather than taking
    `plan.top`, which is `ranked[0] if ready and unheld` - would happily
    recommend buying a row the game will not show, forever."""
    plan = turtle_plan({"thorns": 100, "unlock_defense_upgrades": 300})
    blocked = {c.objective_id for c in plan.candidates if c.status == "blocked"}
    assert "workshop.thorns" in blocked

    result = decision.decide(plan, wallet=100_000)
    assert result.objective_id not in blocked
    assert result.state == "buy"


def test_a_generated_workshop_objective_is_not_held_for_its_builds_provenance() -> None:
    """Finding 2, pinned as it stands today rather than left to be
    rediscovered.

    `workshop_objectives` emits an empty `knowledge_refs` honestly - a
    build's provenance is `builds.v1.json`'s `build_sources`, not a wiki
    fact - so `director._held_reasons`' "unverified citation behind a real
    spend" hold cannot see that every build in the committed pack has
    `rule_verified=False`. Wiring that in from `director` would hold EVERY
    priced Workshop objective (no build is verified), leaving `top` as
    `None` and this module answering `needs_operator` forever - recommending
    nothing at all, which is what `objectives.py` declined to do when it
    ruled that knowledge informs graph shape, not predicate truth. The fix
    belongs on the graph, where the objective can carry its own provenance;
    this test is what will fail, loudly, on the day it lands.
    """
    build = builds.by_id("turtle")
    assert build is not None and build.rule_verified is False

    plan = turtle_plan({"unlock_defense_upgrades": 300})
    priced = [c for c in plan.candidates if c.price is not None]
    assert priced and all(not c.held_by for c in priced)
    assert all(c.knowledge_refs == () for c in plan.candidates)
    assert decision.decide(plan, wallet=1200).state == "buy"


# -- end to end, against the committed pack ------------------------------
def test_the_committed_turtle_build_decides_to_buy_the_unlock_that_starts_the_chain() -> None:
    """The point of this phase, run for real from committed modules:
    `builds.by_id("turtle")` -> `workshop_objectives` -> `director.plan` ->
    `decide`, with nothing synthetic in between.

    On an account nobody has read, the turtle build's own goal - thorns, to
    51 - is blocked behind two unlocks, and Defense Absolute (weight 16)
    outranks Unlock Defense Upgrades (weight 10) on declared value alone. So
    before propagation the planner pointed at a blocked row or at nothing,
    and the chain was never started. Here it points at the unlock, says what
    the unlock leads to, and spends 300 of the 1200 coins the operator read.
    """
    plan = turtle_plan({"unlock_defense_upgrades": 300})
    result = decision.decide(plan, wallet=1200, spend_fraction=.5)

    assert result.state == "buy"
    assert result.objective_id == "workshop.unlock_defense_upgrades"
    assert result.price == 300
    assert result.currency == "coins"
    assert result.path == ("workshop.unlock_defense_upgrades",
                           "workshop.unlock_thorns", "workshop.thorns")
    assert "unlock defense upgrades" in result.reason
    assert "which leads to thorns" in result.reason


def test_the_same_build_with_nothing_priced_sends_someone_to_the_workshop_tab() -> None:
    """The same pipeline, one input removed: no observed price anywhere.
    Nothing is estimated, nothing is carried over from the call above, and
    the answer names the row to go and read - which is the correct first
    move on an account whose Workshop has never been scanned."""
    result = decision.decide(turtle_plan({}), wallet=1200, spend_fraction=.5)

    assert result.state == "observe_price"
    assert result.objective_id == "workshop.unlock_defense_upgrades"
    assert result.price is None
    assert "Workshop price" in result.reason


def test_the_pipeline_still_decides_when_the_account_has_been_read() -> None:
    """A revision with real Workshop readings moves the decision on: the
    first unlock is proven bought (its granted row has a displayed value),
    so the plan advances to the next link in the same chain rather than
    re-recommending what the account already owns."""
    read = revision(defense_absolute=20., health=5.)
    plan = turtle_plan({"unlock_thorns": 750}, read)
    result = decision.decide(plan, wallet=800, spend_fraction=.5)

    assert result.objective_id == "workshop.unlock_thorns"
    assert result.state == "save_coins"
    # 750 is affordable out of 800, but not within half of it.
    assert result.shortfall == math.ceil(750 / .5) - 800


# -- purity --------------------------------------------------------------
def test_decision_module_imports_nothing_that_touches_a_device() -> None:
    """Enforced, not trusted: an allowlist over decision.py's own import
    statements (not its transitive closure - `import director` still pulls
    `cv2`/`numpy` in through `account_state`, and nothing in that chain
    performs I/O at import time). `fleet` is named in the forbidden set
    explicitly: `fleet/reroll_planner.py` is the behaviour this module
    generalises, and importing it would re-couple a general decision layer
    to one account's reroll loop - the exact coupling this phase removes."""
    import ast
    from pathlib import Path

    source = Path(decision.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "math", "dataclasses", "typing", "director"}
    assert imported <= allowed, imported - allowed
    forbidden = {"fleet", "device", "shopping", "transactions", "time", "os",
                 "socket", "ocr", "screen_discovery", "db", "tower_bot"}
    assert not (imported & forbidden), imported & forbidden


def test_the_decision_arms_nothing() -> None:
    """`decide` is a recommendation with its reasoning attached, exactly as
    `plan()` is. Nothing in its source may reach for a tap, a device or a
    transaction - checked as a substring scan over the module's own text,
    the same way `test_director.py` pins `plan()`."""
    import inspect

    source = inspect.getsource(decision.decide)
    for forbidden in ("tap(", "swipe(", "device", "adb", "screenshot", "commit"):
        assert forbidden not in source, forbidden


def test_the_objectives_a_build_generates_are_the_only_graph_this_needs() -> None:
    """A sanity check on the seam, not on either side of it: the generated
    graph is an ordinary `objectives.Objective` tuple, so `director.plan`
    ranks it with no special case and `decide` reads the result with no
    knowledge of where it came from."""
    build = builds.by_id("turtle")
    assert build is not None
    graph = workshop_objectives.workshop_objectives(build, AccountRevision(), prices={})
    assert all(isinstance(o, objectives.Objective) for o in graph)
    assert {c.objective_id for c in turtle_plan({}).candidates} == {o.id for o in graph}
