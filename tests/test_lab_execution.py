"""Offline executor contracts; synthetic observations never calibrate a route."""
from dataclasses import replace
from pathlib import Path

import pytest

from fleet.resource_blocks import LabAction
from lab_visit import LabVisit
from lab_plan import LabVisitOptions
import lab_screen
import labs
import vision
from tests.test_lab_visit import frame, boxes, Device
from tests.test_lab_transactions import LabHarness


def action(slot: int = 1, research: str = 'labs.game-speed', level: int = 1) -> LabAction:
    return LabAction(slot, research, level, 'start', 7, 'route next')


def test_selected_row_reader_does_not_substitute_game_speed() -> None:
    image, text = frame('menu_labs_game_speed_affordable'), boxes('menu_labs_game_speed_affordable')
    selected = lab_screen.read_selected_picker(image, text, research_id='labs.attack-speed')
    assert selected.entry is not None
    assert selected.entry.concept_id == 'labs.attack-speed'
    assert selected.entry.raw_name == 'Attack Speed Lv.1'
    assert lab_screen.read_selected_picker(image, text, research_id='labs.missing').entry is None


def test_confirmation_exposes_semantic_identity_and_level() -> None:
    result = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                          boxes('menu_labs_game_speed_confirmation'))
    assert (result.research_id, result.target_level) == ('labs.game-speed', 1)


def test_selected_slot_reader_does_not_confuse_running_and_idle_slots() -> None:
    image, text = frame('menu_labs_active'), boxes('menu_labs_active')
    selected = lab_screen.read_selected_home(image, text, slot=2, observed_at=1000.)
    assert selected.slot_status == 'idle' and selected.slots_owned == 5
    assert selected.slot_point is not None
    running = lab_screen.read_selected_home(image, text, slot=5, observed_at=1000.)
    assert running.slot_status == 'researching' and running.slot_point is None
    assert running.job.slot == 5


def test_uncalibrated_action_is_retained_without_enabling_a_visit() -> None:
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    requested = action(2, 'labs.attack-speed')
    assert not visit.request(requested)
    assert not visit.active
    assert visit.pending_action == requested
    assert visit.recovery_status == 'lab_route_calibration_required'
    matrix = labs.capabilities()
    assert matrix['route_gates']['unlock_slot_2']['enabled'] is False
    assert matrix['route_gates']['in_battle_labs']['enabled'] is False
    assert matrix['route_gates']['in_battle_missions']['enabled'] is False
    assert matrix['route_gates']['game_speed_slot_1']['evidence'] == 'legacy_fixture_regression'


def test_action_level_mismatch_cannot_open_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(level=2))
    for name in ('menu_labs_slot1_affordable', 'menu_labs_slot1_affordable',
                 'menu_labs_game_speed_affordable', 'menu_labs_game_speed_affordable'):
        h.scan(name)
    assert h.journal.open_transactions() == ()
    assert h.visit._outcome.reason == 'selected_research_mismatch'
    assert (291, 711) not in h.device.taps


def test_action_executes_one_semantic_purchase_and_returns_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(), options=LabVisitOptions(unlock_slot2=False))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    txn = h.journal.open_transactions()[0]
    assert txn.scope.account_id == 'account-a'
    assert txn.before['slot'] == 1 and txn.before['research_id'] == 'labs.game-speed'
    assert h.visit._purchase.strategy_revision == 7
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    h.time += 1
    h.visit.advance(frame('menu_main_labs_unlocked'), (), h.device, h.time,
                    observed_at=h.time, capture_scope=h.scope)
    assert not h.visit.active
    assert h.runtime.snapshot().slots[0].transaction_id == txn.key
    point = lab_screen.read_confirmation(frame('menu_labs_game_speed_confirmation'),
                                         boxes('menu_labs_game_speed_confirmation')).research_point
    assert h.device.taps.count(point) == 1
    assert h.journal.open_transactions() == ()


def test_read_only_inspection_waits_for_confirmed_complete_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('observe')
    h.visit.request(options=LabVisitOptions(start_research=False, unlock_slot2=False))
    h.scan('menu_labs_slot1_affordable')
    assert h.visit._state == 'home'
    assert h.device.taps == []
    h.scan('menu_labs_slot1_affordable')
    assert h.visit._state == 'return'
    assert h.runtime.snapshot().slots[0].confirmed


