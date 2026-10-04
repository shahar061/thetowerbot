from pathlib import Path
from dataclasses import replace
from typing import Any
from battle_reader import BattleReader

import cv2
import pytest

import config
import ocr
from perception import parse_frame
from tests.test_perception import recorded


def test_targeted_reader_uses_fresh_target_and_reconciles_changed_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures/in_run_utility.png'))
    boxes = recorded('in_run_utility')
    observation = parse_frame(frame, boxes, 'battle')
    row = next(r for r in observation.rows if r.upgrade_id == 'cash_bonus')
    reader = BattleReader()
    calls = []
    def full(self: ocr.FrameReads) -> tuple[ocr.TextBox, ...]:
        calls.append('full')
        return boxes
    monkeypatch.setattr(ocr.FrameReads, '_read_battle', full)
    first = ocr.FrameReads(frame)
    reader.read(first, target='cash_bonus', quote=None, scope=(1,))
    assert calls == ['full']
    # Replay crop coordinates from recorded boxes: no old numeric boxes may
    # leak into a new frame through the stable label cache.
    regions = []
    def crop(screen: Any, rect: config.Rect, **kwargs: Any) -> tuple[ocr.TextBox, ...]:
        regions.append(rect)
        return tuple(replace(b, rect=config.Rect(b.rect.x - rect.x + 20,
                     b.rect.y - rect.y + 20, b.rect.w, b.rect.h)) for b in boxes
                     if rect.x <= b.rect.x < rect.x + rect.w and rect.y <= b.rect.y < rect.y + rect.h)
    monkeypatch.setattr(ocr, 'read_region', crop)
    quote = dict(verified=True, wave=observation.combat['wave'])
    second = ocr.FrameReads(frame.copy())
    result = reader.read(second, target='cash_bonus', quote=quote, scope=(1,))
    current = parse_frame(second.screen, result, 'battle')
    target = next(r for r in current.rows if r.upgrade_id == 'cash_bonus')
    assert target.value == row.value
    assert second.battle_targeted
    assert target.price is None, (quote, current.combat)
    assert second.battle_targeted
    assert calls == ['full']
    assert all(rect.h < config.BATTLE_BANDS[(1080, 2400)].panel.h for rect in regions)
    changed = frame.copy()
    label = next(b for b in boxes if b.text.lower().replace(' ', '').startswith('cashbonus'))
    changed[label.rect.y:label.rect.y+label.rect.h,label.rect.x:label.rect.x+label.rect.w] = 0
    reader.read(ocr.FrameReads(changed), target='cash_bonus', quote=quote, scope=(1,))
    assert calls == ['full', 'full']


@pytest.mark.parametrize('fixture,uid,expected', [
    ('in_run_utility','cash_bonus',1), ('in_run_defense','health',10),
    ('in_run_defense_1920','health',33), ('in_run_damage_single_digit','damage',9),
])
def test_real_target_value_crop(fixture: str, uid: str, expected: float) -> None:
    frame = cv2.imread(str(Path(__file__).parent / f'fixtures/{fixture}.png'))
    boxes = recorded(fixture) if (Path(__file__).parent / f'fixtures/ocr/{fixture}.json').exists() else ocr.read(frame)
    observation = parse_frame(frame, boxes, 'battle')
    row = next(r for r in observation.rows if r.upgrade_id == uid)
    value, status = BattleReader.target_regions(row.rect)
    from perception import stat_number
    values = [stat_number(b.text) for b in ocr.read_region(frame, value, upscale=False) if b.confidence >= .9]
    assert expected in values


def test_shared_preflight_calls_targeted_reader_once_and_keeps_backstop(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from types import SimpleNamespace
    import screens
    bot = bot_in_run_on('in_run_lit')
    calls = []
    boxes = recorded('in_run_lit')
    reads = ocr.FrameReads(bot._screen, battle_reader=lambda current: (calls.append(current), boxes)[1])
    bot._battle_full_read_at = time.monotonic()
    assert bot._preflight_boxes(SimpleNamespace(state=screens.ScreenState.IN_RUN), reads) == boxes
    assert len(calls) == 1
    assert bot._battle_panel_visible(reads)
    assert len(calls) == 1
    bot._battle_full_read_at = float('-inf')
    monkeypatch.setattr(reads, 'full', lambda: boxes)
    assert bot._preflight_boxes(SimpleNamespace(state=screens.ScreenState.IN_RUN), reads) == boxes
    assert bot._battle_backstop_scan


def test_unreadable_target_crop_falls_back_to_identification(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures/in_run_utility.png'))
    boxes = recorded('in_run_utility')
    calls = []
    monkeypatch.setattr(ocr.FrameReads, '_read_battle', lambda self: (calls.append('full'), boxes)[1])
    reader = BattleReader()
    reader.read(ocr.FrameReads(frame), target='cash_bonus', quote=None, scope=1)
    monkeypatch.setattr(ocr, 'read_region', lambda *args, **kwargs: ())
    reads = ocr.FrameReads(frame)
    reader.read(reads, target='cash_bonus', quote=None, scope=1)
    assert calls == ['full', 'full']
    assert not reads.battle_targeted


def test_reconciliation_falls_back_when_only_price_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures/in_run_utility.png'))
    boxes = recorded('in_run_utility')
    reader = BattleReader()
    calls = []
    monkeypatch.setattr(ocr.FrameReads, '_read_battle', lambda self: (calls.append('full'), boxes)[1])
    reader.read(ocr.FrameReads(frame), target='cash_bonus', quote=None, scope=1)
    def value_only(screen: Any, rect: config.Rect, **kwargs: Any) -> tuple[ocr.TextBox, ...]:
        return tuple(replace(b, rect=config.Rect(b.rect.x-rect.x+20, b.rect.y-rect.y+20,
                     b.rect.w, b.rect.h)) for b in boxes if b.text == 'x1.00')
    monkeypatch.setattr(ocr, 'read_region', value_only)
    reads = ocr.FrameReads(frame)
    reader.read(reads, target='cash_bonus', quote=None, scope=1)
    assert calls == ['full', 'full']
    assert not reads.battle_targeted


@pytest.mark.parametrize('fixture,uid,value', [
    ('in_run_utility', 'cash_bonus', 1), ('in_run_defense', 'health', 10),
    ('in_run_defense_1920', 'health', 33),
])
def test_native_targeted_path_preserves_real_hud_and_purchase_fields(fixture: str, uid: str, value: float) -> None:
    frame = cv2.imread(str(Path(__file__).parent / f'fixtures/{fixture}.png'))
    reader = BattleReader()
    initial = parse_frame(frame, reader.read(ocr.FrameReads(frame), target=uid, quote=None, scope=1), 'battle')
    reads = ocr.FrameReads(frame)
    boxes = reader.read(reads, target=uid, quote=dict(verified=True,
                        wave=initial.combat['wave'], verify_price=True), scope=1)
    assert reads.battle_targeted
    current = parse_frame(frame, boxes, 'battle')
    assert current.cash == initial.cash
    assert current.combat['wave'] == initial.combat['wave']
    row = next(r for r in current.rows if r.upgrade_id == uid)
    assert row.value == value and row.price is not None
