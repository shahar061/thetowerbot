"""The T1-T4 objective graph, over pure predicates.

Deliberately the same shape as claude-code/tower-autonomy/manifest.json's
task: an id, a dependency list, and a completion test. The difference is what
completion means - a human ticking a box there, a predicate evaluated against
an observed account snapshot here.

Every `satisfied_by` is PURE: no I/O, no clock, no device import. That is what
makes the whole graph unit-testable against synthetic revisions, and
test_no_predicate_performs_io (plus test_predicates_are_deterministic and
test_objectives_module_imports_nothing_that_touches_a_device) enforces it
rather than trusting it. This module imports nothing but the standard library
and account_state - not even `knowledge`, because no predicate here reads a
Fact_'s value at runtime (see "Knowledge citations" below).

The predicate is three-valued. True is provably satisfied, False is provably
not, and None is "the facts needed to decide are absent". None BLOCKS: on the
live account every AccountRevision section except workshop_stats is null, so
a None that readied an objective would point the director at a screen the bot
cannot read. Concretely, that protection lives in classify()/ready(): an
objective only ever leaves `done` for a dependent when its own predicate
returns True, never merely "not False". A root objective (requires=()) with
an unresolved own-state still shows "ready" - not because the None-blocks
rule is waived for it, but because "ready" for a root means only "nothing
outstanding blocks attempting this", which is true vacuously when there is
nothing to wait on. The hazard this whole design exists to prevent - an
unread prerequisite silently counting as met - can only occur through
`requires`, and that path is exactly what the three-valued predicate closes.

Introspectable catalog ids (the phase's sharpest hazard)
----------------------------------------------------------
A predicate closure can embed a wrong concept id - e.g. a family that uses
underscores everywhere except `labs.*`, which uses hyphens, and whose slugs
do not even transliterate 1:1 from the wiki's prose ("coins_per_wave" in
English, `labs.coins-wave` in the catalog, `stats.coins_per_wave` a
different, also-real concept). A typo'd id does not raise: catalog lookups
return None, dict.get returns None, and the predicate keeps returning False
or None forever, looking exactly like an objective nobody has finished. There
is no import-time check for this the way knowledge.py's pack loader checks
`Fact_.value`, because the ids live in code, not in the JSON pack knowledge.py
validates.

`Objective.concept_ids` exists to close that gap: every catalog concept id an
objective's predicate reads (or that an objective's action names) is held
there, not just embedded as a string literal inside a lambda closure, so
tests/test_objectives.py's test_every_concept_id_resolves_in_the_catalog can
walk the whole graph and assert every one resolves via
`concepts.REGISTRY.by_id`. Objectives whose predicate reads an account-level
key that is deliberately NOT catalog-registered (see "Non-catalog account
keys" below) leave `concept_ids` empty rather than populating it with a
lookalike that would fail that test for the wrong reason.

Non-catalog account keys
----------------------------------------------------------
Two families of `account_state.Fact.concept_id` used by this graph are NOT
catalog concepts, on purpose, and must never be added to `concept_ids`:

- `unlocks.tier.{2,3,4}` (the tier spine). `catalog/concepts.v1.json` has no
  `tier.*` namespace at all (grep confirms zero matches), so these are
  bespoke account-level flags the same way `game_version` is a plain field
  with no catalog identity - not an oversight, and not something a future
  edit should "fix" by inventing a `tier.*` catalog family.
- `cards.slots.capacity` / `cards.slots.equipped` (the two Facts
  cards.py's `facts()` ever actually writes to `AccountRevision.cards` - see
  cards.py:200-213). This module hardcodes the same string
  `cards.py.SLOT_CAPACITY_KEY` uses rather than importing cards.py, because
  cards.py pulls in the screen-reading stack this module must stay clear of
  (see "Only two imports" below).

Risk classes, defined
----------------------------------------------------------
`RiskClass` is data on the objective, never a lookup the gate has to guess
(test_one_way_objectives_are_declared_as_such's point, generalised):

- "reversible": nothing is lost by having done it - a respec mechanic exists
  (`labs.workshop-respec`) or nothing was spent at all.
- "refundable": a resource (stones, gems) is spent and not returned, but the
  choice itself is not a permanent commitment - a card slot or an Ultimate
  Weapon slot is capacity, not an identity.
- "one_way": permanent. An Ultimate Weapon *pick* (not the slot - the
  weapon chosen to fill it) cannot be swapped once made, per
  `hazard.uw.first_pick`'s "Ultimate Weapon slot picks are permanent" - true
  of every pick, not just the account's first, so every `uw.pick.*` in this
  graph carries this risk class, not only `uw.pick.1`. The Black Hole Damage
  lab is the other one_way case: `hazard.lab.black_hole_damage` states
  plainly it "cannot be unresearched once taken", an exception to the
  otherwise-reversible lab family that only the objective's own `risk` field
  can express.

Knowledge citations, and the authorises() gate
----------------------------------------------------------
`Objective.knowledge_refs` cites the wiki facts an objective's existence,
ordering or framing rests on, and every one is checked (in the test file, not
here) against `knowledge.KNOWLEDGE.by_id`. Two things are true about every
citation in this graph and are recorded here because they matter:

1. Every fact in knowledge/*.v1.json has `rule_verified: false` at this pack
   version (task 3's extraction, not yet independently checked), so
   `KNOWLEDGE.authorises(fact_id)` is False for all 23 facts, full stop -
   two of them (`priority.gems.spend_order`, used by the lab/card slot-count
   families and lab.labs_speed; `priority.cards.unlock_order`, used by the
   card-unlock family) are additionally tainted by an unresolved conflict
   (IceTae's priority list self-dates to game version 0.16-0.17; the
   Enemy Balance card's effect direction is disputed between two wiki
   pages). Per this phase's governing rule, an unresolved conflict never
   authorises anything.
2. No predicate in this module calls `knowledge.KNOWLEDGE.authorises` or
   reads a `Fact_.value` at all - satisfaction is decided purely from
   AccountRevision's own observed fields. The tainted/unverified facts above
   are used only to decide the SHAPE of the graph (which slot counts are
   worth an objective, what order to chain them, what a params dict names
   for a future executor) - never as a truth value a predicate trusts. There
   is no spend or device action anywhere in this phase for the gate to
   authorise in the first place. This is deliberate, not an oversight: see
   task-4-report.md for the full accounting of which citation is tainted and
   why that is still an honest thing to cite.

Only two imports
----------------------------------------------------------
This module imports only the standard library and `account_state`. Not
`knowledge` (no predicate reads a Fact_ value, so there is nothing to import
it for - the citation strings in `knowledge_refs` are validated by the test
file, which does import it). Not `ultimate_weapons`, `cards`, or anything
device/screen-reading (those transitively import `ocr`, `screen_discovery`,
`time`; `account_state.AccountRevision.ultimate_weapons` already carries a
fully-constructed `ultimate_weapons.UltimateWeaponsRecord` instance, and
Python method dispatch on that instance - `record.weapon(concept_id)` - needs
no import of the defining module, only the object).
test_objectives_module_imports_nothing_that_touches_a_device enforces this
import list as an allowlist rather than trusting the docstring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from account_state import AccountRevision, Fact

RiskClass = Literal["reversible", "refundable", "one_way"]
Status = Literal["done", "ready", "blocked"]

Predicate = Callable[[AccountRevision], bool | None]


@dataclass(frozen=True)
class Action:
    """One executor call, with what must hold before and after.

    `executor` names an existing module-level entry point by string rather
    than holding a reference, so this module's OWN import statements never
    need to name a device/ocr module directly -
    `test_objectives_module_imports_nothing_that_touches_a_device` enforces
    that as an allowlist over this module's direct imports, not its
    transitive closure. It cannot be the transitive closure: this module
    imports `account_state`, which imports `ultimate_weapons`, which
    imports `ocr` and `device`, which import `cv2`/`numpy`/`adbutils` - so
    `import objectives` still needs those packages installed (outside this
    repo's own venv it raises `ModuleNotFoundError: No module named
    'cv2'`, not a clean import). What IS true, and what this buys: no I/O
    runs at import time anywhere in that chain (`device.py` only
    constructs a logger) and no emulator connection is ever made, so this
    graph is exercised in `test_objectives.py` against synthetic
    `AccountRevision`s with no emulator attached - not with no device
    packages installed.
    """

    executor: str
    params: tuple[tuple[str, Any], ...] = ()
    precondition: str | None = None
    postcondition: str | None = None


@dataclass(frozen=True)
class Objective:
    id: str
    requires: tuple[str, ...]
    grants: tuple[str, ...]
    satisfied_by: Predicate
    actions: tuple[Action, ...]
    value: float
    risk: RiskClass
    knowledge_refs: tuple[str, ...]
    # Every catalog concept id (concepts.REGISTRY.by_id) this objective's
    # predicate reads or its actions name. Deliberately empty, never a
    # lookalike, for objectives whose predicate reads a non-catalog account
    # key - see the module docstring's "Non-catalog account keys".
    concept_ids: tuple[str, ...] = ()
    # True only for cards.unlock.* (satisfied_by returns None forever - no
    # AccountRevision shape expresses per-card unlock state) and claim.*
    # (satisfied_by returns False forever - the cadence needs a clock the
    # one-argument predicate signature cannot take). Declared HERE, on the
    # objective, rather than left for a consumer (director.py) to recognise
    # by matching an id prefix: an id-prefix list is a fact a future author
    # has to remember to update, and this phase's whole pattern - underscore
    # ids, opt-in concept-ref validation, a domain-seeded prefix set, and
    # this field - is "the fix that holds is the one that fails the suite
    # rather than asking someone to remember." See
    # test_every_objective_is_satisfiable_or_declares_itself_never_satisfiable
    # in tests/test_objectives.py: it is what turns a forgotten declaration
    # into a red suite rather than a silently-wrong one.
    never_satisfiable: bool = False


# -- generic account-fact helpers ----------------------------------------
def _facts(section: tuple[Fact, ...] | None) -> dict[str, Any] | None:
    """A section as {concept_id: value}, or None when nobody has read it.

    The single most repeated shape in every predicate below, and the single
    place the None-vs-empty distinction is made. () becomes {} - a real,
    empty reading. None stays None - an absence of evidence.
    """
    if section is None:
        return None
    return {f.concept_id: f.value for f in section}


def _has_unlock(revision: AccountRevision, concept_id: str) -> bool | None:
    unlocks = _facts(revision.unlocks)
    if unlocks is None:
        return None
    return bool(unlocks.get(concept_id))


def _lab_at_least(revision: AccountRevision, concept_id: str, level: int) -> bool | None:
    levels = _facts(revision.lab_levels)
    if levels is None:
        return None
    current = levels.get(concept_id)
    if current is None:
        return False
    return bool(current >= level)


def _lab_slots_at_least(revision: AccountRevision, count: int) -> bool | None:
    """`lab_slots_owned`'s own docstring: None means nobody has counted
    them, never that the account owns none - so None here, not 0 or False."""
    owned = revision.lab_slots_owned
    if owned is None:
        return None
    return owned >= count


# The one Fact key cards.py's facts() ever actually writes into
# AccountRevision.cards (cards.SLOT_CAPACITY_KEY) - hardcoded rather than
# imported, see the module docstring's "Only two imports".
_CARDS_SLOT_CAPACITY_KEY = "cards.slots.capacity"


def _cards_capacity_at_least(revision: AccountRevision, count: int) -> bool | None:
    facts = _facts(revision.cards)
    if facts is None:
        return None
    capacity = facts.get(_CARDS_SLOT_CAPACITY_KEY)
    if capacity is None:
        return False
    return bool(capacity >= count)


_UW_UNRESOLVED_OWNERSHIP = frozenset({"unknown", "unreadable"})


def _uw_ownership(revision: AccountRevision, concept_id: str) -> str | None:
    record = revision.ultimate_weapons
    if record is None:
        return None
    weapon = record.weapon(concept_id)
    if weapon is None:
        return None
    return weapon.ownership


def _uw_slot_available(revision: AccountRevision, concept_id: str) -> bool | None:
    """True once the slot can be spent into (not_owned or owned), False while
    still locked, None while the reading itself is unresolved."""
    ownership = _uw_ownership(revision, concept_id)
    if ownership is None or ownership in _UW_UNRESOLVED_OWNERSHIP:
        return None
    return ownership != "locked"


def _uw_picked(revision: AccountRevision, concept_id: str) -> bool | None:
    ownership = _uw_ownership(revision, concept_id)
    if ownership is None or ownership in _UW_UNRESOLVED_OWNERSHIP:
        return None
    return ownership == "owned"


# -- readiness -------------------------------------------------------------
def ready(graph: tuple[Objective, ...], revision: AccountRevision) -> tuple[Objective, ...]:
    """Every objective whose requirements are satisfied and which is not.

    The same set-inclusion test next_task.py uses, with `completed` computed
    from predicates instead of read from a hand-maintained list.
    """
    statuses = classify(graph, revision)
    return tuple(o for o in graph if statuses[o.id] == "ready")


def classify(graph: tuple[Objective, ...], revision: AccountRevision) -> dict[str, Status]:
    """done / ready / blocked for every objective.

    `done` is populated only by `satisfied_by(revision) is True` - neither
    False nor None ever adds an id to it, so an unresolved prerequisite can
    never make a dependent look ready (the None-blocks rule, enforced here
    via `requires`). A root objective's own unresolved state does not gate
    its own readiness the same way: with no `requires`, "ready" only ever
    claims "nothing outstanding blocks attempting this", which holds
    vacuously - see the module docstring.
    """
    done: set[str] = {o.id for o in graph if o.satisfied_by(revision) is True}

    statuses: dict[str, Status] = {}
    for objective in graph:
        if objective.id in done:
            statuses[objective.id] = "done"
        elif set(objective.requires) <= done:
            statuses[objective.id] = "ready"
        else:
            statuses[objective.id] = "blocked"
    return statuses


# -- family 1: the tier spine ----------------------------------------------
_TIER_UNLOCKS: tuple[Objective, ...] = (
    Objective(
        id="tier.unlock.2",
        requires=(),
        grants=("tier.2",),
        # Reaching the gate is what unlocks it, and the unlock is the fact the
        # account carries. Not derived from best-wave: a wave-100 clear that
        # the ladder has not registered is not an unlock.
        satisfied_by=lambda r: _has_unlock(r, "unlocks.tier.2"),
        actions=(Action(executor="director.push", params=(("tier", 1), ("target_wave", 100))),),
        # The multiplier every downstream objective is funded by.
        value=1.8,
        risk="reversible",
        knowledge_refs=("tier.2.unlock_wave", "tier.2.coin_multiplier", "milestone.t1.w100"),
    ),
    Objective(
        id="tier.unlock.3",
        requires=("tier.unlock.2",),
        grants=("tier.3",),
        satisfied_by=lambda r: _has_unlock(r, "unlocks.tier.3"),
        actions=(Action(executor="director.push", params=(("tier", 2), ("target_wave", 100))),),
        value=2.6,
        risk="reversible",
        knowledge_refs=("tier.3.unlock_wave", "tier.3.coin_multiplier"),
    ),
    Objective(
        id="tier.unlock.4",
        requires=("tier.unlock.3",),
        grants=("tier.4",),
        satisfied_by=lambda r: _has_unlock(r, "unlocks.tier.4"),
        actions=(Action(executor="director.push", params=(("tier", 3), ("target_wave", 100))),),
        value=3.4,
        risk="reversible",
        knowledge_refs=("tier.4.unlock_wave", "tier.4.coin_multiplier"),
    ),
)


# -- family 2: labs unlocked, slot growth, and the priority-t1 labs --------
_LAB_OBJECTIVES: tuple[Objective, ...] = (
    Objective(
        id="labs.unlocked",
        requires=(),
        grants=("labs.access",),
        satisfied_by=lambda r: _lab_slots_at_least(r, 1),
        actions=(Action(executor="director.push", params=(("tier", 1), ("target_wave", 30))),),
        value=0.5,
        risk="reversible",
        knowledge_refs=("milestone.t1.w30",),
    ),
    Objective(
        id="lab.slots.2",
        requires=("labs.unlocked",),
        grants=("labs.slots.2",),
        satisfied_by=lambda r: _lab_slots_at_least(r, 2),
        actions=(Action(executor="labs.buy_slot", params=(("slot", 2),)),),
        value=0.45,
        risk="refundable",
        knowledge_refs=("priority.gems.spend_order",),
    ),
    Objective(
        id="lab.slots.3",
        requires=("lab.slots.2",),
        grants=("labs.slots.3",),
        satisfied_by=lambda r: _lab_slots_at_least(r, 3),
        actions=(Action(executor="labs.buy_slot", params=(("slot", 3),)),),
        value=0.4,
        risk="refundable",
        knowledge_refs=("priority.gems.spend_order",),
    ),
    Objective(
        id="lab.slots.4",
        requires=("lab.slots.3",),
        grants=("labs.slots.4",),
        satisfied_by=lambda r: _lab_slots_at_least(r, 4),
        actions=(Action(executor="labs.buy_slot", params=(("slot", 4),)),),
        value=0.35,
        risk="refundable",
        knowledge_refs=("priority.gems.spend_order",),
    ),
    Objective(
        id="lab.slots.5",
        requires=("lab.slots.4",),
        grants=("labs.slots.5",),
        satisfied_by=lambda r: _lab_slots_at_least(r, 5),
        actions=(Action(executor="labs.buy_slot", params=(("slot", 5),)),),
        value=0.3,
        risk="refundable",
        knowledge_refs=("priority.gems.spend_order",),
    ),
)


# priority.labs.t1's three entries, plus two more grounded labs
# (lab.black_hole_damage from hazard.lab.black_hole_damage,
# lab.labs_speed from priority.gems.spend_order's step 9). Each row is
# (id-slug, catalog concept id, target level, value, risk, knowledge_refs).
#
# The concept id appears exactly ONCE per row, here in the table - not once
# in a predicate closure and again in `concept_ids` as two independently
# typed literals. _lab_priority_objectives() below reads it from the same
# tuple element for both `satisfied_by`'s lambda (via a `c=concept_id`
# default-argument capture, the same binding the UW family below uses) and
# `concept_ids`, so a wrong-but-still-valid id swapped into one can no
# longer diverge from the other - editing this table moves both at once.
# This is the fix for a review finding: with a hand-written Objective() per
# lab (the original shape here, and still the shape family 3 avoided from
# the start), the two occurrences were two string literals that happened to
# agree, not one value read twice - concepts.REGISTRY.by_id would still
# validate a *different*, still-real id substituted into just one of them,
# while the predicate silently started reading something else.
#
# lab.black_hole_damage is permanent once taken
# (hazard.lab.black_hole_damage: "cannot be unresearched once taken"),
# unlike the three priority-t1 labs - risk is data per-objective, not a
# family default. Its low value is deliberate: the hazard fact's own
# interdiction is "never_research_if_perma_stall_intended".
_LAB_PRIORITY_TABLE: tuple[tuple[str, str, int, float, RiskClass, tuple[str, ...]], ...] = (
    ("game_speed", "labs.game-speed", 1, 1.0, "reversible",
     ("priority.labs.t1", "milestone.t1.w30")),
    ("coins_wave", "labs.coins-wave", 1, 0.85, "reversible",
     ("priority.labs.t1",)),
    ("defense_absolute", "labs.defense-absolute", 1, 0.7, "reversible",
     ("priority.labs.t1",)),
    ("black_hole_damage", "labs.black-hole-damage", 1, 0.1, "one_way",
     ("hazard.lab.black_hole_damage",)),
    ("labs_speed", "labs.labs-speed", 50, 0.35, "reversible",
     ("priority.gems.spend_order",)),
)


def _lab_priority_objectives() -> tuple[Objective, ...]:
    objs: list[Objective] = []
    for slug, concept_id, level, value, risk, knowledge_refs in _LAB_PRIORITY_TABLE:
        params: tuple[tuple[str, Any], ...] = (("lab", concept_id),)
        if level != 1:
            params = params + (("target_level", level),)
        objs.append(Objective(
            id=f"lab.{slug}",
            requires=("labs.unlocked",),
            grants=(),
            satisfied_by=(lambda r, c=concept_id, lvl=level: _lab_at_least(r, c, lvl)),
            actions=(Action(executor="labs.research", params=params,
                            precondition="lab_slots_owned is not None",
                            postcondition=f"lab_levels reports {concept_id} >= {level}"),),
            value=value,
            risk=risk,
            knowledge_refs=knowledge_refs,
            concept_ids=(concept_id,),
        ))
    return tuple(objs)


# -- family 3: the nine Ultimate Weapon slots and picks ---------------------
# priority.uw.unlock_order's recommended pick order, and
# priority.uw.slot_stone_costs' per-slot stone price at the same index.
# Neither fact is conflict-tainted (only priority.gems.spend_order and
# priority.cards.unlock_order are), though - like every fact in this pack -
# neither is yet rule_verified either.
_UW_PRIORITY_ORDER: tuple[str, ...] = (
    "ultimate-weapons.golden_tower",
    "ultimate-weapons.black_hole",
    "ultimate-weapons.death_wave",
    "ultimate-weapons.spotlight",
    "ultimate-weapons.chrono_field",
    "ultimate-weapons.chain_lightning",
    "ultimate-weapons.inner_land_mines",
    "ultimate-weapons.poison_swamp",
    "ultimate-weapons.smart_missiles",
)
_UW_SLOT_STONE_COSTS: tuple[int, ...] = (5, 50, 150, 300, 800, 1250, 1750, 2400, 3000)


def _uw_objectives() -> tuple[Objective, ...]:
    objs: list[Objective] = []
    for index, concept_id in enumerate(_UW_PRIORITY_ORDER, start=1):
        slot_id = f"uw.slot.{index}"
        pick_id = f"uw.pick.{index}"
        objs.append(Objective(
            id=slot_id,
            requires=() if index == 1 else (f"uw.slot.{index - 1}",),
            grants=(slot_id,),
            satisfied_by=(lambda r, c=concept_id: _uw_slot_available(r, c)),
            actions=(Action(
                executor="ultimate_weapons.unlock_slot",
                params=(("weapon", concept_id), ("stone_cost", _UW_SLOT_STONE_COSTS[index - 1])),
                precondition="ultimate_weapons is not None",
                postcondition=f"{concept_id} ownership is not 'locked'"),),
            value=round(0.9 - 0.05 * (index - 1), 2),
            # Capacity, not identity: spends stones, but does not itself
            # commit to a weapon. The pick that follows is the one_way step.
            risk="refundable",
            knowledge_refs=("priority.uw.unlock_order", "priority.uw.slot_stone_costs"),
            concept_ids=(concept_id,),
        ))
        objs.append(Objective(
            id=pick_id,
            requires=(slot_id,),
            grants=(pick_id,),
            satisfied_by=(lambda r, c=concept_id: _uw_picked(r, c)),
            actions=(Action(
                executor="ultimate_weapons.pick",
                params=(("weapon", concept_id),),
                precondition=f"{slot_id} satisfied",
                postcondition=f"{concept_id} ownership is 'owned'"),),
            value=round(1.0 - 0.05 * (index - 1), 2),
            # hazard.uw.first_pick: slot picks are permanent, for every
            # pick, not only the account's literal first one.
            risk="one_way",
            knowledge_refs=("hazard.uw.first_pick", "priority.uw.unlock_order"),
            concept_ids=(concept_id,),
        ))
    return tuple(objs)


# -- family 4: card slot capacity and the priority-order card unlocks ------
def _cards_slot_objectives() -> tuple[Objective, ...]:
    # priority.gems.spend_order steps 2/4/6/8: card_slots_3, card_slots_5,
    # card_slots_7, card_slots_10.
    thresholds = (3, 5, 7, 10)
    objs: list[Objective] = []
    previous: str | None = None
    for i, count in enumerate(thresholds):
        oid = f"cards.slots.{count}"
        objs.append(Objective(
            id=oid,
            requires=() if previous is None else (previous,),
            grants=(oid,),
            satisfied_by=(lambda r, n=count: _cards_capacity_at_least(r, n)),
            actions=(Action(executor="cards.buy_slot", params=(("capacity", count),),
                            precondition="cards is not None",
                            postcondition=f"cards.slots.capacity >= {count}"),),
            value=round(0.5 - 0.05 * i, 2),
            risk="refundable",
            knowledge_refs=("priority.gems.spend_order",),
        ))
        previous = oid
    return tuple(objs)


# priority.cards.unlock_order's five entries, translated to the real
# hyphenated catalog ids (never transliterated - looked up in
# catalog/concepts.v1.json), paired with the card-slot tier that
# priority.gems.spend_order's steps 2 and 4 say unlocks them.
_CARD_UNLOCKS: tuple[tuple[str, str, str], ...] = (
    ("attack_speed", "cards.attack-speed", "cards.slots.3"),
    ("enemy_balance", "cards.enemy-balance", "cards.slots.3"),
    ("coins", "cards.coins", "cards.slots.3"),
    ("health", "cards.health", "cards.slots.5"),
    ("cash", "cards.cash", "cards.slots.5"),
)


def _no_per_card_unlock_signal(revision: AccountRevision) -> bool | None:
    """AccountRevision.cards carries only the two slot-count Facts
    cards.py's facts() ever writes (cards.slots.equipped/.capacity) - no
    per-card unlock flag reaches an account revision at all, by that
    module's own design ("No card fact is ever produced", cards.py:200-204).

    This is not "unread" (which None would also mean) so much as
    "unrepresentable with the current account model" - but the honest
    three-valued answer is still None rather than False: False would assert
    "provably not unlocked", which this module has no way to prove. See
    task-4-report.md for why None, not the claim family's False, was chosen
    here.
    """
    del revision
    return None


def _card_unlock_objectives() -> tuple[Objective, ...]:
    objs: list[Objective] = []
    for slug, concept_id, gate in _CARD_UNLOCKS:
        objs.append(Objective(
            id=f"cards.unlock.{slug}",
            requires=(gate,),
            grants=(f"cards.unlock.{slug}",),
            satisfied_by=_no_per_card_unlock_signal,
            actions=(Action(executor="cards.unlock", params=(("card", concept_id),),
                            precondition=f"{gate} satisfied"),),
            value=0.25,
            risk="refundable",
            knowledge_refs=("priority.cards.unlock_order", "priority.gems.spend_order"),
            concept_ids=(concept_id,),
            never_satisfiable=True,
        ))
    return tuple(objs)


# -- family 5: recurring claims ---------------------------------------------
# "Claimed recently" is a time-window question and satisfied_by's signature
# is Callable[[AccountRevision], bool | None] - one argument, no `now`.
# claim_schedule.due(state, *, now, ...) is the actual pure decision function
# Phase 0a already built for this cadence, and it is deliberately NOT called
# from here (it cannot be - it needs a clock this signature has no room for).
# False, not None, is the honest value: the predicate isn't missing data, the
# QUESTION ("is it due right now") is simply out of scope for a
# revision-only signature, and a claim is never durably "done" - it recurs.
# This objective will therefore show "ready" whenever nothing else blocks
# it, for as long as this graph exists; task 7's ranking has to know a
# permanently-ready objective like this one will dominate unless it
# specifically defers to claim_schedule.due for cadence, exactly as
# cards.unlock.* (family 4) will dominate for the unrelated reason that its
# own satisfaction can never be observed at all. Both declare
# never_satisfiable=True below so a ranking consumer can act on that fact
# directly rather than recognising the family by id prefix.
_CLAIMS: tuple[Objective, ...] = (
    Objective(
        id="claim.missions",
        requires=(),
        grants=("claim.missions",),
        satisfied_by=lambda r: False,
        actions=(Action(executor="claims.claim_missions"),),
        value=0.2,
        risk="reversible",
        # The closest available wiki fact touching missions: t4w70 unlocks
        # the daily-mission-shards and reroll-shards labs.
        knowledge_refs=("milestone.t4.w70",),
        never_satisfiable=True,
    ),
    Objective(
        id="claim.milestones",
        requires=(),
        grants=("claim.milestones",),
        satisfied_by=lambda r: False,
        actions=(Action(executor="claims.claim_milestones"),),
        value=0.3,
        risk="reversible",
        knowledge_refs=("milestone.t1.total_rewards",),
        never_satisfiable=True,
    ),
)


GRAPH: tuple[Objective, ...] = (
    _TIER_UNLOCKS
    + _LAB_OBJECTIVES
    + _lab_priority_objectives()
    + _cards_slot_objectives()
    + _card_unlock_objectives()
    + _uw_objectives()
    + _CLAIMS
)
