from dataclasses import fields, replace
from pathlib import Path

from strategy import Strategy


def test_import_preserves_purchases_and_local_timing() -> None:
    from strategy_compat import capture_legacy, restore_legacy, plan_owned_values
    source = Strategy.from_config('original')
    snapshot = capture_legacy(source, 'etag')
    local = replace(source, interval=source.interval + 1)
    restored = restore_legacy(local, snapshot)
    assert plan_owned_values(restored) == plan_owned_values(source)
    assert restored.interval == local.interval


def test_every_profile_field_has_one_owner() -> None:
    from strategy_compat import PLAN_FIELDS, BOT_FIELDS
    assert PLAN_FIELDS | BOT_FIELDS == {f.name for f in fields(Strategy)}
    assert not PLAN_FIELDS & BOT_FIELDS


def test_import_is_idempotent_and_original_survives(tmp_path: Path) -> None:
    from fleet.strategy_library import StrategyLibrary
    from strategy_compat import capture_legacy
    source = Strategy.from_config('original')
    library = StrategyLibrary(tmp_path)
    snapshot = capture_legacy(source, 'etag')
    first = library.import_legacy(snapshot, source_key='standalone:original')
    second = library.import_legacy(snapshot, source_key='standalone:original')
    assert first == second
    assert library.read()['revision'] == 1
    row = library.read()['strategies'][0]
    assert row['kind'] == 'legacy'
    assert library.version(row['id'], row['version'])['legacy_snapshot'] == snapshot


def test_import_captures_build_recipe_independently_of_current_registry() -> None:
    import builds
    from strategy_compat import capture_legacy, captured_build
    source = replace(Strategy.from_config('original'), build='opening')
    snapshot = capture_legacy(source, 'etag')
    recipe = captured_build(snapshot)
    assert recipe.id == 'opening'
    assert recipe.weights == builds.by_id('opening').weights


def test_captured_build_survives_registry_removal(monkeypatch) -> None:
    import builds
    from strategy_compat import capture_legacy, restore_legacy
    source = replace(Strategy.from_config('original'), build='opening')
    snapshot = capture_legacy(source, 'etag')
    monkeypatch.setattr(builds, 'by_id', lambda _: None)
    assert restore_legacy(source, snapshot).build == 'opening'


def test_imported_tournament_settings_are_preserved_and_plan_owned() -> None:
    import pytest
    from tournament_policy import TournamentConfig
    from strategy_compat import capture_legacy, restore_legacy, assert_settings_only
    source = replace(Strategy.from_config('original'), tournament=TournamentConfig(enabled=True))
    local = replace(Strategy.from_config('local'), tournament=TournamentConfig(enabled=False))
    restored = restore_legacy(local, capture_legacy(source, 'etag'))
    assert restored.tournament.enabled
    with pytest.raises(ValueError, match='strategy_plan_owns_purchase_settings'):
        assert_settings_only(local, restored)
