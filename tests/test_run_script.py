"""The local launcher rebuilds a dashboard that is not committed to git."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess


ROOT = Path(__file__).parent.parent


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_run_script_bootstraps_ui_dependencies_before_rebuilding(tmp_path: Path) -> None:
    """A source-only checkout can rebuild before the bot starts."""
    script = tmp_path / "run.sh"
    shutil.copy(ROOT / "run.sh", script)
    (tmp_path / "web" / "ui").mkdir(parents=True)
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "commands.log"

    _write_executable(commands / "git", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        commands / "uv",
        """#!/usr/bin/env bash
set -eu
printf 'uv %s\\n' \"$*\" >> \"$RUN_LOG\"
if [[ \"$1\" == sync ]]; then exit 0; fi
if [[ \"$*\" == *'tools.freshness'* ]]; then
  if [[ -e \"$FRESH_ONCE\" ]]; then exit 0; fi
  touch \"$FRESH_ONCE\"
  printf 'no build manifest\\n'
  exit 1
fi
if [[ \"$*\" == *'import config'* ]]; then printf '8765\\n'; exit 0; fi
exit 0
""",
    )
    _write_executable(
        commands / "npm",
        "#!/usr/bin/env bash\nprintf 'npm %s\\n' \"$*\" >> \"$RUN_LOG\"\n",
    )
    _write_executable(commands / "lsof", "#!/usr/bin/env bash\nprintf 'lsof %s\\n' \"$*\" >> \"$RUN_LOG\"\nexit 0\n")

    env = {
        **os.environ,
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "RUN_LOG": str(log),
        "FRESH_ONCE": str(tmp_path / "freshness-ran"),
    }
    completed = subprocess.run(
        ["bash", str(script), "--idle", "--port", "5735"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    commands_run = log.read_text(encoding="utf-8").splitlines()
    assert commands_run.index("npm ci") < commands_run.index("npm run build")
    assert "lsof -tiTCP:8765 -sTCP:LISTEN" in commands_run
    assert "dashboard -> http://127.0.0.1:8765" in completed.stdout
