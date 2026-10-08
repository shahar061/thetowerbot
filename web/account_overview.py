"""Account-scoped overview and lab saving controls, without a fleet-wide read."""

from __future__ import annotations

import asyncio
import http.client
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import db
from fleet.build_route import AccountOverride, LabShareRule, RouteDocument, resolve_route
from fleet.build_route_store import BuildRouteStore, RouteConflict, RouteUnavailable
from fleet.state_view import STATUS_TIMEOUT, build_account, iso
from web.account_catalog import AccountChoice, registered_worker


class LabSavingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_account_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    share_mode: Literal["when_affordable", "save_pct", "labs_first", "just_in_time"]
    share_pct: int = Field(ge=5, le=90)


def _remote_status(choice: AccountChoice) -> Mapping[str, Any] | None:
    if choice.web_port is None:
        return None
    request = UrlRequest(f"http://127.0.0.1:{choice.web_port}/api/status", headers={
        "x-account-scope": choice.key, "x-expected-account-id": choice.account_id or ""})
    try:
        with urlopen(request, timeout=STATUS_TIMEOUT) as response:
            payload = json.load(response)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return payload if isinstance(payload, dict) else None


def _verified(choice: AccountChoice) -> bool:
    current = registered_worker(choice.db_path.parent)
    return (current is not None and current.account_id == choice.account_id
            and current.db_path == choice.db_path
            and db.bound_account(choice.db_path) == choice.account_id)


def _require_savings_consumers(root: Path, local_db: Path | None) -> None:
    """Old workers cannot parse the new override field in the shared route.

    Probe only at publication. A refused connection is a stopped process; silence
    or malformed metadata cannot establish that a route consumer is compatible.
    """
    directory = root / "workers"
    choices = [choice for path in directory.iterdir()
               if path.is_dir() and not path.is_symlink()
               and (choice := registered_worker(path)) is not None
               and (local_db is None or choice.db_path.resolve() != local_db.resolve())]

    def incompatible(choice: AccountChoice) -> str | None:
        request = UrlRequest(f"http://127.0.0.1:{choice.web_port}/api/status")
        try:
            with urlopen(request, timeout=STATUS_TIMEOUT) as response:
                payload = json.load(response)
            capabilities = payload.get("runtime", {}).get("capabilities", [])
            return None if "account_savings" in capabilities else choice.instance
        except URLError as exc:
            return None if isinstance(exc.reason, ConnectionRefusedError) else choice.instance
        except ConnectionRefusedError:
            return None
        except (OSError, ValueError, AttributeError, TypeError, http.client.HTTPException):
            return choice.instance

    if not choices:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(choices))) as pool:
        blocked = sorted(name for name in pool.map(incompatible, choices) if name)
    if blocked:
        raise HTTPException(412, "Restart or stop these worker backends before saving lab settings: "
            + ", ".join(blocked) + ". Their support for account lab savings could not be verified; "
            "the shared strategy was not changed.")


def _savings(choice: AccountChoice | None, route: RouteDocument | None,
             reason: str | None = None) -> dict[str, Any]:
    effective = (resolve_route(route, choice.instance, choice.account_id)
                 if route is not None and choice is not None and choice.instance and choice.account_id else None)
    return {"account_id": choice.account_id if choice else None,
            "worker": choice.instance if choice else None,
            "revision": route.revision if route else None,
            "share_mode": effective.rules.coins.lab_share.mode if effective else None,
            "share_pct": effective.rules.coins.lab_share.pct if effective else None,
            "auto_start": effective.rules.labs.auto_start if effective else False,
            "editable": effective is not None and reason is None,
            "reason": reason}


def register_account_overview_routes(app: FastAPI, *,
        selected: Callable[[Request], AccountChoice | None],
        fleet_root: Callable[[], Path | None], db_path: Path | None,
        local_status: Callable[[Request, AccountChoice], Mapping[str, Any] | None]) -> None:
    def selection(request: Request) -> tuple[AccountChoice | None, Path | None]:
        choice, root = selected(request), fleet_root()
        if choice is None or choice.kind != "worker" or choice.account_id is None or choice.instance is None:
            return None, root
        if not _verified(choice):
            raise HTTPException(409, "selected_account_changed")
        return choice, root

    @app.get("/api/account/state")
    async def account_state(request: Request) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            choice, root = selection(request)
            moment = time.time()
            if choice is None or root is None:
                return {"generated_at": iso(moment), "account_id": choice.account_id if choice else None,
                        "account": None}
            local = db_path is not None and choice.db_path.resolve() == db_path.resolve()
            try:
                registration = json.loads((choice.db_path.parent / "fleet-registration.json").read_text())
            except (OSError, ValueError) as exc:
                raise HTTPException(409, "selected_account_changed") from exc
            member = {"name": choice.instance, "endpoint": registration.get("endpoint"),
                      "lease_id": registration.get("lease_id")}
            account = build_account(root, member, urlopen, moment,
                status_snapshot=lambda: local_status(request, choice) if local else _remote_status(choice))
            if not _verified(choice):
                raise HTTPException(409, "selected_account_changed")
            return {"generated_at": iso(moment), "account_id": choice.account_id, "account": account}
        return await asyncio.to_thread(read)

    @app.get("/api/account/lab-savings")
    async def lab_savings(request: Request) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            choice, root = selection(request)
            if choice is None or root is None:
                return _savings(choice, None, "Select a registered emulator account to configure lab savings.")
            try:
                return _savings(choice, BuildRouteStore(root).read())
            except (RouteUnavailable, OSError, ValueError) as exc:
                return _savings(choice, None, str(exc))
        return await asyncio.to_thread(read)

    @app.put("/api/account/lab-savings")
    async def save_lab_savings(request: Request, body: LabSavingsRequest) -> dict[str, Any]:
        def save() -> dict[str, Any]:
            choice, root = selection(request)
            if choice is None or root is None or choice.account_id != body.expected_account_id:
                raise HTTPException(409, "selected_account_changed")
            store = BuildRouteStore(root)
            try:
                route = store.read()
                if route.revision != body.expected_revision:
                    raise RouteConflict(route)
                old = route.overrides.get(choice.instance)
                patches = old.patches if old is not None and old.account_id == choice.account_id else {}
                override = AccountOverride(choice.account_id, patches,
                                           LabShareRule(body.share_mode, body.share_pct))
                draft = replace(route, overrides={**route.overrides, choice.instance: override})
                # Resolve before publication: e.g. just-in-time requires a ranked lab list.
                resolve_route(draft, choice.instance, choice.account_id)
                _require_savings_consumers(root, db_path)
                if not _verified(choice):
                    raise HTTPException(409, "selected_account_changed")
                saved = store.publish(draft, body.expected_revision, "single_account_lab_savings")
                return _savings(choice, saved)
            except RouteConflict as exc:
                raise HTTPException(409, {"message": "route_revision_changed",
                    "current_revision": exc.current.revision}) from exc
            except RouteUnavailable as exc:
                raise HTTPException(503, exc.reason) from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
        return await asyncio.to_thread(save)
