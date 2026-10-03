"""Card programs keep catalog identity and spending authority explicit."""

import pytest

from pydantic import ValidationError


def test_program_keeps_unowned_targets_and_order() -> None:
    from card_program import parse_program

    program = parse_program({
        "version": 1, "gem_cap": 400,
        "goals": [{"id": "economy", "kind": "acquire", "targets": [
            {"card_id": "cards.damage", "min_level": None}]}],
        "loadouts": [{"id": "farm", "name": "Farm", "priority": ["cards.damage"]}],
        "selected_loadout_id": "farm",
    })
    assert program.goals[0].targets[0].card_id == "cards.damage"
    assert program.loadouts[0].priority == ("cards.damage",)


def _program(**changes: object) -> dict[str, object]:
    raw: dict[str, object] = {"version": 1, "gem_cap": 400, "goals": [],
                              "loadouts": [], "selected_loadout_id": None}
    raw.update(changes)
    return raw


def test_catalog_accepts_sourced_normal_card_levels_and_rejects_above_max() -> None:
    from card_catalog import card_ids, max_level
    from card_program import parse_program

    assert "cards.damage" in card_ids()
    assert max_level("cards.damage") == 7
    assert parse_program(_program(goals=[{"id": "level", "kind": "acquire",
        "targets": [{"card_id": "cards.damage", "min_level": 7}]}])).goals[0].targets[0].min_level == 7
    with pytest.raises(ValueError, match="level"):
        parse_program(_program(goals=[{"id": "level", "kind": "acquire",
            "targets": [{"card_id": "cards.damage", "min_level": 8}]}]))


def test_slot_goal_uses_evidenced_gem_slot_limit() -> None:
    from card_program import parse_program

    goal = {"id": "slots", "kind": "slots", "capacity": 21,
            "when_usable_card": True}
    assert parse_program(_program(goals=[goal])).goals[0].capacity == 21
    with pytest.raises(ValueError, match="slot"):
        parse_program(_program(goals=[{**goal, "capacity": 22}]))


def test_card_goal_gem_block_has_readable_nonspending_projection() -> None:
    from fleet.resource_blocks import _gem_label, gem_automated, validate_gems

    blocks = validate_gems([{"id": "slot2", "type": "unlock_lab_slot", "slot": 2},
                            {"id": "cards.economy", "type": "card_goal",
                             "goal_id": "economy"}])
    assert _gem_label(blocks[1]) == "Card goal: economy"
    assert not gem_automated(blocks[1])


@pytest.mark.parametrize("change", [
    {"goals": [{"id": "x", "kind": "acquire", "targets": [{"card_id": "cards.fake"}]}]},
    {"goals": [{"id": "x", "kind": "acquire", "targets": [{"card_id": "cards.damage"}]},
               {"id": "x", "kind": "slots", "capacity": 2, "when_usable_card": False}]},
    {"loadouts": [{"id": "a", "name": "A", "priority": ["cards.damage", "cards.damage"]}]},
    {"selected_loadout_id": "missing"},
    {"gem_cap": True},
    {"bogus": 1},
])
def test_program_rejects_invalid_identity_and_schema(change: dict[str, object]) -> None:
    from card_program import parse_program

    with pytest.raises((ValueError, ValidationError)):
        parse_program(_program(**change))


def test_legacy_translation_keeps_order_and_does_not_change_input() -> None:
    from card_program import normalize_legacy_gems

    blocks = [{"id": "gems.slot2", "type": "unlock_lab_slot", "slot": 2},
              {"id": "gems.cards", "type": "buy_cards", "purpose": "until_cards",
               "cards": ["Damage", "Plasma Canon"]},
              {"id": "gems.slots", "type": "card_slots", "up_to": 3,
               "when_usable_card": True}]
    program, normalized = normalize_legacy_gems(blocks, None)
    assert program is not None
    assert [goal.id for goal in program.goals] == ["gems.cards", "gems.slots"]
    assert [target.card_id for target in program.goals[0].targets] == [
        "cards.damage", "cards.plasma-canon"]
    assert [block["type"] for block in normalized] == ["unlock_lab_slot", "card_goal", "card_goal"]
    assert blocks[1]["type"] == "buy_cards"


def test_legacy_mission_stays_visible_and_ambiguous_alias_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from card_program import normalize_legacy_gems
    import card_catalog
    from concepts import REGISTRY

    mission = {"id": "missions", "type": "buy_cards", "purpose": "card_missions"}
    assert normalize_legacy_gems([mission], None) == (None, [mission])
    rival = next(entry for entry in REGISTRY.concepts if entry.concept_id == "cards.health")
    ambiguous = replace(rival, aliases=(*rival.aliases, "Damage"))
    monkeypatch.setattr(card_catalog, "REGISTRY", replace(REGISTRY, concepts=tuple(
        ambiguous if entry is rival else entry for entry in REGISTRY.concepts)))
    with pytest.raises(ValueError, match="ambiguous|unknown"):
        normalize_legacy_gems([{"id": "legacy", "type": "buy_cards",
                               "purpose": "until_cards", "cards": ["Damage"]}], None)


def test_unknown_level_limit_preserves_unlock_but_rejects_level(monkeypatch: pytest.MonkeyPatch) -> None:
    import card_catalog
    from card_program import parse_program

    monkeypatch.setattr(card_catalog, "max_level", lambda card_id: None)
    target = {"card_id": "cards.damage"}
    assert parse_program(_program(goals=[{"id": "unlock", "kind": "acquire",
        "targets": [target]}])).goals[0].targets[0].min_level is None
    with pytest.raises(ValueError, match="unsupported"):
        parse_program(_program(goals=[{"id": "level", "kind": "acquire",
            "targets": [{**target, "min_level": 2}]}]))


