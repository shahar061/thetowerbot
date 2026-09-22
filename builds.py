"""Named build recipes - an ordered upgrade preference list, as committed data.

A build says what to buy NEXT and in what order to prefer things; it never
says anything may be bought. The buyer still validates the account, screen,
row, balance, price and transaction, exactly as it did when these weights
lived inside fleet/reroll_planner.py. Keeping that boundary explicit matters
because a build is the one artefact in this repo a human will happily hand-
edit, and a hand-edited preference must not be able to widen permission.

Loaded once at import into a frozen tree and validated HARD, the contract
knowledge.py and catalog/concepts.v1.json both keep: a malformed pack fails
the import rather than degrading quietly. The specific degradation this
guards against is the reason the validation is not optional. A build whose
weights name an upgrade id the catalog does not have does not plan badly, it
plans NOTHING - the ranking loop skips every unknown id, and a build that
skips all of its ids returns "no supported next purchase" forever while
looking, from the outside, like a bot patiently saving coins. A typo must be
an import-time crash naming the offending id, not a bot that quietly does
nothing for a week.

Verification is recorded the way concepts.v1.json records it, two
independent booleans rather than a confidence number: definition_verified
("this build exists and is in use") is true for both entries here, because
they came out of a reroll planner that has been driving real accounts.
rule_verified ("we checked the rule") is FALSE for both, because nobody has
demonstrated these weights are optimal - or even good. They are what the
planner does, honestly labelled, and a later caller that wants to gate a
spend on more than "somebody wrote this down" has a field to read.

Imports upgrades and the standard library, nothing else. Deliberately NOT
knowledge.py, whose Validity and Source records this file's shapes echo:
importing it to share those dataclasses would drag concepts.py and the whole
wiki pack in behind them, and strategy.py - which the scan loop imports -
only wants to ask whether a build id exists. The duplication is four fields
and a status check; the coupling would be the entire knowledge tree.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import upgrades

PACK_PATH = Path(__file__).resolve().parent / "knowledge" / "builds.v1.json"

_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BUILD_ID = re.compile(r"[a-z][a-z0-9_]*")
_TOP_LEVEL = {"schema_version", "pack_version", "note", "build_sources",
              "prerequisites", "builds"}
_BUILD_FIELDS = {"id", "name", "note", "weights", "targets", "focus",
                 "source_refs", "source_url", "validity",
                 "definition_verified", "rule_verified"}
_PREREQUISITE_FIELDS = {"note", "source_refs", "source_url", "validity",
                        "definition_verified", "rule_verified", "requires"}
_VALIDITY_STATUSES = ("known", "unknown")
_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")


def _number(value: Any) -> float:
    """Coerce a JSON number to float, rejecting bool.

    bool is a subclass of int in Python, so an unguarded isinstance check
    lets `"weights": [["damage", true]]` through as a weight of 1.0 - a
    typo that silently reorders the whole build rather than failing.
    """
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


def _weight_pair(pair: Any) -> tuple[str, float]:
    """One `["upgrade_id", number]` row from the pack, as a validated pair.

    A JSON object would have been the obvious shape and is the wrong one:
    see Build.weights for why the order of these rows decides purchases.
    """
    if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not isinstance(pair[0], str):
        raise ValueError(f"a weight must be an [upgrade_id, number] pair, got {pair!r}")
    return pair[0], _number(pair[1])


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a nonempty string")
    return value


def _known_upgrade(upgrade_id: Any, where: str) -> str:
    """The single chokepoint every upgrade id in this pack passes through.

    One function rather than a check per field on purpose: weights, targets,
    focus and both halves of the prerequisite map are four different shapes
    that fail the same way, and a validator that covers three of them is the
    silent-no-op hazard in this module's docstring with extra steps.
    """
    if not isinstance(upgrade_id, str) or upgrades.by_id(upgrade_id) is None:
        raise ValueError(
            f"{where} names unknown upgrade id {upgrade_id!r} - "
            "check upgrades.CATALOG for the real spelling")
    return upgrade_id


@dataclass(frozen=True)
class Validity:
    """When a recorded build is known to hold. Mirrors knowledge.Validity.

    Tri-state rather than a pair of nullable versions: "unknown" is a real,
    common answer here (nobody has pinned these weights to a game version),
    and it is a different claim from "valid from the beginning of time".
    """

    status: str
    game_version_min: str | None
    game_version_max: str | None

    def _validate(self, where: str) -> None:
        bounds = (self.game_version_min, self.game_version_max)
        if self.status == "unknown":
            if any(v is not None for v in bounds):
                raise ValueError(f"{where}: unknown validity cannot declare version bounds")
        elif self.status == "known":
            if not any(v is not None for v in bounds):
                raise ValueError(f"{where}: known validity requires a version bound")
            for bound in bounds:
                if bound is not None and not _VERSION.fullmatch(bound):
                    raise ValueError(f"{where}: {bound!r} is not a game version")
        else:
            raise ValueError(f"{where}: validity status must be one of {_VALIDITY_STATUSES}")


@dataclass(frozen=True)
class Source:
    """Where a build came from. Mirrors knowledge.Source, including the digest.

    The digest is the point: these weights were copied out of a .py file that
    a later phase is expected to edit. When that file changes under us, the
    recorded sha256 stops matching and the copy is visibly stale rather than
    silently divergent.
    """

    id: str
    file: str
    sha256: str
    captured_at: str
    revision: str | None
    content_scope: str

    def _validate(self) -> None:
        for field_name in ("id", "file", "captured_at", "content_scope"):
            _text(getattr(self, field_name), f"build source {field_name}")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError(f"build source {self.id!r} requires a sha256 digest")
        if not _DATE.fullmatch(self.captured_at):
            raise ValueError(f"build source {self.id!r} requires a captured_at date")


@dataclass(frozen=True)
class Prerequisites:
    """Which upgrade must already be owned before another may be bought.

    Shared by every build and stored once, because it describes the game's
    own unlock graph rather than anyone's strategy: Thorns is unbuyable
    before Unlock Thorns on every build there will ever be. Holding a copy
    per build would let two builds disagree about the game itself, and the
    one that was wrong would skip a purchase forever without saying why.
    """

    note: str
    source_refs: tuple[str, ...]
    source_url: str | None
    validity: Validity
    definition_verified: bool
    rule_verified: bool
    # Read-only rather than a plain dict: a frozen dataclass holding a dict
    # is frozen in name only, and this one is a process-wide singleton - a
    # caller that did `prerequisites()["thorns"] = "damage"` would repoint
    # the map for every later reader, with no way to tell where it happened.
    requires: Mapping[str, str]

    def _validate(self) -> None:
        _text(self.note, "prerequisite note")
        for child, parent in self.requires.items():
            _known_upgrade(child, "prerequisites")
            _known_upgrade(parent, f"prerequisite of {child!r}")
            if child == parent:
                raise ValueError(f"prerequisite {child!r} requires itself")
        # A cycle is unreachable in the data today and is checked anyway: the
        # planner treats an unmet prerequisite as "skip this candidate", so a
        # cycle would not loop or crash, it would make every upgrade in the
        # cycle permanently unbuyable - the silent-no-op failure again, this
        # time spelled with two correct upgrade ids.
        for start in self.requires:
            seen = {start}
            node = self.requires[start]
            while node in self.requires:
                if node in seen:
                    raise ValueError(f"prerequisite cycle through {start!r}")
                seen.add(node)
                node = self.requires[node]


@dataclass(frozen=True)
class Build:
    """One named recipe: an ordered preference list over upgrade ids.

    `weights` is a tuple of pairs, not a mapping, because the ORDER is
    load-bearing. The ranking the planner runs is
    `weight / (1 + purchases)` with the position used as the tiebreak, so
    two upgrades on equal effective weight are separated by which one is
    written first. Python's dicts do preserve insertion order, but that
    would make a tiebreak depend on a language guarantee nobody reading this
    file would think to look for; a tuple of pairs says out loud that
    reordering the JSON changes what the bot buys.
    """

    id: str
    name: str
    note: str
    weights: tuple[tuple[str, float], ...]
    # Stop buying this upgrade once its displayed value reaches the target.
    # Read-only mappings for the same singleton-mutation reason as
    # Prerequisites.requires; order carries nothing here, so no tuples.
    targets: Mapping[str, float]
    focus: Mapping[str, str]
    source_refs: tuple[str, ...]
    source_url: str | None
    validity: Validity
    # "This build exists and is in use" and "we checked it is the right
    # thing to do" are separate claims, and for everything in this pack the
    # first is true and the second is not. See the module docstring.
    definition_verified: bool
    rule_verified: bool

    @property
    def upgrade_ids(self) -> tuple[str, ...]:
        """The weighted upgrade ids, in preference order."""
        return tuple(upgrade_id for upgrade_id, _ in self.weights)

    def weight_of(self, upgrade_id: str) -> float | None:
        return next((w for i, w in self.weights if i == upgrade_id), None)

    def _validate(self) -> None:
        if not _BUILD_ID.fullmatch(self.id):
            raise ValueError(f"build id {self.id!r} must be a lowercase slug")
        _text(self.name, f"build {self.id!r} name")
        _text(self.note, f"build {self.id!r} note")
        if not self.weights:
            raise ValueError(f"build {self.id!r} has no weights - an empty build buys nothing")
        seen: set[str] = set()
        for upgrade_id, weight in self.weights:
            _known_upgrade(upgrade_id, f"build {self.id!r} weights")
            if upgrade_id in seen:
                # Two rows for one upgrade means the second is dead: the
                # ranking reads whichever it reaches first and the other
                # never applies, so the file would claim a preference it
                # does not have.
                raise ValueError(f"build {self.id!r} weights {upgrade_id!r} twice")
            seen.add(upgrade_id)
            if weight <= 0:
                # Zero or negative would not merely deprioritise the row, it
                # would rank below every unbought upgrade forever; leave it
                # out of the build instead of writing an unreachable entry.
                raise ValueError(f"build {self.id!r} weights {upgrade_id!r} at {weight}, must be > 0")
        for upgrade_id, target in self.targets.items():
            _known_upgrade(upgrade_id, f"build {self.id!r} targets")
            if upgrade_id not in seen:
                # A target for an upgrade this build never buys is read by
                # nobody: the planner consults targets only for candidates
                # already in the weight list. Rejecting it keeps the file
                # from advertising a cap that does not act on anything.
                raise ValueError(f"build {self.id!r} targets unweighted {upgrade_id!r}")
            if target <= 0:
                raise ValueError(f"build {self.id!r} targets {upgrade_id!r} at {target}, must be > 0")
        for upgrade_id, focus in self.focus.items():
            _known_upgrade(upgrade_id, f"build {self.id!r} focus")
            if upgrade_id not in seen:
                raise ValueError(f"build {self.id!r} describes unweighted {upgrade_id!r}")
            _text(focus, f"build {self.id!r} focus for {upgrade_id!r}")
        for flag in (self.definition_verified, self.rule_verified):
            if type(flag) is not bool:
                raise ValueError(f"build {self.id!r} verification flags must be boolean")
        self.validity._validate(f"build {self.id!r}")


@dataclass(frozen=True)
class BuildPack:
    """Every committed build, plus the game structure they all share."""

    schema_version: int
    pack_version: str
    note: str
    sources: tuple[Source, ...]
    prerequisites: Prerequisites
    builds: tuple[Build, ...]

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> BuildPack:
        """Build the frozen tree, then validate it. Raises ValueError on anything odd.

        KeyError and TypeError are re-raised as ValueError so that every way
        a pack can be wrong - a missing key, a misspelled field name, a
        string where a number belongs - reaches the caller as one exception
        type with the offending detail in the message.
        """
        try:
            if set(raw) != _TOP_LEVEL:
                raise ValueError(
                    f"unexpected build pack fields: {sorted(set(raw) ^ _TOP_LEVEL)}")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("unsupported build pack schema version")
            sources = tuple(Source(**s) for s in raw["build_sources"])
            prerequisite_fields = dict(raw["prerequisites"])
            if set(prerequisite_fields) != _PREREQUISITE_FIELDS:
                raise ValueError(
                    f"unexpected prerequisite fields: "
                    f"{sorted(set(prerequisite_fields) ^ _PREREQUISITE_FIELDS)}")
            prerequisite_fields["validity"] = Validity(**prerequisite_fields["validity"])
            prerequisite_fields["source_refs"] = tuple(prerequisite_fields["source_refs"])
            prerequisite_fields["requires"] = MappingProxyType(
                dict(prerequisite_fields["requires"]))
            prerequisites = Prerequisites(**prerequisite_fields)
            # Not named `builds`: that is this module's own name, and a
            # local shadowing it inside its own loader is the kind of thing
            # that reads fine until somebody adds a module-level call here.
            parsed: list[Build] = []
            for item in raw["builds"]:
                fields = dict(item)
                if set(fields) != _BUILD_FIELDS:
                    raise ValueError(
                        f"unexpected fields on build {fields.get('id')!r}: "
                        f"{sorted(set(fields) ^ _BUILD_FIELDS)}")
                fields["validity"] = Validity(**fields["validity"])
                fields["source_refs"] = tuple(fields["source_refs"])
                fields["weights"] = tuple(_weight_pair(p) for p in fields["weights"])
                fields["targets"] = MappingProxyType(
                    {k: _number(v) for k, v in fields["targets"].items()})
                fields["focus"] = MappingProxyType(dict(fields["focus"]))
                parsed.append(Build(**fields))
            pack = cls(raw["schema_version"], _text(raw["pack_version"], "pack_version"),
                       _text(raw["note"], "pack note"), sources, prerequisites, tuple(parsed))
            pack._validate()
            return pack
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"malformed build pack: {exc}") from exc

    def _validate(self) -> None:
        source_ids = {s.id for s in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate build source id")
        for source in self.sources:
            source._validate()
        if not self.builds:
            raise ValueError("build pack declares no builds")
        if len({b.id for b in self.builds}) != len(self.builds):
            raise ValueError("duplicate build id")
        self.prerequisites._validate()
        for holder in (self.prerequisites, *self.builds):
            label = getattr(holder, "id", "prerequisites")
            if not holder.source_refs or not set(holder.source_refs) <= source_ids:
                raise ValueError(f"{label!r} cites unknown build source")
            if holder.source_url is not None and not isinstance(holder.source_url, str):
                raise ValueError(f"{label!r} source_url must be a string or null")
        for build in self.builds:
            build._validate()

    def by_id(self, build_id: str) -> Build | None:
        return next((b for b in self.builds if b.id == build_id), None)

    @classmethod
    def load(cls, path: Path) -> BuildPack:
        return cls.from_payload(json.loads(path.read_text(encoding="utf-8")))


REGISTRY: BuildPack = BuildPack.load(PACK_PATH)


def by_id(build_id: str) -> Build | None:
    """The committed build named `build_id`, or None if there is no such build.

    None rather than a raise: the callers that ask this are validating user
    input (strategy.py turns the None into a ControlError naming the field),
    and a control-flow exception across that boundary would be noise.
    """
    return REGISTRY.by_id(build_id)


def ids() -> tuple[str, ...]:
    """Every committed build id, for error messages and the browser."""
    return tuple(build.id for build in REGISTRY.builds)


def prerequisites() -> Mapping[str, str]:
    """The unlock graph every build shares: upgrade id -> the id it needs first.

    Read-only: see Prerequisites.requires for why handing out a mutable copy
    of a process-wide singleton is the wrong default.
    """
    return REGISTRY.prerequisites.requires
