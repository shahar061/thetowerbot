#!/usr/bin/env bash
# Sync the code, sync deps, rebuild the dashboard if it is stale, then run the
# bot with the browser dashboard.
#
# The code sync is first because everything below it is downstream of the
# source: the lockfile, the bundle's backend hash, and the bot itself. It
# exists because of a real stall - a fix was merged, `./run.sh` was run, and
# the bot came up on the pre-merge navigate.py, because this script synced
# DEPENDENCIES and never once mentioned git. The screen it could not get off
# looked exactly like the bug that had just been fixed, and the half hour that
# cost is the whole argument for the step.
#
# The other step that matters is the freshness check. `runtime_identity` freezes the
# backend's source hash at import and the built bundle records the backend it
# was built against, so editing Python invalidates the bundle without touching
# a single dashboard source file. Starting the bot then serves a dashboard that
# refuses to drive it - "Runtime mismatch - controls locked" - and the browser
# is a poor place to discover that. This checks before launching, and again
# after building, and fails here with both hashes instead.
#
# Node is needed when the local dashboard bundle is missing or stale. The
# bundle itself is generated and ignored, so a fresh clone takes this path once;
# subsequent starts skip it until the source hash changes. See README's
# Dashboard section.
#
# Extra flags pass through: ./run.sh --idle, ./run.sh --port 5556, etc.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

step() { printf '\033[1m[%s/5]\033[0m %s\n' "$1" "$2"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }
note() { printf '      %s\n' "$1"; }

# --- 1. the code ----------------------------------------------------------
# Fast-forward only, and only ever onto the upstream the current branch
# already tracks. This step must never pick a branch, never merge, never
# rebase, and never resolve a conflict: it exists to close the gap between
# "I merged the fix" and "the bot has the fix", not to move work around.
#
# Every way it can decline says so on its own line, because a sync step that
# skips quietly is the failure it was added to prevent.
step 1 'git sync'
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    note 'not a git checkout - skipping'
else
    branch=$(git symbolic-ref --quiet --short HEAD || true)
    upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)
    if [[ -z "$branch" ]]; then
        note 'detached HEAD - you are pinned on purpose, skipping'
    elif [[ -z "$upstream" ]]; then
        note "$branch tracks nothing - skipping"
    # GIT_TERMINAL_PROMPT=0 so an expired credential fails instead of blocking
    # the start script on a password prompt nobody is watching for. Offline is
    # a warning, not a stop: the bot has to start on a plane.
    elif ! GIT_TERMINAL_PROMPT=0 git fetch --quiet "${upstream%%/*}" 2>/dev/null; then
        note "could not reach ${upstream%%/*} - running what is checked out"
    else
        behind=$(git rev-list --count "HEAD..$upstream")
        ahead=$(git rev-list --count "$upstream..HEAD")
        if (( behind == 0 && ahead == 0 )); then
            note "already current with $upstream"
        elif (( behind == 0 )); then
            note "$branch is $ahead ahead of $upstream - nothing to pull"
        elif (( ahead > 0 )); then
            # Diverged. Fast-forward is not available and anything else is a
            # decision about someone's unpushed work, which is not this
            # script's to make.
            note "$branch has diverged from $upstream ($ahead ahead, $behind behind)"
            note 'leaving it alone - rebase or merge it yourself'
        elif git merge --ff-only --quiet "$upstream" 2>/dev/null; then
            note "fast-forwarded $behind commit(s) to $upstream"
        else
            fail "cannot fast-forward to $upstream - you have local changes:
$(git status --short | sed 's/^/       /')
       commit or stash them, then re-run."
        fi
    fi
fi

# --- 2. dependencies ------------------------------------------------------
step 2 'uv sync'
command -v uv >/dev/null 2>&1 || fail 'uv is not installed - see https://docs.astral.sh/uv/'
# --frozen: install exactly uv.lock, never silently re-resolve it. A start
# script that could rewrite the lockfile would make "it works on my machine"
# depend on who started the bot last.
uv sync --frozen --quiet

# --- 3. the dashboard bundle ---------------------------------------------
step 3 'dashboard freshness'
if uv run --quiet python -m tools.freshness >/dev/null 2>&1; then
    printf '      already current - skipping npm\n'
else
    reason=$(uv run --quiet python -m tools.freshness 2>/dev/null || true)
    printf '      %s\n' "$reason"
    command -v npm >/dev/null 2>&1 || fail \
        "the dashboard bundle is stale and npm is not installed.
       Install Node.js and npm, then re-run."
    if [[ ! -x web/ui/node_modules/.bin/next ]]; then
        note 'installing locked UI dependencies'
        ( cd web/ui && npm ci )
    fi
    ( cd web/ui && npm run build )
    # Re-checked, not assumed: a build that succeeds can still leave the
    # bundle mismatched (a partial publish, or sources changing underneath
    # it). Without this the script would hand the browser the very banner it
    # exists to prevent.
    uv run --quiet python -m tools.freshness >/dev/null 2>&1 || fail \
        "the bundle is STILL stale after building:
       $(uv run --quiet python -m tools.freshness 2>/dev/null || true)"
    printf '      rebuilt and verified\n'
fi

# --- 4. free the port -----------------------------------------------------
# Read the dashboard port, independently of --port (the emulator ADB port).
# Only --web-port changes which local dashboard listener we replace.
port=$(uv run --quiet python -c 'import config; print(config.WEB_PORT)')
args=("$@")
for i in "${!args[@]}"; do
    if [[ "${args[i]}" == '--web-port' && -n "${args[i+1]:-}" ]]; then
        port="${args[i+1]}"
    fi
done

step 4 "freeing port $port"
holders=$(lsof -ti "tcp:$port" 2>/dev/null || true)
if [[ -z "$holders" ]]; then
    printf '      nothing listening\n'
else
    for pid in $holders; do
        # Only ever a previous run of THIS bot. Something else on the port is
        # reported and left alone: a start script that killed by port number
        # alone would eventually take out an unrelated dev server.
        if ps -p "$pid" -o command= 2>/dev/null | grep -q 'tower_bot\.py'; then
            printf '      stopping previous bot (pid %s)\n' "$pid"
            kill "$pid" 2>/dev/null || true
            for _ in {1..50}; do
                ps -p "$pid" >/dev/null 2>&1 || break
                sleep 0.1
            done
            ps -p "$pid" >/dev/null 2>&1 && fail "pid $pid would not stop - kill it and retry"
        else
            fail "port $port is held by pid $pid, which is not this bot:
       $(ps -p "$pid" -o command= 2>/dev/null | head -1)"
        fi
    done
fi

# --- 5. run ---------------------------------------------------------------
step 5 "uv run tower_bot.py --web --dismiss-bluestacks-upgrade ${*:-}"
printf '\n  dashboard -> http://127.0.0.1:%s\n\n' "$port"
# exec, so Ctrl+C reaches the bot itself rather than this wrapper and the
# bot's own shutdown path runs.
exec uv run tower_bot.py --web --dismiss-bluestacks-upgrade "$@"
