"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";
import { X } from "lucide-react";
import { fetchAccountSnapshot } from "@/lib/api";
import type { AccountFact, AccountSnapshot } from "@/lib/account";
import type { RerollMember } from "@/lib/fleet";
import { SharedWorkshopLedger, WorkerBattlePurchases } from "./Purchases";
import { cn } from "@/lib/utils";

function Facts({ title, facts, empty }: { title: string; facts?: AccountFact[] | null; empty: string }): React.JSX.Element {
  return <section className="rounded-lg border bg-card p-4"><h3 className="text-sm font-semibold">{title}</h3>
    {facts?.length ? <dl className="mt-3 divide-y">{facts.map(fact => <div key={fact.concept_id} className="flex flex-wrap items-baseline justify-between gap-2 py-2 text-xs">
      <dt>{fact.evidence.raw_name || fact.concept_id.replaceAll("_", " ")}</dt>
      <dd className="text-right"><span className="font-mono">{fact.value == null ? "Unknown" : String(fact.value)}</span><span className="ml-2 text-faint-foreground">{fact.status} · {new Date(fact.evidence.observed_at * 1000).toLocaleString()}</span></dd>
    </div>)}</dl> : <p className="mt-2 text-xs text-muted-foreground">{empty}</p>}
  </section>;
}

function WorkshopObservations({ member }: { member: RerollMember }): React.JSX.Element {
  const [snapshot, setSnapshot] = useState<AccountSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const load = async (): Promise<void> => {
      if (!member.account_key || !member.account_id || inFlight) { if (active) setLoading(false); return; }
      inFlight = true;
      try {
        const next = await fetchAccountSnapshot(member.account_key);
        if (!active) return;
        if (next.revision && next.revision.account_id !== member.account_id) throw new Error("Account identity changed; these observations were not applied.");
        if (next.error || next.errors.account) throw new Error(next.error ?? next.errors.account!);
        setSnapshot(next); setError(null);
      } catch (failure) {
        if (active) { setError(failure instanceof Error ? failure.message : String(failure)); setSnapshot(null); }
      } finally { inFlight = false; if (active) setLoading(false); }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [member.account_key, member.account_id]);
  if (!member.account_key || !member.account_id) return <p className="text-sm text-muted-foreground">Verified account identity is required for workshop observations.</p>;
  if (loading) return <p role="status" className="text-sm text-muted-foreground">Loading workshop observations…</p>;
  if (error) return <p role="alert" className="text-sm text-danger">{error}</p>;
  return <div className="space-y-3">
    <p className="text-xs text-muted-foreground">{member.workshop_upgrades_bought == null ? "Recorded purchase count unknown." : `${member.workshop_upgrades_bought} recorded workshop buys.`} Permanent upgrades are separate from battle purchases.</p>
    <div className="grid gap-3 lg:grid-cols-2">
      <Facts title="Observed workshop values" facts={snapshot?.revision?.workshop_stats} empty="No workshop values observed for this account yet." />
      <Facts title="Exact workshop levels" facts={snapshot?.revision?.workshop_levels} empty="Exact levels have not been observed." />
      <Facts title="Observed unlocks" facts={snapshot?.revision?.unlocks} empty="No unlock evidence recorded yet." />
    </div>
  </div>;
}

export function AccountInspector({ member, children, onClose }: {
  member: RerollMember; children: React.ReactNode; onClose: () => void;
}): React.JSX.Element {
  const [tab, setTab] = useState("Overview");
  const id = useId();
  return <section aria-label={`Inspect ${member.name}`} className="scroll-mt-6 rounded-xl border border-primary/40 bg-well/40 p-4">
    <div className="flex items-start justify-between gap-3"><div><h2 className="font-heading text-lg font-semibold">{member.name} <span className="text-sm font-normal text-muted-foreground">· Account {member.account_id ?? "unknown"}</span></h2><p className="mt-1 text-xs text-muted-foreground">Account details · your fleet remains above</p></div><button onClick={onClose} aria-label="Close account details" className="rounded-md p-2 text-muted-foreground hover:bg-accent"><X className="size-4" aria-hidden="true" /></button></div>
    <div role="tablist" aria-label="Account detail views" className="my-4 flex gap-1 border-b pb-2">
      {["Overview", "Workshop", "Battle", "Activity"].map((name, index, all) => <button key={name} role="tab" id={`${id}-${name}`} aria-selected={tab === name} aria-controls={`${id}-panel`} tabIndex={tab === name ? 0 : -1}
        onClick={() => setTab(name)} onKeyDown={event => { if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); const next = all[(index + (event.key === "ArrowRight" ? 1 : all.length - 1)) % all.length]; setTab(next); document.getElementById(`${id}-${next}`)?.focus(); } }}
        className={cn("rounded-md px-3 py-1.5 text-xs", tab === name ? "bg-primary/12 font-semibold text-primary" : "text-muted-foreground hover:bg-accent")}>{name}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${tab}`}>
      {tab === "Overview" && children}
      {tab === "Workshop" && <WorkshopObservations key={`${member.account_key}:${member.account_id}`} member={member} />}
      {tab === "Battle" && <><p className="mb-3 text-xs text-muted-foreground">Confirmed run purchases. Live buying intent is not yet available for this worker.</p><WorkerBattlePurchases accountKey={member.account_key} expectedAccountId={member.account_id} /></>}
      {tab === "Activity" && <><SharedWorkshopLedger members={[member]} /><Link className="mt-3 inline-block text-xs text-primary hover:underline" href="/fleet/reroll/history/">Open fleet history →</Link></>}
    </div>
  </section>;
}
