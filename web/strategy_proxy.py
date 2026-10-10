"""Forward only scoped strategy operations to a registered local worker."""
from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import db
from fastapi import HTTPException
from web.account_catalog import AccountChoice, registered_worker


def worker_strategy_request(choice: AccountChoice, path: str,
                            body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.startswith('/api/strategy-studio/') or choice.web_port is None:
        raise HTTPException(422, 'invalid_strategy_proxy_target')
    if registered_worker(choice.db_path.parent) != choice or db.bound_account(choice.db_path) != choice.account_id:
        raise HTTPException(409, 'strategy_worker_binding_changed')
    request = Request(f'http://127.0.0.1:{choice.web_port}{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'x-account-scope': choice.key,
                 'x-expected-account-id': choice.account_id or ''})
    try:
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get('detail', 'strategy_worker_request_failed')
        except (ValueError, OSError):
            detail = 'strategy_worker_request_failed'
        raise HTTPException(exc.code, detail) from None
    except (URLError, OSError) as exc:
        raise HTTPException(503, 'strategy_worker_unavailable') from exc
    if registered_worker(choice.db_path.parent) != choice or db.bound_account(choice.db_path) != choice.account_id:
        raise HTTPException(409, 'strategy_worker_binding_changed')
    return result
