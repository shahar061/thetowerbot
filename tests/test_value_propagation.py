"""Value pushed backward along `requires`, so an enabler is worth what it enables.

The defect these tests pin: `director.score` ranks on `Objective.value`,
which is per-objective and static, and `director.plan` can only ever pick a
candidate that is already ready - so for "unlock A -> unlock B -> buy C", C
is blocked and unpickable while A and B are ranked on their own small
numbers. The chain is never started. `effective_values` credits A with a
discounted share of the best thing downstream of it, and `chain_to` renders
which thing that was, so the recommendation stays checkable by a human
instead of being a number that silently went up.

Two properties get their own tests here because losing either one
reintroduces a real failure rather than a wrong number: `max` (not `sum`)
over dependents, which is what keeps a hub unlock from permanently
dominating and what keeps the explanation to one readable line, and
termination on a cyclic graph, which a `requires` typo in a generated build
file is enough to produce.
"""
from __future__ import annotations

import pytest

import objectives
import value_propagation
from fleet.reroll_planner import _PREREQUISITES, _TURTLE
from value_propagation import DEFAULT_DISCOUNT, chain_to, effective_values


def _objective(oid: str, value: float, requires: tuple[str, ...] = ()) -> objectives.Objective:
    """A minimal graph node. `satisfied_by` is never called by this module -
    propagation is pure graph arithmetic over ids, values and `requires`, and
    reads no account revision at all - so a constant predicate is honest
    here rather than a stub standing in for something."""
    return objectives.Objective(
        id=oid,
        requires=requires,
        grants=(),
        satisfied_by=lambda revision: True,
        actions=(),
        value=value,
        risk="reversible",
        knowledge_refs=(),
    )


# -- the base cases --------------------------------------------------------

def test_an_empty_graph_has_no_effective_values() -> None:
    assert effective_values(()) == {}


def test_a_leaf_keeps_its_own_value() -> None:
    """Nothing depends on it, so it inherits nothing. This is the existing
    behaviour of every objective in the graph today, which is why turning
    propagation on is a strict extension rather than a re-scaling."""
    graph = (_objective("solo", 4.),)

    assert effective_values(graph) == {"solo": 4.}


def test_a_standalone_objective_is_its_own_whole_chain() -> None:
    assert chain_to((_objective("solo", 4.),), "solo") == ("solo",)


def test_an_objective_that_is_not_in_the_graph_still_answers() -> None:
    """A caller holding one build's slice of the graph gets a degraded
    explanation, never a KeyError raised in the middle of rendering a plan."""
    assert chain_to((_objective("solo", 4.),), "elsewhere") == ("elsewhere",)


# -- propagation itself ----------------------------------------------------

def test_a_three_link_chain_compounds_the_discount_at_each_hop() -> None:
    """The motivating shape: goal `c` is worth 10 and blocked, enablers `a`
    and `b` are worth 1 each and ready. At 0.5, `b` inherits 5 and `a`
    inherits half of `b`'s total - 3.5 - so the cheap first step is ranked on
    what it leads to instead of on its own 1."""
    graph = (
        _objective("a", 1.),
        _objective("b", 1., requires=("a",)),
        _objective("c", 10., requires=("b",)),
    )

    values = effective_values(graph, discount=.5)

    assert values["c"] == 10.
    assert values["b"] == 1. + .5 * 10.
    assert values["a"] == 1. + .5 * 6.


def test_the_enabler_outranks_the_goal_it_leads_to_only_through_the_discount() -> None:
    """Sanity on the direction of the credit: the head of a chain never
    inherits MORE than the prize itself unless its own value earns it."""
    graph = (
        _objective("enabler", 1.),
        _objective("goal", 10., requires=("enabler",)),
    )

    values = effective_values(graph)

    assert values["enabler"] < values["goal"]


def test_a_zero_discount_disables_propagation_entirely() -> None:
    """The off switch, pinned so it cannot quietly stop being off: at 0.0
    every objective is worth exactly what it declares, which is precisely
    today's `Objective.value` ranking."""
    graph = (
        _objective("a", 1.),
        _objective("b", 2., requires=("a",)),
        _objective("c", 99., requires=("b",)),
    )

    assert effective_values(graph, discount=0.) == {"a": 1., "b": 2., "c": 99.}


