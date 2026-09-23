"""Build selection, as a ladder over pure inputs.

Every case below is reachable from a synthetic AccountRevision and an
integer, with no device, no database and no emulator - the same property
tests/test_objectives.py enforces for the objective graph, and for the same
reason: a selector that could only be exercised against a live account would
be a selector nobody dares change.

Two of these tests are load-bearing beyond their own assertions.
test_the_reroll_planners_wave_20_rule_still_holds pins the ONE behaviour this
module replaced - fleet/reroll_planner.py's
`stage = "turtle" if (facts.best_tier_1_wave or 0) >= 20 else "opening"` - so
that generalising the mechanism cannot quietly change the decision.
test_build_selection_imports_nothing_from_fleet pins the import direction
that is the whole architectural claim of the feature: the reroll planner is a
consumer of build selection, never its home.
"""
from __future__ import annotations

import dataclasses

import pytest

import build_selection
import builds
import upgrades
from account_state import AccountRevision, Evidence, Fact
from build_selection import DEFAULT_MARGIN, Selection, fit_score, select_build


def fact(concept_id: str, value: object) -> Fact:
    return Fact(concept_id, value, "verified", Evidence(
        1., .99, concept_id, str(value), (0, 0, 1, 1), 1080, 2400, "abc"))


def revision(**overrides: object) -> AccountRevision:
    return dataclasses.replace(AccountRevision(), **overrides)  # type: ignore[arg-type]


def bought(*upgrade_ids: str) -> AccountRevision:
    """An account whose Workshop page shows a value for each named upgrade.

    Reproduces the live shape: only `workshop_stats` is ever populated on the
    real database, keyed by the catalog concept_id, so a test that built its
    account out of any other section would be testing a revision the bot has
    never seen.
    """
    facts = []
    for upgrade_id in upgrade_ids:
        upgrade = upgrades.by_id(upgrade_id)
        assert upgrade is not None, upgrade_id
        facts.append(fact(upgrade.concept_id, 1.))
    return revision(workshop_stats=tuple(facts))


OPENING_TOTAL = sum(weight for _, weight in builds.REGISTRY.by_id("opening").weights)
TURTLE_TOTAL = sum(weight for _, weight in builds.REGISTRY.by_id("turtle").weights)


# -- the regression that matters most --------------------------------------
def test_the_reroll_planners_wave_20_rule_still_holds() -> None:
    """The single line this module generalises, pinned in both directions.

    fleet/reroll_planner.py: `stage = "turtle" if (facts.best_tier_1_wave or
    0) >= 20 else "opening"`. The threshold is read from this module's own
    constant rather than written as 20 here, so the test follows the constant
    if it is ever retuned and fails only if the RULE changes - which is what
    it is here to catch.
    """
    threshold = build_selection.TURTLE_WAVE_THRESHOLD
    assert threshold == 20, "the reroll planner's rule is wave 20"
    below = select_build(revision(), best_tier_1_wave=threshold - 1)
    at = select_build(revision(), best_tier_1_wave=threshold)
    above = select_build(revision(), best_tier_1_wave=threshold + 40)
    assert below.build_id == "opening"
    assert at.build_id == "turtle", "wave 20 itself is turtle, not opening"
    assert above.build_id == "turtle"


def test_an_unmeasured_wave_still_lands_on_opening() -> None:
    """The old line read `(facts.best_tier_1_wave or 0)`, so an account
    nobody had measured got the opening build by being treated as wave zero.
    Same build here, reached honestly: nothing is eligible, and the fallback
    is the floor. The difference is that this one can say which happened."""
    chosen = select_build(revision(), best_tier_1_wave=None)
    assert chosen.build_id == "opening"
    assert chosen.chosen_by == "fallback"


