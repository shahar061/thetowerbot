"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { useAccountSelection } from "@/lib/AccountSelection";
import { addRerollMembers, fetchReroll, fetchRerollJournal, pauseReroll, removeRerollMember, setRerollConcurrency, startReroll } from "@/lib/api";
import type { RerollJournalEntry, RerollMember, RerollSnapshot } from "@/lib/fleet";
import { rerollCoordinatorUrl } from "@/lib/fleetRedirect";
import { SharedWorkshopLedger, WorkerBattlePurchases } from "./Purchases";
import { RerollCard } from "./RerollCard";
import { accountAge, coinsPerSecond } from "@/lib/accountMetrics";

const journalColors = ["#2563eb", "#b45309", "#7c3aed", "#047857", "#be185d", "#0e7490"];

function stableColor(name: string): string {
  let hash = 0;
  for (const char of name) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0;
  return journalColors[hash % journalColors.length];
}

function display(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function observed(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function stateLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function Journal({ entries, worker, onWorker }: { entries: RerollJournalEntry[]; worker: string; onWorker: (name: string) => void }) {
  const [level, setLevel] = useState("all");
  const [diagnostics, setDiagnostics] = useState(false);
  const names = [...new Set(entries.map(entry => entry.instance))].sort();
  const visible = entries.filter(entry => (worker === "all" || entry.instance === worker)
    && (level === "all" || entry.level === level)
    && (diagnostics || entry.kind !== "diagnostic"))
    .sort((a, b) => new Date(typeof a.at === "number" ? a.at * 1000 : a.at).getTime()
      - new Date(typeof b.at === "number" ? b.at * 1000 : b.at).getTime() || a.sequence - b.sequence);
  return <RerollCard title="Shared journal">
    <div className="flex flex-wrap gap-3 text-sm">
      <label>Emulator <select aria-label="Journal emulator" value={worker} onChange={event => onWorker(event.target.value)} className="ml-1 rounded border bg-background p-1"><option value="all">All</option>{names.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      <label>Severity <select aria-label="Journal severity" value={level} onChange={event => setLevel(event.target.value)} className="ml-1 rounded border bg-background p-1"><option value="all">All</option>{["info", "warning", "error"].map(item => <option key={item} value={item}>{item}</option>)}</select></label>
      <label className="flex items-center gap-1"><input type="checkbox" checked={diagnostics} onChange={event => setDiagnostics(event.target.checked)} /> Diagnostics</label>
    </div>
    {visible.length ? <ol className="mt-3 max-h-96 min-w-0 space-y-2 overflow-y-auto text-sm">{visible.map(entry => <li key={entry.sequence} className="min-w-0 break-words rounded border p-2 [overflow-wrap:anywhere]">
      <time className="mr-2 text-muted-foreground">{observed(entry.at)}</time>
      <span className="mr-2 font-semibold" style={{ color: entry.color || stableColor(entry.instance) }}>[{entry.instance}]</span>
      <span className="mr-2 text-muted-foreground">{entry.level} · {entry.kind}</span>{entry.message}
    </li>)}</ol> : <p className="mt-3 text-sm text-muted-foreground">No journal entries match these filters.</p>}
  </RerollCard>;
}

export default function RerollPage() {
  const [pool, setPool] = useState<RerollSnapshot | null>(null);
  const [entries, setEntries] = useState<RerollJournalEntry[]>([]);
  const [journalError, setJournalError] = useState<string | null>(null);
  const [picker, setPicker] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [journalWorker, setJournalWorker] = useState("all");
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(2);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const { accounts, choose } = useAccountSelection();

  const refresh = useCallback(async () => {
    const next = await fetchReroll();
    setPool(next);
    setLimit(next.concurrency_limit ?? 2);
    try { setEntries((await fetchRerollJournal()).entries); setJournalError(null); }
    catch (failure) { setJournalError((failure as Error).message); }
  }, []);
  useEffect(() => {
    let active = true;
    let polling = false;
    let redirecting = false;
    const poll = () => {
      if (polling || redirecting) return;
      polling = true;
      void fetchReroll().then(next => { if (active) { setPool(next); setLimit(next.concurrency_limit ?? 2); setError(null); } })
        .catch((failure: Error) => {
          if (!active) return;
          const destination = rerollCoordinatorUrl(failure, window.location.href);
          if (destination) { redirecting = true; window.location.replace(destination); return; }
          setError(failure.message);
        })
        .finally(() => { polling = false; });
      void fetchRerollJournal().then(value => { if (active) { setEntries(value.entries); setJournalError(null); } })
        .catch((failure: Error) => { if (active) setJournalError(failure.message); });
    };
    poll();
    const timer = window.setInterval(poll, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await action(); await refresh(); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };
  const add = () => void act(async () => { setPool(await addRerollMembers(selected)); setSelected([]); setPicker(false); });
  const members = pool?.members ?? [];
  const counts = {
    running: members.filter(member => member.state === "running").length,
    choice: members.filter(member => member.state === "needs_choice").length,
    replace: members.filter(member => member.state === "replace_manually").length,
    blocked: members.filter(member => !["ready", "start_required", "paused", "running", "starting", "stopping", "capacity_wait", "needs_choice", "replace_manually"].includes(member.state)).length,
  };
  const accountFor = (member: RerollMember) => accounts.find(account => account.running && account.instance === member.name && account.account_id);

  return <div className="mx-auto flex max-w-7xl flex-col gap-4">
    <PageHeader title="Reroll" meta="Fleet · manually prepared emulators" action={<Link href="/fleet/history/" className="text-sm text-primary underline">Provisioning history</Link>} />
    <p className="max-w-3xl text-sm text-muted-foreground">Prepare a separate emulator with Tower installed and unopened, then add it to this pool. Start all launches up to the concurrent worker limit; any others stay paused. Remove a worker before preparing its replacement.</p>
    {error && <p role="alert" className="rounded border border-danger p-3 text-sm text-danger">{error}</p>}
    <RerollCard title="Pool overview" tone="live">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{Object.entries(counts).map(([key, count]) => <div key={key} className="rounded border p-3"><strong className="block text-xl">{count}</strong><span className="text-sm capitalize">{key === "choice" ? "Needs choice" : key === "replace" ? "Replace manually" : key}</span></div>)}</div>
      {pool?.pressure && <p className="mt-3 text-sm text-muted-foreground">Worker slots: {pool.pressure.running} running, {pool.pressure.starting} in transition or review, {pool.pressure.available} available of {pool.pressure.limit}.</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50" onClick={() => setPicker(true)}>Add emulators</button>
        <button className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy || !members.length} onClick={() => void act(() => startReroll())}>Start all</button>
        <button className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy || !members.length} onClick={() => void act(() => pauseReroll())}>Pause all</button>
      </div>
      <div className="mt-3 flex flex-wrap items-end gap-2 text-sm"><label className="flex flex-col gap-1">Concurrent workers <select aria-label="Concurrent workers" value={limit} onChange={event => setLimit(Number(event.target.value))} className="rounded border bg-background p-2">{[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}</select></label><button disabled={busy || limit === (pool?.concurrency_limit ?? 2)} onClick={() => void act(() => setRerollConcurrency(limit))} className="rounded border px-3 py-2 disabled:opacity-50">Save limit</button></div>
      {limit === 4 && <p role="status" className="mt-2 text-sm text-amber-700">Four concurrent emulators can strain memory and slow other apps. Start with two, then increase the limit while watching macOS memory pressure.</p>}
      {!members.length && <p className="mt-4 text-sm text-muted-foreground">No emulators in the pool. Add emulators prepared with Tower installed and unopened.</p>}
      {picker && <div className="mt-4 rounded border p-3 text-sm"><h2 className="font-semibold">Add emulators</h2>
        {!pool?.candidates.length && <p className="mt-2">No host emulators available.</p>}
        <div className="mt-2 space-y-2">{pool?.candidates.map(candidate => {
          const eligible = candidate.state === "ready" || candidate.state === "start_required";
          return <label key={candidate.name} className="flex items-start gap-2 rounded border p-2"><input type="checkbox" disabled={!eligible || busy} checked={selected.includes(candidate.name)} onChange={event => setSelected(current => event.target.checked ? [...current, candidate.name] : current.filter(name => name !== candidate.name))} /><span><strong>{candidate.name}</strong> · {candidate.endpoint} · {stateLabel(candidate.state)}{!eligible && <span className="block text-danger">Unavailable: {stateLabel(candidate.state)}</span>}</span></label>;
        })}</div>
        <div className="mt-3 flex gap-2"><button className="rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50" disabled={busy || !selected.length} onClick={add}>Add selected</button><button className="rounded border px-3 py-2" onClick={() => { setPicker(false); setSelected([]); }}>Cancel</button></div>
      </div>}
    </RerollCard>
    <RerollCard title="Reroll strategy">
      <p className="text-sm text-muted-foreground">Each worker uses its own verified runs and purchases to choose the next Workshop upgrade. It checks the observed price and coin balance before spending.</p>
      <p className="text-sm text-muted-foreground">The first buys establish basic attack, then unlock Defense Absolute and Thorns for early survival. Cash and coin income follow. Battle buying prioritizes unlocked Defense Absolute and Thorns, and skips anything still locked.</p>
      <ol className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
        <li className="rounded border p-3"><strong>1. Survive the opening waves</strong><p className="mt-1 text-muted-foreground">Buy basic attack, then unlock and strengthen Defense Absolute and Thorns.</p></li>
        <li className="rounded border p-3"><strong>2. Reach Tier 1 Wave 60</strong><p className="mt-1 text-muted-foreground">Keep defense ahead of enemy damage and add cash and coin income.</p></li>
        <li className="rounded border p-3"><strong>3. Review the first Ultimate Weapon</strong><p className="mt-1 text-muted-foreground">Earn stones, then choose Golden Tower or Black Hole yourself. If neither is offered, replace that emulator manually.</p></li>
      </ol>
      <p className="mt-3 text-sm text-muted-foreground">Each worker card shows ten projected Workshop buys. Only the first is executable; every buy still needs a fresh price, balance, and screen check. The order updates after confirmed progress.</p>
    </RerollCard>
    {!!members.length && <RerollCard title="Workers"><div className="grid gap-3 lg:grid-cols-2">{members.map(member => {
      const account = accountFor(member);
      return <article key={member.name} className="min-w-0 rounded border p-3 text-sm"><div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="font-semibold">{member.name}</h2><p className="text-muted-foreground">{stateLabel(member.state)} · {member.endpoint}</p></div><div className="flex items-center gap-3">{account && <Link href="/" onClick={() => choose(account.key)} className="text-primary underline">Open account {account.account_id}</Link>}<button aria-label={`${collapsed[member.name] ? "Expand" : "Collapse"} ${member.name}`} aria-expanded={!collapsed[member.name]} aria-controls={`worker-${member.name}`} onClick={() => setCollapsed(current => ({ ...current, [member.name]: !current[member.name] }))} className="rounded border px-2 py-1 text-lg leading-none">{collapsed[member.name] ? "⌄" : "⌃"}</button></div></div>
        <div id={`worker-${member.name}`} hidden={!!collapsed[member.name]}>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">{([
          ["Verified account ID", member.account_id ?? account?.account_id], ["Game screen", member.game_screen], ["Milestone", member.milestone], ["Latest completed tier / wave", member.tier != null && member.wave != null ? `${member.tier} / ${member.wave}` : null], ["Run duration (s)", member.run_duration_seconds], ["Best Tier 1 wave", member.best_tier_1_wave], ["Battle cash", member.battle_cash], ["Run coins", member.run_coins], ["Lifetime coins", member.lifetime_coins], ["Account age", accountAge(member.account_age_days ?? null)], ["Recent CPS", coinsPerSecond(member.recent_cps ?? null)], ["Workshop upgrades bought", member.workshop_upgrades_bought], ["Wallet gems", member.wallet_gems], ["Wallet stones", member.wallet_stones], ["Wallet medals", member.wallet_medals], ["UW result", member.uw_result], ["Last observation", observed(member.observed_at)],
        ] as const).map(([label, value]) => <div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="font-medium">{display(value)}</dd></div>)}</dl>
        {member.lifetime_coins_incomplete && <p className="mt-2 text-xs text-muted-foreground">Lifetime coins await an unreadable run result; showing the last verified baseline.</p>}
        {member.reroll_plan && <div className="mt-3 rounded border p-3"><h3 className="font-semibold">Next Workshop decision</h3><p>{member.reroll_plan.goal} · {member.reroll_plan.item ?? "Operator review"} · {stateLabel(member.reroll_plan.state)}</p><p className="mt-1 text-muted-foreground">{member.reroll_plan.reason}</p><p className="mt-1 text-muted-foreground">Workshop coins: {display(member.reroll_plan.wallet_coins)} · Price: {display(member.reroll_plan.price)} · Lifetime coins: {display(member.reroll_plan.lifetime_coins)}</p>
          {!!member.reroll_plan.next_purchases?.length && <div className="mt-4 border-t pt-3"><h4 className="font-semibold">Next 10 Workshop buys</h4><p className="mt-1 text-xs text-muted-foreground">Projected order; future prices and balances will be checked before each buy.</p><ol aria-label={`Next 10 Workshop buys for ${member.name}`} className="mt-2 grid gap-1.5 sm:grid-cols-2">{member.reroll_plan.next_purchases.map(step => <li key={step.position} className="flex gap-2 rounded border p-2"><span className="font-mono text-muted-foreground">{step.position}.</span><span><span className="font-medium">{step.item}</span>{step.unlock && <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">Unlock</span>}<span className="block text-xs text-muted-foreground">{step.category} · {step.focus}</span></span></li>)}</ol></div>}
        </div>}
        {!member.reroll_plan && member.state === "running" && <p className="mt-3 text-muted-foreground">Workshop plan updates when this worker reaches the main menu.</p>}
        <WorkerBattlePurchases accountKey={member.account_key} />
        <details className="mt-3"><summary className="cursor-pointer font-medium">Details and controls</summary><div className="mt-2 space-y-2"><p>Recent runs: {member.recent_runs?.join(", ") || "—"}</p><p>Evidence: {display(member.evidence)}</p><p>Error: {display(member.error)}</p><div className="flex flex-wrap gap-2"><button disabled={busy} onClick={() => void act(() => startReroll(member.name))} className="rounded border px-2 py-1">Start</button><button disabled={busy} onClick={() => void act(() => pauseReroll(member.name))} className="rounded border px-2 py-1">Pause</button><button disabled={busy} onClick={() => void act(() => removeRerollMember(member.name))} className="rounded border px-2 py-1">Remove from pool</button><button onClick={() => setJournalWorker(member.name)} className="rounded border px-2 py-1">Show journal</button></div></div></details>
        </div>
      </article>;
    })}</div></RerollCard>}
    {!!members.length && <SharedWorkshopLedger members={members} />}
    {journalError && <p role="status" className="text-sm text-muted-foreground">Shared journal unavailable: {journalError}</p>}
    {!journalError && <Journal entries={entries} worker={journalWorker} onWorker={setJournalWorker} />}
  </div>;
}
