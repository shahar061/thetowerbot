"""Throwaway, read-only experiment: measure The Tower's JSON export freshness.

Example: python tools/probe_game_export.py 127.0.0.1:6375 --duration 360
Only reads the named attached device. Never changes the game or bot state.
Prints selected game fields and changed field names, never the full save.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPORT_PATH = "/sdcard/Android/data/com.TechTreeGames.TheTower/files/tool_export.json"
FIELDS = (
    "ExportVersion", "VersionNumber", "DataVersion", "ExportedAt",
    "CurrentTier", "CurrentWave", "RoundActiveBool",
    "Coins", "Gems", "Stones", "Medals", "Cells", "SlotsUnlocked", "LabsUnlocked",
    "CardsBoughtTotal", "PerksPickedCount", "CoinsEarnedThisRound", "RealTimeThisRound",
)


def emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, allow_nan=False), flush=True)


async def command(adb: str, *args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        adb, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        # Do not include file contents or arbitrary device output in errors.
        raise RuntimeError(f"ADB failed with exit code {process.returncode}")
    return stdout


def export_age(data: dict[str, Any]) -> float | None:
    stamp = data.get("ExportedAt")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return round((datetime.now(timezone.utc) - parsed).total_seconds(), 2)
    except ValueError:
        return None


async def probe(args: argparse.Namespace) -> int:
    devices = (await command(args.adb, "devices")).decode().splitlines()
    if not any(line.split() == [args.endpoint, "device"] for line in devices):
        raise RuntimeError("The exact endpoint is not attached and ready; no fallback selected")
    began = time.monotonic()
    deadline = began + args.duration
    previous: dict[str, Any] | None = None
    previous_digest: str | None = None
    last_report = began
    latencies: list[float] = []
    gaps: list[float] = []
    observations = errors = changes = 0
    latest_age: float | None = None
    while True:
        started = time.monotonic()
        try:
            raw = await command(args.adb, "-s", args.endpoint, "exec-out", "cat", EXPORT_PATH)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Export root must be an object")
            latency = (time.monotonic() - started) * 1000
            latest_age = export_age(data)
            latencies.append(latency)
            observations += 1
            digest = hashlib.sha256(raw).hexdigest()
            if digest != previous_digest:
                changed = sorted(
                    key for key in data.keys() | previous.keys()
                    if data.get(key) != previous.get(key)
                ) if previous is not None else []
                if previous is not None:
                    changes += 1
                    try:
                        old = datetime.fromisoformat(previous["ExportedAt"].replace("Z", "+00:00"))
                        new = datetime.fromisoformat(data["ExportedAt"].replace("Z", "+00:00"))
                        gaps.append((new - old).total_seconds())
                    except (KeyError, TypeError, ValueError, AttributeError):
                        pass
                emit("snapshot", elapsed_s=round(time.monotonic() - began, 2),
                     endpoint=args.endpoint, bytes=len(raw), fields=len(data),
                     read_ms=round(latency, 2), export_age_s=latest_age,
                     values={key: data.get(key) for key in FIELDS}, changed_fields=changed)
                previous, previous_digest = data, digest
                last_report = time.monotonic()
            elif time.monotonic() - last_report >= 30:
                emit("unchanged", elapsed_s=round(time.monotonic() - began, 2),
                     export_age_s=latest_age, reads=observations)
                last_report = time.monotonic()
        except (RuntimeError, asyncio.TimeoutError, ValueError) as exc:
            errors += 1
            emit("read_error", kind=type(exc).__name__)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(max(0, args.interval - (time.monotonic() - started)), remaining))
    emit("summary", endpoint=args.endpoint, elapsed_s=round(time.monotonic() - began, 2),
         reads=observations, errors=errors, changes=changes,
         median_read_ms=round(statistics.median(latencies), 2) if latencies else None,
         export_timestamp_gaps_s=gaps, latest_export_age_s=latest_age,
         account_identity_verified=False, bot_state_modified=False)
    return 0 if observations and not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="Exact attached ADB serial; never auto-selects")
    bundled = Path("/Applications/BlueStacks.app/Contents/MacOS/hd-adb")
    parser.add_argument("--adb", default=shutil.which("adb") or (str(bundled) if bundled.is_file() else "adb"))
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error("--duration must be finite and nonnegative")
    if not math.isfinite(args.interval) or args.interval < 0.2:
        parser.error("--interval must be finite and at least 0.2 seconds")
    try:
        return asyncio.run(probe(args))
    except (RuntimeError, OSError, asyncio.TimeoutError) as exc:
        emit("probe_error", kind=type(exc).__name__, message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