# -- rung 1: the operator override -----------------------------------------
def test_the_operator_override_wins_over_a_better_scoring_build() -> None:
    """Automatic selection is a default, never a correction of a human. The
    one thing an operator does with Strategy.build is pin a build the
    automation would not have picked, so overriding them would make the field
    useless."""
    account = bought("defense_absolute", "thorns", "health")
    auto = select_build(account, best_tier_1_wave=40)
    assert auto.build_id == "turtle"
    chosen = select_build(account, requested="opening", best_tier_1_wave=40)
    assert chosen.build_id == "opening"
    assert chosen.chosen_by == "operator"
    assert chosen.scores["turtle"] > chosen.scores["opening"], (
        "the override must be beating a build that actually scores higher, "
        "or this test proves nothing")


def test_the_operator_override_wins_even_when_the_build_is_ineligible() -> None:
    """Untouched means untouched: an operator who pins the opening build on a
    wave-60 account is making a decision, not a mistake the selector should
    repair. The working still shows opening as ineligible, so the browser can
    say so without the selector second-guessing the instruction."""
    chosen = select_build(revision(), requested="opening", best_tier_1_wave=60)
    assert (chosen.build_id, chosen.chosen_by) == ("opening", "operator")
    assert "opening" not in chosen.eligible


def test_an_unknown_requested_build_raises_rather_than_falling_through() -> None:
    """strategy.py rejects unknown ids where they are set, so one arriving
    here bypassed that validation. Falling through to auto-selection would
    silently ignore an explicit human instruction and spend coins on a build
    nobody asked for; the id is in the message so the caller can find it."""
    with pytest.raises(ValueError, match="glass_cannon"):
        select_build(revision(), requested="glass_cannon", best_tier_1_wave=10)


# -- rung 2: eligibility is a hard filter ----------------------------------
def test_an_ineligible_build_is_never_selected() -> None:
    """Turtle drops raw attack entirely. An account that cannot yet survive
    on Thorns must not be handed it however well it scores, so eligibility
    filters before anything is ranked rather than tie-breaking after."""
    account = bought("defense_absolute", "thorns", "unlock_thorns", "health")
    chosen = select_build(account, best_tier_1_wave=5)
    assert chosen.scores["turtle"] > chosen.scores["opening"]
    assert chosen.eligible == frozenset({"opening"})
    assert chosen.build_id == "opening"


def test_an_unmeasured_account_eligibles_nothing_rather_than_guessing() -> None:
    """objectives.py's rule, applied here: None is "the facts needed to
    decide are absent" and it BLOCKS. An unread account that eligibled
    everything would rank builds on evidence it does not have and pay a
    respec for the guess."""
    unmeasured = select_build(revision(), best_tier_1_wave=None)
    assert unmeasured.eligible == frozenset()
    assert unmeasured.unresolved == frozenset(builds.ids())
    assert build_selection.eligibility("turtle", revision()) is None
    assert build_selection.eligibility("turtle", revision(),
                                       best_tier_1_wave=0) is False
    assert build_selection.eligibility("turtle", revision(),
                                       best_tier_1_wave=20) is True


def test_unresolved_is_reported_apart_from_ineligible() -> None:
    """"Nobody has looked" and "the account does not qualify" are different
    instructions - the first is answered by reading the account, the second
    by playing - so a dashboard must be able to tell them apart."""
    measured = select_build(revision(), best_tier_1_wave=30)
    assert measured.unresolved == frozenset()
    assert measured.eligible == frozenset({"turtle"})


def test_every_committed_build_has_an_eligibility_rule() -> None:
    """The table is a placeholder for a declarative field in the pack, which
    makes it exactly the kind of thing a new build forgets to update. A build
    with no rule is not merely untested, it is permanently unselectable."""
    assert set(build_selection._ELIGIBILITY) == set(builds.ids())


def test_a_build_with_no_eligibility_rule_fails_at_import() -> None:
    """The guard above is enforced, not decorative: prove _validate_table
    actually raises, and names the offending build id the way builds.py names
    an offending upgrade id."""
    rules = dict(build_selection._ELIGIBILITY)
    rules.pop("turtle")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_selection, "_ELIGIBILITY", rules)
        with pytest.raises(ValueError, match="turtle"):
            build_selection._validate_table()


