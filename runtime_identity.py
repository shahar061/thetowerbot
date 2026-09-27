"""Frozen Python process identity and the replaceable dashboard identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

API_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    "pyproject.toml",
    "uv.lock",
)


@dataclass(frozen=True)
class BackendIdentity:
    revision: str | None
    source_hash: str | None
    started_at: float

    def payload(self) -> dict[str, str | float | None]:
        return asdict(self)


@dataclass(frozen=True)
class FrontendIdentity:
    source_hash: str | None = None
    expected_backend_hash: str | None = None
    built_at: float | None = None

    def payload(self) -> dict[str, str | float | None]:
        return asdict(self)


def _source_paths(root: Path) -> list[Path]:
    paths = {
        path for pattern in (
            "*.py", "sinks/**/*.py", "web/*.py", "fleet/**/*.py",
            "catalog/**/*.json", "contracts/**/*.json",
        )
        for path in root.glob(pattern) if path.is_file()
    }
    paths.update(
        candidate for name in SOURCE_FILES
        if (candidate := root / name).is_file()
    )
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def source_hash(root: Path = PROJECT_ROOT) -> str | None:
    """Hash Python runtime inputs, returning unknown for an unreadable tree."""
    try:
        paths = _source_paths(root)
        if not paths:
            return None
        digest = hashlib.sha256()
        for path in paths:
            relative = path.relative_to(root).as_posix()
            digest.update(f"{relative}\n".encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        return None


def git_revision(root: Path = PROJECT_ROOT) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, check=True, timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision or None


def capture_backend_identity(
    root: Path = PROJECT_ROOT, *, started_at: float | None = None
) -> BackendIdentity:
    return BackendIdentity(
        revision=git_revision(root),
        source_hash=source_hash(root),
        started_at=time.time() if started_at is None else started_at,
    )


def read_frontend_identity(path: Path) -> FrontendIdentity:
    """Read the manifest being served now; replacement after start is visible."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return FrontendIdentity()
        source = raw.get("hash")
        backend = raw.get("backend_hash")
        built_at = raw.get("built_at")
        return FrontendIdentity(
            source_hash=source if isinstance(source, str) else None,
            expected_backend_hash=backend if isinstance(backend, str) else None,
            built_at=float(built_at) if isinstance(built_at, (int, float)) else None,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return FrontendIdentity()


PROCESS_IDENTITY = capture_backend_identity()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    print(source_hash(args.root) or "")
