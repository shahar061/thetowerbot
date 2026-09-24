"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Pips } from "@/components/Meter";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { useAccountSelection } from "@/lib/AccountSelection";
import { addRerollMembers, hideRerollMembers, pauseReroll, removeRerollMember, restoreRerollMembers, setRerollConcurrency, startNewReroll, startReroll } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { deletable, failureHint, standingFor } from "@/lib/rerollState";
import { cn } from "@/lib/utils";
import { DeviceCard } from "./DeviceCard";
import { FleetLiveCard, verifiedWorkerAccount } from "./FleetLiveCard";
import { AccountInspector } from "./AccountInspector";
import { useRerollWorkspace } from "./RerollWorkspace";
import { NewRerollDialog } from "./NewRerollDialog";
import { RerollCard } from "./RerollCard";

function stateLabel(value: string): string { return standingFor(value).label; }

/** The pool's filters, and what each one is for. "Needs you" is the
 *  reason this row exists at all: on a pool of eight emulators the one card
 *  that has stopped and is waiting for a human is otherwise three screens
 *  down, indistinguishable from the seven that are fine. "Hidden" holds the
 *  cards deleted from the list, and appears only while there are some. */
type Filter = "all" | "attention" | "live" | "idle" | "hidden";

export default function RerollPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading fleet…</p>}><FleetLivePage /></Suspense>;
}

