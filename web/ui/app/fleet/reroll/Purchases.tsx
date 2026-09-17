"use client";

import { useEffect, useState } from "react";
import { RunPurchases } from "@/components/RunPurchases";
import { fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases } from "@/lib/api";
import type { LedgerLine, RunPurchasePayload, RunRow } from "@/lib/types";
import type { RerollMember } from "@/lib/fleet";
import { RerollCard } from "./RerollCard";

type Page = { lines: LedgerLine[]; next: number | null; error?: string };

export function SharedWorkshopLedger({ members }: { members: RerollMember[] }) {
  const [pages, setPages] = useState<Record<string, Page>>({});
  const sources = members.filter(member => member.account_key && member.account_id);
  const sourceKey = sources.map(member => `${member.name}:${member.account_key}`).join("|");
  useEffect(() => {
    let active = true;
    const refresh = () => {
      for (const member of sources) {
        const key = member.account_key!;
        void fetchAccountWorkshopPurchases(key).then(payload => {
          if (!active) return;
          setPages(current => {
            const previous = current[key];
            const rows = new Map<number, LedgerLine>();
            for (const line of [...payload.lines, ...(previous?.lines ?? [])]) rows.set(line.id, line);
            return { ...current, [key]: { lines: [...rows.values()], next: previous ? previous.next : payload.next } };
          });
        }).catch((error: Error) => {
          if (active) setPages(current => ({ ...current, [key]: {
            lines: current[key]?.lines ?? [], next: current[key]?.next ?? null, error: error.message,
          } }));
        });
      }
    };
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => { active = false; window.clearInterval(timer); };
  // Recreate polling when pool membership changes; each request retains its own account scope.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey]);
  const loadOlder = (key: string) => {
    const before = pages[key]?.next;
    if (before == null) return;
    void fetchAccountWorkshopPurchases(key, before).then(payload => setPages(current => ({
      ...current, [key]: { lines: [...(current[key]?.lines ?? []), ...payload.lines], next: payload.next },
    }))).catch((error: Error) => setPages(current => ({ ...current, [key]: {
      ...current[key], error: error.message,
    } })));
  };
  const rows = sources.flatMap(member => (pages[member.account_key!]?.lines ?? [])
    .filter(line => line.detail?.verdict === "bought" || line.detail?.verdict === "free")
    .map(line => ({ member, line }))).sort((a, b) => b.line.ts - a.line.ts || b.line.id - a.line.id);
  return <RerollCard title="Shared Workshop ledger">
    <p className="mt-1 text-sm text-muted-foreground">Confirmed upgrades bought by pool accounts. Load older records for each emulator to see its full history.</p>
    {sources.map(member => <div key={member.name} className="mt-2 flex flex-wrap items-center gap-2 text-xs">
      <span className="font-semibold">[{member.name}]</span>
      <span>{pages[member.account_key!]?.lines.filter(line => line.detail?.verdict === "bought" || line.detail?.verdict === "free").length ?? 0} loaded</span>
      {pages[member.account_key!]?.next != null && <button className="rounded border px-2 py-1" onClick={() => loadOlder(member.account_key!)}>Load older for {member.name}</button>}
      {pages[member.account_key!]?.error && <span role="status" className="text-danger">{pages[member.account_key!].error}</span>}
    </div>)}
    {rows.length ? <ol className="mt-3 max-h-80 space-y-2 overflow-y-auto text-sm">{rows.map(({ member, line }) => <li key={`${member.account_key}:${line.id}`} className="flex flex-wrap gap-x-2 rounded border p-2">
      <time className="text-muted-foreground">{new Date(line.ts * 1000).toLocaleString()}</time>
      <strong>[{member.name}]</strong><span>{line.item ?? "Unknown upgrade"}</span>
      <span className="text-muted-foreground">{line.category ?? ""} · {line.price == null ? "price unread" : `${line.price.toLocaleString()} coins`}</span>
    </li>)}</ol> : <p className="mt-3 text-sm text-muted-foreground">No confirmed Workshop purchases recorded for these accounts yet.</p>}
  </RerollCard>;
}

export function WorkerBattlePurchases({ accountKey }: { accountKey: string | null | undefined }) {
  const [run, setRun] = useState<RunRow | null>(null);
  const [purchases, setPurchases] = useState<RunPurchasePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!accountKey) return;
    let active = true;
    const refresh = () => {
      void fetchAccountRuns(accountKey).then(async runs => {
        const latest = runs[0] ?? null;
        const bought = latest ? await fetchAccountRunPurchases(accountKey, latest.id) : null;
        if (active) { setRun(latest); setPurchases(bought); setError(null); }
      }).catch((failure: Error) => { if (active) setError(failure.message); });
    };
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [accountKey]);
  return <div className="mt-3 rounded border p-3"><h3 className="font-semibold">In-battle upgrades</h3>
    {error ? <p role="status" className="text-danger">Purchase history unavailable: {error}</p>
      : run ? <><p className="my-2 text-xs text-muted-foreground">Run #{run.id} · {run.ended_at == null ? "live" : "completed"}</p><RunPurchases data={purchases} startedAt={run.started_at} /></>
      : <p className="mt-2 text-muted-foreground">No run recorded yet.</p>}
  </div>;
}
