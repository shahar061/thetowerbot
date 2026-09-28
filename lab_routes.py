"""Route authorization is independent of Labs observation and planning support.

Residual trust boundary: offline validation proves a record is internally
consistent with its pixels (fresh OCR, page class, distinct frames), its
capture log and its journal receipt. It cannot detect pixel editing or a
hand-assembled log/journal. Before adding any catalog route, a reviewer must
check that the raw recorder session is archived and that the capture log and
journal came from the recorder on the designated test account, not an editor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any


@dataclass(frozen=True)
class RouteGate:
    enabled: bool
    evidence: str
    reason: str


LEGACY_GAME_SPEED = RouteGate(True, 'legacy_fixture_regression',
    'Existing slot-one Game Speed executor; no continuous live canary is claimed')
UNCALIBRATED = RouteGate(False, 'missing_recorded_sequence',
    'Planning only: recorded selection, confirmation, running result and return required')

MANIFEST = Path(__file__).resolve().parent / 'catalog' / 'lab-routes.v1.json'


def _artifact(root: Path, reference: dict[str, Any]) -> Path:
    """Only hash-pinned local evidence beneath the reviewed evidence root."""
    path = (root / reference['path']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('evidence_path')
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference['sha256']:
        raise ValueError('evidence_digest')
    return path


_RESEARCH_STAGES = ('home', 'home', 'picker', 'picker', 'confirmation', 'confirmation',
                    'running', 'running', 'running', 'return')


# Pixel page expected for each recorded stage. The picker overlay has no
# calibrated page anchor, so it must match no known page; its identity then
# comes only from OCR re-derived from its own pixels (title and named row).
_STAGE_PAGES = {'home': {'LABS'}, 'confirmation': {'LABS'}, 'running': {'LABS'},
                'unlocked': {'LABS'}, 'picker': {'UNKNOWN'}, 'return': {'MAIN_MENU'}}
CAPTURE_LOG_FORMAT = 'towerbot-capture-log'


def _provenance(record: dict[str, Any], stages: tuple[str, ...], root: Path) -> list[float]:
    """Check frames against a hash-pinned capture log in the recorder's format.

    The log is author-supplied JSON, exactly as trustworthy as its source:
    this check proves consistency (a contiguous window of `recorded` entries
    whose digests, times and inputs match), never that a live recorder wrote
    it. See the trust boundary in the module docstring.
    """
    if (record['source'] != 'continuous_live_capture'
            or not all(isinstance(record[k], str) and record[k].strip()
                       for k in ('account_id', 'session_id', 'game_version', 'reviewed_by'))):
        raise ValueError('route_provenance')
    frames = record['frames']
    if tuple(f['stage'] for f in frames) != stages:
        raise ValueError('incomplete_sequence')
    stamps = [f['captured_at'] for f in frames]
    if (not all(type(t) in (int, float) and math.isfinite(t) for t in stamps)
            or any(a >= b for a, b in zip(stamps, stamps[1:]))
            or stamps[-1] - stamps[0] > 90
            or len({f['capture_id'] for f in frames}) != len(frames)):
        raise ValueError('physical_capture_sequence')
    log = json.loads(_artifact(root, record['capture_log']).read_text())
    if (log['format'] != CAPTURE_LOG_FORMAT or log['version'] != 1
            or any(log[k] != record[k] for k in ('session_id', 'account_id', 'game_version'))):
        raise ValueError('capture_log_session')
    entries = log['entries']
    ids = [entry['capture_id'] for entry in entries]
    if len(set(ids)) != len(ids) or frames[0]['capture_id'] not in ids:
        raise ValueError('capture_log_entries')
    first = ids.index(frames[0]['capture_id'])
    window = entries[first:first + len(frames)]
    if len(window) != len(frames) or any(
            entry['capture_id'] != frame['capture_id'] or entry['recording'] != 'recorded'
            or entry['png_sha256'] != frame['image']['sha256']
            or entry['captured_at'] != frame['captured_at']
            or entry.get('input') != frame.get('input')
            for frame, entry in zip(frames, window)):
        raise ValueError('capture_log_entries')
    return stamps


def _fresh_boxes(image: Any) -> tuple[Any, ...]:
    """OCR re-derived from the pinned pixels; never the author's stored boxes."""
    import ocr
    if not ocr.available():
        raise ValueError('ocr_unavailable')
    try:
        return ocr.read(image, strict=True)
    except RuntimeError:
        raise ValueError('ocr_unavailable') from None


