"""Backward value propagation along `requires` edges - why unlocking A is
worth anything at all.

``objectives.Objective.value`` is static and strictly per-objective: it says
what finishing THAT objective is worth, and nothing about what finishing it
makes reachable. ``director.score`` divides exactly that number by the
affordability horizon, and ``director.plan`` then picks ``ranked[0]`` only
when it is ready and unheld - so a blocked objective can never be the
recommendation, and a cheap enabler is ranked on its own small value. Put
those two facts together against a real chain - "unlock defense upgrades"
(weight 10) gates "unlock thorns" (9) gates "thorns" (12), the shape
``fleet/reroll_planner.py``'s ``_PREREQUISITES`` already encodes for the
turtle build - and the planner is structurally unable to walk it: thorns is
blocked and therefore never ``top``; the two unlocks are ready but look
worthless next to anything with a bigger number, so the account never buys
the thing that would unblock the goal. The chain is never started, forever.

This module is the missing half of the score's numerator. It pushes value
BACKWARD along ``requires``: an objective inherits a discounted share of the
best thing that becomes reachable once it is done.

    effective(X) = X.value + discount * max(effective(D) for D in dependents(X))

where a dependent ``D`` is any objective whose ``requires`` contains
``X.id``. A leaf - nothing requires it - keeps exactly its own value, which
is the current behaviour, so the change is a strict extension rather than a
re-scaling of the whole graph.

max, not sum, and the reason is legibility as much as arithmetic
-----------------------------------------------------------------
``sum`` over dependents would answer a different question: "how much does
this unlock in total". Two things go wrong with that answer. A hub - an
unlock that gates six cheap things - permanently outranks a chain that gates
one enormous thing, because six small numbers beat one big one; and the hub's
advantage compounds at every level above it, so once a hub exists it is the
top pick more or less forever, which is precisely the failure the static
value already had, only inverted. The second thing is that ``sum`` produces
no explanation: "unlock defense upgrades, because it is a prerequisite of six
objectives worth 41 between them" is not a sentence a human can act on or
check. ``max`` means "unlock A because it leads to the single best thing",
and the argmax path IS the explanation - ``chain_to`` below renders it as
"unlock defense upgrades -> unlock thorns -> thorns 51", one ``why`` line,
verifiable by eye against the graph.

DEFAULT_DISCOUNT, the one tunable
-----------------------------------
``0.6``: an enabler is credited with 60% of the best thing one step past it,
36% two steps past, 22% three. Raising it toward ``1.0`` makes an enabler
worth as much as the goal it enables, so the ranking collapses into "always
start the longest chain" - every prerequisite of a distant prize outranks
anything immediately useful, and the bot perpetually invests. Lowering it
toward ``0.0`` restores the defect this module exists to fix: at exactly
``0.0`` propagation is disabled and ``effective_values`` reproduces
``Objective.value`` verbatim (pinned by a test, so the "off" switch cannot
quietly stop being off). ``0.6`` is chosen so that two hops of pure
enablement still carry real weight while a third hop does not overwhelm a
decent objective available today. It does not need to be generous: the
enabler is usually also the CHEAPER objective, so it already wins on
``director.score``'s denominator - the discount only has to keep it in the
same league as the goal, not ahead of it.

Cycle safety is not optional
------------------------------
The graph SHOULD be a DAG - ``objectives.GRAPH`` is, today, and every chain
in it is hand-written. But ``requires`` edges are strings, a build file
(``knowledge/builds.v1.json``) can generate them, and a single typo'd id
pointing back up a chain is enough to make ``A -> B -> A``. A naive
recursive definition would then recurse until the interpreter dies, taking
the whole plan (and the bot's dashboard) with it, for what is a data typo.
So: while a node is on the current traversal path, an edge back into it
contributes NO further propagation - the edge that closes the cycle is the
one dropped. Every node still gets a finite, defined value, and the drop is
deterministic because the traversal visits nodes and dependents in sorted-id
order, never in whatever order the graph tuple happened to be built in. The
cut is recorded in the memo and used identically by ``effective_values`` and
``chain_to``, so a cyclic graph does not produce a value and an explanation
that disagree. What it does NOT do is repair the graph or raise: a plan that
is slightly under-valued on one arm is a far better failure than no plan at
all, and finding the typo is a job for whatever validates the build file.

The traversal is an explicit stack rather than Python recursion. Cycle
cutting already bounds the depth by the node count, but a long GENERATED
chain (upgrade tier 1 requires tier 2 requires ... ) is bounded by nothing
in particular, and ``sys.setrecursionlimit`` is not a knob a pure ranking
helper should be reaching for.

Unknown ids in `requires` are ignored, deliberately
-----------------------------------------------------
An id in ``requires`` with no matching objective in ``graph`` is simply not
an edge. It is NOT an error, because this module is routinely handed a
subset: a caller ranking one build's objectives, or a test graph of three
nodes, legitimately contains ``requires`` entries pointing outside it, and
``objectives.classify`` already treats such an id the same way - as a
prerequisite that is not in ``done``, never as a crash. Raising here would
turn "this slice of the graph is not self-contained" into an exception at
the top of ``plan()``, i.e. the dashboard shows nothing at all because one
build file cites an objective that another phase has not merged yet. The
cost of ignoring is a lost propagation edge - an enabler under-credited,
which is exactly the pre-existing behaviour - and that is the cheaper
failure of the two.

``grants`` is not read at all. It labels a capability, not another
objective's id; ``requires`` is the only id-to-id edge in the graph, and it
is the one ``classify()`` itself walks.

Purity
------
No I/O, no clock, no device, no database, and no imports outside the
standard library plus ``objectives`` (which is itself the pure end of the
codebase). Same inputs, same dict, every time.
``tests/test_value_propagation.py`` enforces the import allowlist over this
module's own import statements rather than trusting this paragraph, the way
``objectives.py``, ``director.py`` and ``affordability_horizon.py`` each
enforce their own.
"""
from __future__ import annotations

