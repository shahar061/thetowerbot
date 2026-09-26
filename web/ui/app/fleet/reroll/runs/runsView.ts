import type { RunRow } from "@/lib/types";

/** A run tagged with the emulator (worker DB) it came from. */
export interface FleetRun extends RunRow { emulator: string; accountKey: string; accountId: string }

type Source = { member: { name: string; account_key: string; account_id: string }; runs: RunRow[] };

const endedOrStarted = (run: RunRow): number => run.ended_at ?? run.started_at;

export function mergeRuns(sources: Source[]): FleetRun[] {
  return sources
    .flatMap(({ member, runs }) => runs.map(run => ({
      ...run, emulator: member.name, accountKey: member.account_key, accountId: member.account_id,
    })))
    .sort((a, b) => endedOrStarted(b) - endedOrStarted(a));
}

const finished = (run: RunRow): boolean => run.ended_at !== null;

/** Never a guess: a live run, an abandoned run and a failed read each say so. */
export function killedLabel(run: RunRow): string {
  if (!finished(run)) return "In progress";
  if (run.abandoned) return "Abandoned";
  return run.killed_by ?? "Unreadable";
}

export function killedBy(runs: RunRow[]): { counts: { name: string; count: number; share: number }[]; excluded: number } {
  const done = runs.filter(finished);
  const known = done.filter(run => !run.abandoned && run.killed_by);
  const tally = new Map<string, number>();
  for (const run of known) tally.set(run.killed_by!, (tally.get(run.killed_by!) ?? 0) + 1);
  const counts = [...tally].map(([name, count]) => ({ name, count, share: Math.round(count / known.length * 100) }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  return { counts, excluded: done.length - known.length };
}

export function totalCoins(run: RunRow): number | null {
  if (run.total_coins !== undefined && run.total_coins !== null) return run.total_coins;
  return run.coins === null ? null : run.coins + (run.ad_coins ?? 0);
}

export function recordTip(kind: "wave" | "coin", run: RunRow): string | null {
  const state = kind === "wave" ? run.wave_record : run.coin_record;
  if (!state) return null;
  const prev = kind === "wave" ? run.wave_prev : run.coin_prev;
  const breaker = kind === "wave" ? run.wave_broken_by : run.coin_broken_by;
  const head = kind === "wave" ? `Wave record at T${run.tier}` : "Coin record";
  const beat = prev ? `beat #${prev.run_id} (${prev.value.toLocaleString()})` : "first run";
  const tail = state === "broken"
    ? (typeof breaker === "number" ? `broken by #${breaker}` : "broken")
    : "still standing";
  return `${head} · ${beat} · ${tail}`;
}