# -- rung 3: fit ------------------------------------------------------------
def test_the_fit_score_ranks_by_weighted_progress_not_by_row_count() -> None:
    """Sunk progress is real value here - respec costs coins - so a build the
    account is already most of the way into outranks one it has barely
    started. The weights are the build's own, so two rows worth 16 and 12
    count for more than four cheap ones."""
    defensive = fit_score(builds.by_id("turtle"), bought("defense_absolute", "thorns"))
    attacking = fit_score(builds.by_id("turtle"), bought("damage", "attack_speed"))
    assert defensive == pytest.approx((16 + 12) / TURTLE_TOTAL)
    assert attacking == 0., "turtle weights no raw attack at all"
    assert defensive > attacking


def test_the_fit_score_is_normalised_by_the_builds_own_total_weight() -> None:
    """A fraction of what this build asks for, not a sum of weights. Raw sums
    would rank by build SIZE: opening weights nine rows totalling 442 and
    turtle seven totalling 60, so the same three purchases are a fifth of one
    build and half the other - and a single margin could not mean the same
    thing for both. (Opening does not weight health, so it scores 42 + 40.)"""
    account = bought("defense_absolute", "thorns", "health")
    assert fit_score(builds.by_id("opening"), account) == pytest.approx(82 / OPENING_TOTAL)
    assert fit_score(builds.by_id("turtle"), account) == pytest.approx(31 / TURTLE_TOTAL)
    assert 0. <= fit_score(builds.by_id("opening"), account) <= 1.


def test_the_fit_score_reads_the_concept_ids_the_account_actually_carries() -> None:
    """Pinned with literal strings rather than through upgrades.by_id: the
    mapping from a build's flat upgrade id to a Fact's concept_id
    (`thorns` -> `stats.thorns`, `unlock_thorns` -> `unlocks.thorns`) is the
    join this whole score depends on, and a typo in it does not raise - it
    scores zero forever and looks like an account that has bought nothing."""
    account = revision(workshop_stats=(fact("stats.thorns", 12.),
                                       fact("unlocks.thorns", True)))
    assert fit_score(builds.by_id("turtle"), account) == pytest.approx(
        (12 + 9) / TURTLE_TOTAL)


def test_a_live_shaped_revision_scores_off_workshop_stats_alone() -> None:
    """The shape the bot actually sees. `workshop_stats` is the only section
    a live account carries, and it holds DISPLAYED VALUES - `stats.health` is
    100.0, not "bought twice" - so the score has to be readable off exactly
    this and nothing else, or it is zero on every real account."""
    live = revision(workshop_stats=(fact("stats.damage", 27.),
                                    fact("stats.attack_speed", 1.32),
                                    fact("stats.health", 100.),
                                    fact("stats.thorns", 12.)))
    assert fit_score(builds.by_id("opening"), live) == pytest.approx(
        (100 + 90 + 40) / OPENING_TOTAL)
    assert fit_score(builds.by_id("turtle"), live) == pytest.approx(
        (3 + 12) / TURTLE_TOTAL)


def test_the_score_ignores_the_never_written_workshop_levels_section() -> None:
    """`AccountRevision.workshop_levels` is declared and nothing in the repo
    writes it. A score that read it would be zero on every live account while
    passing any test that filled the field in by hand - working only in its
    own tests. If a producer is ever added, reading it must be a deliberate
    edit to _values, and this test is what makes that edit deliberate."""
    same_facts = (fact("stats.thorns", 12.), fact("stats.health", 100.))
    assert fit_score(builds.by_id("turtle"), revision(workshop_levels=same_facts)) == 0.
    assert fit_score(builds.by_id("turtle"), revision(workshop_stats=same_facts)) > 0.


