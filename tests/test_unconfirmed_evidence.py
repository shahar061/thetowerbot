import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np

import config
import vision
from perception import Observation, ObservedUpgrade
from shopping import PendingPurchase, ShoppingSession


def _session(monkeypatch, evidence_dir: Path | None) -> ShoppingSession:
    session = ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Mock(), Mock())
    session.evidence_dir = evidence_dir
    monkeypatch.setattr(ShoppingSession, '_info_panel_visible', staticmethod(lambda screen: False))
    monkeypatch.setattr(session, '_abort', lambda *args: None)
    return session


def _unlock_row() -> ObservedUpgrade:
    return ObservedUpgrade('unlock_defense_upgrades', 'Unlock Defense Upgrades', 'DEFENSE',
                           'workshop', None, 75, 'available', 1., config.Rect(0, 300, 400, 200),
                           (100, 450), confidence=.98)


def _unreadable(step: int) -> Observation:
    return Observation(None, (), {}, None, 2. + step, None, context='workshop')


def test_inconclusive_acknowledgement_keeps_every_frame_it_read(tmp_path, monkeypatch) -> None:
    session = _session(monkeypatch, tmp_path)
    before = np.full((24, 12, 3), 7, np.uint8)
    session._pending = PendingPurchase(_unlock_row(), 121, frozenset({'health'}),
                                       before_frame=before)
    frames = [np.full((24, 12, 3), value, np.uint8) for value in (1, 2, 3)]
    for step, frame in enumerate(frames):
        session._confirm_purchase(_unreadable(step), 46, Mock(), Mock(), frame)
    assert session._pending is None
    [folder] = tmp_path.glob('unconfirmed-*-unlock_defense_upgrades')
    assert sorted(p.name for p in folder.iterdir()) == [
        'before.png', 'detail.json', 'frame-1.png', 'frame-2.png', 'frame-3.png']
    detail = json.loads((folder / 'detail.json').read_text())
    assert detail['item'] == 'Unlock Defense Upgrades' and detail['coins_before'] == 121
    assert 'tab=None tile=gone coins=46' in detail['evidence']
    assert detail['last_observation']['category'] is None
    skipped = session._bus.publish.call_args.args[0]
    assert skipped.reason == 'unconfirmed' and str(folder) in skipped.detail


def test_without_an_evidence_dir_no_frames_are_held(monkeypatch) -> None:
    session = _session(monkeypatch, None)
    session._pending = pending = PendingPurchase(_unlock_row(), 121, frozenset())
    session._confirm_purchase(_unreadable(0), 46, Mock(), Mock(), np.zeros((4, 4, 3), np.uint8))
    assert pending.seen_frames == [] and pending.before_frame is None