@pytest.mark.parametrize("discount", [-.1, 1.5, float("inf"), float("nan")])
def test_a_discount_outside_zero_to_one_is_refused(discount: float) -> None:
    """There is no honest value to return for a nonsensical discount - a
    negative one makes unlocking something a penalty - and clamping silently
    would reorder every plan while looking like it worked."""
    graph = (_objective("a", 1.),)

    with pytest.raises(ValueError):
        effective_values(graph, discount=discount)
    with pytest.raises(ValueError):
        chain_to(graph, "a", discount=discount)


# -- max, not sum ----------------------------------------------------------

def test_two_dependents_credit_only_the_better_one() -> None:
    """`sum` would let a hub that gates several small things permanently
    outrank a chain that gates one large thing, and would produce an
    explanation ("prerequisite of six objectives worth 41 between them") that
    nobody can check. The hub here inherits from the 8, not from 8+6+5."""
    graph = (
        _objective("hub", 1.),
        _objective("small", 5., requires=("hub",)),
        _objective("medium", 6., requires=("hub",)),
        _objective("best", 8., requires=("hub",)),
    )

    values = effective_values(graph, discount=.5)

    assert values["hub"] == 1. + .5 * 8.
    assert values["hub"] < 1. + .5 * (5. + 6. + 8.)


def test_a_hub_of_many_small_things_loses_to_a_chain_to_one_big_thing() -> None:
    """The ranking consequence of max-not-sum, stated as the comparison a
    human would actually make: five cheap unlocks are not collectively worth
    more than the one objective the account is actually trying to reach."""
    hub = (_objective("hub", 1.),) + tuple(
        _objective(f"hub.dep.{i}", 4., requires=("hub",)) for i in range(5)
    )
    chain = (
        _objective("chain", 1.),
        _objective("chain.goal", 20., requires=("chain",)),
    )

    values = effective_values(hub + chain)

    assert values["chain"] > values["hub"]


# -- diamonds --------------------------------------------------------------

def test_a_diamond_is_valued_through_its_better_arm() -> None:
    """`top` requires both arms; both arms require `root`. `root` inherits
    from the better arm, and each arm inherits the shared tail once."""
    graph = (
        _objective("root", 1.),
        _objective("left", 2., requires=("root",)),
        _objective("right", 3., requires=("root",)),
        _objective("top", 10., requires=("left", "right")),
    )

    values = effective_values(graph, discount=.5)

    assert values["top"] == 10.
    assert values["left"] == 2. + .5 * 10.
    assert values["right"] == 3. + .5 * 10.
    assert values["root"] == 1. + .5 * 8.
    assert chain_to(graph, "root", discount=.5) == ("root", "right", "top")


def test_a_diamond_node_is_solved_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memoization, counted rather than timed: twenty stacked diamonds are
    2**20 distinct paths from the bottom node to the top one, so a
    re-deriving implementation would finalise the shared tail a million times
    over and a ranking function called on every dashboard refresh would stop
    being usable. Each node must be finalised exactly once.

    Counting the `_Propagation` constructions - the one call `_solve` makes
    per finished node - is deterministic under load, unlike a wall-clock
    budget, and fails rather than hanging if memoization regresses.
    """
    real = value_propagation._Propagation
    finalised: list[str] = []

    def counting(**kwargs: object) -> object:
        finalised.append("node")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(value_propagation, "_Propagation", counting)

    levels = 20
    graph: list[objectives.Objective] = [_objective("d0", 1.)]
    for level in range(levels):
        graph.append(_objective(f"l{level}", 1., requires=(f"d{level}",)))
        graph.append(_objective(f"r{level}", 1., requires=(f"d{level}",)))
        graph.append(_objective(f"d{level + 1}", 1., requires=(f"l{level}", f"r{level}")))

    values = effective_values(tuple(graph))

    assert len(finalised) == len(graph)
    assert len(values) == len(graph)


# -- cycles ----------------------------------------------------------------

def test_a_cycle_terminates_and_gives_every_node_a_finite_value() -> None:
    """A single typo'd `requires` id in a generated build file is enough to
    make a -> b -> a. The rule: an edge back into a node already on the
    current traversal path contributes no further propagation, so the cycle
    costs exactly that one edge's credit and nothing else - no recursion
    limit, no hang, and a plan that is slightly under-valued on one arm
    rather than no plan at all."""
    graph = (
        _objective("a", 1., requires=("b",)),
        _objective("b", 2., requires=("a",)),
    )

    values = effective_values(graph, discount=.5)

    # `a` is reached first (sorted order), so `b`'s edge back into it is the
    # one dropped: b keeps its own 2, and a inherits half of that.
    assert values == {"a": 1. + .5 * 2., "b": 2.}


def test_a_cycle_does_not_make_chain_to_loop() -> None:
    graph = (
        _objective("a", 1., requires=("b",)),
        _objective("b", 2., requires=("a",)),
    )

    assert chain_to(graph, "a", discount=.5) == ("a", "b")
    assert chain_to(graph, "b", discount=.5) == ("b",)


def test_a_cycle_hanging_off_a_real_chain_still_values_the_chain() -> None:
    """The cycle is contained: everything reachable outside it is still
    credited normally, so one bad edge does not flatten the rest of the
    graph's ranking."""
    graph = (
        _objective("enabler", 1.),
        _objective("goal", 10., requires=("enabler",)),
        _objective("loop.x", 1., requires=("enabler", "loop.y")),
        _objective("loop.y", 1., requires=("loop.x",)),
    )

    values = effective_values(graph, discount=.5)

    assert values["enabler"] == 1. + .5 * 10.
    assert chain_to(graph, "enabler", discount=.5) == ("enabler", "goal")
    assert all(value == value and value < float("inf") for value in values.values())