def test_a_downward_upgrade_is_not_read_as_no_progress() -> None:
    """Shockwave Frequency and Wall Rebuild are displayed as intervals and
    improve DOWNWARD, so a hand-written `value > 0` credits them on an
    untouched account and a hand-written `value >= target` never credits them
    at all. Every comparison goes through upgrades.target_reached instead,
    which knows the direction; this pins that it is actually being used, for
    two upgrades no committed build weights yet."""
    downward = upgrades.by_id("shockwave_frequency")
    upward = upgrades.by_id("damage")
    assert build_selection._credited(downward, 2., 3.) is True, (
        "a reached downward target is progress")
    assert build_selection._credited(downward, 5., 3.) is False, (
        "an interval still above its target is not")
    assert build_selection._credited(downward, 5., None) is False, (
        "with no target, no number separates an improved interval from the "
        "base value, and inventing progress is worse than crediting none")
    assert build_selection._credited(upward, 27., None) is True


def test_a_target_the_account_has_not_reached_still_counts_as_progress() -> None:
    """`targets` exists to STOP buying, so it must not double as the test for
    whether anything was bought: a Thorns at 12 of the pack's 51 is coins
    already spent, and coins already spent are what the score measures."""
    thorns = upgrades.by_id("thorns")
    assert builds.by_id("turtle").targets["thorns"] == 51.
    assert build_selection._credited(thorns, 12., 51.) is True
    assert build_selection._credited(thorns, 51., 51.) is True


def test_an_unbought_row_scores_nothing() -> None:
    """A zero reading is a row the account has not bought into, not a row
    with a small amount of progress; a False unlock is the same statement."""
    account = revision(workshop_stats=(fact("stats.thorns", 0.),
                                       fact("unlocks.thorns", False),
                                       fact("stats.health", "unreadable")))
    assert fit_score(builds.by_id("turtle"), account) == 0.


