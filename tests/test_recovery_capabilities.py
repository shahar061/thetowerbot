from pathlib import Path
import pytest


def evidence() -> dict:
    return dict(schema_version=1, host_contract='towerbot-dismiss-v1', action='close_overlay',
                model='openai/gpt-5.4-nano', worker='worker-a', account_id='account-a',
                screen='STALL_ESCAPE', target='CLOSE', replay_sha256='a'*64,
                canary_sha256='b'*64, canary_kind='recorded_hardware',
                replay_passed=True, semantic_postcondition='overlay_absent',
                canary_postcondition_confirmed=True, recorded_at=1000., expires_at=2000.,
                operator='local-operator')


def test_registration_requires_separate_activation_and_matching_scope(tmp_path: Path) -> None:
    from recovery_capabilities import CapabilityRegistry, CalibrationEvidence
    store = CapabilityRegistry(tmp_path)
    assert store.active(now=1200.) == ()
    item = CalibrationEvidence.model_validate(evidence())
    identifier = store.register(item, now=1100.)
    assert store.active(now=1200.) == ()
    store.activate(identifier, operator='local-operator', now=1200.)
    grants = store.active(now=1300.)
    assert len(grants) == 1
    assert grants[0].allows(worker='worker-a', account_id='account-a', model=item.model,
                            screen=item.screen, target='CLOSE', action='close_overlay', now=1300.)
    assert not grants[0].allows(worker='other', account_id='account-a', model=item.model,
                                screen=item.screen, target='CLOSE', action='close_overlay', now=1300.)
    assert store.active(now=2100.) == ()
    store.revoke(identifier, operator='local-operator', now=1400.)
    assert store.active(now=1500.) == ()


@pytest.mark.parametrize('patch', [dict(host_contract='old-v0'), dict(action='open_screen'),
    dict(canary_kind='mock'), dict(replay_passed=False), dict(canary_postcondition_confirmed=False),
    dict(semantic_postcondition='screen_visible'), dict(canary_sha256='bad')])
def test_incompatible_evidence_is_rejected(patch: dict) -> None:
    from recovery_capabilities import CalibrationEvidence
    with pytest.raises(ValueError):
        CalibrationEvidence.model_validate(evidence() | patch)


def test_missing_store_is_empty_and_not_created(tmp_path: Path) -> None:
    from recovery_capabilities import CapabilityRegistry
    store = CapabilityRegistry(tmp_path)
    assert store.active(now=1200.) == ()
    assert store.allowed_actions(model='openai/gpt-5.4-nano', now=1200.) == ()
    assert not store.path.exists()


def test_incompatible_or_tampered_active_rows_never_grant(tmp_path: Path) -> None:
    import json
    import sqlite3
    from recovery_capabilities import CapabilityRegistry, CalibrationEvidence
    store = CapabilityRegistry(tmp_path)
    identifier = store.register(CalibrationEvidence.model_validate(evidence()), now=1100.)
    store.activate(identifier, operator='local-operator', now=1150.)
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO evidence VALUES (?,?,1)',
                   ('c' * 64, json.dumps(evidence() | dict(schema_version=0))))
        db.execute('INSERT INTO evidence VALUES (?,?,1)',
                   ('d' * 64, CalibrationEvidence.model_validate(evidence() | dict(worker='forged'))
                    .model_dump_json()))
    assert [g.worker for g in store.active(now=1200.)] == ['worker-a']
    with pytest.raises(ValueError):
        store.activate('e' * 64, operator='local-operator', now=1200.)
    with pytest.raises(ValueError):
        store.activate(identifier, operator=' ', now=1200.)


