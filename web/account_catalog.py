"""Resolve dashboard account choices to isolated, verified worker databases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import db


@dataclass(frozen=True)
class AccountChoice:
    key: str
    account_id: str | None
    instance: str | None
    db_path: Path
    kind: str
    web_port: int | None = None

    def payload(self, *, running: bool = False) -> dict[str, str | bool | None]:
        return {"key": self.key, "account_id": self.account_id,
                "instance": self.instance, "kind": self.kind, "running": running,
                "dashboard_url": f"http://127.0.0.1:{self.web_port}/" if self.web_port else None}


def registered_worker(worker: Path) -> AccountChoice | None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", worker.name):
        return None
    try:
        record = json.loads((worker / "fleet-registration.json").read_text(encoding="utf-8"))
        binding_path = Path(record["binding"])
        if (record.get("state") != "registered" or record.get("instance") != worker.name
                or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", record.get("account_id", ""))
                or not isinstance(record.get("web_port"), int)
                or not 1 <= record["web_port"] <= 65535
                or binding_path.parent.resolve() != (worker / "checkpoints").resolve()
                or not re.fullmatch(r"[0-9a-f]{32}\.json", binding_path.name)):
            return None
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if binding.get("worker_id") != worker.name or any(binding.get(field) != record.get(field)
               for field in ("account_id", "endpoint", "lease_id")):
            return None
        return AccountChoice(f"worker:{worker.name}", record["account_id"],
                             worker.name, worker / "tower_bot.db", "worker",
                             record["web_port"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def account_choices(fleet_root: Path | None, legacy_db: Path | None) -> list[AccountChoice]:
    choices: list[AccountChoice] = []
    if fleet_root is not None:
        workers = fleet_root / "workers"
        if workers.is_dir():
            for worker in sorted(workers.iterdir()):
                if worker.is_dir() and not worker.is_symlink():
                    choice = registered_worker(worker)
                    if choice is not None:
                        choices.append(choice)
    # Duplicate account IDs are an identity incident. Neither worker may
    # claim that account's history until its registration is repaired.
    counts: dict[str, int] = {}
    for choice in choices:
        counts[choice.account_id or ""] = counts.get(choice.account_id or "", 0) + 1
    ambiguous = [choice for choice in choices if counts[choice.account_id or ""] != 1]
    choices = [choice for choice in choices if counts[choice.account_id or ""] == 1]
    for choice in ambiguous:
        if choice.db_path.is_file():
            choices.append(AccountChoice(f"unattributed:{choice.instance}", None,
                                         choice.instance, choice.db_path, "unattributed"))
    for choice in tuple(choices):
        if choice.db_path.is_file() and db.bound_account(choice.db_path) != choice.account_id:
            choices.append(AccountChoice(f"unattributed:{choice.instance}", None,
                                         choice.instance, choice.db_path, "unattributed"))
    if legacy_db is not None and legacy_db.exists() and all(
            choice.db_path.resolve() != legacy_db.resolve() for choice in choices):
        choices.append(AccountChoice("unattributed", None, None, legacy_db, "unattributed"))
    return choices
