"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Meter, Pips } from "@/components/Meter";
import { StatTile } from "@/components/StatTile";
import { Button } from "@/components/ui/button";
import { useAccountSelection } from "@/lib/AccountSelection";
import { addRerollMembers, fetchReroll, fetchRerollJournal, pauseReroll, removeRerollMember, retireRerollMember, setRerollConcurrency, startNewReroll, startReroll, stopRerollInstance } from "@/lib/api";
import type { RerollJournalEntry, RerollMember, RerollSnapshot } from "@/lib/fleet";
import { rerollCoordinatorUrl } from "@/lib/fleetRedirect";
import { LADDER, attentionRank, deviceColor, failureHint, standingFor } from "@/lib/rerollState";
import { cn } from "@/lib/utils";
import { DeviceCard } from "./DeviceCard";
import { NewRerollDialog } from "./NewRerollDialog";
import { PastRerolls } from "./PastRerolls";
import { SharedWorkshopLedger } from "./Purchases";
import { RerollCard } from "./RerollCard";

function observed(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function stateLabel(value: string): string {
  return standingFor(value).label;
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
  const severities = { info: 0, warning: 0, error: 0 } as Record<string, number>;
  for (const entry of entries) if (entry.kind !== "diagnostic") severities[entry.level] = (severities[entry.level] ?? 0) + 1;

  return <RerollCard
    title="Shared journal"
    action={<div className="flex items-center gap-2 font-mono text-[10px]">
      {/* The severity census is the panel's headline: an error the filter is
          currently hiding has to be visible from the collapsed header. */}
      {severities.error ? <span className="rounded bg-danger-surface px-1.5 py-0.5 text-danger">{severities.error} error</span> : null}
      {severities.warning ? <span className="rounded bg-warn-surface px-1.5 py-0.5 text-warn">{severities.warning} warning</span> : null}
      <span className="text-faint-foreground">{visible.length} shown</span>
    </div>}
  >
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-1.5 text-muted-foreground">Emulator <select aria-label="Journal emulator" value={worker} onChange={event => onWorker(event.target.value)} className="rounded-md border bg-background px-2 py-1 text-foreground"><option value="all">All</option>{names.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      <label className="flex items-center gap-1.5 text-muted-foreground">Severity <select aria-label="Journal severity" value={level} onChange={event => setLevel(event.target.value)} className="rounded-md border bg-background px-2 py-1 text-foreground"><option value="all">All</option>{["info", "warning", "error"].map(item => <option key={item} value={item}>{item}</option>)}</select></label>
      <label className="flex items-center gap-1.5 text-muted-foreground"><input type="checkbox" checked={diagnostics} onChange={event => setDiagnostics(event.target.checked)} /> Diagnostics</label>
    </div>
    {visible.length ? <ol className="max-h-96 min-w-0 space-y-1 overflow-y-auto">{visible.map(entry => <li
      key={entry.sequence}
      className={cn("grid min-w-0 grid-cols-[auto_1fr] gap-x-2.5 rounded-md border-l-2 bg-well/60 px-2.5 py-1.5 text-sm",
        entry.level === "error" ? "border-l-danger" : entry.level === "warning" ? "border-l-warn" : "border-l-border-strong")}
    >
      <time className="font-mono text-[11px] leading-5 text-faint-foreground">{observed(entry.at)}</time>
      <div className="min-w-0 break-words [overflow-wrap:anywhere]">
        <span className="mr-2 font-semibold" style={{ color: entry.color || deviceColor(entry.instance) }}>[{entry.instance}]</span>
        <span className={cn("mr-2 font-mono text-[10px] uppercase",
          entry.level === "error" ? "text-danger" : entry.level === "warning" ? "text-warn" : "text-faint-foreground")}>{entry.level}</span>
        <span className="mr-2 font-mono text-[10px] text-faint-foreground">{entry.kind}</span>
        {entry.message}
      </div>
    </li>)}</ol> : <p className="text-sm text-muted-foreground">No journal entries match these filters.</p>}
  </RerollCard>;
}

/** The pool's four filters, and what each one is for. "Needs you" is the
 *  reason this row exists at all: on a pool of eight emulators the one card
 *  that has stopped and is waiting for a human is otherwise three screens
 *  down, indistinguishable from the seven that are fine. */
type Filter = "all" | "attention" | "live" | "idle";

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
  const [filter, setFilter] = useState<Filter>("all");
  const [dialog, setDialog] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
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
  const confirmNew = (value: { keep: string[]; add: string[] }) => void (async () => {
    setBusy(true); setDialogError(null);
    try { setPool(await startNewReroll(value)); setDialog(false); await refresh(); }
    catch (failure) { setDialogError((failure as Error).message); }
    finally { setBusy(false); }
  })();
  const members = useMemo(() => pool?.members ?? [], [pool]);
  const run = pool?.run ?? null;
  const operating = pool?.operation?.state === "running";

  // One pass over the pool, classified through the shared vocabulary rather
  // than by re-listing state strings here. The old page kept its own inline
  // allow-list of "normal" states, which meant every state added on the
  // Python side silently landed in the "blocked" bucket with no label.
  const census = useMemo(() => {
    const counts = { total: members.length, live: 0, attention: 0, transit: 0, idle: 0 };
    for (const member of members) {
      const standing = standingFor(member.state);
      if (standing.needsYou) counts.attention += 1;
      else if (standing.tone === "live") counts.live += 1;
      else if (standing.transient) counts.transit += 1;
      else counts.idle += 1;
    }
    return counts;
  }, [members]);

  const shown = useMemo(() => {
    const matches = (member: RerollMember) => {
      const standing = standingFor(member.state);
      if (filter === "attention") return standing.needsYou;
      if (filter === "live") return standing.tone === "live";
      if (filter === "idle") return !standing.needsYou && standing.tone !== "live";
      return true;
    };
    return members.filter(matches).sort((a, b) =>
      attentionRank(a.state) - attentionRank(b.state) || a.name.localeCompare(b.name));
  }, [members, filter]);

  const accountFor = (member: RerollMember) => accounts.find(account => account.running && account.instance === member.name && account.account_id);

  const pressure = pool?.pressure;
  const slots: ("running" | "starting" | "free")[] = pressure
    ? [
      ...Array<"running">(Math.min(pressure.running, pressure.limit)).fill("running"),
      ...Array<"starting">(Math.max(0, Math.min(pressure.starting, pressure.limit - pressure.running))).fill("starting"),
      ...Array<"free">(Math.max(0, pressure.limit - pressure.running - pressure.starting)).fill("free"),
    ]
    : [];

  const FILTERS: { id: Filter; label: string; count: number; tone: string }[] = [
    { id: "all", label: "All", count: census.total, tone: "text-foreground" },
    { id: "attention", label: "Needs you", count: census.attention, tone: "text-warn" },
    { id: "live", label: "Running", count: census.live, tone: "text-live" },
    { id: "idle", label: "Idle", count: census.idle + census.transit, tone: "text-muted-foreground" },
  ];

  return <div className="mx-auto flex max-w-7xl flex-col gap-4">
    <PageHeader
      title="Reroll"
      meta={run ? `${run.name} · started ${new Date(run.started_at).toLocaleDateString()} · ${census.total} ${census.total === 1 ? "emulator" : "emulators"}` : "No active reroll"}
      action={<Link href="/fleet/history/" className="text-sm text-primary underline">Provisioning history</Link>}
    />
    {error && <p role="alert" className="rounded-lg border border-danger bg-danger-surface p-3 text-sm text-danger">{error}</p>}
    {pool?.operation && pool.operation.state !== "done" && <p role="status" aria-label="Reroll operation"
      className={cn("rounded-lg border p-3 text-sm", pool.operation.state === "failed" ? "border-danger bg-danger-surface text-danger" : "border-warn/40 bg-warn-surface text-warn")}>
      {pool.operation.state === "failed" ? `Last operation failed: ${pool.operation.error}${failureHint(pool.operation.error) ? ` - ${failureHint(pool.operation.error)}` : ""}` :
        pool.operation.kind === "new_run" ? "Starting a new reroll… retiring emulators and shutting them down"
          : pool.operation.kind === "remove" ? `Removing ${pool.operation.target} from the reroll…`
          : pool.operation.kind === "add" ? `Adding ${pool.operation.target ?? "emulators"}… a stopped emulator boots first to check The Tower has never been opened`
          : `Retiring ${pool.operation.target}…`}
    </p>}
    {pool?.stop_failures?.map(item => <p key={item.name} role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-danger bg-danger-surface p-3 text-sm text-danger">
      {item.name} was retired but its emulator is still running.
      <Button size="xs" variant="outline" aria-label={`Shut down ${item.name}`} disabled={busy || operating} onClick={() => void act(() => stopRerollInstance(item.name))}>Shut down</Button>
    </p>)}

    <RerollCard title="Pool overview" tone={census.attention ? "warn" : census.live ? "live" : undefined}>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatTile label="Devices" value={census.total} />
        <StatTile label="Running" value={census.live} tone={census.live ? "live" : "none"} />
        <StatTile label="Needs you" value={census.attention} tone={census.attention ? "warn" : "none"}
          sub={census.attention ? "stopped until you act" : "nothing waiting"} subTone={census.attention ? "warn" : "none"} />
        <StatTile label="Idle or moving" value={census.idle + census.transit}
          sub={census.transit ? `${census.transit} in transition` : undefined} />
      </div>

      {/* Worker slots, as slots. "3 running, 1 starting, 0 available of 4"
          was a sentence a reader had to parse; the pips are the same fact
          countable in peripheral vision. */}
      {pressure && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-well/60 p-2.5">
        <Pips states={slots} label={`${pressure.running} of ${pressure.limit} worker slots running`} />
        <span className="font-mono text-[11px] text-muted-foreground">
          {pressure.running} running · {pressure.starting} in transition or review · {pressure.available} available of {pressure.limit}
        </span>
      </div>}

      <div className="flex flex-wrap items-center gap-2">
        {run
          ? <><Button variant="outline" disabled={busy || operating} onClick={() => { setDialogError(null); setDialog(true); }}>New reroll</Button>
              <Button disabled={operating} onClick={() => setPicker(true)}>Add emulators</Button></>
          : <Button disabled={busy || operating} onClick={() => { setDialogError(null); setDialog(true); }}>Start a reroll</Button>}
        <Button variant="outline" disabled={busy || !members.length || operating} onClick={() => void act(() => startReroll())}>Start all</Button>
        <Button variant="outline" disabled={busy || !members.length || operating} onClick={() => void act(() => pauseReroll())}>Pause all</Button>
        <label className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          Concurrent workers
          <select aria-label="Concurrent workers" value={limit} onChange={event => setLimit(Number(event.target.value))} className="rounded-md border bg-background px-2 py-1 text-foreground">
            {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <Button variant="outline" disabled={busy || limit === (pool?.concurrency_limit ?? 2)} onClick={() => void act(() => setRerollConcurrency(limit))}>Save limit</Button>
      </div>
      {limit === 4 && <p role="status" className="rounded-lg border border-warn/40 bg-warn-surface p-2.5 text-sm text-warn">Four concurrent emulators can strain memory and slow other apps. Start with two, then increase the limit while watching macOS memory pressure.</p>}

      {!members.length && <p className="text-sm text-muted-foreground">{run ? "No emulators in this reroll." : "No active reroll. Start one with emulators prepared with Tower installed and unopened."}</p>}

      {picker && <div className="rounded-lg border border-border p-3 text-sm"><h3 className="font-semibold">Add emulators</h3>
        <p className="mt-1 text-xs text-muted-foreground">Prepare each emulator with Tower installed and unopened before adding it.</p>
        {!pool?.candidates.length && <p className="mt-2">No host emulators available.</p>}
        <div className="mt-2 space-y-2">{pool?.candidates.map(candidate => {
          const eligible = candidate.state === "ready" || candidate.state === "start_required";
          return <label key={candidate.name} className={cn("flex items-start gap-2 rounded-lg border p-2", eligible ? "border-border" : "border-border bg-muted/40 opacity-70")}><input type="checkbox" disabled={!eligible || busy} checked={selected.includes(candidate.name)} onChange={event => setSelected(current => event.target.checked ? [...current, candidate.name] : current.filter(name => name !== candidate.name))} className="mt-0.5" /><span><strong>{candidate.name}</strong> <span className="font-mono text-xs text-faint-foreground">{candidate.endpoint}</span> · {stateLabel(candidate.state)}{!eligible && <span className="block text-danger">Unavailable: {candidate.state.replaceAll("_", " ")}</span>}</span></label>;
        })}</div>
        <div className="mt-3 flex gap-2"><Button disabled={busy || !selected.length} onClick={add}>Add selected</Button><Button variant="outline" onClick={() => { setPicker(false); setSelected([]); }}>Cancel</Button></div>
      </div>}
    </RerollCard>

    {!!members.length && <RerollCard
      title="Devices"
      action={<div className="flex flex-wrap gap-1">{FILTERS.map(option => (
        <button
          key={option.id}
          type="button"
          aria-pressed={filter === option.id}
          onClick={() => setFilter(option.id)}
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
            filter === option.id
              ? "border-primary bg-primary/12 text-foreground"
              : "border-border text-muted-foreground hover:bg-muted",
          )}
        >
          {option.label} <span className={cn("font-mono", option.count ? option.tone : "text-faint-foreground")}>{option.count}</span>
        </button>
      ))}</div>}
    >
      {/* items-start: a stretched grid row gave a short card (a worker still
          starting, with no plan yet) the height of the tall one beside it,
          which read as a card with something missing rather than a card with
          little to say. */}
      {shown.length ? <div className="grid items-start gap-3 lg:grid-cols-2">{shown.map(member => {
        const account = accountFor(member);
        return <DeviceCard
          key={member.name}
          member={member}
          accountKey={member.account_key}
          accountId={account?.account_id ?? member.account_id}
          collapsed={!!collapsed[member.name]}
          onToggle={() => setCollapsed(current => ({ ...current, [member.name]: !current[member.name] }))}
          onStart={() => void act(() => startReroll(member.name))}
          onPause={() => void act(() => pauseReroll(member.name))}
          onRetire={() => void act(() => retireRerollMember(member.name))}
          onRemove={() => void act(() => removeRerollMember(member.name))}
          onJournal={() => setJournalWorker(member.name)}
          onOpenAccount={account ? () => choose(account.key) : undefined}
          busy={busy || operating}
        />;
      })}</div> : <p className="text-sm text-muted-foreground">No device matches this filter.</p>}
    </RerollCard>}

    <RerollCard title="Reroll strategy">
      <p className="text-sm text-muted-foreground">Each worker uses its own verified runs and purchases to choose the next Workshop upgrade. It checks the observed price and coin balance before spending. The first buys establish basic attack, then unlock Defense Absolute and Thorns for early survival; cash and coin income follow.</p>
      {/* The same three rungs every device card draws its progress against,
          so the ladder on a card and the ladder in the explanation cannot
          drift apart - they are one array in lib/rerollState.ts. */}
      <ol className="grid gap-2 sm:grid-cols-3">
        {LADDER.map((step, index) => (
          <li key={step.id} className="relative overflow-hidden rounded-lg border border-border bg-well/40 p-3">
            <div className="flex items-center gap-2">
              <span className="flex size-5 items-center justify-center rounded-full bg-primary/15 font-mono text-[10px] font-bold text-primary">{index + 1}</span>
              <strong className="text-[13px]">{step.title}</strong>
              {step.target ? <span className="ml-auto font-mono text-[10px] text-faint-foreground">to W{step.target}</span> : null}
            </div>
            <p className="mt-1.5 text-xs font-medium">{step.goal}</p>
            <p className="mt-1 text-xs text-muted-foreground">{step.blurb}</p>
            <Meter label={`${step.title} rung`} value={1} max={1} tone={index === 2 ? "warn" : "primary"} className="mt-2.5 h-1" />
          </li>
        ))}
      </ol>
      <p className="text-sm text-muted-foreground">Each device card shows ten projected Workshop buys. Only the first is executable; every buy still needs a fresh price, balance and screen check. The order updates after confirmed progress.</p>
    </RerollCard>

    {!!members.length && <SharedWorkshopLedger members={members} />}
    <PastRerolls refreshKey={run?.number} />
    <NewRerollDialog open={dialog} onClose={() => setDialog(false)} run={run} members={members}
      candidates={pool?.candidates ?? []} busy={busy} error={dialogError} onConfirm={confirmNew} />
    {journalError && <p role="status" className="text-sm text-muted-foreground">Shared journal unavailable: {journalError}</p>}
    {!journalError && <Journal entries={entries} worker={journalWorker} onWorker={setJournalWorker} />}
  </div>;
}
