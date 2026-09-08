"""The objective graph.

Every predicate is pure, so every branch is reachable from a synthetic
AccountRevision with no device and no emulator. Readiness is the same
set-inclusion test claude-code/tower-autonomy/next_task.py uses:

    ready = depends_on subset-of completed

The three-valued predicate is the load-bearing decision. True is provably
done, False is provably not done, and None is "the facts needed to decide are
absent" - which must BLOCK an objective, never ready it. An unread lab_levels
that read as "no labs researched" would make every lab objective look
actionable and point the director at a screen it cannot see.
"""
from __future__ import annotations

import dataclasses

import pytest

import objectives
from account_state import AccountRevision, Evidence, Fact


def fact(concept_id: str, value: object) -> Fact:
    return Fact(concept_id, value, "verified", Evidence(
        1., .99, concept_id, str(value), (0, 0, 1, 1), 1080, 2400, "abc"))


def revision(**overrides: object) -> AccountRevision:
    return dataclasses.replace(AccountRevision(), **overrides)  # type: ignore[arg-type]


# -- the graph itself ----------------------------------------------------
def test_the_graph_is_acyclic() -> None:
    """The generator that produced the engineering manifest cycle-checks; this
    graph has no generator, so the check lives here."""
    by_id = {o.id: o for o in objectives.GRAPH}
    visited: set[str] = set()
    active: set[str] = set()

    def visit(oid: str) -> None:
        assert oid not in active, f"cycle through {oid}"
        if oid in visited:
            return
        active.add(oid)
        for dep in by_id[oid].requires:
            visit(dep)
        active.remove(oid)
        visited.add(oid)

    for oid in by_id:
        visit(oid)


def test_every_requirement_resolves_to_a_real_objective() -> None:
    ids = {o.id: o for o in objectives.GRAPH}
    assert len(ids) == len(objectives.GRAPH), "duplicate objective id"
    for objective in objectives.GRAPH:
        assert set(objective.requires) <= set(ids), objective.id


def test_every_objective_cites_the_knowledge_it_rests_on() -> None:
    """An objective with no citation is a guess with an id."""
    import knowledge

    for objective in objectives.GRAPH:
        assert objective.knowledge_refs, objective.id
        for ref in objective.knowledge_refs:
            assert knowledge.KNOWLEDGE.by_id(ref) is not None, (objective.id, ref)


def test_every_objective_id_is_namespaced() -> None:
    for objective in objectives.GRAPH:
        assert "." in objective.id, objective.id
        assert objective.id == objective.id.lower(), objective.id


def test_the_route_to_tier_four_is_present() -> None:
    ids = {o.id for o in objectives.GRAPH}
    assert {"tier.unlock.2", "tier.unlock.3", "tier.unlock.4",
            "uw.pick.1", "lab.game_speed"} <= ids


def test_one_way_objectives_are_declared_as_such() -> None:
    """The risk class is data on the objective, not a lookup the gate has to
    guess. The first UW pick and the Black Hole Damage lab are permanent."""
    by_id = {o.id: o for o in objectives.GRAPH}
    assert by_id["uw.pick.1"].risk == "one_way"


def test_no_objective_declares_an_unknown_risk_class() -> None:
    for objective in objectives.GRAPH:
        assert objective.risk in ("reversible", "refundable", "one_way"), objective.id


# -- readiness -----------------------------------------------------------
def test_an_objective_with_no_requirements_is_ready_on_an_empty_account() -> None:
    empty = revision()
    statuses = objectives.classify(objectives.GRAPH, empty)
    rootless = [o.id for o in objectives.GRAPH if not o.requires]
    assert rootless
    assert any(statuses[oid] == "ready" for oid in rootless)


def test_an_objective_whose_requirement_is_unmet_is_blocked() -> None:
    statuses = objectives.classify(objectives.GRAPH, revision())
    assert statuses["tier.unlock.3"] == "blocked"