def test_picker_has_its_own_bounded_observation_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.scan('menu_labs_slot1_affordable')
    h.scan('menu_labs_slot1_affordable')
    for _ in range(9):
        h.scan('menu_labs_slot1_affordable')
    assert h.visit._outcome.reason == 'picker_stage_timeout'
    assert h.journal.open_transactions() == ()


def test_unlock_cannot_be_authorized_by_synthetic_post_state() -> None:
    from fleet.resource_blocks import gem_automated
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    visit.request(LabVisitOptions(min_gems=150))
    home = lab_screen.LabHomeReading(True, 'idle', None, None, gem_balance=150,
                                    slot2_status='locked', slot2_price=100, slot2_point=(700, 450))
    assert not visit._unlock_lab_two(home, Device())
    assert not gem_automated({'type': 'unlock_lab_slot', 'slot': 2})


def test_selected_action_never_enters_labs_during_battle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    requested = action()
    h.visit.request(requested)
    h.time += 1
    outcome = h.visit.advance(frame('in_run_four_tabs'), (), h.device, h.time,
                              observed_at=h.time, capture_scope=h.scope)
    assert outcome is not None and outcome.reason == 'not_at_menu'
    assert h.device.taps == []
    assert h.visit.pending_action == requested


def test_offline_general_executor_binds_the_selected_slot_research_and_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected observations exercise control flow only; no calibration is saved."""
    from lab_routes import RouteGate
    from labs import LabJob
    import lab_visit
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    # Bypass the gate ONLY inside this offline unit test. Production stays gated.
    monkeypatch.setattr(lab_visit, 'research_gate', lambda *_: RouteGate(True, 'test_only', 'offline'))
    assert h.visit.request(action(2, 'labs.attack-speed'))
    original_slots = lab_screen.read_slots
    original_home = lab_screen.read_selected_home
    started = [False]

    def observed_slots(image: object, text: tuple, *, observed_at: float) -> labs.LabsReading:
        reading = original_slots(image, text, observed_at=observed_at)
        if started[0] and reading.slots_owned == 5:
            reading = replace(reading, jobs=tuple(
                LabJob(2, 'labs.attack-speed', 'Attack Speed Lv.1', observed_at+100, 100,
                       None, 'unknown', 'researching', .99, job.rect, source_level=0, target_level=1)
                if job.slot == 2 else job for job in reading.jobs))
        return reading

    def home(image: object, text: tuple, *, slot: int, observed_at: float) -> lab_screen.LabHomeReading:
        return replace(original_home(image, text, slot=slot, observed_at=observed_at),
                       coin_balance=583 if started[0] else 613)

    monkeypatch.setattr(lab_visit, 'read_slots', observed_slots)
    monkeypatch.setattr(lab_visit, 'read_selected_home', home)
    original_picker = lab_screen.read_selected_picker

    def picker(image: object, text: tuple, *, research_id: str) -> lab_screen.LabPickerReading:
        reading = original_picker(image, text, research_id=research_id)
        if reading.entry is not None:
            return replace(reading, game_speed=replace(reading.entry, status='available'), buy_point=(830, 1320))
        return reading

    monkeypatch.setattr(lab_visit, 'read_selected_picker', picker)
    h.visit.confirmation_reader = lambda image, text: replace(lab_screen.read_confirmation(image, text),
        name='Attack Speed Lv.1', price=30)
    for name in ('menu_labs_active', 'menu_labs_active', 'menu_labs_game_speed_affordable',
                 'menu_labs_game_speed_affordable', 'menu_labs_game_speed_confirmation',
                 'menu_labs_game_speed_confirmation'):
        h.scan(name)
    txn = h.journal.open_transactions()[0]
    assert txn.scope.account_id == 'account-a'
    assert txn.before['slot'] == 2 and txn.before['research_id'] == 'labs.attack-speed'
    assert txn.before['target_level'] == 1 and txn.price == 30
    started[0] = True
    for _ in range(3):
        h.scan('menu_labs_active')
    assert h.journal.open_transactions() == ()
    assert h.runtime.snapshot().slots[1].transaction_id == txn.key
    assert h.visit._outcome.reason == 'research_confirmed'
    assert h.visit._outcome.observed_coin_spend == 30


@pytest.mark.parametrize('record', [
    {'enabled': True, 'slot': 2, 'research_id': 'labs.attack-speed'},
    {'source': 'synthetic', 'slot': 2, 'research_id': 'labs.attack-speed'},
    {'source': 'continuous_live_capture', 'slot': 2, 'research_id': 'labs.attack-speed',
     'frames': [{'image': 'isolated.png', 'stage': 'running'}]},
])
def test_future_route_records_fail_closed_without_recorded_semantic_sequence(tmp_path: Path, record: dict) -> None:
    import json
    from lab_routes import load_recorded_routes
    manifest = tmp_path / 'routes.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'routes': [record]}))
    assert load_recorded_routes(manifest) == {}


def test_missing_or_invalid_manifest_keeps_only_the_legacy_route(tmp_path: Path) -> None:
    from lab_routes import load_recorded_routes
    path = tmp_path / 'routes.json'
    assert load_recorded_routes(path) == {}
    path.write_text('{')
    assert load_recorded_routes(path) == {}


def _reference(path: Path) -> dict[str, str]:
    import hashlib
    return {'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _log(tmp_path: Path, record: dict, *, entries: list | None = None, **overrides: object) -> None:
    """A TEST-AUTHORED stand-in for the recorder's capture log (never installed)."""
    import json
    document = {'format': 'towerbot-capture-log', 'version': 1, 'session_id': record['session_id'],
                'account_id': record['account_id'], 'game_version': record['game_version'],
                'entries': entries if entries is not None else [
                    {'capture_id': f['capture_id'], 'png_sha256': f['image']['sha256'],
                     'captured_at': f['captured_at'], 'recording': 'recorded', 'input': f.get('input')}
                    for f in record['frames']], **overrides}
    path = tmp_path / 'capture-log.json'
    path.write_text(json.dumps(document))
    record['capture_log'] = _reference(path)


