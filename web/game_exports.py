"""Verified-device export collection and read-only account reporting APIs."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request

import db
from device import endpoint_matches
from game_export import parse_export
from game_export_store import ExportStore, read_export
from web.account_catalog import AccountChoice, registered_worker


logger = logging.getLogger(__name__)
EXPORT_PATH = "/sdcard/Android/data/com.TechTreeGames.TheTower/files/tool_export.json"
POLL_SECONDS = 10.0


class ExportWorkerReloadRequired(RuntimeError):
    """An older worker cannot provide current reporting-scope authority."""


@dataclass(frozen=True)
class ExportSource:
    db_path: Path
    account_id: str
    endpoint: str
    scope_key: str
    verified_since: float
    worker: str


def _registration(choice: AccountChoice) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        current = registered_worker(choice.db_path.parent)
        if (current is None or current != choice or not choice.account_id
                or db.bound_account(choice.db_path) != choice.account_id):
            return None
        record = json.loads((choice.db_path.parent / "fleet-registration.json").read_text())
        binding = json.loads(Path(record["binding"]).read_text())
        return record, binding
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def _timestamp(value: object) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def registered_source(choice: AccountChoice, status: Mapping[str, Any] | None,
                      scope: object = None, *, verified_since: float | None = None) -> ExportSource | None:
    """Resolve only a currently verified, explicitly selected device transport."""
    saved = _registration(choice)
    if saved is None or not isinstance(status, Mapping):
        return None
    record, binding = saved
    bot, runtime = status.get("bot"), status.get("runtime")
    if not isinstance(bot, Mapping) or bot.get("running") is not True or not isinstance(runtime, Mapping):
        return None
    device = runtime.get("device")
    endpoint = record.get("endpoint")
    since, observed = _timestamp(bot.get("since")), _timestamp(binding.get("observed_at"))
    if (not isinstance(endpoint, str) or not isinstance(device, Mapping)
            or not endpoint_matches(endpoint, device.get("serial")) or since is None or observed is None):
        return None
    generation, epoch = binding.get("generation"), 0
    if scope is not None:
        if (getattr(scope, "account_id", None) != choice.account_id
                or getattr(scope, "lease_id", None) != record.get("lease_id")):
            return None
        generation, epoch = getattr(scope, "generation", None), getattr(scope, "epoch", None)
        current_verified = _timestamp(verified_since)
        if current_verified is None:
            return None
        observed = max(observed, current_verified)
    reporting = status.get("reporting_scope")
    if scope is None and not isinstance(reporting, Mapping):
        return None
    if scope is None and isinstance(reporting, Mapping):
        if (reporting.get("account_id") != choice.account_id
                or reporting.get("lease_id") != record.get("lease_id")
                or reporting.get("endpoint") != endpoint):
            return None
        generation, epoch = reporting.get("generation"), reporting.get("epoch")
        verified = _timestamp(reporting.get("verified_since"))
        if verified is None:
            return None
        observed = max(observed, verified)
    if not isinstance(generation, str) or not generation or type(epoch) is not int or epoch < 0:
        return None
    key = json.dumps({"account_id": choice.account_id, "worker": choice.instance,
                      "endpoint": endpoint, "lease_id": record["lease_id"],
                      "generation": generation, "epoch": epoch}, sort_keys=True)
    return ExportSource(choice.db_path, choice.account_id, endpoint, key,
                        max(since, observed), choice.instance or choice.key)


def local_source(choice: AccountChoice, runner: Any, accounts: Any) -> ExportSource | None:
    try:
        if runner is None or runner.verified_account() != choice.account_id:
            return None
        scope = accounts.verified_scope
        if scope is None:
            return None
        return registered_source(choice, {"bot": runner.status(),
            "runtime": {"device": runner.identity()}}, scope,
            verified_since=accounts.verified_identity_at)
    except (OSError, ValueError, AttributeError, RuntimeError):
        return None


def remote_source(choice: AccountChoice) -> ExportSource | None:
    if choice.web_port is None:
        return None
    request = UrlRequest(f"http://127.0.0.1:{choice.web_port}/api/status", headers={
        "x-account-scope": choice.key, "x-expected-account-id": choice.account_id or ""})
    try:
        with urlopen(request, timeout=.5) as response:
            status = json.load(response)
        if isinstance(status, Mapping) and "reporting_scope" not in status:
            raise ExportWorkerReloadRequired("Reload the updated worker to enable verified game exports.")
        return registered_source(choice, status)
    except (OSError, ValueError, TypeError):
        return None


async def read_device_export(endpoint: str) -> bytes:
    """Read the fixed official path over the existing ADB server; never tap."""
    from adbutils import AdbClient
    import config

    def read() -> bytes:
        client = AdbClient(host=config.ADB_HOST, port=config.ADB_PORT, socket_timeout=5)
        matches = [device for device in client.device_list()
                   if endpoint_matches(endpoint, device.serial)]
        if len(matches) != 1:
            raise ValueError("export_device_unavailable")
        # sync.iter_content avoids shell quoting and preserves the JSON bytes.
        content = bytearray()
        for chunk in matches[0].sync.iter_content(EXPORT_PATH):
            content.extend(chunk)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("export_file_too_large")
        return bytes(content)

    return await asyncio.to_thread(read)


async def collect_once(choice: AccountChoice,
                       resolve: Callable[[AccountChoice], ExportSource | None], *,
                       read_file: Callable[[str], Awaitable[bytes]] = read_device_export,
                       now: float | None = None) -> bool:
    source = await asyncio.to_thread(resolve, choice)
    if source is None:
        return False
    raw = await read_file(source.endpoint)
    parsed = await asyncio.to_thread(parse_export, raw)
    if parsed["exported_at"] < source.verified_since:
        return False
    current = await asyncio.to_thread(resolve, choice)
    if source != current:
        return False
    captured_at = time.time() if now is None else now
    return await asyncio.to_thread(ExportStore(source.db_path, source.account_id).ingest,
        raw, endpoint=source.endpoint, scope_key=source.scope_key, captured_at=captured_at)


def read_bound_projection(choice: AccountChoice, *, online: bool = False,
                          now: float | None = None) -> dict[str, Any]:
    if _registration(choice) is None or not choice.account_id:
        return {"available": False, "account_id": choice.account_id,
                "source": "game_export", "reason": "account_binding_changed", "source_online": False}
    result = read_export(choice.db_path, choice.account_id, now=now)
    if result.get("available"):
        registration = _registration(choice)
        if registration is None:
            return {"available": False, "account_id": choice.account_id,
                    "source": "game_export", "reason": "account_binding_changed", "source_online": False}
        record, _ = registration
        try:
            saved = json.loads(result.get("scope_key", ""))
        except (ValueError, TypeError):
            saved = {}
        if saved.get("account_id") != record.get("account_id"):
            return {"available": False, "account_id": choice.account_id,
                    "source": "game_export", "reason": "export_source_changed", "source_online": False}
        if any(saved.get(key) != record.get(key) for key in ("endpoint", "lease_id")):
            online = False
            result["historical_only"] = True
            result["stale"] = True
            result["warnings"] = [*result.get("warnings", []),
                "Historical account export from an earlier worker lease; awaiting a current verified export."]
    result["source_online"] = online
    if not online and isinstance(result.get("current_run"), dict):
        result["current_run"]["estimated_wave"] = None
    return result


class ExportCollector:
    def __init__(self, choices: Callable[[], Sequence[AccountChoice]],
                 resolve: Callable[[AccountChoice], ExportSource | None]) -> None:
        self.choices, self.resolve = choices, resolve
        self.online: dict[str, tuple[str, float]] = {}
        self.errors: dict[str, str] = {}

    async def poll(self) -> None:
        choices = await asyncio.to_thread(self.choices)
        semaphore = asyncio.Semaphore(4)

        async def one(choice: AccountChoice) -> None:
            if choice.kind != "worker" or not choice.account_id:
                return
            async with semaphore:
                try:
                    proof = await asyncio.to_thread(self.resolve, choice)
                    if proof is None:
                        self.online.pop(choice.key, None)
                        return
                    await collect_once(choice, self.resolve)
                    current = await asyncio.to_thread(self.resolve, choice)
                    if current != proof:
                        self.online.pop(choice.key, None)
                        return
                    self.online[choice.key] = (current.scope_key, time.time())
                    self.errors.pop(choice.key, None)
                except Exception as exc:  # one inaccessible emulator must not block peers
                    self.online.pop(choice.key, None)
                    reason = str(exc) if isinstance(exc, ExportWorkerReloadRequired) else type(exc).__name__
                    if self.errors.get(choice.key) != reason:
                        logger.warning("Game export unavailable", extra={"account_id": choice.account_id,
                            "worker": choice.instance, "reason": reason})
                    self.errors[choice.key] = reason

        await asyncio.gather(*(one(choice) for choice in choices))

    async def run(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:
                logger.exception("Game export collector iteration failed")
            await asyncio.sleep(POLL_SECONDS)

    def projection(self, choice: AccountChoice) -> dict[str, Any]:
        scope_key, last_seen = self.online.get(choice.key, ("", 0))
        result = read_bound_projection(choice, online=time.time() - last_seen < 30)
        if result.get("scope_key") != scope_key:
            result["source_online"] = False
            if isinstance(result.get("current_run"), dict):
                result["current_run"]["estimated_wave"] = None
        if choice.key in self.errors:
            result["collection_error"] = self.errors[choice.key]
        return result


def register_game_export_routes(app: FastAPI, *,
        choices: Callable[[], Sequence[AccountChoice]],
        selected: Callable[[Request], AccountChoice | None],
        resolve: Callable[[AccountChoice], ExportSource | None]) -> ExportCollector:
    collector = ExportCollector(choices, resolve)

    @app.get("/api/game-export")
    async def game_export(request: Request) -> dict[str, Any]:
        choice = await asyncio.to_thread(selected, request)
        if choice is None or choice.kind != "worker" or not choice.account_id:
            return {"available": False, "account_id": None, "source": "game_export",
                    "reason": "select_registered_account", "source_online": False}
        result = await asyncio.to_thread(collector.projection, choice)
        current = await asyncio.to_thread(selected, request)
        if current != choice:
            raise HTTPException(409, "selected_account_changed")
        return result

    @app.get("/api/game-exports")
    async def game_exports() -> dict[str, Any]:
        def read() -> dict[str, Any]:
            accounts = [{"worker": choice.instance, "instance": choice.instance,
                         **collector.projection(choice)} for choice in choices()
                        if choice.kind == "worker" and choice.account_id]
            return {"accounts": accounts, "generated_at": time.time()}
        return await asyncio.to_thread(read)

    return collector
