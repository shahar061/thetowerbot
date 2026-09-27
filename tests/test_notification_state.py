"""Notification edges survive restarts without inventing a new reward."""
from __future__ import annotations

import json

import events
import pytest
from evidence_scope import FactScope, ScopeContinuity
from fleet.identity import IdentityEvidence
from notification_state import MissionReceiptBus, NotificationState


SCOPE = {"account_id": "account-1", "lease_id": "lease-1",
         "attempt_id": "attempt-1", "generation": "generation-1"}


def prepare_for_event(state: NotificationState, event: events.MissionClaimed) -> None:
    state.prepare_claim(mission=event.mission, mission_id=event.mission_id,
                        coins=event.coins, gems=event.gems,
                        completed_before=event.completed_before,
                        completed_target=35, visible_before=[], now=100)


def test_two_captures_confirm_but_repeating_one_capture_does_not(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.observe("labs", True, 100, frame_id=1)
    state.observe("labs", True, 101, frame_id=1)
    assert not state.eligible("labs", 101)
    state.observe("labs", True, 102, frame_id=2)
    assert state.eligible("labs", 102)
    assert state.snapshot()["kinds"]["labs"]["generation"] == 1


def test_unreadable_and_one_clear_frame_preserve_generation(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.observe("labs", True, 100, frame_id=1)
    state.observe("labs", True, 101, frame_id=2)
    state.observe("labs", False, 102, frame_id=3)
    state.observe("labs", None, 103, frame_id=4)
    assert not state.eligible("labs", 103)
    assert state.snapshot()["kinds"]["labs"]["generation"] == 1
    state.observe("labs", False, 104, frame_id=5)
    state.observe("labs", False, 105, frame_id=6)
    assert not state.eligible("labs", 105)
    state.observe("labs", True, 106, frame_id=7)
    state.observe("labs", True, 107, frame_id=8)
    assert state.eligible("labs", 107)
    assert state.snapshot()["kinds"]["labs"]["generation"] == 2


def test_unknown_current_mission_reading_holds_action_without_erasing_edge(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    state.observe("missions", None, 102, frame_id=3)
    assert state.snapshot()["kinds"]["missions"]["generation"] == 1
    assert state.snapshot()["state"] == "unknown"
    assert not state.eligible("missions", 102)
    state.observe("missions", True, 103, frame_id=4)
    assert state.eligible("missions", 103)


def test_failure_backs_off_but_new_generation_is_immediate(tmp_path) -> None:
    path = tmp_path / "notification.json"
    state = NotificationState(path, scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    assert state.eligible("missions", 101)
    state.finish("missions", 101, claimed=False)
    assert not state.eligible("missions", 160)
    assert NotificationState(path, scope=SCOPE).eligible("missions", 161)
    state = NotificationState(path, scope=SCOPE)
    state.finish("missions", 161, claimed=False)
    assert not state.eligible("missions", 280)
    assert state.eligible("missions", 281)
    state.observe("missions", False, 282, frame_id=3)
    state.observe("missions", False, 283, frame_id=4)
    state.observe("missions", True, 284, frame_id=5)
    state.observe("missions", True, 285, frame_id=6)
    assert state.eligible("missions", 285)


def test_success_is_deduped_and_uncertain_attempt_remains_pending(tmp_path) -> None:
    path = tmp_path / "notification.json"
    state = NotificationState(path, scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    state.begin("missions", 101)
    state.finish("missions", 102, claimed=None)
    assert not NotificationState(path, scope=SCOPE).eligible("missions", 10_000)
    state.finish("missions", 103, claimed=True)
    assert not state.eligible("missions", 10_000)
    state.finish("missions", 104, claimed=True)
    assert state.snapshot()["last_claim_at"] == 103
    assert json.loads(path.read_text())["last_claim_at"] == 103


def test_later_generation_success_advances_confirmed_claim_time(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    state.finish("missions", 102, claimed=True)
    state.observe("missions", False, 103, frame_id=3)
    state.observe("missions", False, 104, frame_id=4)
    state.observe("missions", True, 105, frame_id=5)
    state.observe("missions", True, 106, frame_id=6)
    state.finish("missions", 107, claimed=True)
    assert state.snapshot()["last_claim_at"] == 107


def test_scope_change_does_not_restore_old_generation_as_actionable(tmp_path) -> None:
    path = tmp_path / "notification.json"
    state = NotificationState(path, scope=SCOPE)
    state.observe("labs", True, 100, frame_id=1)
    state.observe("labs", True, 101, frame_id=2)
    other = NotificationState(path, scope={**SCOPE, "account_id": "account-2"})
    assert not other.eligible("labs", 101)
    assert other.snapshot()["state"] == "unknown"


def test_scope_rotation_archives_unconsumed_receipts(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    event = events.MissionClaimed(
        mission="Kill enemies", coins=120, gems=5,
        completed_before=2, completed_after=3)
    prepare_for_event(state, event)
    state.record_receipt(event, 101)
    other = NotificationState(path, scope={**SCOPE, "account_id": "account-2"})
    assert other.snapshot()["receipts"] == []
    (archive,) = tmp_path.glob("mission-notification-state-archive-*.json")
    saved = json.loads(archive.read_text())
    assert saved["scope"] == SCOPE
    assert saved["receipts"][0]["coins"] == 120


def test_same_account_restart_keeps_uncertain_claim_blocked(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    state.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[], now=101)
    next_scope = {**SCOPE, "generation": "generation-2"}
    restarted = NotificationState(path, scope=next_scope)
    assert restarted.snapshot()["kinds"]["missions"]["uncertain"]
    assert not restarted.eligible("missions", 10_000)
    assert NotificationState(path, scope=next_scope).snapshot()["kinds"]["missions"]["uncertain"]


def test_restart_of_walk_without_reward_tap_does_not_invent_uncertainty(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    next_scope = {**SCOPE, "generation": "generation-2"}
    restarted = NotificationState(path, scope=next_scope)
    assert restarted.snapshot()["pending_claim"] is None
    assert not restarted.snapshot()["kinds"]["missions"]["uncertain"]


def test_new_badge_generation_cannot_release_unresolved_mission_tap(tmp_path) -> None:
    state = NotificationState(tmp_path / "mission-notification-state.json", scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    state.finish("missions", 102, claimed=None)
    for visible, capture in ((False, 3), (False, 4), (True, 5), (True, 6)):
        state.observe("missions", visible, 102 + capture, frame_id=capture)
    assert state.snapshot()["kinds"]["missions"]["generation"] == 2
    assert not state.eligible("missions", 110)
    assert state.snapshot()["kinds"]["missions"]["uncertain"]


def test_scope_rotated_unresolved_tap_stays_blocked_after_new_badge(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.finish("missions", 100, claimed=None)
    next_scope = {**SCOPE, "generation": "generation-2"}
    restarted = NotificationState(path, scope=next_scope)
    restarted.observe("missions", True, 101, frame_id="capture-1")
    restarted.observe("missions", True, 102, frame_id="capture-2")
    assert not restarted.eligible("missions", 102)
    assert restarted.snapshot()["state"] == "unknown"


def test_unreadable_current_frame_never_reports_historical_clear(tmp_path) -> None:
    state = NotificationState(tmp_path / "mission-notification-state.json", scope=SCOPE)
    state.observe("missions", False, 100, frame_id=1)
    state.observe("missions", False, 101, frame_id=2)
    assert state.snapshot()["state"] == "clear"
    state.observe("missions", None, 102, frame_id=3)
    assert state.snapshot()["state"] == "unknown"


def test_unreadable_current_frame_never_reports_historical_claimed_clear(tmp_path) -> None:
    state = NotificationState(tmp_path / "mission-notification-state.json", scope=SCOPE)
    state.observe("missions", True, 100, frame_id=1)
    state.observe("missions", True, 101, frame_id=2)
    state.finish("missions", 102, claimed=True)
    assert state.snapshot()["state"] == "clear"
    state.observe("missions", None, 103, frame_id=3)
    assert state.snapshot()["state"] == "unknown"


def test_confirmed_reward_receipt_is_durable_and_deduped_across_restart(tmp_path) -> None:
    path = tmp_path / "notification.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    event = events.MissionClaimed(mission="Kill enemies", mission_id="kill_enemies",
                                  coins=120, gems=5, completed_before=2,
                                  completed_after=3)
    prepare_for_event(state, event)
    assert state.record_receipt(event, 101)
    assert not state.record_receipt(event, 102)
    restarted = NotificationState(path, scope=SCOPE)
    assert not restarted.record_receipt(event, 103)
    (receipt,) = restarted.snapshot()["receipts"]
    assert len(receipt["key"]) == 64
    assert {key: receipt[key] for key in ("mission_id", "mission", "coins", "gems",
                                           "completed_before", "completed_after", "confirmed_at")} == {
        "mission_id": "kill_enemies", "mission": "Kill enemies", "coins": 120,
        "gems": 5, "completed_before": 2, "completed_after": 3,
        "confirmed_at": 101,
    }
    assert restarted.snapshot()["last_claim_at"] == 101


def test_receipt_persists_before_lossy_event_bus_publication(tmp_path) -> None:
    path = tmp_path / "notification.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)

    class DroppingBus:
        def publish(self, event: events.Event) -> events.Event:
            assert json.loads(path.read_text())["receipts"][0]["gems"] == 5
            assert isinstance(event, events.MissionClaimed)
            assert event.receipt_key == json.loads(path.read_text())["receipts"][0]["key"]
            return event

    wrapped = MissionReceiptBus(DroppingBus(), state)
    event = events.MissionClaimed(mission="Kill enemies", gems=5,
                                  completed_before=2, completed_after=3)
    prepare_for_event(state, event)
    assert wrapped.publish(event).receipt_key == state.snapshot()["receipts"][0]["key"]


def test_duplicate_receipt_cannot_forward_a_second_credit(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.begin("missions", 100)

    class CountingBus:
        count = 0

        def publish(self, event: events.Event) -> events.Event:
            self.count += 1
            return event

    bus = CountingBus()
    wrapped = MissionReceiptBus(bus, state)
    event = events.MissionClaimed(mission="Kill enemies", coins=120, gems=5,
                                  completed_before=2, completed_after=3)
    prepare_for_event(state, event)
    wrapped.publish(event)
    wrapped.publish(event)
    assert bus.count == 1


def test_failed_receipt_flush_can_be_retried_without_phantom_dedup(tmp_path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.begin("missions", 100)
    event = events.MissionClaimed(mission="Kill enemies", gems=5,
                                  completed_before=2, completed_after=3)
    prepare_for_event(state, event)
    save = state._save
    monkeypatch.setattr(state, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        state.record_receipt(event, 101)
    assert state.snapshot()["receipts"] == []
    monkeypatch.setattr(state, "_save", save)
    assert state.record_receipt(event, 102)


def test_scope_rotation_save_failure_keeps_current_intent_and_recovers(tmp_path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    state.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"]], now=101)
    rotated = {**SCOPE, "generation": "generation-2"}
    original_save = NotificationState._save

    def fail_new_scope(self: NotificationState) -> None:
        if self.scope == rotated:
            raise OSError("disk full")
        original_save(self)

    monkeypatch.setattr(NotificationState, "_save", fail_new_scope)
    with pytest.raises(OSError, match="disk full"):
        NotificationState(path, scope=rotated)
    assert json.loads(path.read_text())["pending_claim"]["mission_id"] == "kill"
    assert list(tmp_path.glob("mission-notification-state-archive-*.json"))
    monkeypatch.setattr(NotificationState, "_save", original_save)
    recovered = NotificationState(path, scope=rotated)
    assert recovered.snapshot()["pending_claim"]["mission_id"] == "kill"
    assert not recovered.eligible("missions", 1_000)


def test_read_only_verification_requires_original_scope_and_matching_cards(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    state = NotificationState(path, scope=SCOPE)
    state.begin("missions", 100)
    state.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"],
                                        ["neighbor", "Buy upgrades", "claimable"]], now=101)
    observed = {"screen_id": "missions.daily", "error": None, "completed": 3,
                "completed_target": 35, "claims": (),
                "visible": (("neighbor", "Buy upgrades", "claimable"),)}
    assert state.reconcile_claim(observed, frame_id="capture-1", now=104) == "claimed"
    assert state.snapshot()["pending_claim"] is None
    assert len(state.snapshot()["receipts"]) == 1

    other_path = tmp_path / "other.json"
    other = NotificationState(other_path, scope=SCOPE)
    other.begin("missions", 100)
    other.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"],
                                        ["neighbor", "Buy upgrades", "claimable"]], now=101)
    rotated = NotificationState(other_path, scope={**SCOPE, "generation": "generation-2"})
    assert rotated.reconcile_claim(observed, frame_id="capture-2", now=104) == "unknown"
    assert rotated.snapshot()["receipts"] == []
    assert rotated.snapshot()["pending_claim"] is not None


def test_read_only_no_reward_needs_two_distinct_captures_and_backs_off(tmp_path) -> None:
    state = NotificationState(tmp_path / "notification.json", scope=SCOPE)
    state.begin("missions", 100)
    state.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"]], now=101)
    class Claim:
        mission_id = "kill"
        raw_text = "Kill enemies"
    evidence = {"screen_id": "missions.daily", "error": None, "completed": 2,
                "completed_target": 35, "claims": (Claim(),),
                "visible": (("kill", "Kill enemies", "claimable"),)}
    assert state.reconcile_claim(evidence, frame_id="one", now=104) == "unknown"
    assert state.reconcile_claim(evidence, frame_id="one", now=105) == "unknown"
    assert state.reconcile_claim(evidence, frame_id="two", now=106) == "no_reward"
    assert state.snapshot()["pending_claim"] is None
    assert state.snapshot()["kinds"]["missions"]["retry_at"] == 166


def test_full_worker_restart_retries_bounded_read_only_visits_but_keeps_ambiguous_tap(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    first = NotificationState(path, scope=SCOPE)
    first.begin("missions", 100)
    first.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"],
                                        ["neighbor", "Buy upgrades", "claimable"]], now=101)
    rotated_scope = {**SCOPE, "generation": "generation-2"}
    restarted = NotificationState(path, scope=rotated_scope)
    assert restarted.snapshot()["pending_claim"]["scope"] == SCOPE
    assert restarted.verification_due(105)
    restarted.note_verification_visit(105)
    assert not NotificationState(path, scope=rotated_scope).verification_due(106)
    observed = {"screen_id": "missions.daily", "error": None, "completed": 3,
                "completed_target": 35, "claims": (),
                "visible": (("neighbor", "Buy upgrades", "claimable"),)}
    assert restarted.reconcile_claim(observed, frame_id="new-boot-1", now=106) == "unknown"
    assert restarted.snapshot()["pending_claim"] is not None
    restarted.note_verification_visit(165)
    restarted.note_verification_visit(285)
    assert not restarted.verification_due(10_000)
    assert not restarted.eligible("missions", 10_000)


def test_rotated_positive_reconciliation_requires_matching_typed_continuity(tmp_path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "mission-notification-state.json"
    original_scope = {**SCOPE, "fact_epoch": 7}
    first = NotificationState(path, scope=original_scope)
    first.begin("missions", 100)
    first.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[["kill", "Kill enemies", "claimable"],
                                        ["neighbor", "Buy upgrades", "claimable"]], now=101)
    current_scope = {**original_scope, "generation": "generation-2"}
    restarted = NotificationState(path, scope=current_scope)
    observed = {"screen_id": "missions.daily", "error": None, "completed": 3,
                "completed_target": 35, "claims": (),
                "visible": (("neighbor", "Buy upgrades", "claimable"),)}
    assert restarted.reconcile_claim(observed, frame_id="one", now=102) == "unknown"
    proof = ScopeContinuity(
        FactScope("account-1", "lease-1", "generation-1", 7),
        FactScope("account-1", "lease-1", "generation-2", 7),
        IdentityEvidence("account-1", 101, "identity-101"), tmp_path)
    monkeypatch.setattr(ScopeContinuity, "valid", lambda self, *, now: True)
    assert restarted.reconcile_claim(observed, frame_id="two", now=103,
                                     continuity=proof) == "claimed"
    assert len(restarted.snapshot()["receipts"]) == 1


def test_account_return_restores_archived_unresolved_intent(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    first = NotificationState(path, scope=SCOPE)
    first.begin("missions", 100)
    first.prepare_claim(mission="Kill enemies", mission_id="kill", coins=25,
                        gems=3, completed_before=2, completed_target=35,
                        visible_before=[], now=101)
    NotificationState(path, scope={**SCOPE, "account_id": "other"})
    returned = NotificationState(path, scope={**SCOPE, "generation": "generation-3"})
    assert returned.snapshot()["pending_claim"]["mission_id"] == "kill"
    assert not returned.eligible("missions", 10_000)


def test_confirmed_archived_intent_is_not_resurrected_on_account_return(tmp_path) -> None:
    path = tmp_path / "mission-notification-state.json"
    first = NotificationState(path, scope=SCOPE)
    first.begin("missions", 100)
    event = events.MissionClaimed(mission="Kill enemies", mission_id="kill",
                                  coins=25, gems=3, completed_before=2,
                                  completed_after=3)
    prepare_for_event(first, event)
    rotated = NotificationState(path, scope={**SCOPE, "generation": "generation-2"})
    # Real positive reconciliation after continuity validation will commit a
    # receipt in the new row while the old archive still holds the intent.
    assert rotated.record_receipt(event, 102)
    NotificationState(path, scope={**SCOPE, "account_id": "other"})
    returned = NotificationState(path, scope={**SCOPE, "generation": "generation-3"})
    assert returned.snapshot()["pending_claim"] is None


def _exhausted_intent(tmp_path):
    from notification_state import NotificationState
    scope = {'account_id': 'a', 'lease_id': 'l', 'attempt_id': 'x', 'generation': 'g'}
    state = NotificationState(tmp_path / 'm.json', scope=scope)
    state.begin('missions', 1.)
    state.prepare_claim(mission='Kill 100', mission_id='m1', coins=None, gems=5,
                        completed_before=2, completed_target=5, visible_before=[], now=1.)
    state.finish('missions', 2., claimed=None)
    return state, scope


def test_unproven_mission_intent_is_retained_after_verification_exhaustion(tmp_path) -> None:
    from notification_state import NotificationState
    state, scope = _exhausted_intent(tmp_path)
    t = 2.
    for _ in range(3):
        t += 4000.
        assert state.verification_due(t)
        state.note_verification_visit(t)
    assert not state.retain_exhausted_claim(t)  # The last walk's backoff still runs.
    assert state.snapshot()['kinds']['missions']['uncertain']
    assert state.retain_exhausted_claim(10**9)
    row = state.snapshot()
    assert row['pending_claim'] is None and row['receipts'] == []  # Never credited.
    assert row['retained_intents'][0]['retained_reason'] == 'verification_exhausted'
    assert not row['kinds']['missions']['uncertain'] and not row['kinds']['missions']['prior_uncertain']
    restarted = NotificationState(tmp_path / 'm.json', scope={**scope, 'attempt_id': 'y', 'generation': 'h'})
    assert restarted.snapshot()['pending_claim'] is None
    assert not restarted.verification_due(10**9)
    assert not restarted.snapshot()['kinds']['missions']['uncertain']


def test_operator_resolves_unproven_mission_intent_with_audit(tmp_path, capsys) -> None:
    from tools import resolve_mission_intent
    state, scope = _exhausted_intent(tmp_path)
    intent = state.snapshot()['pending_claim']['intent_id']
    path = str(tmp_path / 'm.json')
    assert resolve_mission_intent.main(['show', '--state', path]) == 0
    assert intent in capsys.readouterr().out
    assert resolve_mission_intent.main(['resolve', '--state', path, '--intent', intent,
        '--operator', 'op', '--evidence', 'reward inbox checked', '--worker-stopped']) == 0
    from notification_state import NotificationState
    row = NotificationState(tmp_path / 'm.json', scope=scope).snapshot()
    assert row['pending_claim'] is None and row['receipts'] == []
    assert row['retained_intents'][0]['operator'] == 'op'
    with pytest.raises(SystemExit):
        resolve_mission_intent.main(['resolve', '--state', path, '--intent', intent,
            '--operator', 'op', '--evidence', 'again', '--worker-stopped'])