def _install(tmp_path: Path, record: dict) -> Path:
    import json
    manifest = tmp_path / 'routes.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'routes': [record]}))
    return manifest


def _copy(tmp_path: Path, name: str, target: str) -> dict[str, str]:
    from tests.test_lab_visit import FIXTURES
    path = tmp_path / f'{target}.png'
    path.write_bytes((FIXTURES / f'{name}.png').read_bytes())
    return _reference(path)


@pytest.fixture
def research_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Schema exercise over distinct recorded legacy PNGs, their real OCR and a
    real offline journal receipt. Its capture log is TEST-AUTHORED: the legacy
    fixtures are isolated captures, not a continuous recording, so nothing
    built here is ever installed in the shipped manifest."""
    import sqlite3
    from tests.test_lab_visit import FIXTURES
    h = LabHarness(tmp_path, monkeypatch)
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    key = h.journal.open_transactions()[0].key
    for _ in range(3):
        h.scan('menu_labs_game_speed_running')
    journal_copy = tmp_path / 'receipt.db'
    with sqlite3.connect(h.journal.path) as source, sqlite3.connect(journal_copy) as target:
        source.backup(target)
    frames = []
    for index, (stage, name) in enumerate([
        ('home', 'menu_labs_slot1_affordable'), ('home', 'menu_labs_slot1_affordable'),
        ('picker', 'menu_labs_game_speed_affordable'), ('picker', 'menu_labs_game_speed_affordable'),
        ('confirmation', 'menu_labs_game_speed_confirmation'), ('confirmation', 'menu_labs_game_speed_confirmation'),
        ('running', 'menu_labs_game_speed_running'), ('running', 'menu_labs_game_speed_running'),
        ('running', 'menu_labs_game_speed_running'), ('return', 'menu_main_labs_unlocked'),
    ]):
        frame_record = {'stage': stage, 'captured_at': 11. + index, 'capture_id': f'test-{index}',
                        'image': _copy(tmp_path, name, str(index))}
        if stage != 'return':
            text_path = tmp_path / f'{index}.json'
            text_path.write_text((FIXTURES / 'ocr' / f'{name}.json').read_text())
            frame_record['ocr'] = _reference(text_path)
        if index in (1, 3, 5, 8):
            tap_index = (1, 3, 5, 8).index(index)
            frame_record['input'] = {'kind': ('open_slot', 'select_research', 'research', 'return_home')[tap_index],
                                     'point': list(h.device.taps[tap_index])}
        frames.append(frame_record)
    record = {'kind': 'research', 'source': 'continuous_live_capture', 'slot': 1,
              'research_id': 'labs.game-speed', 'account_id': 'account-a', 'session_id': 'schema-test-only',
              'game_version': 'test-only', 'reviewed_by': 'schema-test-only', 'layout': [1080, 2400],
              'frames': frames, 'journal': _reference(journal_copy), 'receipt_key': key}
    _log(tmp_path, record)
    return record


def test_research_record_validator_uses_pixels_log_and_real_durable_receipt(tmp_path: Path, research_record: dict) -> None:
    import json
    from lab_routes import _validate_record, load_recorded_routes
    record = research_record
    assert _validate_record(record, tmp_path) == (1, 'labs.game-speed')
    assert load_recorded_routes(_install(tmp_path, record))[(1, 'labs.game-speed')].enabled
    for key, value in (('source', 'offline_composed'), ('account_id', 'wrong-account'), ('kind', None)):
        broken = json.loads(json.dumps(record))
        if value is None:
            del broken[key]
        else:
            broken[key] = value
        assert load_recorded_routes(_install(tmp_path, broken)) == {}


def test_edited_ocr_on_a_recorded_png_is_refused(tmp_path: Path, research_record: dict) -> None:
    import json
    from lab_routes import _validate_record
    text_path = tmp_path / '2.json'
    entries = json.loads(text_path.read_text())
    price = next(e for e in entries if e['text'] == '300')
    price['text'] = '30'
    text_path.write_text(json.dumps(entries))
    research_record['frames'][2]['ocr'] = _reference(text_path)
    with pytest.raises(ValueError, match='ocr_mismatch'):
        _validate_record(research_record, tmp_path)


def test_validation_fails_closed_when_ocr_is_unavailable(tmp_path: Path, research_record: dict,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    from lab_routes import _validate_record, load_recorded_routes
    monkeypatch.setattr(ocr, 'available', lambda: False)
    with pytest.raises(ValueError, match='ocr_unavailable'):
        _validate_record(research_record, tmp_path)
    assert load_recorded_routes(_install(tmp_path, research_record)) == {}


def test_every_stage_is_pixel_classified(tmp_path: Path, research_record: dict) -> None:
    from lab_routes import _validate_record
    # A (different) Labs home frame posing as the picker overlay.
    research_record['frames'][2]['image'] = research_record['frames'][3]['image'] = _copy(
        tmp_path, 'menu_labs_slot1_idle', 'posing')
    _log(tmp_path, research_record)
    with pytest.raises(ValueError, match='stage_page'):
        _validate_record(research_record, tmp_path)


def test_a_state_change_must_show_new_pixels(tmp_path: Path, research_record: dict) -> None:
    from lab_routes import _validate_record
    confirmation = research_record['frames'][4]
    for frame in research_record['frames'][6:9]:
        frame['image'], frame['ocr'] = confirmation['image'], confirmation['ocr']
    _log(tmp_path, research_record)
    with pytest.raises(ValueError, match='state_change_without_new_pixels'):
        _validate_record(research_record, tmp_path)


@pytest.mark.parametrize('damage', ['missing', 'derived', 'gap', 'session', 'digest'])
def test_frames_must_be_a_contiguous_recorded_window_of_the_capture_log(
        tmp_path: Path, research_record: dict, damage: str) -> None:
    from lab_routes import _validate_record, load_recorded_routes
    record = research_record
    entries = [{'capture_id': f['capture_id'], 'png_sha256': f['image']['sha256'],
                'captured_at': f['captured_at'], 'recording': 'recorded', 'input': f.get('input')}
               for f in record['frames']]
    if damage == 'missing':
        del record['capture_log']
        assert load_recorded_routes(_install(tmp_path, record)) == {}
        return
    if damage == 'derived':
        entries[6]['recording'] = 'derived'
    elif damage == 'gap':  # an unlisted capture (and possibly input) between frames
        entries.insert(4, {'capture_id': 'hidden', 'png_sha256': 'x', 'captured_at': 14.5,
                           'recording': 'recorded', 'input': {'kind': 'tap', 'point': [1, 1]}})
    elif damage == 'digest':
        entries[3]['png_sha256'] = '0' * 64
    _log(tmp_path, record, entries=entries, **({'session_id': 'other'} if damage == 'session' else {}))
    with pytest.raises(ValueError, match='capture_log'):
        _validate_record(record, tmp_path)


def _unlock_record(tmp_path: Path, owned_image: str = 'menu_labs_slot1_idle') -> dict:
    """Schema-only: forged OCR boxes plus a real offline journal receipt.

    With the default `owned_image` this is the reviewer's forgery: every
    non-return frame is the same locked PNG and only the stored OCR differs.
    """
    import json
    import sqlite3
    import config
    import ocr
    from tests.test_lab_transactions import authority, prepared
    from transactions import RecoveryEvidence
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_unlock')
    journal.reconcile(txn.key, RecoveryEvidence(category='LABS', currency='gems', wallet_after=513,
        effect_changed=True, observed_at=12., frame_digest='after', scope=scope, operation='lab_unlock',
        slot=2, research_id=None, target_level=None), now=12.)
    journal_copy = tmp_path / 'receipt.db'
    with sqlite3.connect(journal.path) as source, sqlite3.connect(journal_copy) as target:
        source.backup(target)
    original = boxes('menu_labs_slot1_idle')
    locked = tuple(ocr.TextBox('613', b.confidence, b.rect) if b.text == '65' else b for b in original)
    gem = next(b for b in locked if b.text == '613')
    owned = tuple(b for b in locked if b.text not in {'Unlock Znd lab', '100', '613'}) + (
        ocr.TextBox('513', gem.confidence, gem.rect),
        ocr.TextBox('Lab Offline', .99, config.Rect(394, 832, 291, 52)),
        ocr.TextBox('Lab 3', .99, config.Rect(25, 1046, 97, 39)),
        ocr.TextBox('Unlock 3rd lab', .99, config.Rect(351, 1174, 378, 51)))

    def dump(text: tuple) -> str:
        return json.dumps([{'text': b.text, 'confidence': b.confidence,
                            'rect': [b.rect.x, b.rect.y, b.rect.w, b.rect.h]} for b in text])

    frames = []
    for index, (stage, image, text) in enumerate([
            ('home', 'menu_labs_slot1_idle', locked), ('home', 'menu_labs_slot1_idle', locked),
            ('unlocked', owned_image, owned), ('unlocked', owned_image, owned),
            ('return', 'menu_main_labs_unlocked', ())]):
        record = {'stage': stage, 'captured_at': 10. + index, 'capture_id': f'unlock-{index}',
                  'image': _copy(tmp_path, image, f'u{index}')}
        if stage != 'return':
            text_path = tmp_path / f'u{index}.json'
            text_path.write_text(dump(text))
            record['ocr'] = _reference(text_path)
        if index == 1:
            record['input'] = {'kind': 'unlock_slot', 'point': [586, 906]}
        if index == 3:
            record['input'] = {'kind': 'return_home'}
        frames.append(record)
    record = {'kind': 'unlock', 'source': 'continuous_live_capture', 'slot': 2,
              'account_id': 'account-a', 'session_id': 'schema-test-only', 'game_version': 'test-only',
              'reviewed_by': 'schema-test-only', 'layout': [1080, 2400], 'frames': frames,
              'journal': _reference(journal_copy), 'receipt_key': txn.key}
    _log(tmp_path, record)
    return record


def _trust_stored_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record: dict) -> None:
    """EXPLICIT integrity bypass: serve each frame's stored (forged) OCR as if
    it were the fresh read, so the remaining checks can be exercised alone."""
    import json
    import cv2
    import config
    import lab_routes
    import ocr
    by_digest = {}
    for frame in record['frames']:
        if 'ocr' in frame:
            image = cv2.imread(str(tmp_path / frame['image']['path']))
            entries = json.loads((tmp_path / frame['ocr']['path']).read_text())
            by_digest[image.tobytes()] = tuple(ocr.TextBox(e['text'], e['confidence'], config.Rect(*e['rect']))
                                               for e in entries)
    monkeypatch.setattr(lab_routes, '_fresh_boxes', lambda image: by_digest[image.tobytes()])


def test_unlock_validator_semantics_behind_an_explicit_ocr_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive path only with the OCR re-read bypassed in plain sight."""
    from lab_routes import load_recorded_routes, load_recorded_unlocks
    record = _unlock_record(tmp_path, owned_image='menu_labs_slot1_affordable')
    _trust_stored_ocr(monkeypatch, tmp_path, record)
    manifest = _install(tmp_path, record)
    assert load_recorded_unlocks(manifest)[2].enabled
    assert load_recorded_routes(manifest) == {}  # an unlock never enables research
    for key, value in (('input', {'kind': 'unlock_slot', 'point': [1, 1]}), ('source', 'synthetic'), ('slot', 3)):
        broken = dict(record, frames=[dict(f) for f in record['frames']])
        if key == 'input':
            broken['frames'][1]['input'] = value
            _log(tmp_path, broken)
        else:
            broken[key] = value
        assert load_recorded_unlocks(_install(tmp_path, broken)) == {}
    _log(tmp_path, record)


