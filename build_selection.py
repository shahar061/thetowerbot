"""Which committed build an account should be running, and why it changed.

Generalises the one line that was the whole of automatic build selection
before this module existed, `fleet/reroll_planner.py`'s

    stage = "turtle" if (facts.best_tier_1_wave or 0) >= 20 else "opening"

into a ladder - operator override, eligibility, fit, hysteresis, fallback -
without losing what that line already got right. It is deliberately the
smaller half of the feature: this module decides WHICH recipe ranks the next
purchase, `builds.py` holds the recipes, and neither one authorises a spend.
The buyer still validates account, screen, row, balance, price and
transaction, exactly as it did when both lived inside the reroll planner.

The import direction is the architectural claim
----------------------------------------------------------
Nothing here imports `fleet/`. The reroll planner is one fleet-side consumer
of a decision that belongs to the account, not to the reroll goal, and a
`fleet` import from this module would quietly make the general rule a detail
of the special case - after which the director, the advisor and the browser
could only reach build selection by importing a fleet module. That is the
whole point of the phase, so it is enforced rather than trusted:
tests/test_build_selection.py's import allowlist reads this file's own
`import` statements. The threshold below is copied out of the planner with
its provenance written down for the same reason.

Pure, in the sense objectives.py means it: every input is a parameter, no
clock, no device, no database, no screen. The one file read anywhere in the
dependency closure is builds.py's import-time pack load, which happens once,
before any decision, and never during one.

Eligibility lives in code, and should not stay there
----------------------------------------------------------
`builds.Build` has no eligibility field. `knowledge/builds.v1.json` records
what a build BUYS (weights, targets, focus) and nothing about when an account
may run it, so there is no declarative rule to read and `_ELIGIBILITY` below
is a placeholder for a future `eligibility` field in that pack. It is written
as an explicit per-build predicate table, keyed by build id and validated
against the registry at import, rather than as an `if build.id == ...` chain,
so that the day the pack grows the field this table is a list of rules to
port and delete, not logic to disentangle.

Why it is not simply added to the pack now: a declarative rule needs a
vocabulary ("wave >= 20" is a battle-history fact, not an account Fact, and
the pack's validator can only resolve upgrade ids today), and inventing that
vocabulary to express two rules - one of which is a proxy, see below - would
commit the pack to a schema before anyone knows what the third build needs.
The cost of the delay is named: an eligibility rule in Python is a rule no
operator can edit and no pack digest covers.

The table is validated at import the way builds.py validates its pack, and
for the same reason. A build in the registry with no rule here would not fail
loudly, it would be permanently unselectable - a recipe visible in the
browser and in the strategy validator that the planner silently never picks.
That is the silent-no-op hazard builds.py's docstring is about, so a new
build with no eligibility rule is an ImportError naming the build id.

Three-valued, and unknown blocks
----------------------------------------------------------
Predicates obey objectives.py's rule: True is provably eligible, False
provably not, None is "the facts needed to decide are absent" - and None
BLOCKS. An account nobody has measured eligibles nothing and lands on the
fallback, which is the same build today's line gives it (`(None or 0) >= 20`
is False), reached honestly instead of by treating an unread wave as a wave
of zero. The difference is visible in `Selection.chosen_by`: "fallback"
rather than "auto", so a dashboard can tell "we chose this" from "we could
not tell, and this is the floor".

The fit score is second, and today it is nearly idle
----------------------------------------------------------
The two committed rules partition: an account is under wave 20 or it is not,
so exactly one build is ever eligible and the score and the margin decide
nothing. They are here anyway because they are the part that stops the
generalisation from being a rewrite of the same `if`: the moment the pack
holds two builds a mid-game account could both run, the ranking and the
hysteresis are what keep it from oscillating between them, and a selector
that grew those parts only when first needed would grow them under pressure,
in the release that added the build.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Literal, Mapping

import builds
import upgrades
from account_state import AccountRevision

# Verbatim from fleet/reroll_planner.py's choose_next:
#     stage = "turtle" if (facts.best_tier_1_wave or 0) >= 20 else "opening"
# 20 is the only number in the rule this module replaces, and it is recorded
# here rather than imported because fleet/ may not be imported from this file
# (see the module docstring). A copied constant whose origin is written down
# is the smaller hazard; a copied constant whose origin is forgotten is how
# two thresholds drift apart, and nobody notices until the planner and the
# selector disagree about what stage an account is in.
TURTLE_WAVE_THRESHOLD = 20

# The build with no prerequisites, and the floor when nothing is eligible.
# Generic damage and attack speed are never wrong, merely never optimal.
FALLBACK_BUILD_ID = "opening"

# How far ahead a challenger must be before it is worth a respec, as a
# fraction of a build's own weighted set (see fit_score for the scale).
#
# Ten points is chosen against the two numbers that bound it. Below it: one
# purchase. A single weighted row moves a score by its own weight over the
# build total, and the cheapest rows in the committed pack are 3 of 97.5
# (~3 points) and 3 of 60 (5 points), so a margin under ~0.05 lets one
# incidental buy - a wave record nudging one stat - flip the build and spend
# coins undoing itself. Above it: half the build. By ~0.2 an account that has
# visibly grown into another build is still held on the old one, which is the
# opposite failure and the more expensive one, because it is invisible. Ten
# points asks for two or three rows of real progress and no more.
#
# A judgement, not a measurement - `rule_verified` is false on every build in
# the pack for the same honest reason. A caller with an account that thrashes
# anyway passes a bigger margin; nothing here needs to change.
DEFAULT_MARGIN = .1

ChosenBy = Literal["operator", "auto", "incumbent", "fallback"]


@dataclass(frozen=True)
class Selection:
    """One build decision, with the working shown.

    `scores` and `eligible` are not debug output. The decision a dashboard
    has to explain to an operator is "why did it stop using the build I
    watched it use yesterday", and the only answer that settles it is the
    other builds' numbers next to the winner's. Carrying them costs one dict
    on a pure call and saves reconstructing the decision from a log line.

    `unresolved` is kept apart from "ineligible" because the two are
    different instructions. Ineligible means the account does not qualify;
    unresolved means nobody has looked, and the fix is to go and read the
    account, not to buy anything.
    """

    build_id: str
    # Which rung of the ladder decided. "operator" is an explicit human
    # choice, "auto" a change the selector made on fit, "incumbent" the
    # hysteresis holding what was already in place, "fallback" the floor
    # taken because nothing could be shown to be eligible.
    chosen_by: ChosenBy
    # One sentence, for a person. Register of reroll_planner._FOCUS: says
    # what this means for the account, not which branch was taken.
    reason: str
    # Read-only rather than a plain dict: a frozen dataclass holding a dict
    # is frozen in name only, and a caller that edited the scores it was
    # handed would be editing the record of a decision already made.
    scores: Mapping[str, float]
    eligible: frozenset[str]
    unresolved: frozenset[str]


# -- reading the account ---------------------------------------------------
# The smallest reading that counts as "this row has been bought into", used
# where the build declares no target of its own. Compared through
# upgrades.target_reached rather than with a `>` written here, so the
# comparison follows the upgrade's own direction: Shockwave Frequency and
# Wall Rebuild are displayed as intervals and improve DOWNWARD, and a
# hand-written greater-than would read their progress as none, forever.
#
# For an upward upgrade this says "displays more than nothing". For a
# downward one it is unsatisfiable by construction, which is the honest
# answer rather than an oversight: without a target, no number separates an
# improved interval from the base value the account started with, and
# crediting one would invent sunk progress that was never paid for. Neither
# downward upgrade appears in a committed build today; the day one does, that
# build should declare a target for it, and the branch below uses it.
_ANY_PROGRESS = 1e-9


def _values(revision: AccountRevision) -> dict[str, object]:
    """The readable account, flattened to {concept_id: displayed value}.

    `workshop_stats` is the section that matters: it is the only one a live
    account actually carries (objectives.py's docstring says so outright -
    every other section is null on the real database), and it holds DISPLAYED
    VALUES, not levels - `stats.health` is 100.0, not "bought twice".

    `workshop_levels` is deliberately NOT read. It is declared on
    AccountRevision and nothing in the repo writes it, so a score that
    preferred it would be zero on every real account while passing any test
    that populated the field by hand - a selector that works only in its own
    tests. If a producer is ever added, reading it becomes a deliberate edit
    here, not an accident.

    `unlocks` is read even though nothing writes it yet, because it is the
    section objectives.py already reads for the tier spine and the one place
    an `unlocks.*` Fact would land. Today that means a build's unlock rows
    can never be credited - the Workshop observation drops rows with no
    numeric value (account_state._facts), and an unlock tile has none - so
    those weights only ever count in the denominator, equally for every
    build. Reading the section now means the score picks them up the day the
    Unlocks page gets a producer, with no edit here.

    None sections (never read) and () sections (read, and empty) collapse
    together on purpose, unlike objectives._facts: this feeds a ranking, not
    a gate, and an unread account scoring zero everywhere is the correct
    answer for a ranking. The None-vs-empty distinction that must not
    collapse is eligibility's, and that is made in the predicates below,
    where it blocks.
    """
    merged: dict[str, object] = {}
    for section in (revision.unlocks, revision.workshop_stats):
        for fact in section or ():
            merged[fact.concept_id] = fact.value
    return merged


def _credited(upgrade: upgrades.Upgrade, value: object,
              target: float | None) -> bool:
    """Has the account bought into this row?

    Bought, not "reached its target": sunk progress is the thing being
    measured, and a Thorns at 12 of the pack's target of 51 is coins already
    spent. The target is consulted as a SECOND way to say yes, never as a
    floor the row must clear, because `targets` exists to stop buying - using
    it as the test would score a mostly-bought row as untouched, which is the
    opposite of what this function is for.

    Two questions, either of which credits the row: has the account moved off
    nothing (the upward case), and has it reached the number the build names
    (the only statement available for a DOWNWARD upgrade, whose displayed
    interval starts positive and gets smaller - see _ANY_PROGRESS). Both go
    through upgrades.target_reached, so neither comparison is written here in
    a direction that would be wrong for half the catalog.

    A bool is an unlock Fact and is checked first, because bool is an int in
    Python and True would otherwise be compared as the quantity 1. A
    non-numeric value is an unparsed reading, not a quantity, and scores zero
    rather than being guessed at; so does a nan, which target_reached
    rejects for us.
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)):
        return False
    if target is not None and upgrades.target_reached(upgrade.id, float(value), target):
        return True
    return upgrades.target_reached(upgrade.id, float(value), _ANY_PROGRESS)


