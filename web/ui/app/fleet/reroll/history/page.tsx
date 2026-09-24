"use client";

import { useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { fetchRerollJournal } from "@/lib/api";
import type { RerollJournalEntry } from "@/lib/fleet";
import { cn } from "@/lib/utils";
import { useRerollWorkspace } from "../RerollWorkspace";
import { SharedWorkshopLedger } from "../Purchases";
import { PastRerolls } from "../PastRerolls";
import { Journal } from "./Journal";

const tabs = ["Activity", "Purchases", "Rerolls"] as const;
type Tab = typeof tabs[number];

function Activity() {
  const [entries, setEntries] = useState<RerollJournalEntry[]>([]);
  const [worker, setWorker] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    let polling = false;
    const refresh = async (): Promise<void> => {
      if (polling) return;
      polling = true;
      try {
        const journal = await fetchRerollJournal();
        if (active) { setEntries(journal.entries); setError(null); }
      } catch (failure) {
        if (active) setError((failure as Error).message);
      } finally {
        polling = false;
        if (active) setLoading(false);
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return <div className="space-y-3">
    <p className="text-sm text-muted-foreground">Worker activity across attempts. Historical journal entries do not record account identity; they are never attributed to the worker’s current account.</p>
    {error && <p role="alert" className="text-danger">Journal unavailable: {error}</p>}
    {loading ? <p role="status">Loading journal…</p> : <Journal entries={entries} worker={worker} onWorker={setWorker} />}
  </div>;
}

export default function HistoryPage() {
  const { pool, loading, error } = useRerollWorkspace();
  const [tab, setTab] = useState<Tab>("Activity");
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const members = [...(pool?.members ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  return <div className="flex flex-col gap-5">
    <PageHeader title="History" meta="Activity, confirmed purchases and past attempts" />
    <div role="tablist" aria-label="Reroll history" className="flex gap-1 border-b border-border">
      {tabs.map((name, index) => <button key={name} type="button" role="tab" id={`history-tab-${name}`} aria-controls={`history-panel-${name}`} aria-selected={tab === name} tabIndex={tab === name ? 0 : -1}
        ref={element => { buttons.current[index] = element; }}
        className={cn("border-b-2 px-4 py-2 text-sm", tab === name ? "border-primary text-primary" : "border-transparent text-muted-foreground")}
        onClick={() => setTab(name)} onKeyDown={event => {
          let next: number;
          if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
          else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = tabs.length - 1;
          else return;
          event.preventDefault(); setTab(tabs[next]); buttons.current[next]?.focus();
        }}>{name}</button>)}
    </div>
    <div role="tabpanel" id={`history-panel-${tab}`} aria-labelledby={`history-tab-${tab}`}>
      {tab === "Activity" && <Activity />}
      {tab === "Purchases" && <div className="space-y-3">
        {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
        {loading && !pool ? <p role="status">Loading fleet accounts…</p> : <>
          {!!members.filter(member => !member.account_id || !member.account_key).length && <p className="text-muted-foreground">Some workers have no verified account; their purchase histories are unavailable.</p>}
          <SharedWorkshopLedger members={members} />
          <p className="text-sm text-muted-foreground">For accounts from completed attempts, open the Rerolls tab and follow a reroll to its archived accounts.</p>
        </>}
      </div>}
      {tab === "Rerolls" && <PastRerolls refreshKey={pool?.run?.number} />}
    </div>
  </div>;
}
