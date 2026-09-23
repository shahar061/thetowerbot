# Reroll fleet execution package

This package is the execution source of truth for the BlueStacks multi-instance reroll campaign. It extends, rather than replaces, [`../tower-autonomy/`](../tower-autonomy/README.md): that package owns general bot capabilities; this one owns the fleet, fresh-account and GT+BH campaign path.

Read [the task graph](graph.mmd) for dependencies and [the manifest](manifest.json) for machine-readable task selection. Every task has an outcome, external prerequisites, acceptance gate, source-file map and focused verification scope in `tasks/`.

## Start a Claude Code session

```text
Read claude-code/reroll-fleet/CLAUDE.md and claude-code/reroll-fleet/manifest.json. Run the ready-task helper, then take exactly one ready task in a new worktree. Read that task contract before exploring code. Do not start another task or update another task's status.
```

The helper shows only reroll/fleet tasks whose internal and external prerequisites are merged to `main`:

```bash
python3 claude-code/reroll-fleet/next_task.py
```

The current initial task is **M01**. Existing base-plan tasks may run alongside it in their own worktrees: B05, B08, C01, C03, F01, L01 and U01 are currently dependency-ready according to the base package.

## Parallel work rules

- One Claude Code session owns one task ID, branch and sibling worktree.
- A session must not start a task whose internal or external prerequisites are not merged to `main`.
- `M01` can begin now. `B05`, `B08`, `F01` and `U01` are high-value parallel base tasks; they unlock R01/R04 later.
- After M01 and M03, R00 creates a new in-game Tower Account ID on every cloned disposable worker. M05 then qualifies a source by proving two R00-initialized clones can coexist. Fresh BlueStacks instances remain the default until that gate passes; cloning is an optional host-qualified optimization.
- Do not run M02, M03, or M05 concurrently with M01 because they touch device/runtime or identity boundaries. Other tasks become available only through the graph.
- A task is complete only after its acceptance gate passes and its PR merges. A landed partial slice updates the task's current-state section but does not unlock dependents.

## Planning rule

The task contract is the approved implementation plan for its bounded deliverable. A Claude Code worker should read the task file and execute it; it does **not** run a new `writing-plans` cycle for every task. Stop and request a new design/plan only if the task reveals an unbounded subsystem, changes a public cross-task contract, or lacks evidence needed to define safe behaviour.

M01 owns fail-closed worker isolation and an evidence-backed binding sink. R00 owns creating and reading the in-game Tower Account ID, and depends on M01; it is not a completion requirement for M01.

## Task list

| ID | Deliverable | Depends on |
|---|---|---|
| M01 | Fail-closed worker identity, exclusive device leases and isolated runtime roots | Base B01, B03 |
| M02 | Capture-provider boundary and PNG baseline benchmark | M01 |
| R00 | Create a unique Tower Account ID per cloned worker and verify it after restart | M01, M03; Base B05, B08 |
| M05 | Qualify a clone source only after R00 proves concurrent unique Tower identities | M03, R00 |
| M03 | BlueStacks fresh-instance lifecycle and staged clone provisioning adapter | M01; Base B05, B06 |
| R01 | Fresh-account binding and tutorial workflow | M01, R00; Base B05, B08, F01 |
| R02 | Verified early-game progression policy to required unlocks | R01; Base B07, F02, F03, F04 |
| R03 | Time-aware stones plan across supported reward sources | R02; Base B07, T01, T02, T03 |
| R04 | Read, select and verify target UW offers; protect candidates | R03; Base B05, B07, U01 |
| M04 | Restartable campaign supervisor and candidate retention | M03, R04 |
| O07 | Fleet dashboard fresh-instance workflow and qualified template cloning | M03, M05 |
| O06 | Fleet control room and evidence/incident drill-down | M04, O07 |
| V08 | Two-worker reroll campaign qualification | M02, M04, O06; Base B08, V03 |

Fresh-instance provisioning is available through M03 + O07. Cloning is available only after R00 + M05 + M03 + O07: R00 creates and verifies a unique Tower Account ID on every clone; M05 verifies simultaneous sessions; M03 operates the BlueStacks Multi-instance Manager lifecycle; O07 exposes the audited clone request in the dashboard.

M02 keeps the existing ADB PNG capture as the baseline and only enables a managed H.264 provider when measured capacity improves and its recovery semantics pass. H.264 is not a prerequisite for initial campaign qualification.