def test_runtime_observation_keeps_battle_lock_separate_from_ownership() -> None:
    from card_models import CardItem, CardSnapshot
    from evidence_scope import FactScope

    item = CardItem(card_id="cards.enemy-balance", ownership="owned", level=2,
                    equipped=True, battle_locked=True, observed_at=1.0,
                    evidence_ref="capture-1")
    snapshot = CardSnapshot(scope=FactScope("account", "lease", "generation", 1),
                            revision=1, observed_at=1.0, visit_id="visit",
                            items=(item,), collection_complete=False,
                            equipment_complete=False)
    assert snapshot.items[0].ownership == "owned"
    assert snapshot.items[0].battle_locked is True
    assert snapshot.capacity is None
    with pytest.raises(ValidationError):
        CardItem(card_id="cards.enemy-balance", ownership="owned", copies=True,
                 observed_at=1.0, evidence_ref="capture-1")
    with pytest.raises(ValidationError):
        CardSnapshot(scope=snapshot.scope, revision=1, observed_at=1.0,
                     visit_id="visit", items=(item, item), collection_complete=False,
                     equipment_complete=False)


def test_plan_context_requires_explicit_execution_gates() -> None:
    from card_models import CardBudget, CardPlanContext, CardCapabilities
    from evidence_scope import FactScope
    from strategy import CardPolicy

    base = {"scope": FactScope("account", "lease", "generation", 1),
            "visit_id": "visit", "now": 1.0, "program_revision": "rev",
            "program": None, "policy": CardPolicy(), "snapshot": None,
            "budget": CardBudget(cycle_id="cycle", cap=0, spent=0, pending=0),
            "balance": None, "committed_gems": 0, "eligible_goal_ids": None,
            "capabilities": CardCapabilities(inventory=False, buy_one=False,
                buy_ten=False, buy_slot=False, assign=False)}
    with pytest.raises(ValidationError):
        CardPlanContext.model_validate(base)
    context = CardPlanContext.model_validate({**base, "armed": False,
        "paused": False, "in_run": False, "unresolved_operation": None,
        "cards_bought_this_visit": 0})
    assert context.eligible_goal_ids is None


def _command(kind: str, **changes: object) -> dict[str, object]:
    from evidence_scope import FactScope

    raw: dict[str, object] = {
        "idempotency_key": "request-1",
        "scope": FactScope("account", "lease", "generation", 1),
        "program_revision": "revision-1", "kind": kind, "quantity": 1,
        "source": "manual",
    }
    raw.update(changes)
    return raw


def test_command_cycle_required_only_for_paid_actions() -> None:
    from card_models import CardCommand

    assert CardCommand.model_validate(_command("refresh")).budget_cycle_id == ""
    assert CardCommand.model_validate(_command("clear", budget_cycle_id="")).budget_cycle_id == ""
    for kind in ("buy", "slot"):
        with pytest.raises(ValidationError, match="budget_cycle_id"):
            CardCommand.model_validate(_command(kind))
        with pytest.raises(ValidationError, match="budget_cycle_id"):
            CardCommand.model_validate(_command(kind, budget_cycle_id=""))
        assert CardCommand.model_validate(_command(kind, budget_cycle_id="cycle-1")).budget_cycle_id == "cycle-1"


@pytest.mark.parametrize(("kind", "quantity", "valid"), [
    ("buy", 1, True), ("buy", 10, True), ("buy", 2, False),
    ("buy", 11, False), ("slot", 1, True), ("slot", 10, False),
    ("refresh", 1, True), ("refresh", 10, False),
])
def test_command_quantity_matches_action(kind: str, quantity: int, valid: bool) -> None:
    from card_models import CardCommand

    raw = _command(kind, quantity=quantity, budget_cycle_id="cycle-1")
    if valid:
        assert CardCommand.model_validate(raw).quantity == quantity
    else:
        with pytest.raises(ValidationError, match="quantity"):
            CardCommand.model_validate(raw)


def test_apply_and_cancel_require_their_references() -> None:
    from card_models import CardCommand

    with pytest.raises(ValidationError, match="loadout_id"):
        CardCommand.model_validate(_command("apply"))
    with pytest.raises(ValidationError, match="target_operation_id"):
        CardCommand.model_validate(_command("cancel"))
    assert CardCommand.model_validate(_command("apply", loadout_id="farm")).loadout_id == "farm"
    assert CardCommand.model_validate(_command("cancel", target_operation_id="op-1")).target_operation_id == "op-1"


def test_operation_lifecycle_and_unknown_spend_are_explicit() -> None:
    from card_models import CardOperation

    command = _command("refresh")
    statuses = ("queued", "preflight", "dispatched", "verifying", "confirmed",
                "not_applied", "canceled", "blocked", "reconciliation_required")
    for status in statuses:
        operation = CardOperation(operation_id="op-1", command=command, status=status,
                                  created_at=1.0, updated_at=1.0)
        assert operation.spent_gems is None
    with pytest.raises(ValidationError, match="status"):
        CardOperation(operation_id="op-1", command=command, status="succeeded",
                      created_at=1.0, updated_at=1.0)
    assert CardOperation(operation_id="op-1", command=command, status="confirmed",
                         created_at=1.0, updated_at=1.0, spent_gems=0).spent_gems == 0
    with pytest.raises(ValidationError, match="spent_gems"):
        CardOperation(operation_id="op-1", command=command, status="confirmed",
                      created_at=1.0, updated_at=1.0, spent_gems=-1)
