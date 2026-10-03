"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AssignmentStatus } from "./AssignmentStatus";
import { ApiError, fetchFleetStrategies } from "@/lib/api";
import type { StrategyDefinition } from "@/lib/strategyStudio";
import { CardsWorkspace } from "@/components/cards/CardsWorkspace";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/PageHeader";
import { assignFleetCardProgram, fetchFleetCards, fetchCardCatalog, refreshFleetCards, setFleetCardsAutomation,
  selectFleetCardLoadout, fleetCardAccountRequest, fleetCardAuthority,
  type FleetCardsRow, type FleetCardsSnapshot, type FleetCardResult, type CardCatalog, type FleetAccountAction,
  type CardOperation, type CardPreconditions, type CardCommandIntent } from "@/lib/cards";
const catalogContext = { scope: null, accountId: null, worker: null };
const message = (error: unknown): string => error instanceof Error ? error.message : String(error);
const uncertain = (error: unknown): boolean => !(error instanceof ApiError) || error.status >= 500;

export default function FleetCardsPage(): React.JSX.Element {
  const [fleet, setFleet] = useState<FleetCardsSnapshot | null>(null);
  const [catalog, setCatalog] = useState<CardCatalog>({ cards: [], max_gem_slots: 21 });
  const [programs, setPrograms] = useState<StrategyDefinition[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [connection, setConnection] = useState("all");
  const [detail, setDetail] = useState<string | null>(null);
  const [programKey, setProgramKey] = useState("");
  const [loadout, setLoadout] = useState("");
  const [results, setResults] = useState<FleetCardResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(false);
  const readId = useRef(0);
  const alive = useRef(true);
  const flight = useRef(false);
  const intent = useRef<null | { key: string; retryAccounts?: string[]; run: (signal: AbortSignal) => Promise<FleetCardResult[]> }>(null);
  const controller = useRef<AbortController | null>(null);
  const rows = fleet?.accounts ?? [];
  const targets = rows.filter(row => selected.includes(row.account_id));
  const actionKey = JSON.stringify([selected, targets.map(fleetCardAuthority)]);
  const currentKey = useRef(actionKey);
  const load = useCallback(async (): Promise<void> => {
    const id = ++readId.current;
    try {
      const next = await fetchFleetCards();
      if (alive.current && id === readId.current) { setFleet(next); setError(null); }
    } catch (e) { if (alive.current && id === readId.current) setError(message(e)); }
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    void fetchCardCatalog(catalogContext).then(value => { if (alive.current) setCatalog(value); }).catch(e => { if (alive.current) setError(message(e)); });
    void fetchFleetStrategies().then(library => { if (alive.current) setPrograms([...library.templates, ...library.strategies].filter(item => item.baseline.cards)); }).catch(e => { if (alive.current) setError(message(e)); });
    const timer = setInterval(() => void load(), 5000);
    return () => { alive.current = false; clearInterval(timer); controller.current?.abort(); };
  }, [load]);
  useEffect(() => {
    currentKey.current = actionKey; controller.current?.abort(); intent.current = null; flight.current = false;
    setBusy(false); setRetry(false);
  }, [actionKey]);
  async function execute(attempt: NonNullable<typeof intent.current>): Promise<void> {
    if (flight.current || attempt.key !== currentKey.current) return;
    flight.current = true; setBusy(true); setRetry(false); setError(null);
    const abort = new AbortController(); controller.current = abort;
    try {
      const next = await attempt.run(abort.signal);
      if (!alive.current || abort.signal.aborted || attempt.key !== currentKey.current) return;
      setResults(next);
      const retryable = next.some(result => result.status === "unavailable" && attempt.retryAccounts?.includes(result.account_id));
      setRetry(retryable); if (!retryable) intent.current = null;
      await load();
    } catch (e) {
      if (!alive.current || abort.signal.aborted || attempt.key !== currentKey.current) return;
      setError(message(e)); setRetry(uncertain(e)); if (!uncertain(e)) intent.current = null;
    } finally {
      if (alive.current && !abort.signal.aborted && attempt.key === currentKey.current) { flight.current = false; setBusy(false); }
    }
  }
  function start(run: NonNullable<typeof intent.current>["run"], retryAccounts?: string[]): void {
    if (flight.current || intent.current || !targets.length) return;
    const attempt = { key: actionKey, run, retryAccounts }; intent.current = attempt; void execute(attempt);
  }
  function liveAction(enabled?: boolean): void {
    const unavailable = targets.filter(row => !row.worker || !row.cards.preconditions || row.cards.read_only_reason)
      .map(row => ({ account_id: row.account_id, worker: row.worker, status: "unavailable" as const, reason: row.cards.read_only_reason ?? "Current worker identity unavailable" }));
    const live = targets.filter(row => row.worker && row.cards.preconditions && !row.cards.read_only_reason);
    const refresh = live.map(row => ({ ...row.cards.preconditions!, worker: row.worker!, kind: "refresh" as const, idempotency_key: crypto.randomUUID() }));
    const automation = live.map(row => ({ ...row.cards.preconditions!, worker: row.worker!, enabled: !!enabled }));
    start(async signal => [...unavailable, ...(live.length ? (enabled === undefined ? await refreshFleetCards(refresh, signal) : await setFleetCardsAutomation(automation, signal)).results : [])], live.map(row => row.account_id));
  }
  function assign(): void {
    const program = programs.find(item => `${item.id}@${item.version}` === programKey);
    if (!program || !fleet) return;
    const body = { expected_revision: fleet.revision, strategy_id: program.id, strategy_version: program.version,
      targets: targets.map(row => ({ account_id: row.account_id, ...(row.worker ? { worker: row.worker } : {}) })) };
    start(async signal => (await assignFleetCardProgram(body, signal)).results);
  }
  function selectLoadouts(): void {
    if (!fleet) return;
    // Publications share a route CAS: sequential requests chain the returned revision.
    const originalRevision = fleet.revision;
    start(async signal => {
      let revision = originalRevision;
      const output: FleetCardResult[] = [];
      for (const row of targets) {
        if (signal.aborted) break;
        try {
          const response = await selectFleetCardLoadout({ expected_revision: revision, account_id: row.account_id, loadout_id: loadout || null }, signal);
          revision = response.revision;
          output.push({ account_id: row.account_id, worker: row.worker, status: response.assignment.status === "active" ? "accepted" : "pending", assignment: response.assignment });
        } catch (e) { output.push({ account_id: row.account_id, worker: row.worker, status: uncertain(e) ? "unavailable" : "conflict", reason: message(e) }); }
      }
      return output;
    });
  }
  const picked = rows.find(row => row.account_id === detail);
  const disabled = busy || retry || !targets.length;
  const visible = rows.filter(row => `${row.account_id} ${row.worker ?? ""}`.toLowerCase().includes(filter.toLowerCase()) && (connection === "all" || (connection === "online") === !!row.cards.preconditions));
  const loadouts = [...new Map(targets.flatMap(row => (row.assignment?.program.loadouts ?? []).map(item => [item.id, item] as const))).values()];
  return <div className="mx-auto flex max-w-7xl flex-col gap-4 p-4">
    <PageHeader title="Fleet Cards" meta="Account-owned plans, collection evidence and independent budgets" />
    <div className="flex flex-wrap gap-3"><Button variant="outline" onClick={() => void load()}>Reload fleet</Button><Link href="/fleet/reroll/strategies/">Author Cards in Strategy Library</Link></div>
    {error && <p role="alert">{error}</p>}
    <div className="flex flex-wrap gap-3"><label>Filter accounts<input value={filter} onChange={e => setFilter(e.target.value)} className="block rounded border p-2" /></label>
      <label>Connection<select value={connection} onChange={e => setConnection(e.target.value)} className="block rounded border p-2"><option value="all">All accounts</option><option value="online">Online</option><option value="offline">Offline / unverified</option></select></label></div>
    <p className="text-xs text-muted-foreground md:hidden">Scroll horizontally to compare all columns.</p>
    <div className="overflow-x-auto"><table className="w-full min-w-[960px] text-left text-sm"><thead><tr>{["Select", "Account / worker", "Connection / freshness", "Equipment / loadout", "Goals", "Budget", "Next action", "Assignment"].map(label => <th key={label} className="p-2">{label}</th>)}</tr></thead>
      <tbody>{visible.map(row => { const c = row.cards; return <tr key={row.account_id} className="border-t align-top">
        <td className="p-2"><input aria-label={`Select ${row.account_id}`} type="checkbox" checked={selected.includes(row.account_id)} onChange={e => setSelected(previous => e.target.checked ? [...previous, row.account_id] : previous.filter(id => id !== row.account_id))} /></td>
        <td className="p-2"><button className="underline" aria-label={`Inspect ${row.account_id}`} onClick={() => setDetail(row.account_id)}>{row.account_id}</button><p>{row.worker ?? "No worker"}</p></td>
        <td className="p-2">{c.preconditions ? "Online" : "Offline / unverified"}<p>{c.fresh ? "Fresh observations" : "Historical / unknown"}</p></td>
        <td className="p-2">{c.snapshot?.equipped?.length ?? "?"} / {c.snapshot?.capacity ?? "?"}<p>{c.program?.loadouts.find(item => item.id === c.program?.selected_loadout_id)?.name ?? "No active loadout"}</p></td>
        <td className="p-2">{c.preview ? `${c.preview.goals.filter(goal => goal.met === true).length} / ${c.preview.goals.length} met` : "Unknown"}</td>
        <td className="p-2">{c.active_budget ? `${c.active_budget.spent} spent + ${c.active_budget.pending} pending / ${c.active_budget.cap} cycle cap` : "No budget cycle"}<p>Plan cap: {c.program?.gem_cap ?? row.assignment?.program.gem_cap ?? "?"}</p></td>
        <td className="p-2">{c.decision.kind}<p>{c.read_only_reason ?? c.decision.reason}</p></td>
        <td className="p-2">{row.assignment ? <><AssignmentStatus published={row.assignment.published_revision} applied={row.assignment.applied_revision} /><p>{row.assignment.strategy_id} v{row.assignment.strategy_version}</p></> : "No Cards assignment"}</td>
      </tr>; })}</tbody></table></div>
    {!fleet && <p>Loading Cards…</p>}
    <section aria-label="Selected account actions" className="space-y-3 rounded border p-4">
      <p>Selected accounts: {targets.map(row => `${row.account_id}${row.worker ? ` (${row.worker})` : ""}`).join(", ") || "None"}</p>
      <p className="text-sm">Assignments and loadout selection publish configuration. They do not enable automation or start a budget cycle.</p>
      <div className="flex flex-wrap items-end gap-3"><label>Saved Cards program<select value={programKey} onChange={e => setProgramKey(e.target.value)} className="block rounded border p-2"><option value="">Choose an immutable revision</option>{programs.map(item => <option key={`${item.id}@${item.version}`} value={`${item.id}@${item.version}`}>{item.name} v{item.version}</option>)}</select></label><Button disabled={disabled || !programKey} onClick={assign}>Assign program revision</Button>
        <label>Selected loadout<select value={loadout} onChange={e => setLoadout(e.target.value)} className="block rounded border p-2"><option value="">No automatic loadout</option>{loadouts.map(item => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}</select></label><Button disabled={disabled} onClick={selectLoadouts}>Publish loadout selection</Button></div>
      <div className="flex flex-wrap gap-3"><Button disabled={disabled} onClick={() => liveAction()}>Refresh selected accounts</Button><Button disabled={disabled} onClick={() => liveAction(true)}>Enable Cards automation</Button><Button disabled={disabled} onClick={() => liveAction(false)}>Disable Cards automation</Button></div>
      <p className="text-xs">Parent shopping, armed state, reserve and each account’s own cycle still gate spending. Manual purchases are available only in one account’s detail.</p>
      {retry && <div className="flex gap-2"><Button disabled={busy} onClick={() => { if (intent.current) void execute(intent.current); }}>Retry original selected request</Button><Button disabled={busy} variant="outline" onClick={() => { intent.current = null; setRetry(false); }}>Dismiss selected retry</Button></div>}
      <ul aria-label="Per-account results">{results.map((result, index) => <li key={`${result.account_id}-${index}`}>{result.account_id}: {result.status}{result.reason ? ` · ${typeof result.reason === "string" ? result.reason : JSON.stringify(result.reason)}` : ""}</li>)}</ul>
    </section>
    {picked && <FleetCardDetail key={`${picked.account_id}:${picked.worker}`} row={picked} catalog={catalog} onUpdated={load} />}
  </div>;
}

type DetailAction = FleetAccountAction | { action: "automation"; body: CardPreconditions & { worker: string; enabled: boolean } };

function FleetCardDetail({ row, catalog, onUpdated }: { row: FleetCardsRow; catalog: CardCatalog; onUpdated: () => Promise<void> }): React.JSX.Element {
  const [saved, setSaved] = useState(row);
  const [pending, setPending] = useState(false);
  const [retry, setRetry] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [operation, setOperation] = useState<CardOperation | null>(null);
  const request = useRef<DetailAction | null>(null);
  const busy = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const authority = fleetCardAuthority(row);
  const current = useRef(authority);
  const drift = fleetCardAuthority(saved) !== authority;
  const reason = drift ? "Saved authority changed. Reload latest plan before acting." : null;
  useEffect(() => { current.current = authority; controller.current?.abort(); request.current = null; busy.current = false; setPending(false); setRetry(false); setOperation(null); return () => { controller.current?.abort(); }; }, [authority]);
  async function execute(action: DetailAction): Promise<void> {
    if (busy.current || drift || !row.worker || !row.cards.preconditions) return;
    const identity = authority;
    busy.current = true; setPending(true); setRetry(false); setError(null);
    const abort = new AbortController(); controller.current = abort;
    try {
      const result = action.action === "automation" ? (await setFleetCardsAutomation([action.body], abort.signal)).results[0] : await fleetCardAccountRequest<CardOperation>(action, abort.signal);
      if (abort.signal.aborted || current.current !== identity) return;
      if (!result || result.account_id !== row.account_id || result.worker !== row.worker) throw new ApiError(409, "Worker response identity changed");
      if (result.status !== "accepted") throw new ApiError(result.status === "conflict" ? 409 : 503, typeof result.reason === "string" ? result.reason : JSON.stringify(result.reason));
      if (action.action === "operation") setOperation((result.result as CardOperation) ?? null);
      else await onUpdated();
      request.current = null;
    } catch (e) {
      if (abort.signal.aborted || current.current !== identity) return;
      setError(message(e)); if (action.action !== "operation" && uncertain(e)) setRetry(true); else request.current = null;
    } finally { if (!abort.signal.aborted && current.current === identity) { busy.current = false; setPending(false); } }
  }
  function start(action: DetailAction): void { if (request.current || busy.current || drift) return; request.current = structuredClone(action); void execute(request.current); }
  function command(intent: CardCommandIntent): void {
    if (!saved.cards.preconditions || !row.worker || drift) return;
    if ((intent.kind === "buy" || intent.kind === "slot") && !row.cards.active_budget) return;
    start({ action: "command", body: { ...saved.cards.preconditions, ...intent, worker: row.worker, idempotency_key: crypto.randomUUID(), ...((intent.kind === "buy" || intent.kind === "slot") ? { budget_cycle_id: row.cards.active_budget!.cycle_id } : {}) } });
  }
  return <section aria-label={`Cards detail ${row.account_id}`} className="space-y-4">
    <div className="flex flex-wrap gap-3">{[true, false].map(enabled => <Button key={String(enabled)} variant="outline" disabled={pending || retry || drift || !row.worker || !row.cards.preconditions || !!row.cards.read_only_reason} onClick={() => {
      if (row.worker && saved.cards.preconditions) start({ action: "automation", body: { ...saved.cards.preconditions, worker: row.worker, enabled } });
    }}>{enabled ? "Enable" : "Disable"} Cards for {row.account_id}</Button>)}</div>
    <CardsWorkspace context={{ scope: row.worker ? `worker:${row.worker}` : null, accountId: row.account_id, worker: row.worker }}
    data={drift ? { ...row.cards, read_only_reason: reason } : row.cards} catalog={catalog} saved={saved.cards.program ?? saved.assignment?.program ?? null} draft={saved.cards.program ?? saved.assignment?.program ?? null}
    policy={null} pending={pending} preview={drift ? null : row.cards.preview} error={error ?? reason} retryAvailable={retry}
    editDisabledReason="Author this program in Strategy Library, save a revision, then assign it above." applyDisabledReason={reason}
    reloadLabel="Reload latest plan" onRevert={() => { setSaved(row); setError(null); }} onChange={() => {}} onPolicyChange={() => {}} onSave={() => {}}
    onCommand={command} onStartCycle={cap => { if (saved.cards.preconditions && row.worker) start({ action: "cycle", body: { ...saved.cards.preconditions, worker: row.worker, cycle_id: crypto.randomUUID(), cap } }); }}
    inspectedOperation={operation} onInspectEvidence={id => { if (row.cards.preconditions && row.worker) start({ action: "operation", body: { ...row.cards.preconditions, worker: row.worker, operation_id: id } }); }}
    onRetry={() => { if (request.current) void execute(request.current); }} onDiscardRetry={() => { request.current = null; setRetry(false); }} /></section>;
}
