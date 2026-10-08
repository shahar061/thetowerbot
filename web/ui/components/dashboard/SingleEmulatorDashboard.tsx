"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowUpRight, RefreshCw, Radio, Monitor } from "lucide-react";
import { AccountColumn } from "@/app/fleet/state/AccountColumn";
import { EmulatorRecovery } from "@/components/EmulatorRecovery";
import { LedgerEntries } from "@/components/LedgerEntries";
import { RemoteDeviceView } from "@/components/RemoteDeviceView";
import { SectionCard } from "@/components/ui/section-card";
import { fetchSingleAccountState, fetchLedger, type AccountChoice } from "@/lib/api";
import { useAccountSelection } from "@/lib/AccountSelection";
import { groupLines, balancedCurrencies } from "@/lib/ledger";
import { usePageVisible } from "@/lib/usePageVisible";
import type { LedgerPayload } from "@/lib/types";
import type { SingleAccountState } from "@/lib/singleAccount";
import { LabSavings } from "./LabSavings";
import { LiveMonitor } from "./LiveMonitor";

const stamp = (value: string): string => new Date(value).toLocaleString(undefined, {
  month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
});

export function SingleEmulatorDashboard(): React.JSX.Element | null {
  const { selected } = useAccountSelection();
  return selected ? <AccountDashboard key={`${selected.key}:${selected.account_id ?? ""}`} selected={selected} /> : null;
}