def test_a_satisfied_objective_is_done_and_not_ready() -> None:
    """done and ready are exclusive: next_task.py filters completed tasks out
    of its ready list, and so must this."""
    reached = revision(unlocks=(fact("unlocks.tier.2", True),))
    statuses = objectives.classify(objectives.GRAPH, reached)
    assert statuses["tier.unlock.2"] == "done"
    assert objectives.ready(objectives.GRAPH, reached) != ()
    assert "tier.unlock.2" not in {o.id for o in
                                   objectives.ready(objectives.GRAPH, reached)}


def test_satisfying_a_requirement_readies_its_dependent() -> None:
    reached = revision(unlocks=(fact("unlocks.tier.2", True),))
    statuses = objectives.classify(objectives.GRAPH, reached)
    assert statuses["tier.unlock.3"] == "ready"


# -- the three-valued predicate, the load-bearing rule -------------------
def test_an_unknown_fact_blocks_rather_than_readies() -> None:
    """THE test of this module. Every field on the live AccountRevision except
    workshop_stats is null. If a None predicate readied an objective, the
    director's top recommendation would be a lab research job on a screen the
    bot cannot read."""
    by_id = {o.id: o for o in objectives.GRAPH}
    lab = by_id["lab.game_speed"]
    assert lab.satisfied_by(revision(lab_levels=None)) is None
    statuses = objectives.classify(objectives.GRAPH, revision(lab_levels=None))
    assert statuses["lab.game_speed"] == "blocked"


def test_an_empty_reading_is_not_the_same_as_an_unread_one() -> None:
    """() means "we looked and there are none"; None means "nobody looked".
    The first is a fact and may ready an objective; the second may not."""
    by_id = {o.id: o for o in objectives.GRAPH}
    lab = by_id["lab.game_speed"]
    assert lab.satisfied_by(revision(lab_levels=())) is False
    assert lab.satisfied_by(revision(lab_levels=None)) is None


def test_no_predicate_performs_io() -> None:
    """Purity, enforced rather than trusted. A predicate that opened a file or
    read a clock would make the whole graph untestable offline."""
    import builtins
    import time

    opened: list[str] = []
    real_open, real_time = builtins.open, time.time
    builtins.open = lambda *a, **k: opened.append(str(a[:1])) or real_open(*a, **k)
    time.time = lambda: opened.append("time") or real_time()
    try:
        for objective in objectives.GRAPH:
            objective.satisfied_by(revision())
    finally:
        builtins.open, time.time = real_open, real_time
    assert opened == []


def test_the_live_account_shape_produces_no_ready_lab_objective() -> None:
    """Reproduces the real database: only workshop_stats is populated, every
    other section is null. This is the shape the director will actually see on
    day one, and it must not recommend anything it cannot observe."""
    live = revision(workshop_stats=(fact("stats.health", 100.),))
    statuses = objectives.classify(objectives.GRAPH, live)
    labs = [oid for oid, s in statuses.items()
            if oid.startswith("lab.") and s == "ready"]
    assert labs == []


# =========================================================================
# Everything below this line is additional to the brief: the catalog-id
# introspection test the task instructions require (nothing else in the
# phase validates a concept id embedded in Python source - knowledge.py's
# loader only checks ids that appear in the JSON pack), a purity check that
# does not rely solely on I/O-patching, an import allowlist enforcing "no
# device import", and a handful of tests proving the three-valued shape
# (unknown / known-absent / known-present) holds for predicate families
# other than lab.game_speed, which is the only one the brief itself covers.
# =========================================================================