def _page(image: Any) -> str:
    import pages
    import vision
    return pages.classify_page(image, vision.TemplateCache(Path(__file__).resolve().parent / 'templates')).page


def _frames(record: dict[str, Any], root: Path) -> list[tuple[dict[str, Any], Any, tuple[Any, ...]]]:
    """Load pinned frames and verify them from pixels.

    Every stage is page-classified, stored OCR must equal a fresh read of the
    same image, and each state change must show new pixels.
    """
    import cv2
    digests: dict[str, set[str]] = {}
    for frame in record['frames']:  # each pin is re-verified by _artifact below
        digests.setdefault(frame['stage'], set()).add(frame['image']['sha256'])
    if sum(len(group) for group in digests.values()) != len(set().union(*digests.values())):
        raise ValueError('state_change_without_new_pixels')
    result = []
    for frame in record['frames']:
        stage = frame['stage']
        image = cv2.imread(str(_artifact(root, frame['image'])))
        if image is None or [image.shape[1], image.shape[0]] != record['layout']:
            raise ValueError('layout')
        if _page(image) not in _STAGE_PAGES[stage]:
            raise ValueError('stage_page')
        if stage == 'return':
            result.append((frame, image, ()))
            continue
        fresh = _fresh_boxes(image)
        stored = json.loads(_artifact(root, frame['ocr']).read_text())
        if (sorted((e['text'], tuple(int(v) for v in e['rect'])) for e in stored)
                != sorted((b.text, tuple(int(v) for v in b.rect)) for b in fresh)):
            raise ValueError('ocr_mismatch')
        result.append((frame, image, fresh))
    return result


def _inputs(frames: list[dict[str, Any]], expected: dict[int, dict[str, Any]], return_index: int) -> None:
    """Exactly the recorded spend/navigation inputs; nothing else was dispatched."""
    for index, frame in enumerate(frames):
        action = frame.get('input')
        if index in expected:
            if action != expected[index]:
                raise ValueError('input_semantics')
        elif index == return_index:
            if action is None or action.get('kind') != 'return_home':
                raise ValueError('return_input')
        elif action is not None:
            raise ValueError('unexpected_input')


def _receipt(record: dict[str, Any], root: Path, *, operation: str, currency: str, price: int,
             wallet_before: int, acted_at: float, before: dict[str, Any]) -> None:
    """The recorded account's journal holds one settled receipt and one debit."""
    db_path = _artifact(root, record['journal'])
    with sqlite3.connect(f'{db_path.as_uri()}?mode=ro&immutable=1', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        receipt = conn.execute('SELECT * FROM transactions WHERE key=?', (record['receipt_key'],)).fetchone()
        if receipt is None:
            raise ValueError('durable_receipt_required')
        detail = json.loads(receipt['detail'])
        if (receipt['stage'] != 'resolved' or receipt['outcome'] != 'bought'
                or receipt['currency'] != currency or receipt['price'] != price
                or receipt['wallet_before'] != wallet_before
                or receipt['spent'] != price or receipt['acted_at'] != acted_at
                or detail['operation'] != operation
                or detail['scope']['account_id'] != record['account_id']
                or any(detail['before'].get(k) != v for k, v in before.items())):
            raise ValueError('receipt_semantics')
        lines = conn.execute('SELECT delta FROM ledger WHERE currency=? AND kind=\'LAB\' '
            "AND json_extract(detail, '$.transaction_key')=?", (currency, record['receipt_key'])).fetchall()
        if len(lines) != 1 or lines[0]['delta'] != -price:
            raise ValueError('one_durable_debit_required')
        if conn.execute('SELECT 1 FROM currency_commitments WHERE owner=?',
                        (f"purchase:{record['receipt_key']}",)).fetchone():
            raise ValueError('unsettled_reservation')


def _validate_record(record: dict[str, Any], root: Path) -> tuple[int, str]:
    """Verify a research-start sequence and its durable receipt, offline."""
    from lab_screen import read_home, read_selected_home, read_selected_picker, read_confirmation, read_slots

    slot, research = record['slot'], record['research_id']
    if (record['kind'] != 'research' or type(slot) is not int or slot not in range(1, 6)
            # Slot 1 stays Game Speed first; no record can repurpose it.
            or (slot == 1 and research != 'labs.game-speed')):
        raise ValueError('route_identity')
    stamps = _provenance(record, _RESEARCH_STAGES, root)
    readings = []
    for frame, image, boxes in _frames(record, root):
        if frame['stage'] in {'home', 'running'}:
            slots = read_slots(image, boxes, observed_at=frame['captured_at'])
            if not slots.strip_read():
                raise ValueError('complete_strip_required')
            home = (read_home(image, boxes) if slot == 1 else
                    read_selected_home(image, boxes, slot=slot, observed_at=frame['captured_at']))
            readings.append(home)
        elif frame['stage'] == 'picker':
            readings.append(read_selected_picker(image, boxes, research_id=research))
        elif frame['stage'] == 'confirmation':
            readings.append(read_confirmation(image, boxes))
    homes, pickers, dialogs, running = readings[:2], readings[2:4], readings[4:6], readings[6:9]
    row = pickers[0].entry
    if (row is None or row.status != 'available' or row.level is None
            or pickers[0] != pickers[1]
            or any(h.slot_status != 'idle' or h.slot_point is None for h in homes)
            or any(not d.page or d.research_id != research or d.target_level != row.level
                   or d.price != row.cost or d.coin_balance != pickers[0].coin_balance
                   or d.research_point is None for d in dialogs)
            or dialogs[0] != dialogs[1]
            or any(h.coin_balance != pickers[0].coin_balance for h in homes)
            or any(h.job is None or h.job.slot != slot or h.job.concept_id != research
                   or h.coin_balance != pickers[0].coin_balance - row.cost for h in running)):
        raise ValueError('research_semantics')
    # read_home's legacy job lacks levels, so use the modal target and the
    # matching running name in addition to the generalized semantic reader.
    if any(h.job.raw_name != dialogs[0].name for h in running):
        raise ValueError('running_target')
    _inputs(record['frames'], {1: {'kind': 'open_slot', 'point': list(homes[1].slot_point)},
                               3: {'kind': 'select_research', 'point': list(pickers[1].buy_point)},
                               5: {'kind': 'research', 'point': list(dialogs[1].research_point)}}, 8)
    _receipt(record, root, operation='lab_start', currency='coins', price=row.cost,
             wallet_before=pickers[0].coin_balance, acted_at=stamps[5],
             before={'slot': slot, 'research_id': research, 'target_level': row.level,
                     'source_level': row.level - 1})
    return slot, research


# ImportError/RuntimeError: a missing reader dependency must fail closed, never
# break research_gate for the legacy route.
_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
           ImportError, RuntimeError, sqlite3.Error)


