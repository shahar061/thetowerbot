"""What each strategy block did, recorded for the Workshop plan graph."""

from __future__ import annotations

import pytest

from fleet.block_steps import BlockStep, StepLog, candidate_table, unlock_rows
from fleet.build_route_eval import DecisionTrace


def _program() -> list[dict]:
    return [{'id': 'a', 'type': 'unlock', 'upgrade_ids': ['unlock_orbs']},
            {'id': 'b', 'type': 'pool', 'upgrade_ids': ['health']},
            {'id': 'c', 'type': 'condition', 'field': 'best_tier_1_wave', 'op': 'gte', 'value': 20,
             'then': [{'id': 'c1', 'type': 'pool', 'upgrade_ids': ['damage'], 'label': 'Core'}],
             'else': []},
            {'id': 'd', 'type': 'wait'}]


def test_step_log_marks_done_skipped_matched_path_and_not_reached() -> None:
    rejected: list[str] = []
    log = StepLog(rejected)
    program = _program()
    log.push()
    log.enter(program[0]); log.done('Already unlocked')
    log.enter(program[1]); rejected.append('b: health over price cap')
    log.enter(program[2]); log.note('best_tier_1_wave 38 ≥ 20 → then')
    log.push(); log.enter(program[2]['then'][0]); log.pop()
    log.pop()
    steps = log.steps(program, 'c1')
    assert [(s.block_id, s.outcome, s.depth) for s in steps] == [
        ('a', 'done', 0), ('b', 'skipped', 0), ('c', 'matched', 0), ('c1', 'matched', 1),
        ('d', 'not_reached', 0)]
    assert steps[0].note == 'Already unlocked'
    assert steps[1].note == 'health over price cap'
    assert steps[2].note == 'best_tier_1_wave 38 ≥ 20 → then'
    assert steps[3].label == 'Core'


def test_rejections_that_only_say_finished_make_a_block_done() -> None:
    rejected: list[str] = []
    log = StepLog(rejected)
    program = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['orbs', 'lifesteal']}, {'id': 'w', 'type': 'wait'}]
    log.push()
    log.enter(program[0])
    rejected.extend(['p: orbs target reached', 'lifesteal: maxed'])
    log.enter(program[1])
    log.pop()
    steps = log.steps(program, 'w')
    assert (steps[0].outcome, steps[0].note) == ('done', 'lifesteal: maxed')
    assert steps[1].outcome == 'matched'


def test_a_goal_id_inside_save_for_matches_its_container() -> None:
    log = StepLog([])
    program = [{'id': 's', 'type': 'save_for', 'hold': True,
                'goal': [{'id': 's.goal', 'type': 'pool', 'upgrade_ids': ['health'], 'selection': 'value'}]}]
    log.push(); log.enter(program[0]); log.pop()
    assert [(s.block_id, s.outcome) for s in log.steps(program, 's.goal')] == [('s', 'matched')]


def test_nothing_matched_leaves_every_entered_block_passed() -> None:
    rejected: list[str] = []
    log = StepLog(rejected)
    program = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['health']}]
    log.push(); log.enter(program[0]); log.pop()
    (step,) = log.steps(program, 'blocks')
    assert step.outcome == 'skipped'
    assert step.note == 'No eligible upgrade (price, funds or filters)'


def test_weighted_table_has_odds_that_sum_to_one() -> None:
    rows = candidate_table({'damage': 8, 'attack_speed': 4}, 'weighted', 'damage', {'damage': 80, 'attack_speed': 81}.get)
    assert [(r.upgrade_id, r.name, r.category, r.price, r.chosen) for r in rows] == [
        ('damage', 'Damage', 'ATTACK', 80, True), ('attack_speed', 'Attack Speed', 'ATTACK', 81, False)]
    assert sum(r.odds for r in rows) == pytest.approx(1)
    assert rows[0].odds == pytest.approx(2 / 3)
    assert all(r.score is None for r in rows)


def test_value_table_sorts_by_coins_per_weight_point() -> None:
    prices = {'health': 4130, 'coins_per_kill_bonus': 5860, 'attack_speed': 6490}
    rows = candidate_table({'health': 14, 'coins_per_kill_bonus': 20, 'attack_speed': 12}, 'value',
                           'coins_per_kill_bonus', prices.get)
    assert [r.upgrade_id for r in rows] == ['coins_per_kill_bonus', 'health', 'attack_speed']
    assert rows[0].score == pytest.approx(293.0)
    assert rows[0].chosen and not rows[1].chosen
    assert all(r.odds is None for r in rows)


def test_priority_table_keeps_order_and_hides_weights() -> None:
    rows = candidate_table({'damage': 1, 'health': 1}, 'priority', 'health', {'damage': 5, 'health': 9}.get)
    assert [(r.upgrade_id, r.weight, r.odds, r.score, r.chosen) for r in rows] == [
        ('damage', None, None, None, False), ('health', None, None, None, True)]


def test_unknown_upgrade_id_keeps_its_id_as_name() -> None:
    (row,) = candidate_table({'not_an_upgrade': 1}, 'priority', None, lambda uid: None)
    assert (row.name, row.category) == ('not_an_upgrade', '')


def test_unlock_rows_use_group_names_in_ranked_order() -> None:
    group = type('Group', (), {'name': 'Unlock Orbs', 'category': 'DEFENSE'})()
    other = type('Group', (), {'name': 'Unlock Knockback', 'category': 'DEFENSE'})()
    rows = unlock_rows({'unlock_orbs': (group, 800), 'unlock_knockback': (other, 5000)},
                       ['unlock_orbs', 'unlock_knockback'], 'unlock_orbs')
    assert [(r.upgrade_id, r.name, r.price, r.chosen) for r in rows] == [
        ('unlock_orbs', 'Unlock Orbs', 800, True), ('unlock_knockback', 'Unlock Knockback', 5000, False)]


def test_decision_trace_new_fields_default_to_empty() -> None:
    trace = DecisionTrace('rule', 'reason')
    assert (trace.steps, trace.candidates, trace.selection, trace.draw_seed, trace.draw_roll) == ((), (), None, None, None)
    assert isinstance(BlockStep('x', None, 'wait', 'matched'), BlockStep)