# -- catalog-id introspection: the phase's sharpest hazard, made testable --
def test_every_concept_id_resolves_in_the_catalog() -> None:
    """objectives.py references catalog concept ids (labs.*, cards.*,
    ultimate-weapons.*) from Python source, and nothing validates those
    automatically - unlike knowledge.py's Fact_.value, no import-time check
    exists for a concept id embedded in a predicate closure or an Action's
    params. A wrong id (wrong separator, wrong family convention, a
    transliterated slug that isn't the real one) would not raise: the
    predicate would just permanently return False or None, indistinguishable
    from an objective nobody has finished yet.

    Objective.concept_ids exists so this test can reach every such id - the
    ids are held in a declared field, not buried as string literals inside
    lambda closures where no test could ever enumerate them."""
    import concepts

    for objective in objectives.GRAPH:
        for concept_id in objective.concept_ids:
            assert concepts.REGISTRY.by_id(concept_id) is not None, (
                objective.id, concept_id)


def test_concept_ids_are_not_vacuously_empty() -> None:
    """A guard against the test above passing only because nothing ever
    populates concept_ids."""
    with_ids = [o for o in objectives.GRAPH if o.concept_ids]
    assert len(with_ids) >= 10, "expected most families to cite real concept ids"


def test_concept_ids_are_never_the_wrong_separator_family() -> None:
    """The catalog is inconsistent by family (labs./cards./ultimate-weapons.
    use hyphens; stats./unlocks. use underscores in their slugs), so a
    transliteration mistake - `labs.game_speed` instead of the real
    `labs.game-speed` - would still "look" like a plausible id. This asserts
    every labs./cards. concept_id this graph cites uses the hyphenated slug
    convention that catalog/concepts.v1.json actually uses for those two
    families, catching exactly the kind of near-miss concepts.REGISTRY.by_id
    alone would also catch, but pinned here so a regression is legible
    without cross-referencing the catalog by hand."""
    for objective in objectives.GRAPH:
        for concept_id in objective.concept_ids:
            prefix, _, slug = concept_id.partition(".")
            if prefix in ("labs", "cards"):
                assert "_" not in slug, (objective.id, concept_id)


# -- fix round 1: declared concept_ids must match what is actually read ---
def _catalog_shaped_strings(predicate: object) -> set[str]:
    """Every string this predicate closure could hand to a real lookup, from
    two different places a concept id can live in a Callable built the way
    this module builds them:

    - a literal baked directly into the closure's own bytecode
      (`co_consts`) - the shape `lambda r: _lab_at_least(r, "labs.x", 1)`
      uses, where the id is compiled straight into the function body;
    - a bound default argument (`__defaults__`) - the shape
      `lambda r, c=concept_id: _lab_at_least(r, c, 1)` uses, where a loop
      variable is captured once per generated Objective.

    There is no public, stable API that maps "the id(s) a bare
    Callable[[AccountRevision], bool | None] reads" to anything cleaner than
    this for a plain function/lambda - this is deliberately "read the
    closure's captured cells or the source", the honest fallback the task
    brief allows when nothing cleaner exists. A predicate's own docstring
    (a `def`, not a `lambda`, can have one - e.g. cards.unlock.*'s
    `_no_per_card_unlock_signal`) also lands in co_consts as one long
    string; it is never mistaken for a concept id below because it is
    matched only via an exact `concepts.REGISTRY.by_id` lookup later, and a
    multi-line docstring can never equal a short dotted id verbatim.
    """
    found: set[str] = set()
    code = getattr(predicate, "__code__", None)
    if code is not None:
        found.update(c for c in code.co_consts if isinstance(c, str) and "." in c)
    found.update(d for d in (getattr(predicate, "__defaults__", None) or ())
                 if isinstance(d, str) and "." in d)
    return found


def _catalog_shaped_action_params(objective: "objectives.Objective") -> set[str]:
    """The same, for values named in an objective's Action.params - the
    channel cards.unlock.* actually uses (its predicate never reads the
    card id at all; only the action does), so a check that looked only at
    satisfied_by would wrongly flag that family's concept_ids as unused."""
    found: set[str] = set()
    for action in objective.actions:
        for _, value in action.params:
            if isinstance(value, str) and "." in value:
                found.add(value)
    return found