def test_forged_unlock_with_one_png_before_and_after_is_refused(tmp_path: Path) -> None:
    from lab_routes import _validate_unlock, load_recorded_unlocks
    record = _unlock_record(tmp_path)
    assert len({f['image']['sha256'] for f in record['frames'][:4]}) == 1
    # Unchanged pixels cannot show a new state, whatever the stored OCR says.
    with pytest.raises(ValueError, match='state_change_without_new_pixels'):
        _validate_unlock(record, tmp_path)
    assert load_recorded_unlocks(_install(tmp_path, record)) == {}
    # And the forged locked-state OCR (613 gems) contradicts a fresh read (65).
    record['frames'][2]['image'] = record['frames'][3]['image'] = _copy(
        tmp_path, 'menu_labs_slot1_affordable', 'owned')
    _log(tmp_path, record)
    with pytest.raises(ValueError, match='ocr_mismatch'):
        _validate_unlock(record, tmp_path)


def test_forged_unlock_over_an_unrelated_png_is_refused(tmp_path: Path) -> None:
    from lab_routes import _validate_unlock, load_recorded_unlocks
    record = _unlock_record(tmp_path)
    for frames, name in ((record['frames'][:2], 'menu_main'), (record['frames'][2:4], 'menu_missions')):
        unrelated = _copy(tmp_path, name, name)
        for frame in frames:
            frame['image'] = unrelated
    _log(tmp_path, record)
    with pytest.raises(ValueError, match='stage_page'):
        _validate_unlock(record, tmp_path)
    assert load_recorded_unlocks(_install(tmp_path, record)) == {}


