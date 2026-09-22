"""A build's weight list, turned into objectives the director can rank.

Three things these tests exist to protect, in the order they would hurt:

  - An unread level is UNKNOWN, never zero. `workshop_stats` is the only
    section a live account populates, and a row nobody has scanned must
    answer None - which blocks - rather than False, which would make the
    whole build look actionable against a screen the bot has never read.
  - An unobserved price produces NO cost param, so
    `director._price_and_currency` answers `(None, None)` and the decision
    layer says "go read the tab". The price of Defense Absolute level 21 is
    not the price of level 20, so there is no constant to fall back on and
    guessing one would rank a spend nobody ever priced.
  - One objective per MILESTONE, never one per level. A build that targets
    thorns at 51 must produce one objective, not fifty-one.
"""
from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

import builds
import director
import objectives
import upgrades
import workshop_objectives
from account_state import AccountRevision, Evidence, Fact


def fact(concept_id: str, value: object) -> Fact:
    return Fact(concept_id, value, "verified", Evidence(
        1., .99, concept_id, str(value), (0, 0, 1, 1), 1080, 2400, "abc"))


def revision(**stats: object) -> AccountRevision:
    """A revision whose ONLY populated section is workshop_stats.

    Keyword names are `upgrades.CATALOG` ids; they are bridged to the
    `stats.*` concept ids a real reading carries through the same
    `Upgrade.concept_id` property `concepts.py` validates the catalog
    against, so a test cannot quietly key a fact on an id no reader writes.
    """
    return dataclasses.replace(AccountRevision(), workshop_stats=tuple(
        fact(upgrades.by_id(upgrade_id).concept_id, value)
        for upgrade_id, value in stats.items()))


def turtle() -> builds.Build:
    build = builds.by_id("turtle")
    assert build is not None
    return build


def synthetic_build(weights: tuple[tuple[str, float], ...],
                    targets: dict[str, float] | None = None) -> builds.Build:
    """A Build the committed pack does not contain, for a case it does not cover."""
    return builds.Build(
        id="synthetic", name="Synthetic", note="test fixture",
        weights=weights, targets=MappingProxyType(dict(targets or {})),
        focus=MappingProxyType({}), source_refs=("legacy-reroll-planner",),
        source_url=None,
        validity=builds.Validity("unknown", None, None),
        definition_verified=True, rule_verified=False)


def by_id(graph: tuple[objectives.Objective, ...], objective_id: str) -> objectives.Objective:
    return next(o for o in graph if o.id == objective_id)


def shape(graph: tuple[objectives.Objective, ...]) -> tuple[tuple[object, ...], ...]:
    """Everything an objective SAYS, with the closure left out.

    `satisfied_by` is a fresh lambda on every call, so two runs produce equal
    fields and unequal objects; comparing the objects would test Python's
    identity semantics instead of this module's determinism.
    """
    return tuple((o.id, o.requires, o.grants, o.value, o.risk, o.knowledge_refs,
                  o.concept_ids, o.never_satisfiable, o.actions) for o in graph)


# -- the chain -----------------------------------------------------------
def test_the_turtle_build_produces_the_real_three_node_thorns_chain() -> None:
    """Thorns needs Unlock Thorns needs Unlock Defense Upgrades - game
    structure from builds.prerequisites(), translated to objective ids so
    classify() can walk it rather than raw upgrade ids it would never match."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    assert by_id(graph, "workshop.thorns").requires == ("workshop.unlock_thorns",)
    assert by_id(graph, "workshop.unlock_thorns").requires == (
        "workshop.unlock_defense_upgrades",)
    assert by_id(graph, "workshop.unlock_defense_upgrades").requires == ()


def test_classify_walks_the_thorns_chain_to_done_ready_and_blocked() -> None:
    """One reading of Defense Absolute proves Unlock Defense Upgrades was
    bought (the row could not be on the tab otherwise), which readies Unlock
    Thorns - and Thorns stays blocked behind it, because nothing has proven
    that second unlock yet."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision(defense_absolute=12.))

    statuses = objectives.classify(graph, revision(defense_absolute=12.))

    assert statuses["workshop.unlock_defense_upgrades"] == "done"
    assert statuses["workshop.unlock_thorns"] == "ready"
    assert statuses["workshop.thorns"] == "blocked"