def test_a_self_requiring_objective_is_its_own_cut_edge() -> None:
    """The degenerate cycle - an objective listing its own id in `requires`."""
    graph = (_objective("a", 3., requires=("a",)),)

    assert effective_values(graph) == {"a": 3.}
    assert chain_to(graph, "a") == ("a",)


# -- unknown ids -----------------------------------------------------------

def test_an_unknown_requires_id_is_not_an_edge() -> None:
    """A caller ranking one build's slice legitimately cites objectives that
    slice does not contain, and `objectives.classify` already treats such an
    id as an unmet prerequisite rather than a crash. Raising here would mean
    one dangling id in one build file blanks the whole dashboard."""
    graph = (
        _objective("a", 1., requires=("nowhere",)),
        _objective("b", 5., requires=("a", "also.nowhere")),
    )

    values = effective_values(graph, discount=.5)

    assert values == {"a": 1. + .5 * 5., "b": 5.}
    assert "nowhere" not in values


def test_grants_are_not_edges() -> None:
    """`grants` labels a capability, not another objective's id; `requires`
    is the only id-to-id edge, and the only one `classify()` itself walks."""
    granting = objectives.Objective(
        id="a", requires=(), grants=("b",), satisfied_by=lambda revision: True,
        actions=(), value=1., risk="reversible", knowledge_refs=(),
    )
    graph = (granting, _objective("b", 9.))

    assert effective_values(graph) == {"a": 1., "b": 9.}


# -- chain_to agrees with effective_values ---------------------------------

def test_chain_to_follows_the_dependent_that_won_the_max() -> None:
    graph = (
        _objective("start", 1.),
        _objective("dull", 2., requires=("start",)),
        _objective("better", 3., requires=("start",)),
        _objective("prize", 20., requires=("better",)),
    )

    assert chain_to(graph, "start") == ("start", "better", "prize")


def test_chain_to_and_effective_values_agree_step_by_step() -> None:
    """Both are projections of one traversal, so the chain must reconstruct
    the number exactly: writing the argmax a second time is the drift risk
    this module is shaped to avoid."""
    graph = (
        _objective("a", 1.5),
        _objective("b", 2.5, requires=("a",)),
        _objective("c", 4., requires=("a",)),
        _objective("d", 11., requires=("c",)),
    )
    own = {"a": 1.5, "b": 2.5, "c": 4., "d": 11.}

    values = effective_values(graph)
    chain = chain_to(graph, "a")

    assert chain == ("a", "c", "d")
    rebuilt = 0.
    for step in reversed(chain):
        rebuilt = own[step] + DEFAULT_DISCOUNT * rebuilt
    assert values["a"] == pytest.approx(rebuilt)


def test_chain_to_ends_at_the_objective_whose_value_is_inherited() -> None:
    graph = (
        _objective("a", 1.),
        _objective("b", 2., requires=("a",)),
        _objective("goal", 30., requires=("b",)),
    )

    chain = chain_to(graph, "a")

    assert chain[-1] == "goal"
    assert effective_values(graph)[chain[-1]] == 30.