# -- rung 4: hysteresis -----------------------------------------------------
@pytest.fixture
def both_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make opening and turtle eligible at once.

    The two committed rules partition - an account is under wave 20 or it is
    not - so with the shipped pack no two builds are ever eligible together
    and the margin is never consulted. That is a property of today's DATA,
    not of the ladder, and the hysteresis is precisely what must already work
    on the day a third build overlaps with a second. Patching the table is
    how that day is tested before it arrives.
    """
    rules = dict(build_selection._ELIGIBILITY)
    rules["opening"] = lambda revision, best_tier_1_wave: True
    monkeypatch.setattr(build_selection, "_ELIGIBILITY", rules)


def test_a_challenger_inside_the_margin_does_not_displace_the_incumbent(
        both_eligible: None) -> None:
    """The rule that stops thrashing. One cheap purchase moves turtle's score
    by 5 points and opening's by 3; without a margin that 2-point gap would
    re-pick the build, and the account would pay a respec to oscillate
    between two recipes it was making progress in."""
    account = bought("health")
    chosen = select_build(account, incumbent="opening", best_tier_1_wave=40)
    advantage = chosen.scores["turtle"] - chosen.scores["opening"]
    assert 0 < advantage < DEFAULT_MARGIN, "the challenger must genuinely lead"
    assert chosen.build_id == "opening"
    assert chosen.chosen_by == "incumbent"
    assert "respec" in chosen.reason


def test_a_challenger_beyond_the_margin_does_displace_the_incumbent(
        both_eligible: None) -> None:
    """Hysteresis is a brake, not a lock. Half of turtle's weighted set
    against a third of opening's is a real change in what the account looks
    like, and the respec is worth paying for."""
    account = bought("defense_absolute", "thorns", "health")
    chosen = select_build(account, incumbent="opening", best_tier_1_wave=40)
    assert chosen.scores["turtle"] - chosen.scores["opening"] > DEFAULT_MARGIN
    assert chosen.build_id == "turtle"
    assert chosen.chosen_by == "auto"


def test_an_exactly_tying_challenger_leaves_the_incumbent_in_place(
        both_eligible: None) -> None:
    """"Beat by the margin", not "reach it": a challenger that ties, or that
    lands exactly on the margin, has given no reason to spend anything."""
    tie = select_build(revision(), incumbent="turtle", margin=0.,
                       best_tier_1_wave=40)
    assert tie.scores["opening"] == tie.scores["turtle"]
    assert (tie.build_id, tie.chosen_by) == ("turtle", "incumbent"), (
        "even a zero margin needs a strict lead to move")
    # A challenger exactly ON the margin, to the last bit: 31 of turtle's 60
    # against 82 of opening's 442. `>` rather than `>=` is the difference
    # between this and a displacement, and it is a one-character edit.
    edge = select_build(bought("defense_absolute", "thorns", "health"),
                        incumbent="opening", best_tier_1_wave=40,
                        margin=31 / TURTLE_TOTAL - 82 / OPENING_TOTAL)
    assert (edge.build_id, edge.chosen_by) == ("opening", "incumbent")


def test_an_incumbent_that_is_no_longer_eligible_is_not_defended() -> None:
    """The margin answers "is the challenger worth a respec". It is the wrong
    question when the account may not run the incumbent at all any more, and
    a selector that asked it anyway would pin a grown account to the opening
    build forever."""
    chosen = select_build(revision(), incumbent="opening", best_tier_1_wave=40)
    assert (chosen.build_id, chosen.chosen_by) == ("turtle", "auto")


def test_an_unknown_incumbent_is_ignored_rather_than_defended() -> None:
    """A build id that resolves to nothing - a renamed or retired recipe in a
    saved profile - cannot be ranked, so it cannot be held. Ignored quietly
    here rather than raised: unlike `requested`, an incumbent is what the bot
    did last, not what a human asked for, and the answer to a stale one is to
    move on."""
    chosen = select_build(revision(), incumbent="glass_cannon", best_tier_1_wave=10)
    assert (chosen.build_id, chosen.chosen_by) == ("opening", "auto")


def test_a_negative_or_nan_margin_is_rejected() -> None:
    """A nan margin fails every comparison, so it would not hold anything -
    it would silently disable the hysteresis while looking configured."""
    for bad in (-.1, float("nan")):
        with pytest.raises(ValueError, match="margin"):
            select_build(revision(), margin=bad, best_tier_1_wave=10)


# -- rung 5: the fallback ---------------------------------------------------
def test_the_fallback_fires_when_nothing_is_eligible() -> None:
    """The floor, and the same build the reroll planner already falls back on
    before wave 20: generic damage and attack speed are never wrong, merely
    never optimal. Reported as "fallback" rather than "auto" so that "we
    chose this" stays distinguishable from "we could not tell"."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_selection, "_ELIGIBILITY",
                      {build_id: (lambda revision, wave: False)
                       for build_id in builds.ids()})
        chosen = select_build(revision(), best_tier_1_wave=40)
    assert chosen.build_id == build_selection.FALLBACK_BUILD_ID == "opening"
    assert chosen.chosen_by == "fallback"
    assert chosen.eligible == frozenset()


def test_the_fallback_build_has_no_prerequisites() -> None:
    """Why opening is the floor and not merely a default: the pack's shared
    unlock graph gates every other row behind something, and a fallback whose
    first purchase were gated would rank nothing on a fresh account."""
    opening = builds.by_id(build_selection.FALLBACK_BUILD_ID)
    requires = builds.prerequisites()
    assert any(upgrade_id not in requires for upgrade_id in opening.upgrade_ids)
    assert opening.upgrade_ids[0] not in requires


