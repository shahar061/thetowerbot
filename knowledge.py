"""Versioned game knowledge as committed data, with provenance and conflicts.

Loaded once at import into a frozen tree and validated hard: a malformed pack
fails the import rather than degrading quietly, the same contract
catalog/concepts.v1.json keeps. Facts here are game RULES; account_state.Fact
is an OBSERVATION of this account. They are different things and are
deliberately not the same type.

There is no numeric confidence field. Verification is two independent
booleans - definition_verified ("we inventoried it") and rule_verified ("we
checked the rule") - plus a tri-state validity window. Numeric confidence in
this codebase belongs to observations, where it is measured, not to catalog
knowledge, where it would be a vibe with a decimal point.

The load-bearing rule is authorises(): a fact tainted by an unresolved
conflict never authorises anything, however verified it claims to be. The
wiki contradicts itself in fourteen documented places, and a fact caught in
one of them is not a fact yet.

Two more rules a pack author must satisfy:

- a fact's `value` is frozen at load: a JSON array becomes a tuple (all the
  way down through nested arrays), never a mutable list sitting inside this
  otherwise-frozen record. See `_freeze`.
- a fact `value` shaped like <known concept id prefix>.<slug> - a single
  string, or every element of a list of strings - is checked against
  `concepts.REGISTRY.by_id` at load time, by default, with no annotation
  required. An id the catalog does not know is a hard, import-time failure
  naming both the offending id and the fact that carried it. This is
  deliberately default-ON, not opt-in: with roughly forty facts about to be
  written against this contract, a forgotten annotation must never be able
  to let a typo'd id through - the failure mode this phase cannot tolerate
  is a predicate that silently, permanently matches nothing. The rare fact
  whose value merely looks like a concept id opts OUT with
  `value_is_concept_ref: false`. This is this phase's governing rule - an
  unknown fact must make an objective blocked, never satisfied and never
  ready - applied to identifiers: a typo'd id is an unknown fact wearing a
  known fact's clothes.

  The "known id prefix" set is seeded from the id namespace itself - the
  substring before the first dot of every `Concept.concept_id` - not from
  `Concept.domain`. A concept's `domain` taxonomy field and its id's own
  namespace are validated independently in concepts.py and are not
  guaranteed to agree: `reference.*` ids exist whose `domain` field says
  something else entirely (`currencies`, `combat`, ...). Seeding from
  `domain` left that whole family permanently unvalidated - the identical
  silent-False hazard this correction exists to close, just one level down.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import concepts

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FACT_ID = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9_-]*)+")
_TOP_LEVEL = {"schema_version", "pack_version", "sources", "conflicts", "facts"}
_VALIDITY_STATUSES = ("known", "unknown")

# Seeded from the id namespace itself - the substring before the first dot
# of every Concept.concept_id - never from Concept.domain (a taxonomy field
# independent of the id, and blind to the catalog's `reference.*` ids) and
# never from GlossaryEntry.domain (a separately title-cased, differently
# grouped vocabulary, "Labs & Progression", that was never a candidate for
# an id prefix - it contains spaces and ampersands).
_CONCEPT_ID_PREFIXES = frozenset(c.concept_id.split(".", 1)[0] for c in concepts.REGISTRY.concepts)
_CONCEPT_REF_SHAPE = re.compile(
    r"(?:" + "|".join(re.escape(prefix) for prefix in sorted(_CONCEPT_ID_PREFIXES)) + r")\.[a-z0-9_-]+")


def _strings(value: Any) -> tuple[str, ...]:
    # Accepts a tuple as well as a list: a payload built from
    # dataclasses.asdict(some_fact) (as tests/test_knowledge.py's
    # _as_payload helper does) already has this field as a tuple, not the
    # JSON list a freshly loaded pack would hand in.
    if not isinstance(value, (list, tuple)) or any(not isinstance(s, str) or not s for s in value):
        raise ValueError("expected a list of nonempty strings")
    return tuple(value)


def _freeze(value: Any) -> Any:
    """Coerce a JSON array (and any array nested inside it) into a tuple.

    json.loads hands back a plain Python list for any JSON array. Without
    this, a frozen Fact_ would still hold a mutable value underneath its
    immutability - frozen in name only. concepts.py never hits this because
    every one of its list-shaped fields is a *known* field, run through
    _strings(); a knowledge fact's `value` is deliberately generic (a wave
    number, a multiplier, a priority-ordered list of concept ids, ...), so
    the coercion has to be generic too.
    """
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _looks_like_concept_ref(value: Any) -> bool:
    """True iff `value` is shaped like a concept reference: either a single
    string matching <known-id-prefix>.<slug>, or a nonempty tuple where
    every element does. Anything else (a number, a mixed list, an empty
    tuple, a string with an unknown or missing id prefix) is left alone - it
    was never a candidate for catalog validation in the first place.
    """
    if isinstance(value, str):
        return bool(_CONCEPT_REF_SHAPE.fullmatch(value))
    if isinstance(value, tuple) and value and all(isinstance(v, str) for v in value):
        return all(_CONCEPT_REF_SHAPE.fullmatch(v) for v in value)
    return False


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        raise ValueError("game version must have one to four numeric components")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * (4 - len(parts))


@dataclass(frozen=True)
class Source:
    id: str
    file: str
    sha256: str
    captured_at: str
    revision: str | None
    content_scope: str


@dataclass(frozen=True)
class Validity:
    status: str
    game_version_min: str | None
    game_version_max: str | None


@dataclass(frozen=True)
class Conflict:
    id: str
    statement: str
    tainted: tuple[str, ...]
    sources: tuple[str, ...]
    quote: str


@dataclass(frozen=True)
class Fact_:
    """A game RULE, not an observation. See account_state.Fact for the latter.

    `value_is_concept_ref` defaults to True: a `value` shaped like a real
    concept id prefix (see `_CONCEPT_REF_SHAPE`) is checked against
    `catalog/concepts.v1.json` automatically, no annotation required. Set it
    to False to opt a fact OUT - for the rare value that merely looks like a
    concept id but isn't one. See Correction B in this module's docstring.
    """
    id: str
    value: Any
    unit: str | None
    source_refs: tuple[str, ...]
    source_url: str | None
    definition_verified: bool
    rule_verified: bool
    validity: Validity
    value_is_concept_ref: bool = True


@dataclass(frozen=True)
class Pack:
    schema_version: int
    pack_version: str
    sources: tuple[Source, ...]
    conflicts: tuple[Conflict, ...]
    facts: tuple[Fact_, ...]

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> Pack:
        """Validate immutable metadata and references before exposing records."""
        try:
            if set(raw) != _TOP_LEVEL:
                raise ValueError("unexpected knowledge pack schema fields")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("unsupported knowledge pack schema version")
            if not isinstance(raw["pack_version"], str) or not raw["pack_version"]:
                raise ValueError("pack version is required")
            sources = tuple(Source(**s) for s in raw["sources"])
            facts = []
            for item in raw["facts"]:
                fields = dict(item)
                fields["validity"] = Validity(**fields["validity"])
                fields["source_refs"] = _strings(fields["source_refs"])
                fields["value"] = _freeze(fields["value"])
                facts.append(Fact_(**fields))
            conflicts = []
            for item in raw["conflicts"]:
                fields = dict(item)
                fields["tainted"] = _strings(fields["tainted"])
                fields["sources"] = _strings(fields["sources"])
                conflicts.append(Conflict(**fields))
            result = cls(raw["schema_version"], raw["pack_version"], sources,
                         tuple(conflicts), tuple(facts))
            result._validate()
            return result
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"malformed knowledge pack: {exc}") from exc

    def _validate(self) -> None:
        source_ids = {s.id for s in self.sources}
        fact_ids = {f.id for f in self.facts}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source id")
        if len(fact_ids) != len(self.facts):
            raise ValueError("duplicate fact id")
        if len({c.id for c in self.conflicts}) != len(self.conflicts):
            raise ValueError("duplicate conflict id")
        for source in self.sources:
            if not all(isinstance(v, str) and v for v in
                       (source.id, source.file, source.captured_at, source.content_scope)):
                raise ValueError("source snapshot metadata is required")
            if not _SHA256.fullmatch(source.sha256):
                raise ValueError("source snapshot requires a sha256 digest")
            if not _DATE.fullmatch(source.captured_at):
                raise ValueError("source snapshot requires a captured_at date")
        for fact in self.facts:
            if not _FACT_ID.fullmatch(fact.id):
                raise ValueError(f"fact id {fact.id!r} must be namespaced lowercase (domain.slug)")
            if not fact.source_refs or not set(fact.source_refs) <= source_ids:
                raise ValueError(f"fact {fact.id!r} cites unknown source")
            if type(fact.definition_verified) is not bool or type(fact.rule_verified) is not bool:
                raise ValueError(f"fact {fact.id!r} verification flags must be boolean")
            validity = fact.validity
            bounds = (validity.game_version_min, validity.game_version_max)
            if validity.status == "unknown":
                if any(v is not None for v in bounds):
                    raise ValueError(f"fact {fact.id!r}: unknown validity cannot declare version bounds")
            elif validity.status == "known":
                versions = [_version(v) for v in bounds if v is not None]
                if not versions or (len(versions) == 2 and versions[0] > versions[1]):
                    raise ValueError(f"fact {fact.id!r}: known validity requires a consistent version bound")
            else:
                raise ValueError(f"fact {fact.id!r} has an invalid validity status")
            if fact.value_is_concept_ref and _looks_like_concept_ref(fact.value):
                refs = fact.value if isinstance(fact.value, tuple) else (fact.value,)
                unknown = [r for r in refs if concepts.REGISTRY.by_id(r) is None]
                if unknown:
                    raise ValueError(
                        f"fact {fact.id!r} references unknown concept id(s) {unknown!r} - "
                        "check catalog/concepts.v1.json for the real spelling")
        for conflict in self.conflicts:
            if not all(isinstance(v, str) and v for v in (conflict.id, conflict.statement, conflict.quote)):
                raise ValueError("conflict metadata is required")
            if not conflict.tainted or not set(conflict.tainted) <= fact_ids:
                raise ValueError(f"conflict {conflict.id!r} taints unknown fact id")
            if not conflict.sources or not set(conflict.sources) <= source_ids:
                raise ValueError(f"conflict {conflict.id!r} cites unknown source")

    def by_id(self, fact_id: str) -> Fact_ | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def conflicts_for(self, fact_id: str) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if fact_id in c.tainted)

    def authorises(self, fact_id: str) -> bool:
        """The only gate a spend may ever consult.

        True iff the fact exists, rule_verified is True, and no conflict
        taints it. definition_verified alone (we inventoried the name) is
        never enough - only rule_verified (we checked the rule) counts.
        """
        fact = self.by_id(fact_id)
        if fact is None:
            return False
        return fact.rule_verified is True and not self.conflicts_for(fact_id)

    def payload(self) -> dict[str, Any]:
        """Return a detached copy for read-only consumers.

        concepts.py:225 does the same: a consumer that could mutate the
        singleton would make every later reader's answer depend on call
        order.
        """
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def load(cls, directory: Path) -> Pack:
        """Merge every `*.v1.json` pack file in `directory` and validate the union.

        Globbing rather than naming specific files is deliberate: this task
        commits only `tiers.v1.json`, and later tasks add more files to the
        same directory without ever touching this loader. Merging before
        validating is what lets a cross-file citation be legal - a conflict
        declared in one file may taint a fact declared in another.
        """
        paths = sorted(directory.glob("*.v1.json"))
        if not paths:
            raise ValueError(f"no knowledge pack files found in {directory}")
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        schema_versions = {p.get("schema_version") for p in payloads}
        pack_versions = {p.get("pack_version") for p in payloads}
        if len(schema_versions) != 1 or len(pack_versions) != 1:
            raise ValueError("knowledge pack files disagree on schema_version or pack_version")
        merged: dict[str, Any] = {
            # .get(), not [] - a pack file missing this key must still reach
            # from_payload()'s own validation (and its ValueError) below,
            # rather than raising a bare KeyError here.
            "schema_version": payloads[0].get("schema_version"),
            "pack_version": payloads[0].get("pack_version"),
            "sources": [s for p in payloads for s in p.get("sources", [])],
            "conflicts": [c for p in payloads for c in p.get("conflicts", [])],
            "facts": [f for p in payloads for f in p.get("facts", [])],
        }
        return cls.from_payload(merged)


KNOWLEDGE = Pack.load(KNOWLEDGE_DIR)
