"""What the bot buys, how fast it scans, and when it stops.

The policy the scan loop reads, as a value object rather than a module of
constants. Frozen all the way down - a Strategy holds a tuple of frozen
ActionRules - which is what lets Controls.snapshot() hand one straight to
another thread without the defensive copying a mutable structure would need.

Range and structure checks run in __post_init__, so an out-of-range Strategy
cannot be constructed at all. Template existence is deliberately NOT checked
there: it touches the filesystem, and a value object should not do I/O to
know whether it is well formed. Template validation lives in the validated()
method instead, so that a disk-I/O check runs only when explicitly requested.

Imports config, the pure autopilot policy, the committed build pack and the
standard library. The scan loop depends on this module, so this module must
not depend on the web layer.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import builds
import config
from policy import AutopilotPolicy, PolicyError

# A floor because a zero or negative interval is a busy loop against ADB, and
# a ceiling because an hour between scans is indistinguishable from a hang.
MIN_INTERVAL = 0.1
MAX_INTERVAL = 3600.0

AFFORDABILITY = ("digits", "brightness")

# Fields a PATCH may set directly on a Strategy via merged(). `name` is
# absent on purpose: renaming a profile is the store's business (save under
# a new name), not a live edit to the running policy. `actions` is absent
# too - it is handled separately because replacing it means re-parsing a raw
# row list, not copying a scalar. `shopping` is absent for the same reason:
# entries here are copied raw into dataclasses.replace, which would put an
# unparsed patch dict straight into a typed field. `autopilot` follows the
# same strict nested-parser path.
PATCHABLE_FIELDS = (
    "affordability",
    "interval",
    "click_cooldown",
    "auto_navigate",
    "max_runs",
    "navigation_cooldown",
    "screen_confirmations",
    "tap_jitter_px",
    "timing_jitter",
    "tap_delay",
    "target_speed",
    "build",
)

# Longest a cooldown may be. Zero is legal - it means "no cooldown" - but a
# minute between two taps on one button is not a strategy, it is a typo.
MAX_COOLDOWN = 60.0

# Widest tap jitter radius, derived rather than chosen. The buy point sits
# at the centre of the price strip (config.buy_point), and that strip is the
# smallest thing the bot ever taps - every other tap target is a template
# centre on a crop at least 100px on its shortest side. A radius past half
# the strip's height can put the tap outside the button, where it buys
# nothing while the bot still publishes a Tapped event: a silent failure,
# not a visible one. Deriving the ceiling from PRICE_REGION means
# re-measuring it for a new resolution moves this with it.
MAX_TAP_JITTER_PX = config.PRICE_REGION.h / 2
# Beyond ±50% the configured interval stops describing the actual pace.
MAX_TIMING_JITTER = 0.5
# A two-second pause before each tap is already pathological; more than that
# is a hang, not a strategy.
MAX_TAP_DELAY = 2.0

# The debounce depth. 0 would defeat the screen tracker entirely; double
# figures would make a transition take most of a minute at a 2s interval.
MIN_CONFIRMATIONS = 1
MAX_CONFIRMATIONS = 10


class ControlError(ValueError):
    """A value the caller may not set.

    `field` names the offending key, so the browser can highlight the input
    that caused it. `code` names the *kind* of failure - "not_found",
    "conflict", or the default "invalid" - so an HTTP layer can map one to
    404, 409 or 422 without string-matching the message text. A message is
    for a human to read; a status must never depend on its wording.

    Defined here rather than in control.py because control.py will need to
    import this module for Strategy - the reverse import would be a cycle.
    """

    def __init__(self, field: str, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


def _in_range(field_name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ControlError(field_name, f"{field_name} must be between {low} and {high}")


# Dataclasses do not enforce their annotations, and this policy is fed
# arbitrary client JSON. `enabled="no"` is truthy, so without these tables a
# row the user switched OFF keeps being bought - silent wrong behaviour, not
# a validation nicety. One table per class, shared between __post_init__
# (which enforces it) and _offending_field (which reads it to name a field).
_RULE_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,),
    "template": (str,),
    "enabled": (bool,),
    "threshold": (int, float),
    "brightness_ratio": (int, float),
}

_STRATEGY_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,),
    "affordability": (str,),
    "interval": (int, float),
    "click_cooldown": (int, float),
    "navigation_cooldown": (int, float),
    "tap_jitter_px": (int, float),
    "timing_jitter": (int, float),
    "tap_delay": (int, float),
    "screen_confirmations": (int,),
    "auto_navigate": (bool,),
    "max_runs": (int,),
    "target_speed": (int, float),
    "build": (str,),
}

# Fields whose declared type includes None, so None is not a type error.
_OPTIONAL = frozenset({"max_runs", "target_speed", "target", "coin_budget",
                       "coin_budget_pct", "build"})


def _has_type(value: Any, types: tuple[type, ...]) -> bool:
    # bool is a subclass of int in Python, so an unguarded isinstance check
    # would let max_runs=True through as a run limit of 1.
    if bool not in types and isinstance(value, bool):
        return False
    return isinstance(value, types)


def _wrong_type(
    values: Mapping[str, Any], expected: Mapping[str, tuple[type, ...]]
) -> str | None:
    """The first key in `values` whose type contradicts `expected`, if any."""
    for key, types in expected.items():
        if key not in values:
            continue
        value = values[key]
        if value is None and key in _OPTIONAL:
            continue
        if not _has_type(value, types):
            return key
    return None


def _check_types(
    values: Mapping[str, Any], expected: Mapping[str, tuple[type, ...]]
) -> None:
    key = _wrong_type(values, expected)
    if key is not None:
        wanted = " or ".join(t.__name__ for t in expected[key])
        if key in _OPTIONAL:
            wanted += " or null"
        got = type(values[key]).__name__
        raise ControlError(key, f"{key} must be {wanted}, not {got}")


def _own_values(instance: Any) -> dict[str, Any]:
    """The instance's fields as a plain dict.

    Not dataclasses.asdict(): that deep-copies and recurses into nested
    dataclasses, and all these checks need is a shallow look at each field.
    """
    return {f.name: getattr(instance, f.name) for f in dataclasses.fields(instance)}


def _offending_field(values: Mapping[str, Any]) -> str:
    """Which key made the dataclass raise a bare TypeError.

    dataclasses report a type mismatch without saying which field, and the
    browser needs a field name to highlight the right input. Re-check each
    value against the annotation to find it; fall back to the whole object
    when nothing obvious is wrong.
    """
    return _wrong_type(values, _STRATEGY_TYPES) or "strategy"


@dataclass(frozen=True)
class ActionRule:
    """One upgrade the bot may buy, and how sure it must be before buying.

    Mirrors config.Action, plus `enabled`. The two stay separate because
    Action is what vision and affordability take, and ActionRule is what a
    human edits - as_action() is the whole adapter between them.
    """

    name: str
    template: str
    enabled: bool = True
    threshold: float = config.DEFAULT_THRESHOLD
    brightness_ratio: float = config.DEFAULT_BRIGHTNESS_RATIO

    def __post_init__(self) -> None:
        # Types before values: the emptiness and range checks below all
        # assume the field is already the type it claims to be.
        _check_types(_own_values(self), _RULE_TYPES)
        if not self.name:
            raise ControlError("name", "an action needs a name")
        if not self.template:
            raise ControlError("template", f"{self.name} needs a template file")
        # Exclusive at zero: a threshold of 0 matches literally anything, so
        # the bot would tap wherever the template happened to correlate best.
        if not 0.0 < self.threshold <= 1.0:
            raise ControlError("threshold", "threshold must be above 0 and at most 1")
        # Inclusive at zero, unlike threshold: config documents 0.0 as
        # "disable the brightness check", which is a real choice.
        _in_range("brightness_ratio", self.brightness_ratio, 0.0, 1.0)

    def as_action(self) -> config.Action:
        """The shape find_and_click_image() and affordable() already take."""
        return config.Action(
            name=self.name,
            template=self.template,
            threshold=self.threshold,
            brightness_ratio=self.brightness_ratio,
        )


def _parse_action_rows(raw: Any) -> tuple[ActionRule, ...]:
    """Turn a raw action list into ActionRules, naming what it got wrong.

    The one place that parses a client-supplied action row - used by both
    from_dict() (a whole document) and Strategy.merged() (a partial patch).
    Two independent per-row parsers is how "unknown field" ends up meaning
    two different things depending which endpoint you hit; this way there is
    only one meaning to keep straight.
    """
    if not isinstance(raw, (list, tuple)):
        raise ControlError(
            "actions", f"actions must be a list or tuple, not {type(raw).__name__!r}"
        )
    rule_fields = {f.name for f in dataclasses.fields(ActionRule)}
    rules: list[ActionRule] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ControlError(
                "actions",
                f"each action must be a mapping, not {type(entry).__name__!r}",
            )
        for key in entry:
            if key not in rule_fields:
                raise ControlError(key, f"unknown action field {key!r}")
        try:
            rules.append(ActionRule(**entry))
        except TypeError as exc:
            raise ControlError("actions", str(exc)) from None
    return tuple(rules)


CATEGORIES: tuple[str, ...] = ("ATTACK", "DEFENSE", "UTILITY")
CARD_BATCHES: tuple[str, ...] = ("x1", "x10")

MAX_TAPS_PER_VISIT = 200
MAX_CARDS_PER_VISIT = 50

_SHOPPING_RULE_TYPES: dict[str, tuple[type, ...]] = {
    "name": (str,), "category": (str,), "enabled": (bool,),
    "target": (int, float),
}

_CARD_TYPES: dict[str, tuple[type, ...]] = {
    "enabled": (bool,), "gem_floor": (int,),
    "max_per_visit": (int,), "batch": (str,),
}
_SHOPPING_TYPES: dict[str, tuple[type, ...]] = {
    "enabled": (bool,), "armed": (bool,),
    "visit_every_n_runs": (int,), "max_taps_per_visit": (int,),
    "coin_reserve": (int,), "coin_budget": (int,), "coin_budget_pct": (float, int),
    "allow_unlocks": (bool,),
}

_CLAIMS_TYPES: dict[str, tuple[type, ...]] = {
    "enabled": (bool,), "missions_every_hours": (int, float),
    "milestones_on_new_best": (bool,),
}

MIN_CLAIM_HOURS, MAX_CLAIM_HOURS = 0.1, 168.0


@dataclass(frozen=True)
class ShoppingRule:
    """One menu row the bot may buy.

    `name` is the identity: it is matched, normalised, against the row names
    OCR reads off the page (see shopping._row_named). Nothing else addresses
    a row any more.

    `category` is the one thing OCR cannot supply. A row on the DEFENSE tab
    is invisible until that tab is open, so category is what tells the bot
    which tabs to open and in what order - see
    Shopping.categories_in_priority_order().
    """

    name: str
    category: str
    enabled: bool = True
    target: float | None = None

    def __post_init__(self) -> None:
        _check_types(_own_values(self), _SHOPPING_RULE_TYPES)
        if not self.name:
            raise ControlError("name", "a shopping row needs a name")
        if self.category not in CATEGORIES:
            raise ControlError("category", f"category must be one of {CATEGORIES}")
        if self.target is not None and (
            not math.isfinite(self.target) or self.target < 0
        ):
            raise ControlError("target", "target must be finite and non-negative")


@dataclass(frozen=True)
class CardPolicy:
    """How many gems the bot may turn into cards, and where it must stop.

    `gem_floor` is inclusive-at-zero on purpose: spending down to nothing is
    a real choice, unlike a negative floor, which is not a choice at all.
    """

    enabled: bool = False
    gem_floor: int = 40
    max_per_visit: int = 2
    batch: str = "x1"

    def __post_init__(self) -> None:
        _check_types(_own_values(self), _CARD_TYPES)
        if self.gem_floor < 0:
            raise ControlError("gem_floor", "gem_floor may not be negative")
        _in_range("max_per_visit", self.max_per_visit, 1, MAX_CARDS_PER_VISIT)
        if self.batch not in CARD_BATCHES:
            raise ControlError("batch", f"batch must be one of {CARD_BATCHES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "gem_floor": self.gem_floor,
            "max_per_visit": self.max_per_visit, "batch": self.batch,
        }


@dataclass(frozen=True)
class Shopping:
    """The between-runs spending policy.

    `enabled` and `armed` are two switches, not one mode. enabled+unarmed is
    the rehearsal: navigate, read, decide, publish, tap nothing. `armed` is
    the only field in this whole config that stands between a miscalibrated
    template and a currency you cannot get back, so it is its own boolean
    rather than a value of something else.

    `workshop` order IS priority, matching Strategy.actions - and the order
    the CATEGORIES are visited in is derived from it rather than fixed, so
    reordering rows is the only control anyone needs.
    """

    enabled: bool = False
    armed: bool = False
    visit_every_n_runs: int = 1
    max_taps_per_visit: int = 40
    coin_reserve: int = 0
    coin_budget: int | None = 0
    # A share of the coins the visit opened with, applied on top of
    # coin_budget. The absolute figure alone cannot survive its own
    # purchases: every level bought raises the next price, so a constant
    # eventually sits under every row on the page and refuses the lot.
    coin_budget_pct: float | None = None
    allow_unlocks: bool = False
    workshop: tuple[ShoppingRule, ...] = ()
    cards: CardPolicy = CardPolicy()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workshop", tuple(self.workshop))
        _check_types(_own_values(self), _SHOPPING_TYPES)
        names = [rule.name for rule in self.workshop]
        if len(set(names)) != len(names):
            raise ControlError("workshop", "shopping row names must be unique")
        _in_range("visit_every_n_runs", self.visit_every_n_runs, 1, 100)
        _in_range("max_taps_per_visit", self.max_taps_per_visit, 1, MAX_TAPS_PER_VISIT)
        if self.coin_reserve < 0:
            raise ControlError("coin_reserve", "coin_reserve may not be negative")
        if self.coin_budget is not None and self.coin_budget < 0:
            raise ControlError("coin_budget", "coin_budget may not be negative")
        if self.coin_budget_pct is not None and not 0 < self.coin_budget_pct <= 1:
            raise ControlError(
                "coin_budget_pct",
                "coin_budget_pct must be a share above 0 and at most 1")

    def rows_for(self, category: str) -> tuple[ShoppingRule, ...]:
        """Enabled rows on one tab, still in priority order."""
        return tuple(
            rule for rule in self.workshop
            if rule.category == category and rule.enabled
        )

    def categories_in_priority_order(self) -> tuple[str, ...]:
        """Which tabs to visit, in the order the row list implies.

        Derived rather than fixed. Only one tab is readable at a time, so
        honouring a global priority order literally would mean re-checking
        every tab after every purchase - thrashing tabs and burning the tap
        budget on navigation. Visiting each tab once, in the order its
        highest-priority row appears, spends in the intended order and costs
        two tab taps.

        Tabs whose every row is disabled are skipped: visiting one is taps
        spent to read a page nothing will be bought from.
        """
        seen: list[str] = []
        for rule in self.workshop:
            if rule.enabled and rule.category not in seen:
                seen.append(rule.category)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "armed": self.armed,
            "visit_every_n_runs": self.visit_every_n_runs,
            "max_taps_per_visit": self.max_taps_per_visit,
            "coin_reserve": self.coin_reserve,
            "coin_budget": self.coin_budget,
            "coin_budget_pct": self.coin_budget_pct,
            "allow_unlocks": self.allow_unlocks,
            "workshop": [
                {
                    "name": r.name,
                    "category": r.category,
                    "enabled": r.enabled,
                    "target": r.target,
                }
                for r in self.workshop
            ],
            "cards": self.cards.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Shopping:
        """Parse a stored or posted shopping policy, naming what it got wrong.

        The one place a raw shopping dict is parsed, used by both
        Strategy.from_dict() and Strategy.merged() - the same rule
        _parse_action_rows follows, and for the same reason: two parsers is
        how "unknown field" comes to mean two different things.
        """
        if not isinstance(raw, Mapping):
            raise ControlError(
                "shopping", f"shopping must be a mapping, not {type(raw).__name__!r}"
            )
        known = {f.name for f in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise ControlError(key, f"unknown shopping field {key!r}")

        values = {k: v for k, v in raw.items() if k not in ("workshop", "cards")}
        if "workshop" in raw:
            values["workshop"] = _parse_shopping_rows(raw["workshop"])
        if "cards" in raw:
            cards = raw["cards"]
            if not isinstance(cards, Mapping):
                raise ControlError("cards", "cards must be a mapping")
            card_fields = {f.name for f in dataclasses.fields(CardPolicy)}
            for key in cards:
                if key not in card_fields:
                    raise ControlError(key, f"unknown cards field {key!r}")
            values["cards"] = CardPolicy(**cards)
        try:
            return cls(**values)
        except ControlError:
            raise
        except (TypeError, ValueError) as exc:
            raise ControlError("shopping", str(exc)) from None


@dataclass(frozen=True)
class Claims:
    """When to spend a menu trip on a free reward.

    `enabled` alone, with no separate `armed`: unlike a purchase, a claim
    cannot spend anything, and the walks already refuse to tap what they
    cannot read. A rehearsal mode here would rehearse nothing.

    `missions_every_hours` defaults to the game's own 8h reset. Bounded at
    both ends - under 6 minutes is a menu trip every few scans, over a week
    is a cadence that never fires on a bot nobody leaves running that long.
    """

    enabled: bool = False
    missions_every_hours: float = 8.0
    milestones_on_new_best: bool = True

    def __post_init__(self) -> None:
        # Types before values, for the same reason as in Shopping: _in_range
        # on a str raises a bare TypeError instead of naming the field.
        _check_types(_own_values(self), _CLAIMS_TYPES)
        if not MIN_CLAIM_HOURS <= self.missions_every_hours <= MAX_CLAIM_HOURS:
            raise ControlError(
                "missions_every_hours",
                f"must be between {MIN_CLAIM_HOURS} and {MAX_CLAIM_HOURS} hours",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "missions_every_hours": self.missions_every_hours,
            "milestones_on_new_best": self.milestones_on_new_best,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Claims:
        known = {f.name for f in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise ControlError(key, f"unknown claims field {key!r}")
        return cls(**raw)


def _parse_shopping_rows(raw: Any) -> tuple[ShoppingRule, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ControlError(
            "workshop", f"workshop must be a list, not {type(raw).__name__!r}"
        )
    rule_fields = {f.name for f in dataclasses.fields(ShoppingRule)}
    rules: list[ShoppingRule] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ControlError("workshop", "each shopping row must be a mapping")
        for key in entry:
            if key not in rule_fields:
                raise ControlError(key, f"unknown shopping row field {key!r}")
        try:
            rules.append(ShoppingRule(**entry))
        except TypeError as exc:
            raise ControlError("workshop", str(exc)) from None
    return tuple(rules)


# Added here rather than alongside _STRATEGY_TYPES's other entries: Shopping
# is defined after that dict, so a literal entry there would reference a name
# that does not exist yet.
_STRATEGY_TYPES["shopping"] = (Shopping,)
_STRATEGY_TYPES["autopilot"] = (AutopilotPolicy,)
_STRATEGY_TYPES["claims"] = (Claims,)


@dataclass(frozen=True)
class Strategy:
    """The whole decision policy, as one immutable value.

    `actions` order IS priority. A separate priority field would be a second
    way to say what the tuple already says, and the two would drift the first
    time a row was inserted.
    """

    name: str
    actions: tuple[ActionRule, ...]
    affordability: str = "digits"
    interval: float = config.SCAN_INTERVAL_SECONDS
    click_cooldown: float = config.CLICK_COOLDOWN_SECONDS
    auto_navigate: bool = False
    max_runs: int | None = None
    # Read by BotRunner when it builds the bot, not by the scan loop: both of
    # these configure a stateful collaborator (ScreenTracker's debounce depth,
    # Navigator's rate limit) that is constructed once and carries state
    # across scans. Changing them under a running tracker has no correct
    # answer, so the dashboard labels them "applies on next Start".
    navigation_cooldown: float = config.NAVIGATION_COOLDOWN_SECONDS
    screen_confirmations: int = config.SCREEN_CONFIRMATIONS
    # Read fresh every scan, unlike the two above: nothing holds state
    # across scans on their behalf, so an edit in the browser lands on the
    # very next tap without a restart.
    tap_jitter_px: float = config.TAP_JITTER_PX
    timing_jitter: float = config.TIMING_JITTER
    tap_delay: float = config.TAP_DELAY_SECONDS

    # Which in-battle game speed to hold, or None to leave the widget alone.
    # None rather than 1.0 as the default on purpose: a bot that has never
    # been told to manage the speed must not quietly tap a hand-set speed
    # back down the first time it enters a run.
    target_speed: float | None = None
    # Between-runs spending. Defaults to a policy that buys nothing, so a
    # strategy file written before this existed loads and behaves the same.
    shopping: Shopping = Shopping()
    # OCR-guided in-run spending is opt-in. An old profile has no key and
    # therefore receives this disabled value without changing legacy scans.
    autopilot: AutopilotPolicy = AutopilotPolicy()
    # Free-reward claim cadence. Off by default, so a profile written before
    # this existed loads and behaves identically.
    claims: Claims = Claims()

    # Which committed recipe in knowledge/builds.v1.json ranks the next
    # purchase, or None to name no build at all. None rather than a default
    # build id: naming one here would silently change what every strategy
    # file written before this existed says it wants, and "this profile has
    # not chosen a build" is a different answer from "this profile chose the
    # opening build". Only the id is stored - the weights live in the pack,
    # so a build that is retuned there does not need every saved profile
    # rewritten.
    build: str | None = None

    def __post_init__(self) -> None:
        # Normalise before validating: from_dict will hand in a list, and the
        # loop must never be given something a caller could append to.
        object.__setattr__(self, "actions", tuple(self.actions))

        # Types before values, for the same reason as in ActionRule: _in_range
        # on a str raises a bare TypeError, and "no" in `auto_navigate` is
        # truthy rather than wrong.
        _check_types(_own_values(self), _STRATEGY_TYPES)

        if not self.actions:
            raise ControlError("actions", "a strategy needs at least one action")
        names = [rule.name for rule in self.actions]
        if len(set(names)) != len(names):
            raise ControlError("actions", "action names must be unique")
        if self.affordability not in AFFORDABILITY:
            raise ControlError(
                "affordability", f"affordability must be one of {AFFORDABILITY}"
            )
        _in_range("interval", self.interval, MIN_INTERVAL, MAX_INTERVAL)
        _in_range("click_cooldown", self.click_cooldown, 0.0, MAX_COOLDOWN)
        _in_range("navigation_cooldown", self.navigation_cooldown, 0.0, MAX_COOLDOWN)
        _in_range("tap_jitter_px", self.tap_jitter_px, 0.0, MAX_TAP_JITTER_PX)
        _in_range("timing_jitter", self.timing_jitter, 0.0, MAX_TIMING_JITTER)
        _in_range("tap_delay", self.tap_delay, 0.0, MAX_TAP_DELAY)
        _in_range(
            "screen_confirmations",
            self.screen_confirmations,
            MIN_CONFIRMATIONS,
            MAX_CONFIRMATIONS,
        )
        if self.max_runs is not None and self.max_runs < 1:
            raise ControlError("max_runs", "max_runs must be null or at least 1")
        if self.target_speed is not None and self.target_speed not in config.TARGET_SPEEDS:
            # A membership check rather than a range: every legal value needs
            # a readout template to be recognised by, so a target between two
            # known speeds is one the bot could tap toward forever without
            # ever matching it. The list grows by harvesting templates, never
            # by widening a bound.
            #
            # TARGET_SPEEDS, not SPEED_VALUES: x0.0 is readable but not
            # targetable - see that constant's comment for why holding the
            # game stopped is a soft hang rather than a slow setting.
            raise ControlError(
                "target_speed",
                f"target_speed must be null or one of {list(config.TARGET_SPEEDS)}",
            )
        if self.build is not None and builds.by_id(self.build) is None:
            # Membership against the committed pack, not a free string: a
            # build id nothing resolves is a policy that ranks nothing, and
            # the bot would sit there buying nothing while the profile
            # claimed to have a plan. Fail where the value is set instead.
            raise ControlError(
                "build", f"build must be null or one of {list(builds.ids())}"
            )

    @classmethod
    def from_config(cls, name: str = "default") -> Strategy:
        """The shipped defaults, as a Strategy.

        Keeps config.ACTIONS and config.SHOPPING_ROWS meaningful: they stay
        the origin of the defaults - what a fresh clone starts from -
        without staying the source of truth.
        """
        return cls(
            name=name,
            actions=tuple(
                ActionRule(
                    name=action.name,
                    template=action.template,
                    threshold=action.threshold,
                    brightness_ratio=action.brightness_ratio,
                )
                for action in config.ACTIONS
            ),
            shopping=Shopping(
                workshop=tuple(
                    ShoppingRule(name=row_name, category=category, enabled=enabled)
                    for row_name, category, enabled in config.SHOPPING_ROWS
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped. What the store writes and the browser receives."""
        return {
            "name": self.name,
            "actions": [
                {
                    "name": rule.name,
                    "template": rule.template,
                    "enabled": rule.enabled,
                    "threshold": rule.threshold,
                    "brightness_ratio": rule.brightness_ratio,
                }
                for rule in self.actions
            ],
            "affordability": self.affordability,
            "interval": self.interval,
            "click_cooldown": self.click_cooldown,
            "auto_navigate": self.auto_navigate,
            "max_runs": self.max_runs,
            "navigation_cooldown": self.navigation_cooldown,
            "screen_confirmations": self.screen_confirmations,
            "tap_jitter_px": self.tap_jitter_px,
            "timing_jitter": self.timing_jitter,
            "tap_delay": self.tap_delay,
            "target_speed": self.target_speed,
            "build": self.build,
            "shopping": self.shopping.to_dict(),
            "autopilot": self.autopilot.to_dict(),
            "claims": self.claims.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Strategy:
        """Parse a stored or posted strategy, naming any field it gets wrong.

        Unknown keys are an error, not something to ignore: a typo in a
        hand-edited file is exactly the case where the file says one thing
        and the bot does another, which is what this design exists to stop.
        """
        known = {f.name for f in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise ControlError(key, f"unknown field {key!r}")
        for required in ("name", "actions"):
            if required not in raw:
                raise ControlError(required, f"{required} is required")

        rules = _parse_action_rows(raw["actions"])
        shopping = (
            Shopping.from_dict(raw["shopping"]) if "shopping" in raw else Shopping()
        )
        try:
            autopilot = (
                AutopilotPolicy.from_dict(raw["autopilot"])
                if "autopilot" in raw
                else AutopilotPolicy()
            )
        except PolicyError as exc:
            raise ControlError(exc.field, str(exc)) from None
        claims = Claims.from_dict(raw["claims"]) if "claims" in raw else Claims()

        values = {
            k: raw[k]
            for k in raw
            if k not in ("actions", "shopping", "autopilot", "claims")
        }
        try:
            return cls(
                actions=rules,
                shopping=shopping,
                autopilot=autopilot,
                claims=claims,
                **values,
            )
        except ControlError:
            raise
        except (TypeError, ValueError) as exc:
            # Anything that gets past __post_init__'s own checks and still
            # fails - a missing or duplicated argument, say - arrives as a
            # bare TypeError; find which field it was so the browser can
            # point at the right input rather than the whole form.
            raise ControlError(_offending_field(values), str(exc)) from None

    def merged(self, patch: Mapping[str, Any]) -> Strategy:
        """Validate `patch` against this Strategy and return the result.

        The validate-and-merge half of Controls.apply(): unlike from_dict(),
        which parses a whole document and rejects anything it does not
        recognise, this ignores keys outside PATCHABLE_FIELDS, "actions",
        "shopping" and "autopilot" rather than erroring on them -
        Controls.apply() feeds it
        session state (like `paused`) that this type deliberately does not
        own, and that is not a typo to reject, just a field for someone else.

        What patch DOES touch is still validated whole: the candidate goes
        through the same constructor - and so the same __post_init__ - as
        every other Strategy, so a bad field never reaches self. Reuses
        _parse_action_rows, Shopping.from_dict() and
        AutopilotPolicy.from_dict() rather than parsing nested values itself,
        so posted values mean the same thing here as in from_dict().
        """
        updates: dict[str, Any] = {
            key: patch[key] for key in PATCHABLE_FIELDS if key in patch
        }
        if "actions" in patch:
            updates["actions"] = _parse_action_rows(patch["actions"])
        if "shopping" in patch:
            updates["shopping"] = Shopping.from_dict(patch["shopping"])
        if "autopilot" in patch:
            try:
                updates["autopilot"] = AutopilotPolicy.from_dict(patch["autopilot"])
            except PolicyError as exc:
                raise ControlError(exc.field, str(exc)) from None
        if not updates:
            return self
        try:
            return dataclasses.replace(self, **updates)
        except TypeError as exc:
            # Belt and braces with __post_init__'s own checks: PATCHABLE_FIELDS
            # only ever names real fields, so this should be unreachable, but
            # a bare TypeError with no field name is worse than a defensive
            # catch that names one.
            raise ControlError(_offending_field(updates), str(exc)) from None

    def validated(self, template_dir: Path | None = None) -> Strategy:
        """Check what construction could not: that the templates are on disk.

        Returns self so it can be used inline - `store.save(s.validated())`.
        Disabled rows are checked too: a disabled row is one checkbox away
        from running, and letting it hold a broken template only moves the
        failure to whenever someone switches it on.

        A template arrives in the same client JSON as the profile name and is
        joined to a path just as the name is, so it gets the same suspicion:
        it must name a file INSIDE the template directory, not merely a file
        that exists. vision.py passes an absolute template straight to
        cv2.imread, so an unchecked one lets the request body choose which
        file on disk the bot reads.
        """
        root = template_dir if template_dir is not None else config.TEMPLATE_DIR

        def _check_template(name: str, template: str) -> None:
            # Belt and braces with the rule's own type check: this runs on
            # whatever self holds, and frozen is not the same as unreachable.
            if not isinstance(template, str):
                raise ControlError("template", f"{name}: template must be a filename")
            full = root / template
            # Containment, not a no-separators rule: config.NAV_TARGETS uses
            # names like "nav/claim.png", so a legitimate template may live in
            # a subdirectory. What must never be legitimate is leaving the
            # directory - an absolute path (which Path.__truediv__ silently
            # takes whole, discarding root) or a "../" walk out of it.
            inside = full.resolve().is_relative_to(root.resolve())
            if Path(template).is_absolute() or not inside:
                raise ControlError("template", f"{name}: template must be inside {root}")
            if not full.is_file():
                raise ControlError(
                    "template", f"{name}: no template file {template!r} in {root}"
                )

        for rule in self.actions:
            _check_template(rule.name, rule.template)
        return self


# A profile name becomes a filename, and arrives off a URL. Anything outside
# this set is refused before it is ever joined to a path.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_name(name: str) -> str:
    """Return `name` if it is safe to turn into a filename, else raise.

    Refusing outright rather than resolving-and-checking: the set of names
    worth having is small and obvious, and a rule you can read in one line
    has nowhere for a traversal to hide.
    """
    # fullmatch, not match: `$` matches at end-of-string OR just before a
    # trailing "\n", and .match() does not require consuming the rest of the
    # string either way - "mine\n" would slip through as an "accepted" name
    # and land in a filename with a literal newline in it.
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ControlError(
            "name",
            "a strategy name must be 1-64 characters of letters, digits, "
            "'-' or '_'",
        )
    return name


class StrategyStore:
    """Named strategies as JSON files in one directory.

    No caching. A profile is read when it is asked for, so a file edited by
    hand while the dashboard is open is picked up on the next load rather
    than at the next restart.
    """

    # Hidden, and with no .json suffix, so it can neither collide with a
    # profile named "active" nor be picked up by names()'s glob.
    _ACTIVE = ".active"

    def __init__(self, directory: Path | None = None) -> None:
        # Not bound as a default argument: that would evaluate
        # config.STRATEGY_DIR once at import time, and a test that
        # monkeypatches the constant afterwards would never be seen.
        self.directory = (
            directory if directory is not None else config.STRATEGY_DIR
        )

    def path_for(self, name: str) -> Path:
        return self.directory / f"{validate_name(name)}.json"

    def names(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        # Sorted so the browser's list is stable between polls, and so two
        # listings compare equal when nothing changed. glob("*.json") never
        # matches the .active pointer file, so it is excluded without a
        # special case.
        #
        # Filtered through load()'s own rule so the listing and the loader
        # agree: a hand-placed "my strategy.json" is a valid glob hit whose
        # stem validate_name refuses, and offering a name that cannot then be
        # opened is worse than omitting a file nobody could have opened.
        found: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                found.append(validate_name(path.stem))
            except ControlError:
                continue
        return sorted(found)

    def load(self, name: str) -> Strategy:
        path = self.path_for(name)
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raise ControlError(
                "name", f"no strategy named {name!r}", "not_found"
            ) from None
        except json.JSONDecodeError as exc:
            raise ControlError("name", f"{name}.json is not valid JSON: {exc}") from None
        return Strategy.from_dict(raw)

    def save(self, strategy: Strategy) -> None:
        """Validate, then write atomically.

        Temp file in the SAME directory, then os.replace: replace is atomic
        within a filesystem, and a same-directory temp file is what
        guarantees there is only one filesystem involved. What this
        guarantees is that a reader of `name.json` always sees a complete
        document - either the previous save's or this one's. It does not
        order concurrent writers: two saves of the same profile still race,
        and the last replace wins whole.

        No lock, because the writers that matter are not in one process: the
        bot runs alongside the web server, so a threading.Lock would guard
        the half of the problem that is already the smaller half. Making
        every writer's temp path unique is what keeps the race to "one of
        the two documents wins" instead of "the two interleave into one".
        """
        strategy.validated()
        # Redundant with path_for()'s own call below, but deliberately kept:
        # this one runs before mkdir, so an invalid name fails before it can
        # create the strategies directory as a side effect.
        validate_name(strategy.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(strategy.name)
        # Unique per writer, not merely per target: a later stage saves on
        # every settings change, so two saves of the SAME profile are the
        # expected case. Sharing one temp path would let their write_text
        # calls interleave into a single spliced file, and leave whichever
        # writer replaced second with a FileNotFoundError from the other's
        # finally clause. The .gitignore pattern "strategies/.*.tmp" still
        # matches this name.
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            # Indented and newline-terminated: these files are meant to be
            # read in a diff and edited by hand.
            tmp.write_text(json.dumps(strategy.to_dict(), indent=2) + "\n")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    @property
    def _active_path(self) -> Path:
        return self.directory / self._ACTIVE

    def active_name(self) -> str:
        """Which profile the bot loads. Falls back rather than failing.

        A pointer can outlive its target - someone deletes a file by hand -
        and a bot that refuses to start because of a stale one-line file is
        worse than one that picks a surviving profile and says so.
        """
        try:
            name = self._active_path.read_text().strip()
        except FileNotFoundError:
            name = ""
        if name and self.exists(name):
            return name
        remaining = self.names()
        return remaining[0] if remaining else "default"

    def exists(self, name: str) -> bool:
        """True if `name` is both safe and on disk."""
        try:
            return self.path_for(name).is_file()
        except ControlError:
            return False

    def set_active(self, name: str) -> None:
        # No mkdir: exists(name) can only be true if self.directory already
        # holds name.json, so the directory is guaranteed to exist.
        if not self.exists(name):
            raise ControlError("name", f"no strategy named {name!r}", "not_found")
        self._active_path.write_text(f"{name}\n")

    def delete(self, name: str) -> None:
        """Remove a profile, refusing the two states with no recovery.

        Order matters: the last-profile guard runs before the active-profile
        guard. A lone profile is necessarily the active one, so checking
        active-first would make the last-profile branch unreachable - the
        "last strategy" message would never fire, and a test written to
        cover it would exercise the active guard instead without saying so.
        """
        if not self.exists(name):
            raise ControlError("name", f"no strategy named {name!r}", "not_found")
        if len(self.names()) <= 1:
            raise ControlError(
                "name", "the last strategy cannot be deleted", "conflict"
            )
        if name == self.active_name():
            raise ControlError(
                "name",
                f"{name!r} is active - activate another strategy first",
                "conflict",
            )
        self.path_for(name).unlink()

    def ensure_seeded(self) -> Strategy:
        """Guarantee a loadable active profile, and return it.

        Called once at startup. Writes default.json from config.ACTIONS only
        when the directory holds nothing - seeding on every launch would
        silently reset a tuned profile back to the shipped defaults, which is
        the worst version of this bug to find late.
        """
        if not self.names():
            self.save(Strategy.from_config("default"))

        # active_name() checks that the pointer's target exists, not that it
        # parses, so its answer can still be a corrupt file - and a bot that
        # will not start because one profile was hand-edited badly is the
        # same failure the pointer fallback above exists to avoid. Try the
        # pointer first, then every surviving profile, and take the first
        # that loads. dict.fromkeys keeps that order without retrying the
        # pointer's own profile a second time.
        tried: list[str] = []
        for name in dict.fromkeys([self.active_name(), *self.names()]):
            tried.append(name)
            try:
                strategy = self.load(name)
            except ControlError:
                continue
            # Whatever was fallen back to, write it down so the next reader
            # agrees with this one rather than falling back again.
            current = ""
            if self._active_path.is_file():
                current = self._active_path.read_text().strip()
            if current != name:
                self.set_active(name)
            return strategy

        # Nothing on disk parses. Raising beats quietly seeding over the top:
        # a silent reseed is indistinguishable from a tuned profile having
        # been reset, and it would overwrite the very file whose contents the
        # owner still needs in order to repair it.
        raise ControlError(
            "name",
            f"no loadable strategy in {self.directory} (tried {', '.join(tried)})",
            "not_found",
        )
