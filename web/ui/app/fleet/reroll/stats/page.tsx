"use client";

import { useEffect, useState } from "react";
import { ChartLine, Timer, Trophy } from "lucide-react";
import { Meter } from "@/components/Meter";
import { StatsPanels } from "@/components/StatsPanels";
import { fetchAccountStats } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import type { StatsPayload } from "@/lib/types";
import { deviceColor, LADDER, ladderProgress } from "@/lib/rerollState";
import { useRerollWorkspace } from "../RerollWorkspace";
import { memberIdentity, timeLabel } from "../statsHelpers";

type Result = { data: StatsPayload | null; error: string | null };
const TARGETS = [20, 30, 60, 100];

function EmulatorStats({ member, result }: { member: RerollMember; result?: Result }): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const data = result?.data;
  const best = data?.summary?.best_tier_1_wave ?? null;
  const progress = ladderProgress(best);
  const color = deviceColor(member.name);
  return <article aria-label={`Stats for ${member.name}`} className="min-w-0 rounded-xl border border-border bg-card p-5" style={{ borderTop: `3px solid ${color}` }}>
    <header className="flex flex-wrap items-start justify-between gap-2">
      <div><h2 className="font-heading text-lg font-semibold" style={{ color }}>{member.name}</h2>
        <p className="font-mono text-xs text-muted-foreground">Account {member.account_id ?? "not verified"}</p></div>
      <span className="rounded-full bg-muted px-2 py-1 text-xs text-muted-foreground">{member.state.replaceAll("_", " ")}</span>
    </header>
    {!result ? <p role="status" className="py-8 text-sm text-muted-foreground">Loading emulator stats…</p>
      : result.error ? <p role="alert" className="mt-4 rounded-lg bg-warn-surface p-3 text-sm text-warn">{result.error}</p>
      : data && <>
        <div className="my-5 flex items-end justify-between gap-3">
          <div><p className="text-xs text-muted-foreground">Highest Tier 1 wave</p><p className="mt-1 font-mono text-4xl font-semibold tracking-tight">{best ?? "—"}</p></div>
          <div className="space-y-1 text-right text-xs text-muted-foreground"><p>{data.summary?.total_runs ?? data.runs.length} completed runs</p><p>{timeLabel(data.summary?.play_seconds)} recorded play</p></div>
        </div>
        <section aria-label={`Milestone progress for ${member.name}`} className="mb-5 rounded-lg bg-muted/40 p-3">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs"><span className="font-medium">{LADDER[progress.rung].title}</span><span className="text-muted-foreground">{progress.remaining === null ? "Ultimate Weapon choice" : `Next: T1 W${LADDER[progress.rung].target}`}</span></div>
          <Meter label={`Reroll progress for ${member.name}`} value={progress.percent} max={100} unknown={best === null} tone="primary" />
          <p className="mt-2 text-xs text-muted-foreground">{best === null ? "No verified Tier 1 run yet" : progress.remaining === null ? "Wave 60 reached · choose your Ultimate Weapon" : `${progress.remaining} waves to the next milestone`}</p>
        </section>
        {data.runs.length ? <>
          <p className="mb-3 text-xs text-muted-foreground">Recent-run stats · latest {data.runs.length} completed runs across all tiers</p>
          <StatsPanels stats={data} compact view="wave" />
          <button type="button" aria-expanded={expanded} className="mt-4 w-full rounded-lg border border-border py-2 text-sm hover:bg-accent" onClick={() => setExpanded(value => !value)}>{expanded ? "Hide detailed charts" : "Run length, taps & screen events"}</button>
          {expanded && <div className="mt-4"><StatsPanels stats={data} compact view="details" /></div>}
        </> : <p className="py-4 text-sm text-muted-foreground">No completed runs recorded yet.</p>}
      </>}
  </article>;
}