def test_every_requirement_resolves_to_an_objective_in_the_same_graph() -> None:
    """A requirement pointing at an id no objective carries is not an error
    anywhere - classify() does a subset test - it just pins the objective to
    `blocked` forever. Exactly the silent no-op builds.py warns about."""
    for build in builds.REGISTRY.builds:
        graph = workshop_objectives.workshop_objectives(build, revision())
        ids = {o.id for o in graph}
        for objective in graph:
            assert set(objective.requires) <= ids, objective.id


def test_an_untracked_prerequisite_is_named_in_the_precondition() -> None:
    """Turtle weights cash_bonus but not unlock_cash_bonuses (the opening
    build buys that, a stage earlier), so there is no objective for the edge
    to point at. The gate is real, so it is written where a human reading the
    plan sees it instead of vanishing."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    cash_bonus = by_id(graph, "workshop.cash_bonus")

    assert cash_bonus.requires == ()
    assert "unlock_cash_bonuses" in (cash_bonus.actions[0].precondition or "")


# -- the three-valued predicate ------------------------------------------
def test_an_unread_level_is_unknown_rather_than_zero() -> None:
    """The load-bearing one. A build targets thorns at 51; an account nobody
    has read must not answer "0, so not yet" - it must answer "I do not know"."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    assert by_id(graph, "workshop.thorns").satisfied_by(revision()) is None


def test_an_unread_level_blocks_everything_downstream_of_it() -> None:
    """None must BLOCK, which is the whole reason it is not False: a False
    would have readied Thorns against an unlock nobody has ever observed."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    statuses = objectives.classify(graph, revision())

    assert statuses["workshop.thorns"] == "blocked"
    assert statuses["workshop.unlock_thorns"] == "blocked"
    # The root of the chain is still "ready": with nothing outstanding to
    # wait on, "ready" claims only that nothing blocks attempting it - the
    # same vacuous reading objectives.classify() documents for a root.
    assert statuses["workshop.unlock_defense_upgrades"] == "ready"


def test_an_unreadable_fact_value_is_unknown_rather_than_zero() -> None:
    """A Fact whose value is not a finite number is a reading that failed,
    not a level of zero."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())
    unreadable = dataclasses.replace(AccountRevision(), workshop_stats=(
        fact("stats.thorns", None), fact("stats.defense_absolute", "12")))

    assert by_id(graph, "workshop.thorns").satisfied_by(unreadable) is None
    assert by_id(graph, "workshop.unlock_defense_upgrades").satisfied_by(unreadable) is None