def fit_score(build: builds.Build, revision: AccountRevision) -> float:
    """How much of `build`'s weighted set the account already has, in [0, 1].

    Sunk progress is real value here, unlike in most planning problems: the
    upgrades a build wants are bought with coins that a respec does not give
    back, so an account two thirds of the way into a build is two thirds of a
    build richer than one that is not, and a selector blind to that will
    happily spend the difference to move sideways.

    Normalised by the build's OWN total weight - a fraction of what this
    build asks for, not a sum of weights. Raw summed weight would rank by
    build size: `opening` weights twelve rows totalling 97.5 and `turtle`
    seven totalling 60, so on any account opening could win by being longer,
    which measures the pack and not the account. The fraction also makes one
    DEFAULT_MARGIN mean the same thing for every build, now and for whatever
    the pack holds later.

    Credit per row is binary. Finer would mean comparing displayed values
    against a scale the pack does not carry: a Damage of 27 and a Health of
    100 are not the same amount of anything, and the one scale it does carry
    (`targets`) exists to STOP buying, so using it as a denominator would
    score a mostly-bought Thorns as barely started.

    Known bias, named rather than hidden: the readable evidence is the
    displayed Workshop value, and the always-visible base stats read nonzero
    before anything is bought, so this credits `damage`, `attack_speed` and
    `health` on an untouched account - at most (12+11+3)/97.5 of `opening`
    and 3/60 of `turtle`. It inflates the early build on early accounts,
    which is where the early build belongs, so the bias points in the
    harmless direction; it is still a bias, and it is what a declarative
    "satisfied when" rule in the pack would fix.
    """
    values = _values(revision)
    total = sum(weight for _, weight in build.weights)
    if total <= 0:
        # Unreachable against a validated pack (builds.py rejects a build
        # with no weights, and every weight must be > 0). Guarded anyway
        # because the alternative is a ZeroDivisionError from a scoring
        # function, which would take down a decision that has a perfectly
        # good answer available.
        return 0.
    earned = 0.
    for upgrade_id, weight in build.weights:
        upgrade = upgrades.by_id(upgrade_id)
        if upgrade is None:
            # Also unreachable against a validated pack, for the same reason
            # builds.py checks it at import: an id the catalog does not have
            # has no concept_id to look up, so it can only score zero.
            continue
        if _credited(upgrade, values.get(upgrade.concept_id),
                     build.targets.get(upgrade_id)):
            earned += weight
    return earned / total


