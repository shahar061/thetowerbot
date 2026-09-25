"""Durable route revisions cannot be silently replaced or reset."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from fleet.build_route import RouteDocument
from fleet.build_route_store import BuildRouteStore, RouteConflict, RouteUnavailable


def test_empty_store_reads_compatibility_without_creating_a_file(tmp_path: Path) -> None:
    store = BuildRouteStore(tmp_path)
    assert store.read() == RouteDocument.compatibility()
    assert not (tmp_path / "build-route.json").exists()


def test_publish_creates_new_immutable_revision_and_audit(tmp_path: Path) -> None:
    store = BuildRouteStore(tmp_path)
    saved = store.publish(RouteDocument.compatibility(), 0, "operator")
    assert saved.revision == 1
    assert store.read() == saved
    assert store.revisions() == (saved,)
    history = json.loads((tmp_path / "build-route-history" / "1.json").read_text())
    assert history["audit"]["actor"] == "operator"
    assert history["audit"]["source_revision"] == 0
    assert history["audit"]["target_revision"] == 1


def test_stale_publish_returns_current_revision(tmp_path: Path) -> None:
    store = BuildRouteStore(tmp_path)
    saved = store.publish(RouteDocument.compatibility(), 0, "first tab")
    with pytest.raises(RouteConflict) as failure:
        store.publish(RouteDocument.compatibility(), 0, "second tab")
    assert failure.value.current == saved


def test_rollback_creates_new_revision_without_mutating_history(tmp_path: Path) -> None:
    store = BuildRouteStore(tmp_path)
    one = store.publish(RouteDocument.compatibility(), 0, "operator")
    changed = replace(one, baseline=replace(one.baseline, workshop=replace(
        one.baseline.workshop, coin_spend_limit_pct=30)))
    two = store.publish(changed, 1, "operator")
    rolled = store.rollback(1, 2, "operator")
    assert (two.revision, rolled.revision) == (2, 3)
    assert rolled.baseline == one.baseline
    assert [route.revision for route in store.revisions()] == [one.revision, two.revision, rolled.revision]


def test_failed_atomic_replace_keeps_previous_revision_readable(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    store = BuildRouteStore(tmp_path)
    one = store.publish(RouteDocument.compatibility(), 0, "operator")
    real_replace = __import__("os").replace

    def fail_current(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "build-route.json":
            raise OSError("simulated replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr("fleet.build_route_store.os.replace", fail_current)
    with pytest.raises(OSError, match="simulated"):
        store.publish(one, 1, "operator")
    assert store.read() == one
    assert store.revisions() == (one,)
    monkeypatch.undo()
    assert store.publish(one, 1, "operator").revision == 2


def test_missing_current_after_publication_fails_closed(tmp_path: Path) -> None:
    store = BuildRouteStore(tmp_path)
    store.publish(RouteDocument.compatibility(), 0, "operator")
    (tmp_path / "build-route.json").unlink()
    with pytest.raises(RouteUnavailable, match="current route missing"):
        store.read()


def test_two_publishers_cannot_both_claim_revision_one(tmp_path: Path) -> None:
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def publish() -> None:
        store = BuildRouteStore(tmp_path)
        barrier.wait()
        try:
            store.publish(RouteDocument.compatibility(), 0, "operator")
            outcomes.append("saved")
        except RouteConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["conflict", "saved"]