function FleetLivePage(): React.JSX.Element {
  const { pool, setPool, refresh, loading, error: poolError } = useRerollWorkspace();
  const [picker, setPicker] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(2);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState<Filter>("all");
  const [confirmingBulk, setConfirmingBulk] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const { accounts } = useAccountSelection();
  const search = useSearchParams()?.toString() ?? "";
  const [inspection, setInspection] = useState<{ worker: string; account: string; identity: string } | null>(null);
  const inspectorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const query = new URLSearchParams(search);
    setInspection(query.has("worker") ? { worker: query.get("worker")!, account: query.get("account") ?? "", identity: query.get("identity") ?? "" } : null);
  }, [search]);
  const inspect = (member: RerollMember): void => {
    const next = { worker: member.name, account: member.account_key ?? "", identity: member.account_id ?? "" };
    setInspection(next);
    window.history.replaceState(null, "", `${window.location.pathname}?${new URLSearchParams(next)}`);
    window.requestAnimationFrame(() => inspectorRef.current?.scrollIntoView?.({ behavior: "auto", block: "nearest" }));
  };
  const closeInspection = (): void => {
    setInspection(null); window.history.replaceState(null, "", window.location.pathname);
  };

  const poolLimit = pool?.concurrency_limit;
  useEffect(() => { if (poolLimit != null) setLimit(poolLimit); }, [poolLimit]);

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
  // Deleting a card only hides it: Start all / Pause all still cover every
  // member, so those (and the empty state) go by allMembers.
  const allMembers = useMemo(() => pool?.members ?? [], [pool]);
  const members = useMemo(() => allMembers.filter(member => !member.hidden), [allMembers]);
  const hiddenMembers = useMemo(() => allMembers.filter(member => member.hidden), [allMembers]);
  // Restoring the last hidden card removes the Hidden chip it was chosen from.
  const view: Filter = filter === "hidden" && !hiddenMembers.length ? "all" : filter;
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
      if (view === "attention") return standing.needsYou;
      if (view === "live") return standing.tone === "live";
      if (view === "idle") return !standing.needsYou && standing.tone !== "live";
      return true;
    };
    return (view === "hidden" ? hiddenMembers : members.filter(matches)).sort((a, b) =>
      a.name.localeCompare(b.name));
  }, [members, hiddenMembers, view]);
  // Delete all takes only the cards that carry the delete icon.
  const bulkTargets = view === "hidden" ? shown : shown.filter(deletable);
  const bulk = () => void act(() => (view === "hidden" ? restoreRerollMembers : hideRerollMembers)(
    bulkTargets.map(member => member.name)));

  const accountFor = (member: RerollMember) => accounts.find(account => verifiedWorkerAccount(member, account));
  const inspected = inspection && allMembers.find(member => member.name === inspection.worker
    && (member.account_key ?? "") === inspection.account && (member.account_id ?? "") === inspection.identity);


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
    ...(hiddenMembers.length ? [{ id: "hidden" as const, label: "Hidden", count: hiddenMembers.length, tone: "text-muted-foreground" }] : []),
  ];

  return <div className="mx-auto flex max-w-7xl flex-col gap-4">
    <PageHeader
      title="Fleet Live"
      meta={run ? `${run.name} · started ${new Date(run.started_at).toLocaleDateString()} · ${census.total} ${census.total === 1 ? "emulator" : "emulators"}${hiddenMembers.length ? ` · ${hiddenMembers.length} hidden` : ""}` : "No active reroll"}
      action={<Link href="/fleet/history/" className="text-sm text-primary underline">Provisioning history</Link>}
    />
    {loading && <p role="status" className="text-sm text-muted-foreground">Loading fleet…</p>}
    {(error || poolError) && <p role="alert" className="rounded-lg border border-danger bg-danger-surface p-3 text-sm text-danger">{error || poolError}</p>}
    {pool?.operation && pool.operation.state !== "done" && <p role="status" aria-label="Reroll operation"
      className={cn("rounded-lg border p-3 text-sm", pool.operation.state === "failed" ? "border-danger bg-danger-surface text-danger" : "border-warn/40 bg-warn-surface text-warn")}>
      {pool.operation.state === "failed" ? `Last operation failed: ${pool.operation.error}${failureHint(pool.operation.error) ? ` - ${failureHint(pool.operation.error)}` : ""}` :
        pool.operation.kind === "new_run" ? "Starting a new reroll… stopping the bots on emulators not kept"
          : pool.operation.kind === "remove" ? `Removing ${pool.operation.target} from the reroll…`
          : `Adding ${pool.operation.target ?? "emulators"}… a stopped emulator boots first to check The Tower has never been opened`}
    </p>}

    <RerollCard title="Fleet controls" tone={census.attention ? "warn" : census.live ? "live" : undefined}>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <span><strong className="mr-1 text-lg text-foreground">{census.total}</strong> emulators</span>
        <span><strong className="mr-1 text-lg text-live">{census.live}</strong> running</span>
        <span><strong className="mr-1 text-lg text-warn">{census.attention}</strong> need attention</span>
        <span><strong className="mr-1 text-lg text-foreground">{census.idle + census.transit}</strong> idle or moving</span>
        {pressure && <span className="ml-auto flex items-center gap-2"><Pips states={slots} label={`${pressure.running} of ${pressure.limit} worker slots running`} />{pressure.available} worker slots free</span>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {run
          ? <><Button variant="outline" disabled={busy || operating} onClick={() => { setDialogError(null); setDialog(true); }}>New reroll</Button>
              <Button disabled={operating} onClick={() => setPicker(true)}>Add emulators</Button></>
          : <Button disabled={busy || operating} onClick={() => { setDialogError(null); setDialog(true); }}>Start a reroll</Button>}
        <Button variant="outline" disabled={busy || !allMembers.length || operating} onClick={() => void act(() => startReroll())}>Start all</Button>
        <Button variant="outline" disabled={busy || !allMembers.length || operating} onClick={() => void act(() => pauseReroll())}>Pause all</Button>
        <label className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          Concurrent workers
          <select aria-label="Concurrent workers" value={limit} onChange={event => setLimit(Number(event.target.value))} className="rounded-md border bg-background px-2 py-1 text-foreground">
            {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <Button variant="outline" disabled={busy || limit === (pool?.concurrency_limit ?? 2)} onClick={() => void act(() => setRerollConcurrency(limit))}>Save limit</Button>
      </div>
      {limit === 4 && <p role="status" className="rounded-lg border border-warn/40 bg-warn-surface p-2.5 text-sm text-warn">Four concurrent emulators can strain memory and slow other apps. Start with two, then increase the limit while watching macOS memory pressure.</p>}

      {!allMembers.length && <p className="text-sm text-muted-foreground">{run ? "No emulators in this reroll." : "No active reroll. Start one with emulators prepared with Tower installed and unopened."}</p>}

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


    {!!allMembers.length && <RerollCard
      title="Devices"
      action={<div className="flex flex-wrap items-center gap-1">{FILTERS.map(option => (
        <button
          key={option.id}
          type="button"
          aria-pressed={view === option.id}
          onClick={() => setFilter(option.id)}
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
            view === option.id
              ? "border-primary bg-primary/12 text-foreground"
              : "border-border text-muted-foreground hover:bg-muted",
          )}
        >
          {option.label} <span className={cn("font-mono", option.count ? option.tone : "text-faint-foreground")}>{option.count}</span>
        </button>
      ))}
        <Button size="xs" variant="outline" className="ml-1" disabled={busy || operating || !bulkTargets.length}
          onClick={() => view === "hidden" ? bulk() : setConfirmingBulk(true)}>
          {view === "hidden" ? "Restore all" : "Delete all"}
        </Button>
      </div>}
    >
      {shown.length ? <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">{shown.map(member => (
        <FleetLiveCard key={`${member.name}:${member.account_key}:${member.account_id}`} member={member} account={accountFor(member)} onInspect={() => inspect(member)}
          actions={<>
            <Button size="xs" variant="outline" disabled={busy || operating} onClick={() => void act(() => startReroll(member.name))}>Start</Button>
            <Button size="xs" variant="outline" disabled={busy || operating} onClick={() => void act(() => pauseReroll(member.name))}>Pause</Button>
            {deletable(member) && <Button size="xs" variant="ghost" disabled={busy || operating}
              aria-label={member.hidden ? `Restore ${member.name}` : `Delete ${member.name} from the list`}
              onClick={() => void act(() => (member.hidden ? restoreRerollMembers : hideRerollMembers)([member.name]))}>{member.hidden ? "Restore" : "Hide"}</Button>}
          </>} />
      ))}</div> : <p className="text-sm text-muted-foreground">No device matches this filter.</p>}
      <ConfirmDialog open={confirmingBulk} onOpenChange={setConfirmingBulk}
        title={`Delete ${bulkTargets.length} ${bulkTargets.length === 1 ? "device" : "devices"} from the list?`}
        footer={<><Button variant="outline" onClick={() => setConfirmingBulk(false)}>Cancel</Button>
          <Button onClick={() => { setConfirmingBulk(false); bulk(); }}>Delete {bulkTargets.length}</Button></>}>
        <p>Only cards that aren&apos;t Ready or Running are deleted, and only from this list. Their workers and emulators keep running, and Start all and Pause all still include them. Bring them back from the Hidden filter.</p>
      </ConfirmDialog>
    </RerollCard>}

    <div ref={inspectorRef}>
      {inspected && <AccountInspector key={`${inspected.name}:${inspected.account_key}:${inspected.account_id}:${inspected.lease_id}`} member={inspected} onClose={closeInspection}>
        <DeviceCard member={inspected} accountKey={inspected.account_key} accountId={inspected.account_id}
          collapsed={!!collapsed[inspected.name]} onToggle={() => setCollapsed(current => ({ ...current, [inspected.name]: !current[inspected.name] }))}
          onStart={() => void act(() => startReroll(inspected.name))} onPause={() => void act(() => pauseReroll(inspected.name))}
          onRemove={() => void act(() => removeRerollMember(inspected.name))}
          onHide={() => void act(() => (inspected.hidden ? restoreRerollMembers : hideRerollMembers)([inspected.name]))}
          onJournal={() => { window.location.href = "/fleet/reroll/history/"; }} busy={busy || operating} />
      </AccountInspector>}
      {inspection && !inspected && !loading && <p role="status" className="rounded-lg border border-warn/30 bg-warn-surface p-3 text-sm text-warn">This account attempt is no longer active. Select an emulator to inspect its current account. <button className="underline" onClick={closeInspection}>Dismiss</button></p>}
    </div>
    <NewRerollDialog open={dialog} onClose={() => setDialog(false)} run={run} members={pool?.members ?? []}
      candidates={pool?.candidates ?? []} busy={busy} error={dialogError} onConfirm={confirmNew} />
  </div>;
}