def test_forged_unlock_in_the_production_catalog_layout_enables_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import lab_routes
    from fleet.resource_blocks import gem_automated
    record = _unlock_record(tmp_path)
    catalog = tmp_path / 'catalog'
    catalog.mkdir()
    manifest = catalog / 'lab-routes.v1.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'routes': [record]}))
    monkeypatch.setattr(lab_routes, 'MANIFEST', manifest)  # evidence root = tmp_path
    lab_routes._recorded.cache_clear()
    try:
        assert lab_routes.recorded_unlocks() == {}
        assert not lab_routes.unlock_gate(2).enabled
        assert not gem_automated({'type': 'unlock_lab_slot', 'slot': 2})
        assert lab_routes.route_gates()['unlock_slot_2']['enabled'] is False
    finally:
        lab_routes._recorded.cache_clear()


def test_unlock_gate_is_data_driven_but_shipped_manifest_enables_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import lab_routes
    from fleet.resource_blocks import gem_automated
    shipped = json.loads(lab_routes.MANIFEST.read_text())
    assert shipped['schema_version'] == 1 and shipped['routes'] == []
    assert lab_routes.recorded_unlocks() == {} and lab_routes.recorded_routes() == {}
    assert not lab_routes.unlock_gate(2).enabled
    monkeypatch.setattr(lab_routes, 'recorded_unlocks',
                        lambda: {2: lab_routes.RouteGate(True, 'validated_recorded_sequence', 'test')})
    assert lab_routes.unlock_gate(2).enabled and not lab_routes.unlock_gate(3).enabled
    assert gem_automated({'type': 'unlock_lab_slot', 'slot': 2})
    assert lab_routes.route_gates()['unlock_slot_2']['enabled'] is True


@pytest.mark.parametrize('text', ['Game Speed Lv.1', 'GameSpeed Lv.1', 'Game  Speed Lv.1', 'game speed Lv.1'])
def test_research_identity_tolerates_ocr_spacing_like_the_legacy_filter(text: str) -> None:
    assert lab_screen._research_identity(text) == 'labs.game-speed'