function AccountDashboard({ selected }: { selected: AccountChoice }): React.JSX.Element {
  const [data, setData] = useState<SingleAccountState | null>(null);
  const [ledger, setLedger] = useState<LedgerPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sections, setSections] = useState<Record<string, boolean>>({});
  const [kind, setKind] = useState<string | null>(null);
  const [monitor, setMonitor] = useState(false);
  const visible = usePageVisible();
  const expectedId = selected.account_id;
  const remote = Boolean(selected.dashboard_url && typeof window !== "undefined"
    && new URL(selected.dashboard_url).origin !== window.location.origin);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    let pending = false;
    const poll = async (): Promise<void> => {
      if (pending) return;
      pending = true;
      setLoading(true);
      // Independent sections: a ledger failure must not blank the account.
      await Promise.allSettled([
        fetchSingleAccountState().then(value => {
          if (!active) return;
          if (value.account_id !== expectedId) throw new Error("Account changed; refresh the account selection before continuing.");
          setData(value); setError(null);
        }).catch((failure: Error) => { if (active) setError(failure.message); }),
        fetchLedger().then(value => {
          if (!active) return;
          if (expectedId && value.account_id !== expectedId) throw new Error("Ledger account changed; refresh the account selection.");
          setLedger(value); setLedgerError(null);
        }).catch((failure: Error) => { if (active) setLedgerError(failure.message); }),
      ]);
      pending = false;
      if (active) setLoading(false);
    };
    void poll();
    const interval = window.setInterval(() => void poll(), selected.running ? 5000 : 15000);
    return () => { active = false; window.clearInterval(interval); };
  }, [expectedId, selected.running, visible, refresh]);

  const account = data?.account;
  const allEntries = groupLines(ledger?.lines ?? []);
  const entries = allEntries.filter(entry => kind === null || entry.head.kind === kind).slice(0, 20);
  const kinds = [...new Set(allEntries.map(entry => entry.head.kind))];
  const live = account?.online && account.bot.live;

  return <div className="single-dashboard mx-auto flex w-full max-w-[1800px] min-w-0 flex-col gap-5">
    <header className="sd-hero">
      <div className="min-w-0">
        <p className="sd-eyebrow"><Radio size={13} aria-hidden="true" /> SINGLE EMULATOR</p>
        <h1>Account overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">Your tower, from the next upgrade to the next lab.</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full border px-3 py-1.5 text-xs ${live ? "border-live/30 bg-live-surface text-live" : "text-muted-foreground"}`}>
          {live ? "Live account" : "Saved account"}
        </span>
        <button type="button" aria-label="Refresh overview" disabled={loading}
          onClick={() => setRefresh(n => n + 1)} className="sd-button">
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </header>

    <nav aria-label="Overview sections" className="sd-nav">
      <a href="#account-state">Account state</a><a href="#lab-jar">Lab jar</a>
      <a href="#recent-ledger">Ledger</a><a href="#live-monitor">Live monitor</a>
      <Link href="/account/" className="md:ml-auto">Inspect evidence <ArrowUpRight size={13} aria-hidden="true" /></Link>
    </nav>

    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" role="status">
      <p>{selected.running ? "Refreshes automatically while this tab is visible." : "No live bot connected. Saved progress and history remain available."}</p>
      {data && <p>Snapshot fetched <time dateTime={data.generated_at}>{stamp(data.generated_at)}</time></p>}
    </div>
    {error && <div role="alert" className="rounded-lg border border-danger/40 bg-danger-surface p-3 text-sm text-danger">
      Could not refresh account: {error} {data ? "Last successful snapshot remains below." : "Account state is unavailable."}
    </div>}

    <section id="account-state" aria-label="Account state" className="fs-root sd-state scroll-mt-24">
      {!data && !error && <div className="rounded-xl border bg-card p-8 text-sm text-muted-foreground">Loading account state…</div>}
      {data && !account && <div className="rounded-xl border bg-card p-6 text-sm text-muted-foreground">
        No verified state for this account yet. Saved ledger entries remain available below.
      </div>}
      {account && <AccountColumn account={account} accent="var(--primary)" changedAt={Date.parse(data!.generated_at)}
        shownCount={1} open={sections} onToggleSection={(key, value) => setSections(previous => ({ ...previous, [key]: value }))} />}
    </section>

    <section id="lab-jar" aria-label="Lab jar settings" className="scroll-mt-24">
      <LabSavings accountKey={selected.key} expectedAccountId={expectedId} split={account?.coin_split ?? null}
        onSaved={() => setRefresh(n => n + 1)} />
    </section>

    <SectionCard id="recent-ledger" title="Recent ledger"
      className="min-w-0" contentClassName="min-w-0" action={<Link href="/ledger/" className="sd-text-link">Full ledger <ArrowUpRight size={14} aria-hidden="true" /></Link>}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">Latest saved entries · local dates and times · game currencies</p>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">Show
          <select aria-label="Recent ledger kind" value={kind ?? ""} onChange={event => setKind(event.target.value || null)}
            className="max-w-full rounded-md border bg-background px-3 py-2 text-foreground">
            <option value="">All activity</option>
            {kinds.map(value => <option key={value} value={value}>{value.toLowerCase().replaceAll("_", " ")}</option>)}
          </select>
        </label>
      </div>
      {ledgerError && <p role="alert" className="text-sm text-danger">Could not refresh ledger: {ledgerError}. {ledger ? "Previously loaded entries shown." : ""}</p>}
      {!ledger && !ledgerError ? <p className="text-sm text-muted-foreground">Loading ledger…</p>
        : ledger && entries.length === 0 ? <p className="text-sm text-muted-foreground">{kind ? "No matching recent entries." : "No ledger entries recorded yet."}</p>
          : <LedgerEntries entries={entries} balanced={balancedCurrencies(ledger)} kind={kind} onKindChange={setKind} />}
      {ledger && (ledger.next !== null || allEntries.length > 20) && <Link href="/ledger/" className="sd-text-link">Browse older entries in the full ledger <ArrowUpRight size={14} aria-hidden="true" /></Link>}
    </SectionCard>

    <section id="live-monitor" className="scroll-mt-24 rounded-xl border bg-card">
      <button type="button" aria-expanded={monitor} aria-controls="monitor-content" onClick={() => setMonitor(open => !open)}
        className="flex w-full items-center gap-3 p-4 text-left text-sm font-semibold">
        <Monitor size={17} aria-hidden="true" /> Live monitor
        <span className="ml-auto text-xs font-normal text-muted-foreground">{monitor ? "Hide" : "Show"} {selected.running ? "screen & activity" : "emulator options"}</span>
      </button>
      {monitor && <div id="monitor-content" className="min-w-0 border-t p-3 sm:p-4">
        {selected.running ? remote ? <div className="flex flex-wrap items-start gap-4">
          <RemoteDeviceView dashboardUrl={selected.dashboard_url!} scope={selected.key} instance={selected.instance} />
          <a href={selected.dashboard_url!} className="sd-button">Open worker controls <ArrowUpRight size={14} aria-hidden="true" /></a>
        </div> : <LiveMonitor /> : <EmulatorRecovery />}
      </div>}
    </section>
  </div>;
}