def test_declared_concept_ids_match_what_the_objective_actually_references() -> None:
    """THE regression test for a review finding on this module: the lab
    family used to write each catalog id twice per objective - once inside
    satisfied_by's lambda, once in concept_ids - as two independently typed
    string literals that happened to agree. Nothing forced them to move
    together, and test_every_concept_id_resolves_in_the_catalog alone would
    not have caught a same-family swap (a *different*, still-valid labs.*
    id substituted into just one of the two) - the declared id would still
    resolve, while the predicate silently started reading something else.

    The structural fix (this commit) makes every family that populates
    concept_ids read the id from a single bound loop variable shared by the
    predicate (or, for cards.unlock.*, the action) and concept_ids, so the
    two can no longer diverge by editing only one of them - see
    _lab_priority_objectives, _uw_objectives, _cards_slot_objectives and
    _card_unlock_objectives in objectives.py. This test is the detection
    layer on top of that construction: it extracts every catalog-id-shaped
    value the objective's predicate or actions could actually produce and
    asserts it is exactly the declared set - proving the two are one value,
    not two coincidentally-equal ones, and catching a future regression to
    the old hand-written, independently-literal shape even if the
    structural guarantee is later undone by accident."""
    import concepts

    for objective in objectives.GRAPH:
        referenced = _catalog_shaped_strings(objective.satisfied_by) | \
            _catalog_shaped_action_params(objective)
        catalog_referenced = {c for c in referenced if concepts.REGISTRY.by_id(c) is not None}
        assert catalog_referenced == set(objective.concept_ids), (
            objective.id, catalog_referenced, objective.concept_ids)


# -- purity, enforced two ways, not one -----------------------------------
def test_predicates_are_deterministic() -> None:
    """Calling a predicate twice on the same revision must give the same
    answer. test_no_predicate_performs_io only catches open()/time.time();
    this catches any other hidden mutable-state read (a module global, a
    counter, anything) that patching two functions would miss."""
    sample = revision(
        unlocks=(fact("unlocks.tier.2", True), fact("unlocks.tier.3", False)),
        lab_levels=(fact("labs.game-speed", 3),),
        lab_slots_owned=2,
        cards=(fact("cards.slots.capacity", 5),),
    )
    for objective in objectives.GRAPH:
        first = objective.satisfied_by(sample)
        second = objective.satisfied_by(sample)
        assert first == second, objective.id


