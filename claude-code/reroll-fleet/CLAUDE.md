# Reroll fleet execution instructions

This folder coordinates the multi-instance reroll campaign. The general automation program remains in `../tower-autonomy/`; external prerequisites in this package refer to task IDs in that manifest.

## Select work

1. Run `python3 claude-code/reroll-fleet/next_task.py`.
2. Select one listed task, or a user-named task that the helper reports ready.
3. Read `tasks/<ID>.md` before inspecting source.
4. Treat the task contract as the implementation plan. Do not recursively create a new plan for its bounded scope.

## Isolate work

- Fetch `origin/main` and verify the base revision.
- Create `codex/<lowercase-id>-<short-description>` with `--no-track` in a sibling worktree.
- One task ID per branch/worktree. Never change another task's state.
- Never push directly to `main`.

## Safety invariants

- No action may occur unless the exact device endpoint, lease and account/attempt binding agree.
- Missing, duplicated or stale identity evidence blocks action; never fall back to an arbitrary attached device.
- Every worker has its own database, strategy root, evidence root, checkpoint root and web port.
- A transaction records intent before an action and resolves only from a post-action observation.
- A frame must have a capture timestamp and sequence number. A stale or wrong-orientation frame cannot authorize a tap.
- Preserve candidate and winner accounts before releasing/recycling capacity. The established account is never in the disposable pool.
- Keep unknown, locked, unavailable, unreadable and failed states distinct.

## Verification and finish

- Run only the focused test files named in the task contract, with `-p no:allure_pytest` for Python tests. Do not run a whole test suite.
- Rebuild the tracked web bundle when a runtime/API change requires it.
- A live emulator is required only where a task contract explicitly says so. Never simulate a completed live gate from a fixture alone.
- Stage the finished scoped diff and show its stat plus focused results. Ask the user before committing.
- After approval, commit, push the feature branch and open a PR. Do not merge without explicit user instruction.
