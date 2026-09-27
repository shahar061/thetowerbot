"""Durable recovery eligibility, independent of scans, providers and devices."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from recovery_budget import RecoveryBudget


@pytest.fixture
def episodes(tmp_path: Path) -> Any:
    import recovery_episodes
    return recovery_episodes.RecoveryEpisodes(tmp_path)


def namespace(worker: str = "worker", account: str = "account") -> Any:
    from recovery_episodes import EpisodeNamespace
    return EpisodeNamespace(worker=worker, account_id=account)


def open_episode(store: Any, *, scope: str = "boot-1", now: float = 1_000.0,
                 ns: Any = None, fingerprint: str = "stall") -> Any:
    return store.get_or_open(namespace=ns or namespace(), fingerprint=fingerprint,
                             scope_token=scope, now=now)


def reserve(store: Any, episode: Any, action_id: str, *, source: str = "deterministic",
            scope: str = "boot-1", ns: Any = None) -> bool:
    return store.reserve_action(namespace=ns or namespace(), incident_id=episode.incident_id,
                                expected_revision=episode.revision, scope_token=scope,
                                action_id=action_id, source=source)


def finish(store: Any, episode: Any, action_id: str, outcome: str = "no_effect") -> Any:
    return store.record_outcome(namespace=namespace(), incident_id=episode.incident_id,
                                action_id=action_id, outcome=outcome)


def test_restart_reuses_incident_and_call_budget(episodes: Any, tmp_path: Path) -> None:
    first = open_episode(episodes)
    assert first.opened and first.blocker is None
    ledger = RecoveryBudget(tmp_path)
    for request_id in ("r1", "r2"):
        assert ledger.reserve(request_id=request_id, incident_id=first.episode.incident_id,
                              day="2026-09-27", maximum_microusd=1)
    restarted = type(episodes)(tmp_path)
    reused = open_episode(restarted, scope="boot-2")
    assert reused.episode.incident_id == first.episode.incident_id
    assert not reused.opened and reused.blocker == "scope_mismatch"
    rebound = restarted.rebind_scope(namespace=namespace(), incident_id=reused.episode.incident_id,
                                     expected_revision=reused.episode.revision, scope_token="boot-2")
    assert rebound.incident_id == first.episode.incident_id
    assert not RecoveryBudget(tmp_path).reserve(request_id="r3", incident_id=rebound.incident_id,
                                                day="2026-09-27", maximum_microusd=1)
    assert not reserve(episodes, first.episode, "old-worker")
    assert reserve(restarted, rebound, "new-worker", scope="boot-2")


def test_combined_action_allowance_and_duplicate_ids_survive_restart(episodes: Any,
                                                                  tmp_path: Path) -> None:
    episode = open_episode(episodes).episode
    for index, source in enumerate(("deterministic", "model", "deterministic")):
        assert reserve(episodes, episode, f"a{index}", source=source)
        episode = finish(episodes, episode, f"a{index}")
        episodes = type(episodes)(tmp_path)
        assert not reserve(episodes, episode, f"a{index}", source=source)
    assert episode.status == "exhausted"
    assert episode.actions_used == 3 and episode.actions_remaining == 0
    assert not reserve(episodes, episode, "fourth")
    assert open_episode(episodes, now=10_000).blocker == "exhausted"


def test_uncertain_action_blocks_restart_rebind_close_and_new_dispatch(episodes: Any,
                                                                     tmp_path: Path) -> None:
    episode = open_episode(episodes).episode
    assert reserve(episodes, episode, "uncertain")
    restarted = type(episodes)(tmp_path)
    existing = open_episode(restarted)
    assert existing.blocker == "unresolved"
    assert existing.episode.unresolved_action_ids == ("uncertain",)
    assert not reserve(restarted, existing.episode, "retry")
    uncertain = finish(restarted, episode, "uncertain", "unknown")
    assert uncertain.status == "unresolved" and uncertain.actions_used == 1
    with pytest.raises(ValueError, match="unresolved"):
        restarted.rebind_scope(namespace=namespace(), incident_id=episode.incident_id,
                               expected_revision=uncertain.revision, scope_token="boot-2")
    with pytest.raises(ValueError, match="unresolved"):
        restarted.close(namespace=namespace(), incident_id=episode.incident_id,
                        expected_revision=uncertain.revision, outcome="invalidated", now=1_001)
    resolved = finish(restarted, episode, "uncertain", "not_dispatched")
    assert resolved.actions_used == 1 and resolved.status == "open"
    assert finish(restarted, episode, "uncertain", "not_dispatched") == resolved
    with pytest.raises(ValueError, match="conflicting"):
        finish(restarted, episode, "uncertain", "confirmed")


def test_cooldown_restart_clock_reversal_and_duplicate_close(episodes: Any, tmp_path: Path) -> None:
    episode = open_episode(episodes).episode
    # A later observation establishes a durable high-water mark.
    open_episode(episodes, now=2_000)
    closed = episodes.close(namespace=namespace(), incident_id=episode.incident_id,
                            expected_revision=episode.revision, outcome="recovered", now=900)
    assert closed.cooldown_until == 2_600
    reopened = type(episodes)(tmp_path)
    assert open_episode(reopened, now=800).blocker == "cooldown"
    assert open_episode(reopened, now=2_599).blocker == "cooldown"
    duplicate = reopened.close(namespace=namespace(), incident_id=closed.incident_id,
                               expected_revision=closed.revision, outcome="recovered", now=2_599)
    assert duplicate.cooldown_until == 2_600
    next_episode = open_episode(reopened, now=2_600)
    assert next_episode.opened and next_episode.episode.incident_id != episode.incident_id
    assert reopened.get(namespace=namespace(), incident_id=episode.incident_id).status == "closed"


def test_namespaces_do_not_share_allowances_or_authorize_cross_account_actions(episodes: Any) -> None:
    first = open_episode(episodes).episode
    other_account = namespace(account="other")
    other_worker = namespace(worker="other")
    second = open_episode(episodes, ns=other_account).episode
    third = open_episode(episodes, ns=other_worker).episode
    assert len({first.incident_id, second.incident_id, third.incident_id}) == 3
    assert not reserve(episodes, first, "cross-account", ns=other_account)
    assert episodes.get(namespace=other_account, incident_id=first.incident_id) is None
    assert reserve(episodes, first, "unique")
    assert not reserve(episodes, second, "unique", ns=other_account)
    assert reserve(episodes, second, "other-action", ns=other_account)
    assert reserve(episodes, third, "third-action", ns=other_worker)


def test_atomic_open_and_action_races_share_one_incident_and_grant(episodes: Any,
                                                                tmp_path: Path) -> None:
    other = type(episodes)(tmp_path)
    gate = Barrier(2)

    def opening(store: Any) -> Any:
        gate.wait()
        return open_episode(store)

    with ThreadPoolExecutor(max_workers=2) as workers:
        opened = list(workers.map(opening, (episodes, other)))
    assert sum(item.opened for item in opened) == 1
    assert opened[0].episode.incident_id == opened[1].episode.incident_id
    episode = opened[0].episode

    def action(pair: tuple[Any, str]) -> bool:
        gate.wait()
        return reserve(pair[0], episode, pair[1])

    with ThreadPoolExecutor(max_workers=2) as workers:
        granted = list(workers.map(action, ((episodes, "a1"), (other, "a2"))))
    assert sorted(granted) == [False, True]
    assert episodes.lookup(namespace=namespace(), fingerprint="stall").actions_used == 1


def test_scope_change_cas_preserves_count_and_rejects_old_action_snapshot(episodes: Any) -> None:
    episode = open_episode(episodes).episode
    assert reserve(episodes, episode, "first")
    current = finish(episodes, episode, "first")
    rebound = episodes.rebind_scope(namespace=namespace(), incident_id=episode.incident_id,
                                   expected_revision=current.revision, scope_token="new-lease")
    assert rebound.actions_used == 1
    assert not reserve(episodes, current, "stale")
    with pytest.raises(ValueError, match="revision"):
        episodes.rebind_scope(namespace=namespace(), incident_id=episode.incident_id,
                              expected_revision=current.revision, scope_token="old-lease")
    assert reserve(episodes, rebound, "fresh", scope="new-lease")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1, True])
def test_invalid_clock_rejected(episodes: Any, now: float) -> None:
    with pytest.raises(ValueError):
        open_episode(episodes, now=now)


def test_fingerprint_change_does_not_reuse_other_incident(episodes: Any) -> None:
    first = open_episode(episodes).episode
    second = open_episode(episodes, fingerprint="other-stall").episode
    assert first.incident_id != second.incident_id
    assert episodes.lookup(namespace=namespace(), fingerprint="missing") is None


def test_closed_episode_cannot_dispatch_and_old_action_id_cannot_be_reused(episodes: Any) -> None:
    episode = open_episode(episodes).episode
    assert reserve(episodes, episode, "once")
    known = finish(episodes, episode, "once", "confirmed")
    closed = episodes.close(namespace=namespace(), incident_id=episode.incident_id,
                            expected_revision=known.revision, outcome="recovered", now=1_000)
    assert not reserve(episodes, closed, "late")
    with pytest.raises(ValueError, match="closed"):
        episodes.rebind_scope(namespace=namespace(), incident_id=closed.incident_id,
                              expected_revision=closed.revision, scope_token="new-lease")
    next_episode = open_episode(episodes, now=1_600).episode
    assert not reserve(episodes, next_episode, "once")
    assert reserve(episodes, next_episode, "fresh")


def test_other_account_cannot_reconcile_or_close_incident(episodes: Any) -> None:
    episode = open_episode(episodes).episode
    assert reserve(episodes, episode, "action")
    other = namespace(account="other")
    with pytest.raises(ValueError, match="namespace"):
        episodes.record_outcome(namespace=other, incident_id=episode.incident_id,
                                action_id="action", outcome="confirmed")
    current = episodes.get(namespace=namespace(), incident_id=episode.incident_id)
    with pytest.raises(ValueError, match="namespace"):
        episodes.close(namespace=other, incident_id=episode.incident_id,
                        expected_revision=current.revision, outcome="recovered", now=1_000)
    assert episodes.get(namespace=namespace(), incident_id=episode.incident_id).status == "unresolved"