def test_cli_checks_artifact_digests_and_needs_separate_activation(tmp_path: Path, capsys) -> None:
    import hashlib
    import json
    import time
    from tools.calibrate_recovery import main
    from recovery_capabilities import CapabilityRegistry
    replay, canary = tmp_path / 'replay.jsonl', tmp_path / 'canary.jsonl'
    replay.write_text('synthetic replay')
    canary.write_text('synthetic canary')
    now = time.time()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(evidence() | dict(
        replay_sha256=hashlib.sha256(b'synthetic replay').hexdigest(),
        canary_sha256=hashlib.sha256(b'wrong').hexdigest(),
        recorded_at=now - 10, expires_at=now + 3600)))
    fleet = tmp_path / 'fleet'
    args = ['--fleet-root', str(fleet), 'register', str(manifest), '--replay', str(replay),
            '--recorded-canary', str(canary)]
    with pytest.raises(SystemExit):
        main(args)
    assert CapabilityRegistry(fleet).active(now=now) == ()
    document = json.loads(manifest.read_text())
    document['canary_sha256'] = hashlib.sha256(b'synthetic canary').hexdigest()
    manifest.write_text(json.dumps(document))
    main(args)
    identifier = capsys.readouterr().out.strip()
    assert CapabilityRegistry(fleet).active(now=time.time()) == ()
    main(['--fleet-root', str(fleet), 'activate', identifier, '--operator', 'local-operator'])
    assert CapabilityRegistry(fleet).allowed_actions(model=document['model'], now=time.time()) == (
        'close_overlay',)
    assert [row[1] for row in CapabilityRegistry(fleet).audit()] == ['register', 'activate']


def test_settings_assist_placeholder_reads_only_validated_grants(tmp_path: Path) -> None:
    import time
    from recovery_capabilities import CapabilityRegistry, CalibrationEvidence
    from recovery_policy import RecoverySettings
    from recovery_settings import RecoverySettingsStore
    store = RecoverySettingsStore(tmp_path)
    with pytest.raises(ValueError, match='assist_uncalibrated'):
        store.save(RecoverySettings(mode='assist'), expected_settings_revision=1,
                   expected_policy_revision=1)
    now = time.time()
    registry = CapabilityRegistry(tmp_path)
    identifier = registry.register(CalibrationEvidence.model_validate(
        evidence() | dict(recorded_at=now - 10, expires_at=now + 3600)), now=now)
    assert store.read().assist_allowed_actions == ()
    registry.activate(identifier, operator='local-operator', now=now)
    assert store.read().assist_allowed_actions == ('close_overlay',)
    with pytest.raises(ValueError, match='assist_uncalibrated'):
        store.save(RecoverySettings(mode='assist', model='other/model'),
                   expected_settings_revision=1, expected_policy_revision=1)
    saved = store.save(RecoverySettings(mode='assist'), expected_settings_revision=1,
                       expected_policy_revision=1)
    assert saved.settings.mode == 'assist'


def test_active_column_without_activation_audit_never_grants(tmp_path: Path) -> None:
    import sqlite3
    from recovery_capabilities import CapabilityRegistry, CalibrationEvidence
    store = CapabilityRegistry(tmp_path)
    identifier = store.register(CalibrationEvidence.model_validate(evidence()), now=1100.)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE evidence SET active=1 WHERE id=?', (identifier,))
    assert store.active(now=1200.) == ()
    store.activate(identifier, operator='local-operator', now=1150.)
    assert len(store.active(now=1200.)) == 1
    store.revoke(identifier, operator='local-operator', now=1160.)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE evidence SET active=1 WHERE id=?', (identifier,))
    assert store.active(now=1200.) == ()


def test_authority_writers_replace_config_epoch_after_commit(tmp_path: Path) -> None:
    from recovery_capabilities import CapabilityRegistry, CalibrationEvidence
    from recovery_policy import RecoverySettings
    from recovery_settings import RecoverySettingsStore
    from recovery_status import read_config_epoch
    seen = {read_config_epoch(tmp_path)}
    store = RecoverySettingsStore(tmp_path)
    store.save(RecoverySettings(mode='shadow'), shadow_worker='worker-a',
               expected_settings_revision=1, expected_policy_revision=1)
    seen.add(read_config_epoch(tmp_path))
    registry = CapabilityRegistry(tmp_path)
    identifier = registry.register(CalibrationEvidence.model_validate(evidence()), now=1100.)
    registry.activate(identifier, operator='local-operator', now=1150.)
    seen.add(read_config_epoch(tmp_path))
    registry.revoke(identifier, operator='local-operator', now=1160.)
    seen.add(read_config_epoch(tmp_path))
    assert len(seen) == 4 and None in seen