def _load(manifest: Path, evidence_root: Path | None) -> dict[tuple[int, str], RouteGate]:
    """Malformed, partial and isolated evidence cannot add execution support.

    Lab slot unlocks are never recorded routes: lab_unlock_rollout.py gates them.
    """
    try:
        document = json.loads(manifest.read_text())
        if document.get('schema_version') != 1 or not isinstance(document.get('routes'), list):
            return {}
    except (OSError, ValueError, AttributeError):
        return {}
    root = evidence_root or manifest.parent
    research: dict[tuple[int, str], RouteGate] = {}
    for record in document['routes']:
        try:
            if record['kind'] == 'unlock':
                continue
            research[_validate_record(record, root)] = RouteGate(True, 'validated_recorded_sequence',
                'Recorded semantic sequence and one durable debit verified')
        except _ERRORS:
            continue
    return research


def load_recorded_routes(manifest: Path, *, evidence_root: Path | None = None) -> dict[tuple[int, str], RouteGate]:
    return _load(manifest, evidence_root)


@lru_cache(maxsize=1)
def _recorded() -> dict[tuple[int, str], RouteGate]:
    """Calibration changes are reviewed deployment inputs; reload on restart."""
    return _load(MANIFEST, MANIFEST.parent.parent)


def recorded_routes() -> dict[tuple[int, str], RouteGate]:
    return _recorded()


def research_gate(slot: int, research_id: str) -> RouteGate:
    return recorded_routes().get((slot, research_id),
        LEGACY_GAME_SPEED if (slot, research_id) == (1, 'labs.game-speed') else UNCALIBRATED)


def route_gates() -> dict[str, dict[str, object]]:
    return {
        'game_speed_slot_1': asdict(LEGACY_GAME_SPEED),
        'general_research': asdict(UNCALIBRATED),
        **{f'research:{slot}:{research}': asdict(gate)
           for (slot, research), gate in recorded_routes().items()},
        'native_repeat': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Repeat controls and persisted on/off state are uncalibrated')),
        **{f'in_battle_{page}': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Pending work waits for MAIN_MENU/GAME_OVER; no calibrated return to the same run'))
           for page in ('labs', 'missions')},
    }