export default function FleetStatsPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const members = [...(pool?.members ?? [])].filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const [results, setResults] = useState<Record<string, Result>>({});
  const [metric, setMetric] = useState<"play_seconds" | "elapsed_seconds">("play_seconds");
  useEffect(() => {
    let active = true;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const scoped = JSON.parse(identity) as RerollMember[];
    async function load(member: RerollMember): Promise<void> {
      const key = memberIdentity(member);
      let result: Result;
      if (!member.account_key || !member.account_id) result = { data: null, error: "Waiting for a verified account." };
      else {
        try {
          const data = await fetchAccountStats(member.account_key, member.account_id);
          if (data.account_id !== member.account_id) throw new Error("Account changed or stats are unavailable. Waiting for matching account history.");
          result = { data, error: null };
        } catch (failure) { result = { data: null, error: failure instanceof Error ? failure.message : "Stats unavailable" }; }
      }
      if (!active) return;
      setResults(current => ({ ...current, [key]: result }));
      const timer = setTimeout(() => { timers.delete(timer); void load(member); }, 15000);
      timers.add(timer);
    }
    scoped.forEach(member => { void load(member); });
    return () => { active = false; timers.forEach(clearTimeout); };
  }, [identity]);
  return <main className="space-y-6">
    <header><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / stats</p>
      <h1 className="text-3xl font-semibold tracking-tight">Every run. Every milestone.</h1>
      <p className="mt-2 text-sm text-muted-foreground">Compare the fleet’s pace, spot progress, and explore each account’s run history.</p></header>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {loading && !pool ? <p role="status">Loading fleet…</p> : !members.length ? <p className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">No visible emulators in this fleet.</p> : <>
      <section aria-label="Milestone benchmarks" className="overflow-hidden rounded-xl border border-border bg-card">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b p-5">
          <div><h2 className="flex items-center gap-2 text-lg font-semibold"><Timer className="size-5 text-primary" /> Milestone benchmarks</h2><p className="mt-1 text-xs text-muted-foreground">Full recorded history · confirmed at the end of the first reaching run</p></div>
          <div aria-label="Benchmark clock" className="flex rounded-lg border p-1">{(["play_seconds", "elapsed_seconds"] as const).map(value => <button key={value} type="button" aria-pressed={metric === value} className={`rounded-md px-3 py-1.5 text-xs ${metric === value ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent"}`} onClick={() => setMetric(value)}>{value === "play_seconds" ? "Recorded play" : "Elapsed time"}</button>)}</div>
        </header>
        <div className="overflow-x-auto"><table className="w-full min-w-[620px] text-sm"><thead><tr className="bg-muted/30 text-left text-xs text-muted-foreground"><th className="px-5 py-3 font-medium">Emulator / account</th>{TARGETS.map(wave => <th key={wave} className="px-4 py-3 font-medium">T1 W{wave}</th>)}</tr></thead>
          <tbody>{members.map(member => <tr key={memberIdentity(member)} className="border-t"><th scope="row" className="px-5 py-4 text-left font-medium"><span style={{ color: deviceColor(member.name) }}>{member.name}</span><span className="mt-1 block font-mono text-[10px] font-normal text-muted-foreground">{member.account_id ?? "Unverified"}</span></th>
            {TARGETS.map(wave => {
              const result = results[memberIdentity(member)];
              const benchmark = result?.data?.benchmarks?.find(row => row.tier === 1 && row.wave === wave);
              const value = benchmark?.[metric];
              const samples = members.flatMap(item => { const v = results[memberIdentity(item)]?.data?.benchmarks?.find(row => row.tier === 1 && row.wave === wave)?.[metric]; return v == null ? [] : [v]; });
              const fastest = value != null && samples.length > 1 && value === Math.min(...samples);
              return <td key={wave} className="px-4 py-4 align-top"><span className="font-mono text-sm">{!result ? "Loading…" : result.error || !benchmark ? "Unavailable" : value == null ? "Not reached" : timeLabel(value)}</span>
                {value != null && <div className="mt-2 h-1 w-24 overflow-hidden rounded-full bg-muted" aria-hidden="true"><div className="h-full rounded-full" style={{ width: `${Math.max(3, value / (Math.max(...samples) || 1) * 100)}%`, background: deviceColor(member.name) }} /></div>}
                {fastest && <span className="mt-1 flex items-center gap-1 text-[10px] text-live"><Trophy className="size-3" /> Fastest recorded</span>}
              </td>;
            })}</tr>)}</tbody></table></div>
        <p className="border-t px-5 py-3 text-xs text-muted-foreground">{metric === "play_seconds" ? "Sums completed-run durations through the reaching run; excludes time between runs." : "Time from the first recorded run to the reaching run’s end; includes time between runs."} A run can cross several milestones. These are recorded timings, not exact wave-crossing timestamps.</p>
      </section>
      <div className="flex items-center gap-2"><ChartLine className="size-4 text-primary" /><h2 className="font-semibold">Emulator stats</h2><span className="text-xs text-muted-foreground">Updates every 15 seconds</span></div>
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2 2xl:grid-cols-3">{members.map(member => <EmulatorStats key={memberIdentity(member)} member={member} result={results[memberIdentity(member)]} />)}</div>
    </>}
  </main>;
}
