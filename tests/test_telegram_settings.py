from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from telegram_settings import (
    TelegramProfile,
    TelegramSettingsError,
    TelegramSettingsStore,
    default_profile,
    legacy_telegram_interval_from_env,
)


def test_profiles_persist_independently(tmp_path: Path) -> None:
    path = tmp_path / "telegram-settings.json"
    store = TelegramSettingsStore(path)
    single = store.load("single").model_copy(update={"interval_minutes": 15})
    store.save("single", single)

    again = TelegramSettingsStore(path)
    assert again.load("single").interval_minutes == 15
    assert again.load("fleet").interval_minutes == 60
    assert path.stat().st_mode & 0o777 == 0o600


def test_every_save_changes_revision_even_when_profile_returns_to_same_values(tmp_path: Path) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    original, first_revision = store.load_with_revision("single")
    store.save("single", original.model_copy(update={"enabled": False}))
    _disabled, middle_revision = store.load_with_revision("single")
    store.save("single", original)
    current, final_revision = store.load_with_revision("single")
    assert current == original
    assert final_revision is not None and final_revision != middle_revision != first_revision
    assert store.load_with_revision("fleet")[1] is None


def test_corrupt_file_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "telegram-settings.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(TelegramSettingsError):
        TelegramSettingsStore(path).load("single")
    assert path.read_text(encoding="utf-8") == "{bad"


@pytest.mark.parametrize("fields", [["tier_wave", "tier_wave"], ["screen"]])
def test_rejects_invalid_fields_without_changing_saved_profile(
    tmp_path: Path, fields: list[str],
) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    before = store.load("fleet")
    with pytest.raises(ValueError, match="invalid_telegram_fields"):
        store.save("fleet", TelegramProfile(enabled=True, interval_minutes=15, fields=fields))
    assert store.load("fleet") == before


@pytest.mark.parametrize("interval", [True, 0, 1441, 2.5])
def test_interval_must_be_a_whole_minute_in_range(interval: object) -> None:
    with pytest.raises(ValidationError):
        TelegramProfile(enabled=True, interval_minutes=interval, fields=[])


def test_legacy_environment_sets_only_initial_default(tmp_path: Path) -> None:
    seconds = legacy_telegram_interval_from_env({"TELEGRAM_SUMMARY_SECONDS": "900"})
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json", initial_interval_seconds=seconds)
    assert store.load("single").interval_minutes == 15
    assert store.load("fleet").interval_minutes == 60
    assert default_profile("single", 30).interval_minutes == 60