def test_objectives_module_imports_nothing_that_touches_a_device() -> None:
    """Enforced, not trusted: an allowlist over objectives.py's own import
    statements (not its transitive closure - account_state legitimately
    imports the device-adjacent modules that back its own field types).
    Catches a future edit that reaches for `device`, `shopping`,
    `transactions`, or any I/O-capable stdlib module (time, os, socket, ...)
    to make a predicate "smarter"."""
    import ast
    from pathlib import Path

    source = Path(objectives.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    allowed = {"__future__", "dataclasses", "typing", "account_state"}
    assert imported <= allowed, imported - allowed
    forbidden = {"device", "shopping", "transactions", "time", "os", "socket",
                 "ultimate_weapons", "cards", "ocr", "screen_discovery"}
    assert not (imported & forbidden), imported & forbidden


# -- the three-valued shape, proven for families beyond lab.game_speed -----
def test_labs_unlocked_distinguishes_unknown_from_zero_slots() -> None:
    """lab_slots_owned's own docstring: None means nobody has counted them,
    never that the account owns none. labs.unlocked is the one objective in
    this graph whose predicate reads that field directly."""
    by_id = {o.id: o for o in objectives.GRAPH}
    labs_unlocked = by_id["labs.unlocked"]
    assert labs_unlocked.satisfied_by(revision(lab_slots_owned=None)) is None
    assert labs_unlocked.satisfied_by(revision(lab_slots_owned=0)) is False
    assert labs_unlocked.satisfied_by(revision(lab_slots_owned=1)) is True


def test_tier_unlock_distinguishes_unknown_from_read_but_absent() -> None:
    """unlocks=None ("the Unlocks page has never been read") must not read
    the same as unlocks=() or a populated tuple that simply lacks this
    account's tier-2 flag ("read, and it says no")."""
    by_id = {o.id: o for o in objectives.GRAPH}
    tier_2 = by_id["tier.unlock.2"]
    assert tier_2.satisfied_by(revision(unlocks=None)) is None
    assert tier_2.satisfied_by(revision(unlocks=())) is False
    assert tier_2.satisfied_by(revision(unlocks=(fact("unlocks.tier.3", True),))) is False
    assert tier_2.satisfied_by(revision(unlocks=(fact("unlocks.tier.2", True),))) is True


def test_uw_pick_distinguishes_unknown_from_not_owned_from_owned() -> None:
    """The Ultimate Weapons record is a single optional field carrying all
    nine weapons at once; this proves the three states survive that shape
    too, not just the simpler Fact-tuple sections."""
    import ultimate_weapons

    by_id = {o.id: o for o in objectives.GRAPH}
    uw_pick_1 = by_id["uw.pick.1"]
    assert uw_pick_1.satisfied_by(revision(ultimate_weapons=None)) is None

    unread = ultimate_weapons.unknown_record()
    assert uw_pick_1.satisfied_by(revision(ultimate_weapons=unread)) is None

    locked = ultimate_weapons.unknown_record(ownership="locked")
    assert uw_pick_1.satisfied_by(revision(ultimate_weapons=locked)) is False

    not_owned = dataclasses.replace(
        locked, system_status="unlocked",
        weapons=tuple(dataclasses.replace(w, ownership="not_owned") for w in locked.weapons))
    assert uw_pick_1.satisfied_by(revision(ultimate_weapons=not_owned)) is False

    owned = dataclasses.replace(
        not_owned,
        weapons=tuple(
            dataclasses.replace(w, ownership="owned")
            if w.concept_id == "ultimate-weapons.golden_tower" else w
            for w in not_owned.weapons))
    assert uw_pick_1.satisfied_by(revision(ultimate_weapons=owned)) is True


def test_cards_unlock_has_no_observable_channel_and_says_so() -> None:
    """Documents the family-4 design choice directly: unlike every other
    family, no AccountRevision shape can ever make this predicate True or
    False - cards.py never writes a per-card Fact. None always, by
    construction, not merely on an empty revision."""
    by_id = {o.id: o for o in objectives.GRAPH}
    card = by_id["cards.unlock.attack_speed"]
    assert card.satisfied_by(revision()) is None
    fully_populated = revision(
        cards=(fact("cards.slots.capacity", 10), fact("cards.slots.equipped", 5)),
        unlocks=(fact("unlocks.tier.2", True),),
    )
    assert card.satisfied_by(fully_populated) is None


def test_claims_are_never_done_but_are_not_permanently_unknown_either() -> None:
    """The family-5 design choice, the opposite one from family 4: False, not
    None, because the predicate's signature structurally cannot ask the real
    question (cadence needs a clock; satisfied_by takes only a revision)."""
    by_id = {o.id: o for o in objectives.GRAPH}
    for oid in ("claim.missions", "claim.milestones"):
        assert by_id[oid].satisfied_by(revision()) is False
        assert by_id[oid].requires == ()


def test_cards_slots_are_really_observable_unlike_cards_unlock() -> None:
    """Unlike cards.unlock.*, cards.slots.* reads a Fact cards.py actually
    writes (SLOT_CAPACITY_KEY), so all three states are reachable."""
    by_id = {o.id: o for o in objectives.GRAPH}
    slots_3 = by_id["cards.slots.3"]
    assert slots_3.satisfied_by(revision(cards=None)) is None
    assert slots_3.satisfied_by(revision(cards=())) is False
    assert slots_3.satisfied_by(revision(cards=(fact("cards.slots.capacity", 2),))) is False
    assert slots_3.satisfied_by(revision(cards=(fact("cards.slots.capacity", 3),))) is True


# -- never_satisfiable: a declaration a forgotten update cannot silently skip -
def _maximal_revision() -> AccountRevision:
    """One revision generous enough to satisfy every legitimately-
    satisfiable objective in GRAPH at once - not tailored per-objective
    (a per-objective revision could be gamed into always passing, which
    would make the probe below vacuous), and low-maintenance: a lab, card,
    or Ultimate Weapon concept id added to a future objective is picked up
    automatically through Objective.concept_ids rather than needing a new
    line here.

    Every account-state channel a `satisfied_by` predicate in this module
    reads is set to its most permissive value:
    - `unlocks`: every tier flag true.
    - `lab_slots_owned`: a large owned count (covers labs.unlocked and
      every lab.slots.N threshold).
    - `lab_levels`: every concept id any objective cites, at a level no
      real threshold in the graph exceeds. Concept ids belonging to other
      families (cards.unlock.*, uw.*) end up in here too, harmlessly -
      `_lab_at_least` only ever looks up the one id it was given.
    - `cards`: a slot capacity/equipped count above every threshold.
    - `ultimate_weapons`: every one of the nine weapons already `owned` -
      satisfies both `_uw_slot_available` (anything but 'locked') and
      `_uw_picked` (exactly 'owned') for every uw.slot.*/uw.pick.* pair at
      once.
    """
    import ultimate_weapons

    all_concept_ids = {c for o in objectives.GRAPH for c in o.concept_ids}
    return revision(
        unlocks=(fact("unlocks.tier.2", True), fact("unlocks.tier.3", True),
                 fact("unlocks.tier.4", True)),
        lab_slots_owned=99,
        lab_levels=tuple(fact(cid, 999) for cid in all_concept_ids),
        cards=(fact("cards.slots.capacity", 999), fact("cards.slots.equipped", 999)),
        ultimate_weapons=ultimate_weapons.unknown_record(ownership="owned"),
    )


def test_every_objective_is_satisfiable_or_declares_itself_never_satisfiable() -> None:
    """The fourth appearance of this phase's one recurring shape: a fact
    someone has to remember (an underscore id, an opt-in validation flag, a
    domain-seeded prefix set, and now a per-objective declaration) needs a
    test that turns forgetting it into a red suite, not a silent gap. Every
    objective must either be provably satisfiable by SOME synthetic
    revision, or declare `never_satisfiable=True` up front. A third
    never-satisfiable family added later under a new id prefix - the exact
    gap an id-prefix list in a *consumer* module would have left open -
    fails HERE if its author forgets the declaration, because its
    `satisfied_by` will not return True against `_maximal_revision()`."""
    maximal = _maximal_revision()
    for objective in objectives.GRAPH:
        if objective.never_satisfiable:
            continue
        assert objective.satisfied_by(maximal) is True, (
            objective.id, "does not declare never_satisfiable, but no "
            "synthetic revision proved it satisfiable - either "
            "_maximal_revision() needs to be more generous for this "
            "objective, or it should declare never_satisfiable=True")


def test_never_satisfiable_objectives_are_exactly_the_two_known_families() -> None:
    """Pins WHICH objectives declare it, so a future edit that adds the
    flag to something satisfiable (silencing the probe above rather than
    fixing it) is itself caught."""
    flagged = {o.id for o in objectives.GRAPH if o.never_satisfiable}
    expected = {o.id for o in objectives.GRAPH
                if o.id.startswith(("cards.unlock.", "claim."))}
    assert flagged == expected
    assert flagged  # sanity: the families actually exist in the graph
