from dataclasses import replace
from pathlib import Path
import json

import cv2
import config
import ocr
import perception
from fleet.workshop_prices import PriceQuote

FIXTURES = Path('tests/fixtures')


def frame(name):
    return cv2.imread(str(FIXTURES / f'{name}.png'))


def boxes(name):
    return tuple(ocr.TextBox(b['text'],b['confidence'],config.Rect(*b['rect']))
        for b in json.loads((FIXTURES/'ocr'/f'{name}.json').read_text()))


def replay(monkeypatch, name):
    readings = boxes(name)
    calls = {'full':0,'roi':0}
    def full(image):
        calls['full'] += 1
        return readings
    def region(image, rect):
        calls['roi'] += 1
        return tuple(replace(b, rect=config.Rect(b.rect.x-rect.x+ocr.CROP_PADDING,
            b.rect.y-rect.y+ocr.CROP_PADDING,b.rect.w,b.rect.h)) for b in readings
            if perception.contains(rect,b.rect))
    monkeypatch.setattr(ocr,'read',full)
    monkeypatch.setattr(ocr,'read_region',region)
    return calls


def test_catalog_target_reuses_layout_but_reads_current_semantics_and_periodic_full(monkeypatch):
    calls = replay(monkeypatch,'menu_workshop_attack')
    screen = frame('menu_workshop_attack')
    cache = perception.WorkshopReader(reconcile_every=3)
    quote = PriceQuote(30,0,'observed',level_confidence='exact',modifier_signature='none')
    first = cache.read(screen,target='damage',quote=quote,scope=('account','run1','epoch1'))
    second = cache.read(screen,target='damage',quote=quote,scope=('account','run1','epoch1'))
    assert second.rows[0].price == first.rows[0].price == 30
    assert calls['full'] == 1 and calls['roi'] >= 2
    cache.read(screen,target='damage',quote=quote,scope=('account','run1','epoch1'))
    cache.read(screen,target='damage',quote=quote,scope=('account','run1','epoch1'))
    assert calls['full'] == 2
    assert cache.counters['full:periodic'] == 1
    cache.read(screen,target='damage',quote=quote,scope=('account','run2','epoch1'))
    assert calls['full'] == 3
    assert cache.counters['full:scope_changed'] == 1


def test_coin_quote_never_enables_battle_or_unknown_modifier_fast_path(monkeypatch):
    calls = replay(monkeypatch,'menu_workshop_attack')
    reader = perception.WorkshopReader()
    quote = PriceQuote(30,0,'catalog_estimate')
    for _ in range(2):
        reader.read(frame('menu_workshop_attack'),target='damage',quote=quote,scope='account')
    assert calls['full'] == 2
    assert reader.counters['full:quote_untrusted'] == 2


def test_layout_transition_invalidates_target_and_falls_back_to_fresh_full(monkeypatch):
    replay(monkeypatch,'menu_workshop_attack')
    reader = perception.WorkshopReader()
    quote = PriceQuote(30,0,'observed',level_confidence='exact',modifier_signature='none')
    reader.read(frame('menu_workshop_attack'),target='damage',quote=quote,scope='account')
    calls = replay(monkeypatch,'menu_workshop_utility')
    result = reader.read(frame('menu_workshop_utility'),target='damage',quote=quote,scope='account')
    assert result.category == 'UTILITY'
    assert all(r.upgrade_id != 'damage' for r in result.rows)
    assert calls['full'] == 1


def test_recorded_unreadable_price_replay_compares_baseline_ocr_calls(monkeypatch):
    # Use the committed regression pixels and actual local OCR (no provider).
    screen = frame('menu_workshop_defense_price_below_floor')
    original_full, original_roi = ocr.read, ocr.read_region
    calls = {'full':0,'roi':0}
    def full(image, **kwargs):
        if image.shape == screen.shape:
            calls['full'] += 1
        return original_full(image, **kwargs)
    def roi(image,region):
        calls['roi'] += 1
        return original_roi(image,region)
    monkeypatch.setattr(ocr,'read',full)
    monkeypatch.setattr(ocr,'read_region',roi)
    baseline = [perception.observe_frame(screen,'workshop') for _ in range(2)]
    baseline_calls = calls.copy()
    calls.update(full=0,roi=0)
    reader = perception.WorkshopReader()
    quote = PriceQuote(586,7,'observed',level_confidence='exact',modifier_signature='none')
    optimized = [reader.read(screen,target='defense_absolute',quote=quote,scope='account') for _ in range(2)]
    def outcome(observation):
        row = next(r for r in observation.rows if r.upgrade_id=='defense_absolute')
        assert row.tap is not None and 890 <= row.tap[0] <= 1000 and 680 <= row.tap[1] <= 740
        return row.status,row.price,row.value
    assert [outcome(o) for o in baseline] == [outcome(o) for o in optimized]
    assert outcome(optimized[-1])[:3] == ('available',586,9.76)
    print('Unreadable fixture OCR baseline=',baseline_calls,'candidate=',calls,
          'verified observations=2; no spending performed')
