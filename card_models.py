"""Strict card program and runtime contracts shared by standalone and fleet."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

import card_catalog
from evidence_scope import BalanceInterval, FactScope


Id = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^\S+$")]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=60)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              allow_inf_nan=False, validate_default=True,
                              arbitrary_types_allowed=True)


class CardTarget(_StrictModel):
    card_id: Id
    min_level: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_catalog(self) -> Self:
        if self.card_id not in card_catalog.card_ids():
            raise ValueError(f"unknown card id {self.card_id}")
        if self.min_level is not None:
            maximum = card_catalog.max_level(self.card_id)
            if maximum is None:
                raise ValueError(f"level targeting unsupported for {self.card_id}")
            if self.min_level > maximum:
                raise ValueError(f"level exceeds evidenced maximum for {self.card_id}")
        return self


class AcquireGoal(_StrictModel):
    id: Id
    kind: Literal["acquire"]
    targets: tuple[CardTarget, ...] = Field(min_length=1, max_length=20)

    @field_validator("targets", mode="before")
    @classmethod
    def tuple_targets(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def distinct_targets(self) -> Self:
        ids = [target.card_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate card target")
        return self


class SlotGoal(_StrictModel):
    id: Id
    kind: Literal["slots"]
    capacity: int = Field(ge=2)
    when_usable_card: bool = False

    @model_validator(mode="after")
    def supported_capacity(self) -> Self:
        if self.capacity > card_catalog.max_card_slots():
            raise ValueError("slot capacity exceeds evidenced maximum")
        return self


class CardLoadout(_StrictModel):
    id: Id
    name: DisplayName
    priority: tuple[Id, ...] = ()

    @field_validator("priority", mode="before")
    @classmethod
    def tuple_priority(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def valid_priority(self) -> Self:
        if len(self.priority) > len(card_catalog.card_ids()):
            raise ValueError("card priority exceeds catalog cardinality")
        if len(set(self.priority)) != len(self.priority):
            raise ValueError("duplicate card priority")
        if set(self.priority) - card_catalog.card_ids():
            raise ValueError("unknown card in priority")
        return self


class CardProgram(_StrictModel):
    version: Literal[1]
    goals: tuple[AcquireGoal | SlotGoal, ...] = Field(default=(), max_length=20)
    loadouts: tuple[CardLoadout, ...] = Field(default=(), max_length=20)
    selected_loadout_id: Id | None = None
    gem_cap: int = Field(ge=0)

    @field_validator("goals", "loadouts", mode="before")
    @classmethod
    def tuple_rows(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def unique_identities(self) -> Self:
        for label, rows in (("goal", self.goals), ("loadout", self.loadouts)):
            ids = [row.id for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")
        if self.selected_loadout_id is not None and self.selected_loadout_id not in {
                row.id for row in self.loadouts}:
            raise ValueError("selected loadout id does not exist")
        return self


CardObservationField = Literal["ownership", "level", "copies", "copies_needed", "maxed", "equipped", "battle_locked"]


class CardFieldEvidence(_StrictModel):
    """Authority belongs to the observed field, even in mixed-history items."""
    scope: FactScope | None = None
    visit_id: Id | None = None
    observed_at: float = Field(ge=0)
    evidence_ref: Id
    confidence: float | None = Field(default=None, ge=0, le=1)
    frame_digest: Id | None = None
    layout_id: Id | None = None


class CardItem(_StrictModel):
    card_id: Id
    ownership: Literal["owned", "unowned", "locked", "unavailable", "unknown"]
    level: int | None = Field(default=None, ge=1)
    copies: int | None = Field(default=None, ge=0)
    copies_needed: int | None = Field(default=None, ge=0)
    maxed: bool | None = None
    equipped: bool | None = None
    battle_locked: bool | None = None
    observed_at: float = Field(ge=0)
    evidence_ref: Id
    confidence: float | None = Field(default=None, ge=0, le=1)
    frame_digest: Id | None = None
    layout_id: Id | None = None
    field_evidence: dict[CardObservationField, CardFieldEvidence] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_card(self) -> Self:
        if self.card_id not in card_catalog.card_ids():
            raise ValueError("unknown card id")
        return self


class CardSnapshot(_StrictModel):
    scope: FactScope
    revision: int = Field(ge=0)
    observed_at: float = Field(ge=0)
    visit_id: Id
    confidence: float | None = Field(default=None, ge=0, le=1)
    frame_digest: Id | None = None
    layout_id: Id | None = None
    items: tuple[CardItem, ...] = ()
    capacity: int | None = Field(default=None, ge=0)
    equipped: tuple[Id, ...] | None = None
    collection_complete: bool
    equipment_complete: bool
    equipment_evidence: CardFieldEvidence | None = None

    @field_validator("items", "equipped", mode="before")
    @classmethod
    def tuple_observations(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def distinct_items(self) -> Self:
        if len({item.card_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate card observation")
        if self.equipped is not None and len(set(self.equipped)) != len(self.equipped):
            raise ValueError("duplicate equipped card")
        return self


class RewardItem(_StrictModel):
    position: int = Field(ge=0)
    card_id: Id
    quantity: int = Field(ge=1)
    level_before: int | None = Field(default=None, ge=1)
    level_after: int | None = Field(default=None, ge=1)
    copies_before: int | None = Field(default=None, ge=0)
    copies_after: int | None = Field(default=None, ge=0)


class CardCapabilities(_StrictModel):
    inventory: bool
    buy_one: bool
    buy_ten: bool
    buy_slot: bool
    assign: bool
    layout_id: Id | None = None
    reasons: dict[str, str] = Field(default_factory=dict)


class CardCommand(_StrictModel):
    idempotency_key: Id
    scope: FactScope
    program_revision: Id
    kind: Literal["refresh", "buy", "slot", "apply", "clear", "cancel"]
    quantity: int = Field(ge=1, le=10)
    loadout_id: Id | None = None
    target_operation_id: Id | None = None
    source: Literal["manual", "automatic"]
    goal_id: Id | None = None
    budget_cycle_id: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def action_bounds(self) -> Self:
        if self.budget_cycle_id and any(char.isspace() for char in self.budget_cycle_id):
            raise ValueError("budget_cycle_id must be a token")
        if self.kind in {"buy", "slot"} and not self.budget_cycle_id:
            raise ValueError("budget_cycle_id is required for paid actions")
        if self.quantity not in ((1, 10) if self.kind == "buy" else (1,)):
            raise ValueError("quantity is unsupported for this action")
        if self.kind == "apply" and self.loadout_id is None:
            raise ValueError("loadout_id is required for apply")
        if self.kind == "cancel" and self.target_operation_id is None:
            raise ValueError("target_operation_id is required for cancel")
        return self


class CardOperation(_StrictModel):
    operation_id: Id
    command: CardCommand
    status: Literal["queued", "preflight", "dispatched", "verifying", "confirmed",
                    "not_applied", "canceled", "blocked", "reconciliation_required"]
    reason: str | None = None
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)
    transaction_key: Id | None = None
    spent_gems: int | None = Field(default=None, ge=0)
    snapshot_before: CardSnapshot | None = None
    snapshot_after: CardSnapshot | None = None
    rewards: tuple[RewardItem, ...] = ()
    cancel_requested: bool = False

    @field_validator("rewards", mode="before")
    @classmethod
    def tuple_rewards(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def distinct_rewards(self) -> Self:
        if len({reward.position for reward in self.rewards}) != len(self.rewards):
            raise ValueError("duplicate reward position")
        return self


class CardBudget(_StrictModel):
    cycle_id: Id
    cap: int = Field(ge=0)
    spent: int = Field(ge=0)
    pending: int = Field(ge=0)

    @model_validator(mode="after")
    def within_cap(self) -> Self:
        if self.spent + self.pending > self.cap:
            raise ValueError("card budget exceeds cap")
        return self


class LoadoutResolution(_StrictModel):
    desired: tuple[Id, ...] | None = None
    missing: tuple[Id, ...] = ()
    reason: str | None = None

    @field_validator("desired", "missing", mode="before")
    @classmethod
    def tuple_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CardDecision(_StrictModel):
    kind: Literal["refresh", "buy", "slot", "apply", "wait", "done"]
    reason: str
    goal_id: Id | None = None
    loadout_id: Id | None = None
    quantity: int = Field(default=1, ge=1)
    desired: tuple[Id, ...] | None = None

    @field_validator("desired", mode="before")
    @classmethod
    def tuple_desired(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CardQuote(_StrictModel):
    """Observed total price for exactly one supported card action."""

    kind: Literal["buy", "slot"]
    quantity: int = Field(ge=1, le=10)
    price: int = Field(gt=0)
    capacity: int | None = Field(default=None, ge=0)
    scope: FactScope
    visit_id: Id
    observed_at: float = Field(ge=0)
    evidence_ref: Id
    layout_id: Id

    @model_validator(mode="after")
    def action_bounds(self) -> Self:
        if self.quantity not in ((1, 10) if self.kind == "buy" else (1,)):
            raise ValueError("unsupported quote quantity")
        if self.kind == "slot" and self.capacity is None:
            raise ValueError("slot quote requires source capacity")
        if self.kind == "buy" and self.capacity is not None:
            raise ValueError("buy quote has no source capacity")
        return self


class CardPlanContext(_StrictModel):
    """Runtime-only inputs; every authority gate must be passed explicitly."""

    scope: FactScope
    visit_id: Id
    now: float = Field(ge=0)
    program_revision: Id
    program: CardProgram | None
    policy: Any  # Existing strategy.CardPolicy; avoid a strategy import cycle.
    snapshot: CardSnapshot | None
    budget: CardBudget
    balance: BalanceInterval | None
    committed_gems: int = Field(ge=0)
    eligible_goal_ids: tuple[Id, ...] | None
    capabilities: CardCapabilities
    armed: bool
    paused: bool
    in_run: bool
    unresolved_operation: CardOperation | None
    cards_bought_this_visit: int = Field(ge=0)
    config_owner: Literal["local", "fleet", "studio", "unavailable"] = "unavailable"
    evidence_after: float | None = Field(default=None, ge=0)
    quotes: tuple[CardQuote, ...] = ()
    command: CardCommand | None = None

    @field_validator("eligible_goal_ids", "quotes", mode="before")
    @classmethod
    def tuple_eligible_goals(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def policy_type(self) -> Self:
        from strategy import CardPolicy
        if not isinstance(self.policy, CardPolicy):
            raise ValueError("policy must be CardPolicy")
        return self