from dataclasses import dataclass

import objectives

# How much of the best reachable objective's effective value an enabler
# inherits, per hop. See the module docstring for what raising and lowering
# it does; 0.0 disables propagation entirely.
DEFAULT_DISCOUNT: float = .6


@dataclass(frozen=True)
class _Propagation:
    """One node's solved answer: the value, and the dependent that produced it.

    Both halves come out of the SAME traversal and are stored together, which
    is the whole point of this type. `effective_values` needs the number and
    `chain_to` needs the argmax edge; computing them in two functions would
    be two implementations of one rule that must agree - the exact drift this
    codebase keeps designing out (see `objectives.py`'s single-source lab
    table, and `control.py`'s refusal to keep a second action parser). Here
    the number cannot disagree with the sentence explaining it, because the
    sentence is read off the edges that produced the number.

    `via` is None for a leaf (nothing depends on this objective) and also for
    a node whose every dependent was cut as a back edge - in both cases
    `value` is just the objective's own value, and the chain ends here.
    """

    value: float
    via: str | None


def _own_values(graph: tuple[objectives.Objective, ...]) -> dict[str, float]:
    """Each objective's own declared value, keyed by id.

    A graph containing the same id twice is malformed, and the last one wins
    here - the same thing `objectives.classify` does with its own statuses
    dict. Not worth an exception this module would be the only place to
    raise.
    """
    return {objective.id: float(objective.value) for objective in graph}


def _dependents(graph: tuple[objectives.Objective, ...]) -> dict[str, tuple[str, ...]]:
    """id -> the ids that require it, sorted, deduplicated.

    Sorted, not graph-ordered, so that the tie-break between two dependents
    of equal effective value (and the choice of which edge a cycle loses) is
    a property of the DATA rather than of the order someone happened to
    concatenate the graph tuple in - see `objectives.GRAPH`, which is five
    families stitched together and could be stitched in any order without
    anyone considering it a behaviour change.

    An id in `requires` that names no objective in `graph` produces no edge
    at all: see the module docstring for why that is safer than raising.
    """
    known = _own_values(graph).keys()
    collected: dict[str, set[str]] = {oid: set() for oid in known}
    for objective in graph:
        for required in objective.requires:
            if required in collected:
                collected[required].add(objective.id)
    return {oid: tuple(sorted(deps)) for oid, deps in collected.items()}


def _solve(graph: tuple[objectives.Objective, ...], discount: float) -> dict[str, _Propagation]:
    """The one traversal both public functions read.

    Iterative depth-first post-order with memoization. Memoized, because a
    diamond (two enablers of one goal, both required by a third) would
    otherwise re-derive the shared tail once per path into it, and a graph of
    chained diamonds is exponential in the number of nodes - a ranking
    function called once per dashboard refresh has no business being
    exponential in anything.

    The cycle rule, concretely: `on_path` holds exactly the ids on the
    current stack, so a dependent found in it is an ancestor, and following
    it would close a loop. That edge is skipped when it is encountered and,
    because the ancestor is by definition not yet memoized, it is skipped
    again in the fold below - one rule, applied in one place, not two
    conditions that must stay in agreement. Everything else reachable is
    still counted, so a cycle costs exactly the propagation across its
    closing edge and nothing more.
    """
    own = _own_values(graph)
    dependents = _dependents(graph)
    memo: dict[str, _Propagation] = {}

    # Sorted, for the same reason _dependents sorts: which edge a cycle loses
    # must not depend on the graph tuple's construction order.
    for root in sorted(own):
        if root in memo:
            continue
        # (id, index of the next dependent to expand) - an explicit stack,
        # not recursion; see the module docstring.
        stack: list[tuple[str, int]] = [(root, 0)]
        on_path: set[str] = {root}
        while stack:
            oid, index = stack[-1]
            deps = dependents[oid]
            if index < len(deps):
                stack[-1] = (oid, index + 1)
                dependent = deps[index]
                if dependent in on_path or dependent in memo:
                    # Cycle-closing edge, or a node some earlier path already
                    # solved. Neither needs expanding; the fold below reads
                    # the memo (and finds nothing for the former).
                    continue
                stack.append((dependent, 0))
                on_path.add(dependent)
                continue

            stack.pop()
            on_path.discard(oid)
            best: _Propagation | None = None
            winner: str | None = None
            for dependent in deps:
                reached = memo.get(dependent)
                if reached is None:
                    # On the current path: this is the cut cycle edge.
                    continue
                # Strictly greater, over dependents already sorted by id, so
                # equal-valued dependents resolve to the lexicographically
                # first one - total and reproducible, never "whichever the
                # iteration happened to reach last".
                if best is None or reached.value > best.value:
                    best, winner = reached, dependent
            inherited = 0. if best is None else discount * best.value
            memo[oid] = _Propagation(value=own[oid] + inherited, via=winner)

    return memo


