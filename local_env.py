"""Load machine-local secrets from the git-ignored ``.env`` beside the bot.

The bot's credentials (``CLAUDE_OPENROUTER_API_KEY``, ``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID``) are read from the process environment. This fills that
environment from ``.env`` once, at startup, so they need not be exported in
every shell. Fleet workers are subprocesses and inherit the result.

A variable already set in the environment wins over the file, so a one-off
``export`` still overrides it. Only variable names are ever logged.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger("tower_bot.local_env")

DEFAULT_PATH = Path(__file__).resolve().parent / ".env"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # Unquoted: " #" starts a trailing comment; a bare "#" is part of the value.
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def load_local_env(path: Path = DEFAULT_PATH,
                   env: MutableMapping[str, str] | None = None) -> list[str]:
    """Set each ``KEY=value`` in ``path`` that ``env`` does not already have.

    Returns the names that were set. A missing file is the normal case on a
    machine configured through the shell, and loads nothing.
    """
    target = os.environ if env is None else env
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    loaded: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, raw = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.fullmatch(name):
            # The line number only: a malformed line may still hold a secret.
            logger.warning("%s:%d is not KEY=value - skipped", path.name, number)
            continue
        if name in target:
            continue
        target[name] = _value(raw)
        loaded.append(name)
    if loaded:
        logger.info("loaded %s from %s", ", ".join(loaded), path.name)
    return loaded