def test_equal_valued_dependents_break_the_tie_on_id_not_graph_order() -> None:
    """`objectives.GRAPH` is five families concatenated and could be
    concatenated in any order without anyone calling that a behaviour
    change, so neither the chain nor the cut cycle edge may depend on it."""
    forward = (
        _objective("root", 1.),
        _objective("aaa", 5., requires=("root",)),
        _objective("zzz", 5., requires=("root",)),
    )
    reversed_order = (forward[0], forward[2], forward[1])

    assert chain_to(forward, "root") == ("root", "aaa")
    assert chain_to(reversed_order, "root") == ("root", "aaa")
    assert effective_values(forward) == effective_values(reversed_order)


# -- the real chain this feature exists for --------------------------------

def _turtle_graph() -> tuple[objectives.Objective, ...]:
    """The live turtle build as an objective graph: `fleet.reroll_planner`'s
    own weights and prerequisites, read from that module rather than copied,
    so a future edit to either one is felt here.

    Two of the prerequisites it declares (`unlock_cash_bonuses`,
    `unlock_coin_bonuses`) are not themselves weighted turtle upgrades, so
    they are exactly the "id with no matching objective" case - ignored as
    non-edges, which is how a real slice of a real plan arrives.
    """
    return tuple(
        _objective(upgrade_id, float(weight),
                   requires=(_PREREQUISITES[upgrade_id],) if upgrade_id in _PREREQUISITES else ())
        for upgrade_id, weight in _TURTLE
    )


def test_the_turtle_unlock_outranks_the_upgrade_that_statically_beats_it() -> None:
    """The whole point of the feature, on real data. `defense_absolute`
    (weight 16) statically beats `unlock_defense_upgrades` (10), so the
    planner buys the upgrade and never the unlock - except `defense_absolute`
    REQUIRES that unlock, so nothing is ever bought at all. Once the unlock
    inherits from the best thing it leads to, it wins, and the plan can
    finally walk the chain."""
    graph = _turtle_graph()

    values = effective_values(graph)

    assert values["defense_absolute"] > 10.  # the static ranking, unchanged
    assert values["unlock_defense_upgrades"] > values["defense_absolute"]


def test_the_turtle_chain_explains_itself_as_a_readable_path() -> None:
    """The user-visible sentence: "unlock defense upgrades -> unlock thorns
    -> thorns". `unlock_thorns` (9 + 0.6*12 = 16.2) edges out
    `defense_absolute` (16, a leaf), so the chain names thorns - the
    objective the turtle build is actually trying to reach."""
    graph = _turtle_graph()

    assert chain_to(graph, "unlock_defense_upgrades") == (
        "unlock_defense_upgrades", "unlock_thorns", "thorns",
    )
    assert effective_values(graph)["unlock_defense_upgrades"] == pytest.approx(
        10. + DEFAULT_DISCOUNT * (9. + DEFAULT_DISCOUNT * 12.)
    )


def test_the_turtle_leaves_keep_their_own_weights() -> None:
    """Nothing in the turtle set depends on these, so propagation must leave
    them exactly where they were - the extension is strict."""
    values = effective_values(_turtle_graph())

    assert values["health"] == 3.
    assert values["thorns"] == 12.
    assert values["coins_per_kill_bonus"] == 5.


# -- purity, enforced rather than trusted ----------------------------------

def test_effective_values_is_deterministic() -> None:
    graph = (
        _objective("a", 1.),
        _objective("b", 2., requires=("a",)),
        _objective("c", 3., requires=("a",)),
        _objective("d", 4., requires=("b", "c")),
    )

    assert effective_values(graph) == effective_values(graph)
    assert chain_to(graph, "a") == chain_to(graph, "a")


def test_propagation_never_mutates_the_graph_it_is_given() -> None:
    """`Objective.value` stays the declared, auditable number a human wrote
    down; the propagated number is a derived view, which is what makes it
    possible to show both in one explanation."""
    graph = (
        _objective("a", 1.),
        _objective("b", 9., requires=("a",)),
    )

    effective_values(graph)

    assert [objective.value for objective in graph] == [1., 9.]


def test_value_propagation_module_imports_nothing_that_touches_a_device() -> None:
    """An allowlist over this module's own import statements (not its
    transitive closure), the same discipline objectives.py, director.py and
    affordability_horizon.py each apply to themselves."""
    import ast
    from pathlib import Path

    source = Path(value_propagation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "dataclasses", "objectives"}
    assert imported <= allowed, imported - allowed
    forbidden = {"device", "shopping", "transactions", "time", "os", "socket",
                 "random", "db", "ocr", "screen_discovery", "tower_bot"}
    assert not (imported & forbidden), imported & forbidden