def _validated_discount(discount: float) -> float:
    """A discount outside [0, 1] is a configuration bug, not an observation.

    Unlike `affordability_horizon`, which answers a missing measurement with
    `None` because "unknown" is a real and useful answer there, this function
    has no honest value to return for a nonsensical discount: a negative one
    makes an enabler worth LESS than its own value (unlocking something
    becomes a penalty), and one above 1.0 makes a long chain's head outvalue
    the prize at its end without bound. Clamping silently would reorder every
    plan while looking like it worked, so this raises at the call that passed
    the bad number instead - the caller that owns the tunable is the only
    place that can actually fix it.
    """
    if not (0. <= discount <= 1.):
        raise ValueError(
            f"discount must be between 0.0 and 1.0 inclusive, got {discount!r}"
        )
    return discount


def effective_values(graph: tuple[objectives.Objective, ...], *,
                     discount: float = DEFAULT_DISCOUNT) -> dict[str, float]:
    """Every objective's value plus its discounted best downstream prize.

    Keys are exactly the ids present in `graph` - never an id that only ever
    appeared inside someone's `requires`, which this module treats as a
    non-edge rather than as a node (see the module docstring). So a caller
    can index this dict with any `objective.id` from the same graph and never
    need a `.get(..., default)`.

    Returns a plain `dict[str, float]`, not the `Objective`s re-valued: this
    module never rewrites the graph. `Objective.value` stays the declared,
    auditable number a human wrote down; the propagated number is a derived
    view a ranker may consult, and keeping them in two places is what makes
    it possible to show both in an explanation.
    """
    return {oid: solved.value for oid, solved
            in _solve(graph, _validated_discount(discount)).items()}


def chain_to(graph: tuple[objectives.Objective, ...], objective_id: str, *,
             discount: float = DEFAULT_DISCOUNT) -> tuple[str, ...]:
    """The path forward from `objective_id` through the winning dependent at
    each step, ending at the objective whose value is being inherited.

    This is the user-visible half of the feature: `chain_to(graph,
    "unlock_defense_upgrades")` is `("unlock_defense_upgrades",
    "unlock_thorns", "thorns")`, which a caller renders as "unlock defense
    upgrades -> unlock thorns -> thorns 51" - the sentence that makes a
    recommendation to buy a cheap unlock checkable by a human instead of an
    unexplained number that went up.

    Agrees with `effective_values` by construction: both are projections of
    the single `_solve` traversal, reading the same `via` edges that produced
    the same numbers. Writing the argmax logic a second time here would be
    two implementations of one rule that must agree.

    `(objective_id,)` alone means the chain ends immediately - nothing
    depends on this objective, so it inherits nothing. An id that is not in
    `graph` at all gets the same answer rather than a `KeyError`, for the
    same reason an unknown `requires` id is not an error: a caller holding a
    slice of the graph should get a degraded explanation, not an exception in
    the middle of rendering a plan.
    """
    solved = _solve(graph, _validated_discount(discount))
    chain: list[str] = [objective_id]
    seen: set[str] = {objective_id}
    current = solved.get(objective_id)
    while current is not None and current.via is not None and current.via not in seen:
        chain.append(current.via)
        seen.add(current.via)
        current = solved.get(current.via)
    # `seen` cannot actually trigger: _solve already dropped every
    # cycle-closing edge, so the `via` edges form a DAG and walking them
    # terminates. It stays as a structural guarantee that this function
    # cannot loop forever no matter what a future edit does to the memo,
    # because "the planner hangs" is not an acceptable failure mode for a
    # graph typo.
    return tuple(chain)
