"""Durable operator choice for a supervised bot's scan loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fleet.identity import Attempt


def read_intent(root: Path) -> dict[str, Any] | None:
    try:
        row = json.loads((Path(root) / "bot-intent.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(row, dict) or row.get("desired_state") not in {"running", "stopped"}:
        raise ValueError("bot_intent_invalid")
    return row


def write_intent(root: Path, attempt: Attempt, account_id: str | None,
                 desired_state: str) -> None:
    if desired_state not in {"running", "stopped"}:
        raise ValueError("bot intent state invalid")
    path = Path(root) / "bot-intent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"worker_id": attempt.worker_id, "endpoint": attempt.endpoint,
           "lease_id": attempt.lease_id, "attempt_id": attempt.attempt_id,
           "account_id": account_id, "desired_state": desired_state}
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(row, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
