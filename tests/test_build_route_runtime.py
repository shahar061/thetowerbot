"""Workers acknowledge only validated, account-bound route revisions."""

from __future__ import annotations

from pathlib import Path

import pytest

import db
from account_state import AccountState
from fleet.build_route import RouteDocument
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore, RouteUnavailable
from fleet.reroll_progress import RerollProgress
from strategy import Strategy


def test_worker_loads_published_revision_and_acknowledges_account(tmp_path: Path) -> None:
    saved = BuildRouteStore(tmp_path).publish(RouteDocument.compatibility(), 0, "operator")
    worker = BuildRouteRuntime(tmp_path, "Air_38", "a1")
    assert worker.current() == saved
    assert worker.applied_revision() is None
    worker.acknowledge(saved.revision, "a1")
    assert worker.applied_revision() == 1
    assert BuildRouteRuntime(tmp_path, "Air_38", "a2").applied_revision() is None


def test_worker_fails_closed_on_corrupt_route(tmp_path: Path) -> None:
    (tmp_path / "build-route.json").write_text("{broken")
    worker = BuildRouteRuntime(tmp_path, "Air_38", "a1")
    with pytest.raises(RouteUnavailable):
        worker.current()
    assert worker.applied_revision() is None
    assert worker.error() is not None


def test_wrong_account_cannot_acknowledge(tmp_path: Path) -> None:
    worker = BuildRouteRuntime(tmp_path, "Air_38", "a1")
    with pytest.raises(ValueError, match="account"):
        worker.acknowledge(0, "a2")
    assert worker.applied_revision() is None


def test_corrupt_route_disables_worker_spending_but_keeps_progress_available(tmp_path: Path) -> None:
    root = tmp_path / "workers" / "Air_38"
    root.mkdir(parents=True)
    db.bind_account(root / "tower_bot.db", "a1")
    (tmp_path / "build-route.json").write_text("{broken")
    progress = RerollProgress(root, "a1", AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, "Air_38", "a1")
    policy = progress.shopping_policy(Strategy.from_config().shopping)
    assert not policy.enabled
    assert policy.workshop == ()
    assert progress.route_error is not None
