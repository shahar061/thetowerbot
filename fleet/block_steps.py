"""What each strategy block did during one evaluation, for the Workshop plan graph.

Display only: nothing here takes part in choosing an upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import upgrades

# Reject phrases that mean "already finished" rather than "turned away".
DONE_MARKERS = (' target reached', ' level cap reached', ' max purchases reached', ': maxed')
DEFAULT_NOTES = {
    'pool': 'No eligible upgrade (price, funds or filters)',
    'save_for': 'Nothing to save for',
    'unlock': 'No unlock step available',
    'condition': 'Nothing to buy in the branch it chose',
    'while_saving': 'Not saving for this goal',
    'buy': 'Not eligible or not affordable',
}


@dataclass(frozen=True)
class BlockStep:
    block_id: str
    label: str | None
    kind: str
    outcome: str  # done | skipped | matched | not_reached
    note: str = ''
    depth: int = 0


@dataclass(frozen=True)
class CandidateRow:
    upgrade_id: str
    name: str
    category: str
    price: int | None
    weight: float | None
    odds: float | None  # weighted selection only, 0..1
    score: float | None  # value selection only: price / weight
    chosen: bool = False


@dataclass
class _Entry:
    block: Mapping[str, Any]
    depth: int
    mark: int
    end: int | None = None
    done: bool = False
    note: str = ''


def _prefix(line: str) -> str | None:
    return line.split(': ', 1)[0] if ': ' in line else None


def _strip(line: str, block_id: str) -> str:
    return line[len(block_id) + 2:] if line.startswith(f'{block_id}: ') else line


def _owned_prefixes(block: Mapping[str, Any]) -> set[str]:
    owned = {block['id'], *block.get('upgrade_ids', ())}
    if block.get('upgrade_id'):
        owned.add(block['upgrade_id'])
    for goal in block.get('goal', ()):
        owned.update({goal['id'], *goal.get('upgrade_ids', ())})
        if goal.get('upgrade_id'):
            owned.add(goal['upgrade_id'])
    return owned


def _descendant_ids(block: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ('then', 'else', 'blocks', 'goal'):
        for child in block.get(key, ()) or ():
            found.add(child['id'])
            found |= _descendant_ids(child)
    return found


class StepLog:
    """Record blocks in the order evaluate_program visits them.

    push/pop wrap each call that walks a block list; enter starts a block.
    A block's rejected lines are those added between its enter and the next
    sibling's enter (or the list's pop).
    """

    def __init__(self, rejected: list[str]) -> None:
        self._rejected = rejected
        self._entries: list[_Entry] = []
        self._open: list[int] = []

    def push(self) -> None:
        self._open.append(-1)

    def pop(self) -> None:
        self._close_current()
        self._open.pop()

    def enter(self, block: Mapping[str, Any]) -> None:
        self._close_current()
        self._entries.append(_Entry(block, len(self._open) - 1, len(self._rejected)))
        self._open[-1] = len(self._entries) - 1

    def done(self, note: str) -> None:
        entry = self._entries[self._open[-1]]
        entry.done, entry.note = True, note

    def note(self, text: str) -> None:
        self._entries[self._open[-1]].note = text

    def _close_current(self) -> None:
        if self._open and self._open[-1] >= 0 and self._entries[self._open[-1]].end is None:
            self._entries[self._open[-1]].end = len(self._rejected)

    def _matched_path(self, matched_id: str | None) -> set[int]:
        if matched_id is None:
            return set()
        order = range(len(self._entries) - 1, -1, -1)
        target = next((i for i in order if self._entries[i].block['id'] == matched_id), None)
        if target is None:
            target = next((i for i in order if matched_id in _descendant_ids(self._entries[i].block)), None)
        if target is None:
            return set()
        path, depth = {target}, self._entries[target].depth
        for index in range(target - 1, -1, -1):
            if self._entries[index].depth < depth:
                path.add(index)
                depth = self._entries[index].depth
        return path

    def _step(self, entry: _Entry, matched: bool) -> BlockStep:
        block = entry.block
        def made(outcome: str, note: str) -> BlockStep:
            return BlockStep(block['id'], block.get('label'), block['type'],
                           outcome, note, entry.depth)
        if matched:
            return made('matched', entry.note)
        if entry.done:
            return made('done', entry.note)
        owned = _owned_prefixes(block)
        own = [line for line in self._rejected[entry.mark:entry.end] if _prefix(line) in owned]
        if own and all(any(marker in line for marker in DONE_MARKERS) for line in own):
            return made('done', _strip(own[-1], block['id']))
        note = entry.note or (_strip(own[-1], block['id']) if own
                              else DEFAULT_NOTES.get(block['type'], 'Nothing to do'))
        return made('skipped', note)

    def steps(self, program: Sequence[Mapping[str, Any]], matched_id: str | None) -> tuple[BlockStep, ...]:
        path = self._matched_path(matched_id)
        result = [self._step(entry, index in path) for index, entry in enumerate(self._entries)]
        top = [entry.block['id'] for entry in self._entries if entry.depth == 0]
        ids = [block['id'] for block in program]
        if top and top[-1] in ids:
            for block in program[ids.index(top[-1]) + 1:]:
                result.append(BlockStep(block['id'], block.get('label'), block['type'], 'not_reached', 'Not reached', 0))
        return tuple(result)


def candidate_table(weights: Mapping[str, float], selection: str, chosen: str | None,
                    price: Callable[[str], int | None]) -> tuple[CandidateRow, ...]:
    total = sum(weights.values())
    shows_weight = selection in ('weighted', 'value')
    rows = []
    for uid, weight in weights.items():
        cost = price(uid)
        upgrade = upgrades.by_id(uid)
        rows.append(CandidateRow(
            uid, upgrade.name if upgrade else uid, upgrade.category if upgrade else '', cost,
            float(weight) if shows_weight else None,
            weight / total if selection == 'weighted' and total > 0 else None,
            cost / weight if selection == 'value' and cost is not None and weight > 0 else None,
            uid == chosen))
    # Stable sorts: equal keys keep the block's own order, as the engine does.
    if selection == 'value':
        rows.sort(key=lambda row: (row.score is None, row.score or 0.0))
    elif selection == 'cheapest':
        rows.sort(key=lambda row: (row.price is None, row.price or 0))
    return tuple(rows)


def unlock_rows(steps: Mapping[str, tuple[Any, int]], ranked: Sequence[str], chosen: str) -> tuple[CandidateRow, ...]:
    return tuple(CandidateRow(tile, steps[tile][0].name, steps[tile][0].category, steps[tile][1],
                              None, None, None, tile == chosen) for tile in ranked)