# -- eligibility -----------------------------------------------------------
# A predicate takes the account and the best Tier 1 wave it has reached, and
# answers objectives.py's three values: True eligible, False not, None "the
# facts needed to decide are absent", which BLOCKS.
#
# best_tier_1_wave is a parameter rather than a field read off the revision
# because AccountRevision has no such field: the number is computed from
# battle history by fleet/reroll_metrics.py and reaches the planner on
# RerollFacts. Inventing a `stats.*` concept id for it here would be a string
# literal nothing resolves - the exact hazard objectives.py's "Introspectable
# catalog ids" section is about - so it is passed in, honestly, by the caller
# that already has it.
Eligibility = Callable[[AccountRevision, int | None], bool | None]


def _opening_eligible(revision: AccountRevision,
                      best_tier_1_wave: int | None) -> bool | None:
    """Below the turtle threshold - the reroll planner's `else` branch.

    False above it, not True: the planner's line is a partition, and a build
    that stayed eligible forever would sit in the ranking next to turtle on
    every grown account, carrying the base-stat bias fit_score names. The
    floor an account falls back to when nothing is eligible is a separate
    rule (FALLBACK_BUILD_ID), and keeping the two apart is what lets the
    fallback say "opening" while eligibility honestly says "not this one".
    """
    if best_tier_1_wave is None:
        return None
    return best_tier_1_wave < TURTLE_WAVE_THRESHOLD