def test_a_boolean_fact_value_is_not_read_as_a_level_of_one() -> None:
    """bool subclasses int in Python, so an unguarded isinstance check would
    turn a True somebody wrote into a displayed value of 1.0."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())
    flagged = dataclasses.replace(AccountRevision(),
                                  workshop_stats=(fact("stats.thorns", True),))

    assert by_id(graph, "workshop.thorns").satisfied_by(flagged) is None


# -- targets --------------------------------------------------------------
def test_a_level_at_the_build_target_is_done() -> None:
    """Turtle stops buying Thorns at 51; exactly 51 is reached, not short."""
    at_target = revision(thorns=51.)
    graph = workshop_objectives.workshop_objectives(turtle(), at_target)

    assert by_id(graph, "workshop.thorns").satisfied_by(at_target) is True
    assert objectives.classify(graph, at_target)["workshop.thorns"] == "done"


def test_a_level_above_the_build_target_is_done() -> None:
    above = revision(thorns=60.)
    graph = workshop_objectives.workshop_objectives(turtle(), above)

    assert objectives.classify(graph, above)["workshop.thorns"] == "done"


def test_a_level_below_the_build_target_is_ready_once_its_chain_is_proven() -> None:
    """Below target is provably not done - False, not None - and the chain
    above it is proven by the same reading, so it is ready to attempt."""
    below = revision(thorns=10.)
    graph = workshop_objectives.workshop_objectives(turtle(), below)

    assert by_id(graph, "workshop.thorns").satisfied_by(below) is False
    assert objectives.classify(graph, below)["workshop.thorns"] == "ready"


def test_a_target_is_compared_against_the_displayed_value_not_a_level() -> None:
    """builds.Build.targets is a displayed value (51 thorn damage), and
    workshop_stats records displayed values. A target read as a level would
    never fire against the numbers a real reading carries."""
    assert turtle().targets["thorns"] == 51.
    graph = workshop_objectives.workshop_objectives(turtle(), revision(thorns=50.9))

    assert by_id(graph, "workshop.thorns").satisfied_by(revision(thorns=50.9)) is False
    assert by_id(graph, "workshop.thorns").satisfied_by(revision(thorns=51.)) is True


def test_a_downward_improving_target_is_a_floor_rather_than_a_ceiling() -> None:
    """Shockwave Frequency is an interval: it improves DOWNWARD. Direction
    comes from upgrades.target_reached, so a hand-written `>=` here cannot
    mark it done only once it has got worse."""
    build = synthetic_build((("shockwave_frequency", 5.),), {"shockwave_frequency": 3.})
    reached = revision(shockwave_frequency=2.)
    graph = workshop_objectives.workshop_objectives(build, reached)

    assert by_id(graph, "workshop.shockwave_frequency").satisfied_by(reached) is True
    assert by_id(graph, "workshop.shockwave_frequency").satisfied_by(
        revision(shockwave_frequency=4.)) is False


# -- untargeted: "advance this once more" --------------------------------
def test_an_untargeted_upgrade_asks_for_one_more_purchase() -> None:
    """The open design question, pinned. Health has no target and no end
    state, so its milestone is relative: past where it stood when the plan
    was made. False at the moment of generation - nothing bought yet - and
    True as soon as a later reading is higher."""
    anchor = revision(health=30.)
    graph = workshop_objectives.workshop_objectives(turtle(), anchor)
    health = by_id(graph, "workshop.health")

    assert health.satisfied_by(anchor) is False
    assert health.satisfied_by(revision(health=30.)) is False
    assert health.satisfied_by(revision(health=31.)) is True


def test_an_untargeted_upgrade_with_no_baseline_declares_itself_never_satisfiable() -> None:
    """"Further along than an unknown number" has no true answer from ANY
    revision, so the objective says so on itself rather than leaving a
    permanently-None predicate to look like work in progress."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())
    health = by_id(graph, "workshop.health")

    assert health.never_satisfiable is True
    assert health.satisfied_by(revision(health=30.)) is None


def test_an_untargeted_upgrade_with_a_baseline_is_satisfiable() -> None:
    graph = workshop_objectives.workshop_objectives(turtle(), revision(health=30.))

    assert by_id(graph, "workshop.health").never_satisfiable is False


def test_a_downward_improving_upgrade_advances_when_its_value_falls() -> None:
    """One more purchase of Shockwave Frequency LOWERS the number. Advance is
    expressed through upgrades.target_reached for exactly this row."""
    build = synthetic_build((("shockwave_frequency", 5.),))
    anchor = revision(shockwave_frequency=10.)
    advanced = by_id(workshop_objectives.workshop_objectives(build, anchor),
                     "workshop.shockwave_frequency")

    assert advanced.satisfied_by(revision(shockwave_frequency=8.)) is True
    assert advanced.satisfied_by(revision(shockwave_frequency=12.)) is False


