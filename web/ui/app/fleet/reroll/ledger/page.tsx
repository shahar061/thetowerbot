"use client";

import { useEffect, useRef, useState } from "react";
import { Receipt, RefreshCw } from "lucide-react";
import { LedgerBalance, LedgerEntries, type LedgerTableEntry } from "@/components/LedgerEntries";
import { fetchAccountLedger, type LedgerQuery } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { balancedCurrencies, currencyOptions, FILTER_KINDS, groupLines, sortCurrencies } from "@/lib/ledger";
import type { LedgerPayload } from "@/lib/types";
import { deviceColor } from "@/lib/rerollState";
import { useRerollWorkspace } from "../RerollWorkspace";
import { memberIdentity } from "../statsHelpers";

type Source = { data: LedgerPayload | null; error: string | null; busy: boolean };
type Batch = { key: string; sources: Record<string, Source> };

export default function FleetLedgerPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const [emulator, setEmulator] = useState("all");
  const [kind, setKind] = useState<string | null>(null);
  const [currency, setCurrency] = useState("all");
  const [rehearsals, setRehearsals] = useState(false);
  const [revision, setRevision] = useState(0);
  const [batch, setBatch] = useState<Batch>({ key: "", sources: {} });
  const epoch = useRef(0);
  const pending = useRef(new Set<string>());
  const members = [...(pool?.members ?? [])].filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  const selected = members.filter(member => emulator === "all" || member.name === emulator);
  const identity = JSON.stringify(selected.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const query = JSON.stringify({ kind: kind ?? undefined, currency: currency === "all" ? undefined : currency, includeRehearsals: rehearsals });
  const batchKey = JSON.stringify([identity, query, revision]);
  const sources = batch.key === batchKey ? batch.sources : {};

  useEffect(() => {
    const generation = ++epoch.current;
    const scoped = JSON.parse(identity) as RerollMember[];
    const options = JSON.parse(query) as LedgerQuery;
    scoped.forEach(member => {
      const key = memberIdentity(member);
      const load = async (): Promise<void> => {
        let source: Source;
        try {
          if (!member.account_key || !member.account_id) throw new Error("Waiting for a verified account.");
          const data = await fetchAccountLedger(member.account_key, member.account_id, options);
          if (data.account_id !== member.account_id) throw new Error("Account changed or ledger is unavailable. Waiting for matching history.");
          source = { data, error: null, busy: false };
        } catch (failure) { source = { data: null, error: failure instanceof Error ? failure.message : "Ledger unavailable", busy: false }; }
        if (epoch.current !== generation) return;
        setBatch(current => ({ key: batchKey, sources: { ...(current.key === batchKey ? current.sources : {}), [key]: source } }));
      };
      void load();
    });
    return () => { epoch.current = generation + 1; };
  }, [identity, query, batchKey]);

  async function loadOlder(): Promise<void> {
    const generation = epoch.current;
    await Promise.all(selected.map(async member => {
      const key = memberIdentity(member);
      const source = sources[key];
      const lock = `${generation}:${key}`;
      if (!source?.data || source.data.next === null || pending.current.has(lock) || !member.account_key || !member.account_id) return;
      pending.current.add(lock);
      setBatch(current => ({ ...current, sources: { ...current.sources, [key]: { ...source, busy: true, error: null } } }));
      try {
        const next = await fetchAccountLedger(member.account_key, member.account_id, { ...JSON.parse(query), before: source.data.next });
        if (next.account_id !== member.account_id) throw new Error("Account changed; refresh the ledger.");
        if (epoch.current !== generation) return;
        const lines = [...new Map([...source.data.lines, ...next.lines].map(line => [line.id, line])).values()].sort((a, b) => b.id - a.id);
        setBatch(current => ({ ...current, sources: { ...current.sources, [key]: { data: { ...next, lines }, error: null, busy: false } } }));
      } catch (failure) {
        if (epoch.current === generation) setBatch(current => ({ ...current, sources: { ...current.sources, [key]: { ...source, error: failure instanceof Error ? failure.message : "Could not load older entries", busy: false } } }));
      } finally { pending.current.delete(lock); }
    }));
  }

  const entries: LedgerTableEntry[] = selected.flatMap(member => {
    const data = sources[memberIdentity(member)]?.data;
    if (!data || !member.account_id) return [];
    return groupLines(data.lines).map(entry => ({ ...entry, key: `${memberIdentity(member)}:${entry.key}`,
      balanced: balancedCurrencies(data), source: { name: member.name, accountId: member.account_id!, color: deviceColor(member.name) } }));
  }).sort((a, b) => b.head.ts - a.head.ts || a.source!.name.localeCompare(b.source!.name) || b.head.id - a.head.id);
  const currencies = sortCurrencies([...(currency === "all" ? [] : [currency]), ...Object.values(sources).flatMap(source => source.data ? currencyOptions(source.data, source.data.lines) : [])]);
  const fetching = selected.some(member => !sources[memberIdentity(member)] || sources[memberIdentity(member)].busy);
  const older = Object.values(sources).some(source => source.data?.next != null);
  const hasErrors = Object.values(sources).some(source => source.error);
  return <main className="space-y-6">
    <header className="flex flex-wrap items-end justify-between gap-4"><div><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / ledger</p>
      <h1 className="text-3xl font-semibold tracking-tight">Follow every transaction.</h1><p className="mt-2 text-sm text-muted-foreground">Purchases, rewards, and balance changes across the fleet, with every entry tied to its account.</p></div>
      <button type="button" className="flex items-center gap-2 rounded-lg border px-4 py-2 text-sm hover:bg-accent" onClick={() => setRevision(value => value + 1)}><RefreshCw className="size-4" /> Refresh latest</button></header>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    <section aria-label="Ledger filters" className="flex flex-wrap items-end gap-4 rounded-xl border bg-card p-4">
      <label className="space-y-1 text-xs text-muted-foreground"><span className="block">Emulator</span><select aria-label="Emulator" className="rounded-md border bg-background px-3 py-2 text-sm text-foreground" value={emulator} onChange={event => setEmulator(event.target.value)}><option value="all">All emulators</option>{members.map(member => <option key={member.name} value={member.name}>{member.name}</option>)}</select></label>
      <label className="space-y-1 text-xs text-muted-foreground"><span className="block">Transaction type</span><select aria-label="Transaction type" className="rounded-md border bg-background px-3 py-2 text-sm text-foreground" value={kind ?? "all"} onChange={event => setKind(event.target.value === "all" ? null : event.target.value)}><option value="all">All transactions</option>{FILTER_KINDS.map(value => <option key={value} value={value}>{value.toLowerCase().replaceAll("_", " ")}</option>)}</select></label>
      <label className="space-y-1 text-xs text-muted-foreground"><span className="block">Currency</span><select aria-label="Currency" className="rounded-md border bg-background px-3 py-2 text-sm text-foreground" value={currency} onChange={event => setCurrency(event.target.value)}><option value="all">All currencies</option>{currencies.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label className="flex items-center gap-2 py-2 text-sm"><input type="checkbox" checked={rehearsals} onChange={event => setRehearsals(event.target.checked)} /> Include rehearsals</label>
    </section>
    {loading && !pool ? <p role="status">Loading fleet…</p> : !selected.length ? <p className="rounded-xl border border-dashed p-8 text-center text-muted-foreground">No visible emulators match this selection.</p> : <>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{selected.map(member => {
        const source = sources[memberIdentity(member)];
        return <article key={memberIdentity(member)} aria-label={`Balances for ${member.name}`} className="rounded-xl border bg-card p-4" style={{ borderTop: `2px solid ${deviceColor(member.name)}` }}><h2 className="font-medium" style={{ color: deviceColor(member.name) }}>{member.name}</h2><p className="mt-1 font-mono text-[10px] text-muted-foreground">Account {member.account_id ?? "not verified"}</p>
          {!source ? <p className="mt-3 text-xs text-muted-foreground">Loading ledger…</p> : <>{source.error && <p role="alert" className="mt-3 text-xs text-warn">{source.error}</p>}{source.data && <div className="mt-3 flex flex-wrap gap-4 font-mono text-sm">{currencyOptions(source.data, source.data.lines).map(value => <span key={value} className="flex flex-col gap-1"><span className="text-[10px] uppercase text-muted-foreground">{value}</span><LedgerBalance currency={value} value={source.data!.balances[value] ?? null} balanced={balancedCurrencies(source.data!)} /></span>)}</div>}</>}
        </article>;
      })}</div>
      <section aria-label="Fleet transactions" className="space-y-4 rounded-xl border bg-card p-5"><header className="flex flex-wrap items-center justify-between gap-2"><h2 className="flex items-center gap-2 font-semibold"><Receipt className="size-4 text-primary" /> Transaction history</h2><p className="text-xs text-muted-foreground">{entries.length} loaded entries · balances belong to individual accounts</p></header>
        {fetching && <p role="status" className="text-sm text-muted-foreground">Reading account ledgers…</p>}
        {entries.length ? <LedgerEntries entries={entries} kind={kind} onKindChange={setKind} withSources /> : !fetching && <p className="py-4 text-sm text-muted-foreground">{hasErrors ? "Some account ledgers are unavailable. Refresh to retry." : "No transactions match these filters."}</p>}
        {older && <button type="button" disabled={Object.values(sources).some(source => source.busy)} onClick={() => { void loadOlder(); }} className="rounded-lg border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50">Load older entries</button>}
        <p className="border-t pt-3 text-xs text-muted-foreground">Snapshot of recorded history. Refresh latest reloads the newest entries; use Load older entries to explore further back.</p>
      </section>
    </>}
  </main>;
}