def _turtle_eligible(revision: AccountRevision,
                     best_tier_1_wave: int | None) -> bool | None:
    """At or above Tier 1 wave 20.

    A proxy, and worth saying so: the real prerequisite for dropping raw
    attack and letting Thorns kill is that the account can afford and has
    unlocked the defense spine, and wave 20 is the observable that has stood
    in for it since the reroll planner was written. It is reproduced exactly
    rather than improved, because this phase generalises the mechanism and a
    better predicate is a change to the rule - one that should land with the
    declarative eligibility field and be visible as a pack edit.

    None on an unmeasured account, where the old line read `(None or 0)` and
    silently meant "wave zero". Same build results; this one can say why.
    """
    if best_tier_1_wave is None:
        return None
    return best_tier_1_wave >= TURTLE_WAVE_THRESHOLD


_ELIGIBILITY: Mapping[str, Eligibility] = MappingProxyType({
    "opening": _opening_eligible,
    "turtle": _turtle_eligible,
})


def _validate_table() -> None:
    """One rule per committed build, checked at import. See the docstring.

    A registry build with no rule would be permanently unselectable and a
    rule for a build the registry does not have would be dead code that reads
    like a supported option; both are quiet, and both are found here.
    """
    known = set(builds.ids())
    missing = sorted(known - set(_ELIGIBILITY))
    extra = sorted(set(_ELIGIBILITY) - known)
    if missing:
        raise ValueError(
            f"build_selection has no eligibility rule for {missing} - add one "
            "to _ELIGIBILITY, or the planner will never pick these builds")
    if extra:
        raise ValueError(
            f"build_selection has eligibility rules for unknown builds {extra} - "
            f"check knowledge/builds.v1.json, which declares {sorted(known)}")
    if FALLBACK_BUILD_ID not in known:
        raise ValueError(
            f"the fallback build {FALLBACK_BUILD_ID!r} is not in the pack - "
            "a fallback nothing resolves is a planner that ranks nothing")


_validate_table()


def eligibility(build_id: str, revision: AccountRevision, *,
                best_tier_1_wave: int | None = None) -> bool | None:
    """May this account run this build? True / False / None ("cannot tell").

    Raises ValueError on an unknown build id rather than answering False: a
    caller asking about a build that does not exist has a bug, and "no" is an
    answer it would believe.
    """
    rule = _ELIGIBILITY.get(build_id)
    if rule is None:
        raise ValueError(
            f"unknown build id {build_id!r} - the pack declares {list(builds.ids())}")
    return rule(revision, best_tier_1_wave)


# -- the ladder ------------------------------------------------------------
def _percent(fraction: float) -> str:
    return f"{round(fraction * 100)}%"


