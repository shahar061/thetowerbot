"use client";

import { useEffect, useState } from "react";
import { fetchAccountWorkshopSummary } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { deviceColor } from "@/lib/rerollState";
import type { WorkshopPurchaseSummary } from "@/lib/types";
import { memberIdentity } from "../statsHelpers";

const CATEGORIES = ["ATTACK", "DEFENSE", "UTILITY"] as const;
type Category = typeof CATEGORIES[number];
type Result = { data: WorkshopPurchaseSummary | null; error: string | null };

function countFor(data: WorkshopPurchaseSummary | null | undefined, category: string, item?: string): number {
  return data?.items.reduce((sum, row) => sum + (row.category.toUpperCase() === category && (item === undefined || row.item === item) ? row.count : 0), 0) ?? 0;
}

function Bars({ members, results, category, item, max }: {
  members: RerollMember[]; results: Record<string, Result>; category: string; item?: string; max: number;
}): React.JSX.Element {
  return <div className="space-y-1.5">{members.map(member => {
    const result = results[memberIdentity(member)];
    const count = countFor(result?.data, category, item);
    return <div key={memberIdentity(member)} className="grid grid-cols-[minmax(5rem,7rem)_minmax(0,1fr)_2rem] items-center gap-2 text-xs">
      <span className="truncate" title={member.name}>{member.name}</span>
      <div className="h-2.5 rounded-full bg-muted"><div className="h-full rounded-full transition-[width]" style={{ backgroundColor: deviceColor(member.name), width: `${max ? count / max * 100 : 0}%` }} /></div>
      <span className="text-right font-mono tabular-nums">{result ? result.error ? "—" : count : "…"}</span>
    </div>;
  })}</div>;
}

export function WorkshopComparison({ members }: { members: RerollMember[] }): React.JSX.Element {
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const [results, setResults] = useState<Record<string, Result>>({});
  const [selected, setSelected] = useState<Category | null>(null);

  useEffect(() => {
    let active = true;
    const scoped = JSON.parse(identity) as RerollMember[];
    const load = async (): Promise<void> => {
      await Promise.all(scoped.map(async member => {
        let result: Result;
        if (!member.account_key || !member.account_id) result = { data: null, error: "Waiting for a verified account" };
        else {
          try {
            const data = await fetchAccountWorkshopSummary(member.account_key, member.account_id);
            if (data.account_id !== member.account_id) throw new Error("Account changed; waiting for matching purchase history");
            result = { data, error: null };
          } catch (failure) { result = { data: null, error: failure instanceof Error ? failure.message : "Purchase history unavailable" }; }
        }
        if (active) setResults(current => ({ ...current, [memberIdentity(member)]: result }));
      }));
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [identity]);

  const categoryMax = Math.max(0, ...CATEGORIES.flatMap(category => members.map(member => countFor(results[memberIdentity(member)]?.data, category))));
  const items = selected ? [...new Set(members.flatMap(member => results[memberIdentity(member)]?.data?.items
    .filter(row => row.category.toUpperCase() === selected).map(row => row.item) ?? []))].sort((a, b) => a.localeCompare(b)) : [];
  const itemMax = selected ? Math.max(0, ...items.flatMap(item => members.map(member => countFor(results[memberIdentity(member)]?.data, selected, item)))) : 0;
  const pending = members.some(member => !results[memberIdentity(member)]);
  const available = members.some(member => results[memberIdentity(member)]?.data);
  const partial = members.some(member => results[memberIdentity(member)]?.error);

  return <section aria-label="Workshop upgrade comparison" className="overflow-hidden rounded-xl border border-border bg-card p-5">
    <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">Workshop upgrade comparison</h2>
        <p className="mt-1 text-xs text-muted-foreground">Confirmed buys across each account’s full ledger · tap a category for item details</p></div>
      <div className="flex flex-wrap gap-2">{members.map(member => <span key={memberIdentity(member)} className="inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-xs"><i className="size-2.5 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} />{member.name}</span>)}</div>
    </header>
    <div className="grid gap-3 md:grid-cols-3">{CATEGORIES.map(category => {
      const total = members.reduce((sum, member) => sum + countFor(results[memberIdentity(member)]?.data, category), 0);
      return <button key={category} type="button" aria-label={`${category[0]}${category.slice(1).toLowerCase()} upgrades`} aria-pressed={selected === category} onClick={() => setSelected(current => current === category ? null : category)}
        className={`rounded-lg border p-4 text-left transition-colors hover:bg-accent/50 focus-visible:outline-2 focus-visible:outline-primary ${selected === category ? "border-primary bg-primary/5" : "border-border"}`}>
        <span className="block font-heading text-sm font-semibold capitalize">{category.toLowerCase()}</span>
        <span className="mb-4 block font-mono text-xs text-muted-foreground">{pending ? "Loading purchase history…" : !available ? "Purchase history unavailable" : `${total} confirmed buys${partial ? " · partial" : ""}`}</span>
        <Bars members={members} results={results} category={category} max={categoryMax} />
      </button>;
    })}</div>
    {selected && <div className="mt-5 border-t pt-5"><h3 className="mb-1 font-semibold capitalize">{selected.toLowerCase()} upgrades</h3>
      <p className="mb-4 text-xs text-muted-foreground">Each bar counts confirmed purchases of that item by one emulator.</p>
      {items.length ? <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{items.map(item => <div key={item} className="rounded-lg border border-border bg-muted/20 p-3">
        <h4 className="mb-3 text-sm font-medium">{item}</h4><Bars members={members} results={results} category={selected} item={item} max={itemMax} />
      </div>)}</div> : <p className="text-sm text-muted-foreground">No confirmed purchases in this category yet.</p>}
    </div>}
    {members.map(member => results[memberIdentity(member)]?.error && <p key={memberIdentity(member)} role="status" className="mt-3 text-xs text-danger">{member.name}: {results[memberIdentity(member)].error}</p>)}
  </section>;
}