# -- the shape of an answer -------------------------------------------------
def test_every_path_through_the_ladder_gives_a_reason() -> None:
    """The reason is written for a person - it is what the browser shows an
    operator asking why the bot stopped using yesterday's build - so an empty
    one is a broken answer, not a cosmetic defect. All four rungs are
    exercised here, and the set of chosen_by values proves it."""
    account = bought("health")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_selection, "_ELIGIBILITY",
                      {**build_selection._ELIGIBILITY,
                       "opening": lambda revision, wave: True})
        held = select_build(account, incumbent="opening", best_tier_1_wave=40)
    every = [select_build(account, requested="turtle", best_tier_1_wave=1),
             select_build(account, best_tier_1_wave=40),
             held,
             select_build(account, best_tier_1_wave=None)]
    assert {chosen.chosen_by for chosen in every} == {
        "operator", "auto", "incumbent", "fallback"}
    for chosen in every:
        assert chosen.reason.strip(), chosen
        assert chosen.reason.endswith("."), chosen.reason
        assert chosen.build_id in builds.ids()


def test_the_selection_carries_a_score_for_every_build() -> None:
    """The working, not just the verdict: an operator asking why turtle lost
    needs turtle's number next to the winner's, and reconstructing it from a
    log line after the fact is how a dashboard ends up lying."""
    chosen = select_build(bought("health"), best_tier_1_wave=40)
    assert set(chosen.scores) == set(builds.ids())
    assert all(0. <= score <= 1. for score in chosen.scores.values())
    with pytest.raises(TypeError):
        chosen.scores["turtle"] = 1.  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        chosen.build_id = "turtle"  # type: ignore[misc]


# -- purity -----------------------------------------------------------------
def test_selection_is_deterministic() -> None:
    """Same inputs, same answer, including the tie-break between two builds
    on an identical score. An ordering that fell back on set iteration would
    pass a single run and re-pick the build on the next one - the exact
    thrash the margin exists to prevent, arriving by a different door."""
    account = bought("health", "damage")
    for wave in (None, 5, 20, 60):
        first = select_build(account, incumbent="opening", best_tier_1_wave=wave)
        second = select_build(account, incumbent="opening", best_tier_1_wave=wave)
        assert first == second, wave
        assert isinstance(first, Selection)


def test_ties_break_on_pack_order(both_eligible: None) -> None:
    """Two builds on zero progress tie. The tie-break is the order the pack
    declares, so it cannot change under a rename - and it is pinned here
    because "whatever the set iterated first" would also pass one run."""
    chosen = select_build(revision(), best_tier_1_wave=40)
    assert chosen.scores["opening"] == chosen.scores["turtle"] == 0.
    assert chosen.build_id == builds.ids()[0] == "opening"


def test_selecting_a_build_performs_no_io() -> None:
    """Purity enforced rather than trusted. builds.py reads its pack once at
    import, before any decision; a decision itself must not open a file or
    read a clock, or the whole ladder stops being testable offline."""
    import builtins
    import time

    touched: list[str] = []
    real_open, real_time = builtins.open, time.time
    builtins.open = lambda *a, **k: touched.append(str(a[:1])) or real_open(*a, **k)
    time.time = lambda: touched.append("time") or real_time()
    try:
        select_build(bought("health"), incumbent="opening", best_tier_1_wave=40)
    finally:
        builtins.open, time.time = real_open, real_time
    assert touched == []


def test_build_selection_imports_nothing_from_fleet() -> None:
    """The architectural claim of this whole phase, enforced over this
    module's own import statements (not its transitive closure -
    account_state legitimately imports the device-adjacent modules that back
    its field types). The reroll planner is a CONSUMER of build selection; an
    import the other way would make the general rule a detail of one fleet
    goal, after which the director, the advisor and the browser could only
    reach it through a fleet module.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(build_selection.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    assert "fleet" not in imported
    allowed = {"__future__", "dataclasses", "types", "typing",
               "account_state", "builds", "upgrades"}
    assert imported <= allowed, imported - allowed
    forbidden = {"fleet", "device", "shopping", "transactions", "director",
                 "strategy", "time", "os", "socket", "sqlite3", "db"}
    assert not (imported & forbidden), imported & forbidden
