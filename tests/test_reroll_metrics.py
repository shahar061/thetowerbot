"""Reroll overview reports only verified observations."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from fleet.reroll_metrics import observed_metrics


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_saved_runs_and_verified_live_wallet(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER, ended_at REAL, tier INTEGER, wave INTEGER, coins INTEGER)")
        conn.execute("INSERT INTO runs VALUES (1, 1, 1, 75, 150)")
    def fetch(url: str, *, timeout: float) -> Response:
        assert timeout == .2
        if url.endswith("/api/accounts?local_only=true"):
            return Response(b'{"active":"worker:Air_2","accounts":[{"key":"worker:Air_2","account_id":"42","running":true}]}')
        return Response(b'{"wallet":230,"run":{"elapsed":31.5}}')

    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=10002, running=True, fetch=fetch)
    assert result["best_tier_1_wave"] == 75
    assert result["run_coins"] == 150
    assert result["wallet_coins"] == 230
    assert result["run_duration_seconds"] == 31.5


def test_unverified_live_account_does_not_supply_wallet(tmp_path: Path) -> None:
    def fetch(_url: str, *, timeout: float) -> Response:
        return Response(b'{"active":"worker:other","accounts":[]}')

    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=10002, running=True, fetch=fetch)
    assert "wallet_coins" not in result
