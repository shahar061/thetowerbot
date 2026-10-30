"""Official exports are durable reporting observations, never action evidence."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

import db
from game_export_store import ExportStore, read_export


BASE = 1_793_356_200.0


def stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def export(at: float = BASE, **changes: object) -> dict:
    value = {'ExportVersion': 1, 'VersionNumber': 1253, 'DataVersion': 31,
             'ExportedAt': stamp(at), 'Coins': 10.5, 'Gems': 20,
             'RoundActiveBool': True, 'CurrentTier': 2, 'CurrentWave': 20,
             'RealTimeThisRound': 100.0, 'GameplayTimeThisRound': 400.0,
             'CoinsEarnedThisRound': 12.5, 'CellsEarnedThisRound': 0,
             'ResearchLevel': [1], 'LabResearchInt': [0], 'LabActiveBool': [True],
             'UpgradeWorkshopLevel': [10], 'CardLevel': [1], 'CardUnlocked': [True]}
    value.update(changes)
    return value


def store(tmp_path: Path, account: str = 'a') -> ExportStore:
    path = tmp_path / 'worker.db'
    db.bind_account(path, account)
    return ExportStore(path, account)


def ingest(target: ExportStore, value: dict, *, scope: str = 'generation-1',
           endpoint: str = 'device:1', captured: float | None = None) -> bool:
    timestamp = datetime.fromisoformat(value['ExportedAt']).timestamp()
    return target.ingest(value, endpoint=endpoint, scope_key=scope,
                         captured_at=captured if captured is not None else timestamp + 1)


def test_missing_reads_do_not_create_any_database(tmp_path: Path) -> None:
    path = tmp_path / 'missing.db'
    result = read_export(path, 'a', now=BASE)
    assert not result['available']
    assert result['account_id'] == 'a'
    assert result['source'] == 'game_export'
    assert list(tmp_path.iterdir()) == []


def test_separate_storage_dedup_and_source_evidence_age(tmp_path: Path) -> None:
    target = store(tmp_path)
    before = target.db_path.read_bytes()
    assert ingest(target, export())
    assert not ingest(target, export(), captured=BASE + 200)
    result = read_export(target.db_path, 'a', now=BASE + 661)
    assert result['available'] and result['stale']
    assert result['captured_at'] == BASE + 1
    assert result['exported_at'] == BASE
    assert result['age_seconds'] == 661
    assert result['wallet']['coins'] == 10.5
    assert result['endpoint'] == 'device:1'
    assert result['scope_key'] == 'generation-1'
    assert target.path == tmp_path / 'game-export.sqlite3'
    assert target.db_path.read_bytes() == before
    with sqlite3.connect(target.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM export_snapshots').fetchone()[0] == 1


def test_reject_bad_times_and_binding_without_touching_store(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    assert not ingest(target, export(BASE - 1))
    assert not ingest(target, export(BASE + 60), captured=BASE + 1)
    assert not ingest(target, export(BASE + 60), captured=BASE)
    assert not target.ingest(b'{broken', endpoint='device:1', scope_key='scope', captured_at=BASE)
    assert not target.ingest(export(), endpoint='', scope_key='scope', captured_at=BASE)
    assert not ExportStore(target.db_path, 'b').ingest(export(), endpoint='device:1', scope_key='s', captured_at=BASE)
    assert not read_export(target.db_path, 'b', now=BASE)['available']
    assert target.projection(now=BASE)['exported_at'] == BASE


def test_account_partition_survives_worker_database_rebind(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    with sqlite3.connect(target.db_path) as conn:
        conn.execute("UPDATE account_identity SET account_id='b' WHERE id=1")
    other = ExportStore(target.db_path, 'b')
    assert ingest(other, export(Coins=99))
    assert not target.projection(now=BASE)['available']
    assert other.projection(now=BASE)['wallet']['coins'] == 99
    with sqlite3.connect(target.db_path) as conn:
        conn.execute("UPDATE account_identity SET account_id='a' WHERE id=1")
    assert target.projection(now=BASE)['wallet']['coins'] == 10.5


def report(number: int) -> dict:
    return {'BattleDate': stamp(BASE - 1000 + number), 'Tier': 2, 'Wave': number,
            'RealTime': 150.5, 'GameTime': 600.5, 'CoinsEarned': 12.5,
            'KilledBy': 999, 'FutureMetric': {'value': number}}


def test_report_import_is_stable_and_survives_rolling_thirty(tmp_path: Path) -> None:
    target = store(tmp_path)
    reports = [report(number) for number in range(30)]
    assert ingest(target, export(BattleHistory=reports))
    original_ids = {r['report']['BattleDate']: r['id'] for r in target.projection(now=BASE)['runs']}
    assert ingest(target, export(BASE + 300, BattleHistory=list(reversed(reports))))
    assert len(target.projection(now=BASE + 300)['runs']) == 30
    assert ingest(target, export(BASE + 600, BattleHistory=reports[1:] + [report(30)]))
    runs = target.projection(now=BASE + 600)['runs']
    assert len(runs) == 31
    assert next(r for r in runs if r['report']['BattleDate'] == reports[0]['BattleDate'])['id'] == original_ids[reports[0]['BattleDate']]
    assert next(r for r in runs if r['report']['Wave'] == 0)['report'] == reports[0]
    assert all(r['account_id'] == 'a' and r['source'] == 'game_export' for r in runs)
    with db.reader(target.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_journal_changes_are_sampled_and_completed_labs_are_exact(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    assert target.projection(now=BASE)['journal'] == []
    completed = {'dateCompleted': stamp(BASE + 250), 'labNumber': 0, 'level': 2}
    assert ingest(target, export(BASE + 300, Coins=13.5, ResearchLevel=[2], CompletedLabs=[completed]))
    journal = target.projection(now=BASE + 300)['journal']
    interval = next(row for row in journal if row['kind'] == 'snapshot_change')
    assert interval['observed_from'] == BASE and interval['observed_to'] == BASE + 300
    assert interval['occurred_at'] is None
    assert 'between' in interval['summary'].lower()
    assert interval['changes']['wallet']['after']['coins'] == 13.5
    exact = next(row for row in journal if row['kind'] == 'lab_completed')
    assert exact['occurred_at'] == BASE + 250
    assert exact['changes']['raw'] == completed
    assert ingest(target, export(BASE + 600, Coins=13.5, ResearchLevel=[2], CompletedLabs=[completed]))
    assert len(target.projection(now=BASE + 600)['journal']) == 2


def test_corrected_report_reuses_identity_and_carries_source_metadata(tmp_path: Path) -> None:
    target = store(tmp_path)
    original = report(42)
    assert ingest(target, export(BattleHistory=[original]))
    original_id = target.projection(now=BASE)['runs'][0]['id']
    corrected = {**original, 'CoinsEarned': 13.75}
    assert ingest(target, export(BASE + 300, BattleHistory=[corrected]))
    runs = target.projection(now=BASE + 300)['runs']
    assert len(runs) == 1 and runs[0]['id'] == original_id
    assert runs[0]['report'] == corrected
    assert runs[0]['scope_key'] == 'generation-1'
    assert runs[0]['endpoint'] == 'device:1'


def test_projection_is_bounded_without_truncating_retained_history(tmp_path: Path) -> None:
    target = store(tmp_path)
    reports = [report(number) for number in range(501)]
    completed = [{'dateCompleted': stamp(BASE - number), 'labNumber': 0, 'level': number}
                 for number in range(201)]
    assert ingest(target, export(BattleHistory=reports, CompletedLabs=completed))
    result = target.projection(now=BASE)
    assert len(result['runs']) == 500
    assert len(result['journal']) == 200
    assert result['runs'][0]['wave'] == 500
    with sqlite3.connect(target.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM export_reports').fetchone()[0] == 501
        assert conn.execute('SELECT COUNT(*) FROM export_journal').fetchone()[0] == 201


def test_wave_estimate_is_bounded_separate_and_requires_continuity(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    assert target.projection(now=BASE + 20)['current_run']['estimated_wave'] is None
    assert ingest(target, export(BASE + 300, CurrentWave=50, RealTimeThisRound=400, GameplayTimeThisRound=1600))
    run = target.projection(now=BASE + 450)['current_run']
    assert run['observed_wave'] == 50 and run['estimated_wave'] == 65
    assert run['wave'] == 50 and run['estimate_label'] == 'Estimated'
    assert target.projection(now=BASE + 601)['current_run']['estimated_wave'] is None
    assert target.projection(now=BASE + 1000)['current_run']['estimated_wave'] is None
    assert ingest(target, export(BASE + 600, CurrentWave=80, RealTimeThisRound=700, GameplayTimeThisRound=2800), scope='generation-2')
    assert target.projection(now=BASE + 650)['current_run']['estimated_wave'] is None
    assert ingest(target, export(BASE + 900, CurrentWave=2, RealTimeThisRound=10))
    assert target.projection(now=BASE + 950)['current_run']['estimated_wave'] is None


@pytest.mark.parametrize('change', [{'RoundActiveBool': False}, {'CurrentTier': 3}])
def test_stopped_or_changed_tier_disables_wave_estimate(tmp_path: Path, change: dict) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    assert ingest(target, export(BASE + 300, CurrentWave=50, RealTimeThisRound=400, **change))
    assert target.projection(now=BASE + 350)['current_run']['estimated_wave'] is None


def test_elapsed_time_reset_or_endpoint_change_disables_estimate(tmp_path: Path) -> None:
    target = store(tmp_path)
    assert ingest(target, export())
    assert ingest(target, export(BASE + 300, CurrentWave=50, RealTimeThisRound=150, GameplayTimeThisRound=600))
    assert target.projection(now=BASE + 350)['current_run']['estimated_wave'] is None
    assert ingest(target, export(BASE + 600, CurrentWave=80, RealTimeThisRound=450, GameplayTimeThisRound=1800), endpoint='device:2')
    assert target.projection(now=BASE + 650)['current_run']['estimated_wave'] is None


def test_preserves_exact_raw_snapshot_and_unknown_schema_read_is_unavailable(tmp_path: Path) -> None:
    target = store(tmp_path)
    raw = json.dumps(export(), indent=2)
    assert target.ingest(raw, endpoint='device:1', scope_key='scope', captured_at=BASE + 1)
    with sqlite3.connect(target.path) as conn:
        assert conn.execute('SELECT raw FROM export_snapshots').fetchone()[0] == raw
    other = tmp_path / 'other'
    other.mkdir()
    old = store(other)
    with sqlite3.connect(old.path) as conn:
        conn.execute('CREATE TABLE legacy(value TEXT)')
    assert not old.projection(now=BASE)['available']