# -- unlocks --------------------------------------------------------------
def test_an_unlock_upgrade_emits_exactly_one_objective() -> None:
    """A tile bought once has no ladder to climb."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision(thorns=10.))

    unlocks = [o for o in graph if o.id == "workshop.unlock_thorns"]

    assert len(unlocks) == 1


def test_an_unlock_is_proven_by_a_row_it_grants() -> None:
    """shopping.py's own proof: a granted row cannot appear on the tab until
    its unlock is bought, so seeing Thorns settles Unlock Thorns."""
    owned = revision(thorns=10.)
    graph = workshop_objectives.workshop_objectives(turtle(), owned)

    assert by_id(graph, "workshop.unlock_thorns").satisfied_by(owned) is True


def test_a_missing_granted_row_is_unknown_rather_than_not_unlocked() -> None:
    """The panel shows a handful of rows at a time; the other reading of a
    missing row is "OCR lost it". Never False."""
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    for rev in (revision(), revision(health=30.)):
        assert by_id(graph, "workshop.unlock_thorns").satisfied_by(rev) is None


def test_an_unlock_carries_the_concept_ids_of_the_rows_that_prove_it() -> None:
    graph = workshop_objectives.workshop_objectives(turtle(), revision())

    assert by_id(graph, "workshop.unlock_thorns").concept_ids == (
        "unlocks.thorns", "stats.thorns")


# -- price ----------------------------------------------------------------
def test_an_observed_price_lands_where_the_director_finds_it() -> None:
    """Asserted by calling director._price_and_currency, not by reading the
    params by eye: the param KEY is the contract, and a typo'd one does not
    raise, it silently makes a priced objective look unpriced."""
    graph = workshop_objectives.workshop_objectives(
        turtle(), revision(thorns=10.), prices={"thorns": 4200})

    assert director._price_and_currency(by_id(graph, "workshop.thorns")) == (4200, "coins")


def test_an_unobserved_price_emits_no_cost_param_at_all() -> None:
    """The absence is the signal: (None, None) is what makes the decision
    layer answer observe_price instead of ranking a spend nobody priced."""
    graph = workshop_objectives.workshop_objectives(
        turtle(), revision(thorns=10.), prices={"thorns": 4200})

    unpriced = by_id(graph, "workshop.defense_absolute")

    assert all(key != workshop_objectives.COIN_COST_PARAM
               for key, _ in unpriced.actions[0].params)
    assert director._price_and_currency(unpriced) == (None, None)


def test_no_prices_at_all_prices_nothing() -> None:
    graph = workshop_objectives.workshop_objectives(turtle(), revision(thorns=10.))

    for objective in graph:
        assert director._price_and_currency(objective) == (None, None)


def test_a_price_is_never_carried_over_from_another_upgrade() -> None:
    """Nothing is estimated, interpolated or borrowed - a build where one row
    is priced leaves every other row unpriced."""
    graph = workshop_objectives.workshop_objectives(
        turtle(), revision(thorns=10.), prices={"thorns": 4200})

    priced = [o.id for o in graph if director._price_and_currency(o)[0] is not None]

    assert priced == ["workshop.thorns"]


@pytest.mark.parametrize("price", [-1, True, 12.5, "4200", None])
def test_a_price_that_is_not_a_readable_coin_count_is_treated_as_unobserved(
        price: object) -> None:
    """A negative price is fleet/reroll_planner.py's own "unreadable"
    sentinel; a bool, a float or a string is a caller mistake. Every one of
    them is safer as "go read the tab" than as a number handed to the ranker."""
    graph = workshop_objectives.workshop_objectives(
        turtle(), revision(thorns=10.), prices={"thorns": price})  # type: ignore[dict-item]

    assert director._price_and_currency(by_id(graph, "workshop.thorns")) == (None, None)


def test_a_free_upgrade_keeps_its_observed_price_of_zero() -> None:
    """Zero is an observation, not an absence."""
    graph = workshop_objectives.workshop_objectives(
        turtle(), revision(thorns=10.), prices={"thorns": 0})

    assert director._price_and_currency(by_id(graph, "workshop.thorns")) == (0, "coins")


def test_the_cost_param_key_is_the_one_the_director_maps_to_coins() -> None:
    assert director._COST_PARAM_CURRENCIES[workshop_objectives.COIN_COST_PARAM] == "coins"


# -- shape, order, determinism -------------------------------------------
def test_one_objective_per_weighted_upgrade_and_never_one_per_level() -> None:
    """A target of 51 is one objective, not fifty-one."""
    for build in builds.REGISTRY.builds:
        graph = workshop_objectives.workshop_objectives(build, revision(thorns=1.))
        assert len(graph) == len(build.weights)
        assert len({o.id for o in graph}) == len(graph)


def test_the_output_is_ordered_by_the_builds_weight_order() -> None:
    """Position is load-bearing: builds.Build.weights is a tuple of pairs
    rather than a mapping because it breaks ties between equal effective
    weights, and re-sorting this tuple would throw that tiebreak away."""
    build = turtle()
    graph = workshop_objectives.workshop_objectives(build, revision(thorns=10.))

    assert [o.id for o in graph] == [f"workshop.{i}" for i in build.upgrade_ids]


def test_the_same_inputs_produce_the_same_objectives_every_time() -> None:
    build, rev, prices = turtle(), revision(thorns=10., health=30.), {"thorns": 4200}

    first = workshop_objectives.workshop_objectives(build, rev, prices=prices)
    second = workshop_objectives.workshop_objectives(build, rev, prices=prices)

    assert shape(first) == shape(second)


def test_the_predicates_are_deterministic_too() -> None:
    build, rev = turtle(), revision(thorns=10., health=30.)
    graph = workshop_objectives.workshop_objectives(build, rev)

    for objective in graph:
        assert objective.satisfied_by(rev) is objective.satisfied_by(rev)


def test_reading_the_revision_does_not_change_it() -> None:
    build, rev = turtle(), revision(thorns=10.)
    before = dataclasses.asdict(rev)

    workshop_objectives.workshop_objectives(build, rev, prices={"thorns": 1})

    assert dataclasses.asdict(rev) == before


def test_every_objective_names_what_to_buy_and_who_would_buy_it() -> None:
    graph = workshop_objectives.workshop_objectives(turtle(), revision(thorns=10.))

    for objective, (upgrade_id, _) in zip(graph, turtle().weights):
        action, = objective.actions
        assert action.executor == workshop_objectives.EXECUTOR
        assert action.params[0] == ("upgrade", upgrade_id)
        assert action.postcondition


def test_value_is_the_builds_own_weight() -> None:
    build = turtle()
    graph = workshop_objectives.workshop_objectives(build, revision(thorns=10.))

    for objective in graph:
        upgrade_id = objective.id.removeprefix(workshop_objectives.ID_PREFIX)
        assert objective.value == build.weight_of(upgrade_id)


def test_every_concept_id_resolves_in_the_catalog() -> None:
    """A typo'd concept id does not raise - the lookup returns None and the
    predicate reads nothing, forever, looking exactly like an objective
    nobody has finished."""
    import concepts

    for build in builds.REGISTRY.builds:
        for objective in workshop_objectives.workshop_objectives(build, revision()):
            for concept_id in objective.concept_ids:
                assert concepts.REGISTRY.by_id(concept_id) is not None, concept_id


def test_a_workshop_objective_rests_on_no_wiki_citation() -> None:
    """A build's provenance is builds.v1.json's build_sources, a different
    corpus. Citing an unrelated wiki fact to fill the field would hand
    director._held_reasons a footnote the objective does not rest on."""
    for objective in workshop_objectives.workshop_objectives(turtle(), revision()):
        assert objective.knowledge_refs == ()


# -- purity ---------------------------------------------------------------
def test_workshop_objectives_module_imports_nothing_that_touches_a_device() -> None:
    """Enforced, not trusted: an allowlist over this module's own import
    statements (not its transitive closure), the same discipline
    objectives.py, director.py and affordability_horizon.py apply to
    themselves. `fleet` is forbidden outright - the weights came from there
    and the coupling did not."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(workshop_objectives.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "math", "typing", "builds", "objectives", "upgrades",
               "account_state"}
    assert imported <= allowed, imported - allowed
    forbidden = {"fleet", "device", "shopping", "transactions", "time", "os",
                 "socket", "ocr", "screen_discovery", "db", "tower_bot", "random"}
    assert not (imported & forbidden), imported & forbidden
