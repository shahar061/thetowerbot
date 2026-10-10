"""Explicit paths and live identity authority for a strategy executor."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, ContextManager

import db
from evidence_scope import FactScope


class StrategyTargetError(ValueError):
    """A plan target no longer has its expected execution authority."""


@dataclass(frozen=True)
class StrategyTargetContext:
    target_id: str
    scope: FactScope
    state_dir: Path
    db_path: Path
    library_root: Path
    assignment_root: Path
    capabilities: frozenset[str]
    read_scope: Callable[[], FactScope | None]
    guard_scope: Callable[[], ContextManager[None]] = nullcontext

    @property
    def account_id(self) -> str:
        return self.scope.account_id

    def verify_current(self) -> None:
        current = self.read_scope()
        if current is None or (current.account_id, current.lease_id,
                               current.generation, current.epoch) != (
                self.scope.account_id, self.scope.lease_id,
                self.scope.generation, self.scope.epoch):
            raise StrategyTargetError('strategy_target_scope_changed')
        if db.bound_account(self.db_path) != self.account_id:
            raise StrategyTargetError('strategy_target_database_changed')