def select_build(revision: AccountRevision, *, requested: str | None = None,
                 incumbent: str | None = None, margin: float = DEFAULT_MARGIN,
                 best_tier_1_wave: int | None = None) -> Selection:
    """The build this account should run, and one sentence saying why.

    `requested` is `Strategy.build`: an operator naming a recipe. It wins
    untouched, eligible or not. Automatic selection is a default for a
    profile that has not chosen, never a correction of a human who has - a
    selector that overrode an operator "for their own good" would be
    unusable, because the one thing an operator does with a build field is
    pin a build the automation would not have picked.

    An unknown `requested` id RAISES. strategy.py already rejects unknown ids
    where they are set (ControlError, naming the field), so one arriving here
    did not come through a validated Strategy, and the two ways to treat it
    are both bad: falling through to auto-selection silently ignores an
    explicit human instruction and spends coins on a build the operator did
    not ask for, while answering with the unknown id hands the planner a
    recipe nothing resolves, which ranks nothing and buys nothing forever.
    ValueError rather than strategy.ControlError because this is a caller bug
    rather than user input - the user input was validated upstream - and
    because importing strategy for its exception type would drag config and
    the whole autopilot stack into a module whose purity is the point.

    `incumbent` is the build already in place. A challenger must BEAT it by
    `margin`, not tie it or edge it: without that, every wave record that
    nudges one score re-picks the build, and the account pays a respec to
    oscillate between two recipes it was making progress in.
    """
    # `not (margin >= 0)` rather than `margin < 0`, so that a nan margin is
    # rejected too: every comparison against nan is False, so a nan would not
    # raise and would not hold anything - it would silently disable the
    # hysteresis while looking like a configured one.
    if not (margin >= 0):
        raise ValueError(f"margin must be a non-negative number, got {margin!r}")

    registry = builds.REGISTRY.builds
    scores: Mapping[str, float] = MappingProxyType(
        {build.id: fit_score(build, revision) for build in registry})
    verdicts = {build.id: eligibility(build.id, revision,
                                      best_tier_1_wave=best_tier_1_wave)
                for build in registry}
    eligible = frozenset(i for i, v in verdicts.items() if v is True)
    unresolved = frozenset(i for i, v in verdicts.items() if v is None)

    def selection(build_id: str, chosen_by: ChosenBy, reason: str) -> Selection:
        return Selection(build_id, chosen_by, reason, scores, eligible, unresolved)

    if requested is not None:
        chosen = builds.by_id(requested)
        if chosen is None:
            raise ValueError(
                f"requested build {requested!r} is not in the pack, which declares "
                f"{list(builds.ids())} - strategy.py rejects unknown ids where they "
                "are set, so this one bypassed that validation")
        return selection(requested, "operator",
                         f"{chosen.name} is the operator's own choice, so automatic "
                         "selection stands aside.")

    # Pack order is the tiebreak, so two builds on an identical score resolve
    # the same way on every call and on every machine. Deliberately not the
    # id's alphabetical order, which would silently re-rank the pack the day
    # somebody renames a build.
    order = {build.id: position for position, build in enumerate(registry)}
    ranked = sorted(eligible, key=lambda i: (-scores[i], order[i]))

    if not ranked:
        fallback = builds.by_id(FALLBACK_BUILD_ID)
        assert fallback is not None  # _validate_table, at import
        return selection(
            FALLBACK_BUILD_ID, "fallback",
            f"Nothing can be shown to fit this account yet, so it falls back on "
            f"{fallback.name}: generic damage and attack speed are never wrong, "
            "merely never optimal.")

    leader = ranked[0]
    leader_build = builds.by_id(leader)
    assert leader_build is not None  # eligible ids come from the registry
    if incumbent is not None and incumbent in eligible:
        # An incumbent that is no longer eligible is not defended: it is not
        # that the challenger failed to earn the respec, it is that the
        # account may not run the old build at all any more.
        advantage = scores[leader] - scores[incumbent]
        if advantage <= margin:
            held = builds.by_id(incumbent)
            assert held is not None  # eligible ids come from the registry
            if leader == incumbent:
                return selection(
                    incumbent, "incumbent",
                    f"{held.name} is still the best fit this account can run, with "
                    f"{_percent(scores[incumbent])} of its upgrades already bought.")
            return selection(
                incumbent, "incumbent",
                f"Staying on {held.name}: {leader_build.name} is only "
                f"{_percent(advantage)} ahead, short of the {_percent(margin)} that "
                "would make a respec worth paying for.")

    if len(ranked) == 1:
        # Worth its own sentence rather than reporting a fit score nothing
        # was compared against: with the committed pack this is every auto
        # selection there is, and "Turtle fits best, 0% bought" would read
        # like a ranking that happened when no ranking happened.
        return selection(
            leader, "auto",
            f"{leader_build.name} is the only build this account qualifies for "
            "right now.")
    return selection(
        leader, "auto",
        f"{leader_build.name} fits this account best, with "
        f"{_percent(scores[leader])} of its upgrades already bought.")
