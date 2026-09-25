"""Saved copies are versioned independently of published account assignments."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fleet.build_route import RouteDocument, resolve_route
from fleet.strategy_library import StrategyLibrary, LibraryConflict


def test_save_versions_do_not_publish_and_old_version_is_immutable(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    template = library.read()["templates"][0]
    first = library.save(expected_revision=0, name="My opening", source_template="opening",
                         baseline=template["baseline"])
    saved = first["strategies"][0]
    baseline = saved["baseline"]
    baseline["workshop"]["coin_spend_limit_pct"] = 35
    second = library.save(expected_revision=1, name="My opening", source_template="opening",
                          baseline=baseline, strategy_id=saved["id"])
    assert second["strategies"][0]["version"] == 2
    assert library.version(saved["id"], 1)["baseline"]["workshop"]["coin_spend_limit_pct"] == 100
    assert not (tmp_path / "build-route.json").exists()
    with pytest.raises(LibraryConflict):
        library.save(expected_revision=0, name="Stale", source_template="opening", baseline=baseline)


def test_templates_are_protected_and_invalid_blocks_rejected(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    templates = library.read()["templates"]
    assert [(item["id"], item["builtin"]) for item in templates] == [("opening", True), ("turtle", True)]
    baseline = templates[0]["baseline"]
    assert baseline["workshop"]["mode"] == "blocks"
    with pytest.raises(ValueError, match="protected"):
        library.save(expected_revision=0, name="Overwrite", source_template="opening",
                     baseline=baseline, strategy_id="opening")
    baseline["workshop"]["blocks"] = [{"id": "bad", "type": "script", "code": "run()"}]
    with pytest.raises(ValueError):
        library.save(expected_revision=0, name="Unsafe", source_template="opening", baseline=baseline)


def test_assignment_takes_precedence_and_replacement_falls_back_safely() -> None:
    raw = RouteDocument.compatibility().to_dict()
    assigned = RouteDocument.compatibility().to_dict()["baseline"]
    assigned["workshop"]["coin_spend_limit_pct"] = 35
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = 80
    raw["overrides"] = {"Air_1": {"account_id": "A", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 5}}}}
    raw["assignments"] = {"Air_1": {"account_id": "A", "strategy_id": "custom-1",
        "strategy_version": 1, "strategy_name": "Safe", "baseline": assigned}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_1", "A").workshop.coin_spend_limit_pct == 35
    replacement = resolve_route(route, "Air_1", "B")
    assert replacement.workshop.coin_spend_limit_pct == 100
    assert replacement.override_state == "inactive_account_changed"
    assert RouteDocument.from_dict(route.to_dict()) == route


def test_builtin_versions_are_assignable_but_only_version_one_exists(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    assert library.version("opening", 1)["builtin"] is True
    assert library.version("turtle", 1)["source_template"] == "turtle"
    with pytest.raises(ValueError):
        library.version("opening", 2)


def test_strategy_names_are_unique_casefolded_and_reserved(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    for name in (" Opening ", "TURTLE"):
        with pytest.raises(ValueError, match="name.*exists"):
            library.save(expected_revision=0, name=name, source_template="opening", baseline=baseline)
    saved = library.save(expected_revision=0, name="My build", source_template="opening", baseline=baseline)
    with pytest.raises(ValueError, match="name.*exists"):
        library.save(expected_revision=1, name=" MY BUILD ", source_template="opening", baseline=baseline)
    assert library.save(expected_revision=1, name="MY BUILD", source_template="opening", baseline=baseline,
                        strategy_id=saved["strategies"][0]["id"])["revision"] == 2


def test_concurrent_duplicate_saves_cannot_create_two_names(tmp_path: Path) -> None:
    baseline = StrategyLibrary(tmp_path).read()["templates"][0]["baseline"]
    ready = Barrier(2)

    def save_copy(name: str) -> str:
        library = StrategyLibrary(tmp_path)
        ready.wait(timeout=5)
        try:
            library.save(expected_revision=0, name=name, source_template="opening", baseline=baseline)
            return "saved"
        except LibraryConflict:
            # A caller retrying against the fresh revision must still lose the duplicate-name race.
            with pytest.raises(ValueError, match="name.*exists"):
                library.save(expected_revision=1, name=name, source_template="opening", baseline=baseline)
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save_copy, ["My build", "MY BUILD"]))
    assert sorted(outcomes) == ["duplicate", "saved"]
    assert len(StrategyLibrary(tmp_path).read()["strategies"]) == 1
