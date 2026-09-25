"""Validated local preferences for outbound Telegram status digests.

Credentials remain in the process environment. This file contains only
non-secret presentation and scheduling choices.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
from pathlib import Path
from typing import Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import config

TelegramMode = Literal["single", "fleet"]
SINGLE_FIELDS = ("screen", "scans", "wallet", "run", "runs_completed", "taps", "skips", "last_error")
FLEET_FIELDS = ("tier_wave", "lifetime_coins", "milestone", "errors")


class TelegramSettingsError(RuntimeError):
    """Persisted preferences could not be read or safely replaced."""


class TelegramProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)
    interval_minutes: int = Field(ge=1, le=1440, strict=True)
    fields: list[str]


def validate_fields(mode: TelegramMode, profile: TelegramProfile) -> None:
    if mode not in ("single", "fleet"):
        raise ValueError("invalid_telegram_mode")
    allowed = set(SINGLE_FIELDS if mode == "single" else FLEET_FIELDS)
    if len(profile.fields) != len(set(profile.fields)) or set(profile.fields) - allowed:
        raise ValueError("invalid_telegram_fields")


def default_profile(
    mode: TelegramMode, initial_interval_seconds: float = config.TELEGRAM_SUMMARY_SECONDS,
) -> TelegramProfile:
    if mode not in ("single", "fleet"):
        raise ValueError("invalid_telegram_mode")
    minutes = initial_interval_seconds / 60
    interval = int(minutes) if math.isfinite(minutes) and minutes.is_integer() and 1 <= minutes <= 1440 else 60
    return TelegramProfile(
        enabled=True, interval_minutes=interval,
        fields=list(SINGLE_FIELDS if mode == "single" else FLEET_FIELDS),
    )


def legacy_telegram_interval_from_env(env: Mapping[str, str] | None = None) -> float:
    raw = (os.environ if env is None else env).get("TELEGRAM_SUMMARY_SECONDS", "")
    try:
        return float(raw) if raw else config.TELEGRAM_SUMMARY_SECONDS
    except ValueError:
        return config.TELEGRAM_SUMMARY_SECONDS


class TelegramSettingsStore:
    """Atomically read and replace the two profiles across local processes."""

    def __init__(
        self, path: Path, *, initial_interval_seconds: float = config.TELEGRAM_SUMMARY_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.initial_interval_seconds = initial_interval_seconds
        self._lock = threading.RLock()

    def _read_document(self) -> dict[str, object]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1}
        except (OSError, ValueError) as exc:
            raise TelegramSettingsError("telegram_settings_unreadable") from exc
        if (not isinstance(document, dict) or document.get("version") != 1
                or set(document) - {"version", "single", "fleet", "revisions"}):
            raise TelegramSettingsError("telegram_settings_unreadable")
        revisions = document.get("revisions", {})
        if (not isinstance(revisions, dict) or set(revisions) - {"single", "fleet"}
                or any(not isinstance(value, str) or len(value) != 32
                       or any(character not in "0123456789abcdef" for character in value)
                       for value in revisions.values())):
            raise TelegramSettingsError("telegram_settings_unreadable")
        for mode in ("single", "fleet"):
            if mode in document:
                try:
                    profile = TelegramProfile.model_validate(document[mode])
                    validate_fields(mode, profile)
                except (ValidationError, ValueError, TypeError) as exc:
                    raise TelegramSettingsError("telegram_settings_unreadable") from exc
        return document

    def load(self, mode: TelegramMode) -> TelegramProfile:
        return self.load_with_revision(mode)[0]

    def load_with_revision(self, mode: TelegramMode) -> tuple[TelegramProfile, str | None]:
        if mode not in ("single", "fleet"):
            raise ValueError("invalid_telegram_mode")
        with self._lock:
            document = self._read_document()
            revisions = document.get("revisions", {})
            revision = revisions.get(mode) if isinstance(revisions, dict) else None
            if mode not in document:
                interval = self.initial_interval_seconds if mode == "single" else config.TELEGRAM_SUMMARY_SECONDS
                return default_profile(mode, interval), revision
            return TelegramProfile.model_validate(document[mode]), revision

    def save(self, mode: TelegramMode, profile: TelegramProfile) -> TelegramProfile:
        if mode not in ("single", "fleet"):
            raise ValueError("invalid_telegram_mode")
        profile = TelegramProfile.model_validate(profile.model_dump())
        validate_fields(mode, profile)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock_path = self.path.with_name(f".{self.path.name}.lock")
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                raise TelegramSettingsError("telegram_settings_unwritable") from exc
            temporary: Path | None = None
            try:
                with os.fdopen(lock_fd, "r+") as lock_file:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                    document = self._read_document()
                    document[mode] = profile.model_dump()
                    revisions = dict(document.get("revisions", {}))
                    revisions[mode] = uuid4().hex
                    document["revisions"] = revisions
                    temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                        json.dump(document, file, sort_keys=True)
                        file.write("\n")
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temporary, self.path)
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError as exc:
                raise TelegramSettingsError("telegram_settings_unwritable") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return profile
