from __future__ import annotations

import logging
from pathlib import Path

import pytest

from local_env import load_local_env


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_loads_nothing(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert load_local_env(tmp_path / ".env", env) == []
    assert env == {}


def test_parses_supported_line_forms(tmp_path: Path) -> None:
    path = _write(tmp_path, "\n".join([
        "# comment",
        "",
        "PLAIN=value",
        "export EXPORTED=exported",
        'DOUBLE="with spaces"',
        "SINGLE='sk-or-#1'",
        "  PADDED  =  padded  ",
        "EMPTY=",
        "EQUALS=a=b",
        "TRAILING=abc  # note",
        "HASH=a#b",
    ]))
    env: dict[str, str] = {}
    loaded = load_local_env(path, env)
    assert env == {
        "PLAIN": "value", "EXPORTED": "exported", "DOUBLE": "with spaces",
        "SINGLE": "sk-or-#1", "PADDED": "padded", "EMPTY": "", "EQUALS": "a=b",
        "TRAILING": "abc", "HASH": "a#b",
    }
    assert loaded == list(env)


def test_shell_environment_wins(tmp_path: Path) -> None:
    path = _write(tmp_path, "CLAUDE_OPENROUTER_API_KEY=from-file\nOTHER=x\n")
    env = {"CLAUDE_OPENROUTER_API_KEY": "from-shell"}
    assert load_local_env(path, env) == ["OTHER"]
    assert env["CLAUDE_OPENROUTER_API_KEY"] == "from-shell"


@pytest.mark.parametrize("line", ["no_equals_sign", "=value", "1BAD=x", "BAD NAME=x"])
def test_malformed_lines_are_skipped(tmp_path: Path, line: str) -> None:
    path = _write(tmp_path, f"{line}\nGOOD=1\n")
    env: dict[str, str] = {}
    assert load_local_env(path, env) == ["GOOD"]
    assert env == {"GOOD": "1"}


def test_logs_names_never_values(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write(tmp_path, "CLAUDE_OPENROUTER_API_KEY=sk-or-secret\nbroken sk-or-leak\n")
    with caplog.at_level(logging.DEBUG):
        load_local_env(path, {})
    assert "CLAUDE_OPENROUTER_API_KEY" in caplog.text
    assert "sk-or-secret" not in caplog.text
    assert "sk-or-leak" not in caplog.text
