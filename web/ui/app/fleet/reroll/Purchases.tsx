"use client";

import { useEffect, useState } from "react";
import { RunPurchases } from "@/components/RunPurchases";
import { cn } from "@/lib/utils";
import { fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases } from "@/lib/api";
import type { LedgerLine, RunPurchasePayload, RunRow } from "@/lib/types";
import type { RerollMember } from "@/lib/fleet";
import { Button } from "@/components/ui/button";
import { deviceColor } from "@/lib/rerollState";
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
    <p className="text-sm text-muted-foreground">Confirmed upgrades bought by pool accounts, newest first. Each emulator loads 100 records at a time; load older to see further back.</p>

    {/* Per-device loading state, not a per-device total. This list is
        paginated, so any sum here would be the sum of whatever happens to be
        loaded - a number that moves when you press "load older" is worse
        than no number. The device's own card carries the real count. */}
    <div className="flex flex-wrap gap-2">{sources.map(member => {
      const page = pages[member.account_key!];
      const loaded = page?.lines.filter(line => line.detail?.verdict === "bought" || line.detail?.verdict === "free").length ?? 0;
      return <div key={member.name} className="flex items-center gap-2 rounded-md border border-border px-2 py-1 text-xs">
        <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} aria-hidden="true" />
        <span className="font-medium">{member.name}</span>
        <span className="font-mono text-faint-foreground">{loaded} loaded</span>
        {page?.next != null && <Button size="xs" variant="outline" onClick={() => loadOlder(member.account_key!)}>Load older for {member.name}</Button>}
        {page?.error && <span role="status" className="text-danger">{page.error}</span>}
      </div>;
    })}</div>

    {rows.length ? <ol className="max-h-80 space-y-1 overflow-y-auto">{rows.map(({ member, line }) => <li
      key={`${member.account_key}:${line.id}`}
      className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-baseline gap-x-2.5 rounded-md bg-well/60 px-2.5 py-1.5 text-sm"
    >
      <time className="font-mono text-[11px] text-faint-foreground">{new Date(line.ts * 1000).toLocaleTimeString()}</time>
      <span className="min-w-0 truncate">
        <span className="mr-2 font-medium" style={{ color: deviceColor(member.name) }}>{member.name}</span>
        {line.item ?? "Unknown upgrade"}
        {line.category ? <span className="ml-2 font-mono text-[10px] uppercase text-faint-foreground">{line.category}</span> : null}
      </span>
      {/* An unread price is its own outcome, not a zero: the buy was
          confirmed, the row's number was not legible on the frame. */}
      <span className={cn("font-mono text-xs", line.price == null ? "text-warn" : "text-muted-foreground")}>
        {line.price == null ? "price unread" : `${line.price.toLocaleString()} coins`}
      </span>
    </li>)}</ol> : <p className="text-sm text-muted-foreground">No confirmed Workshop purchases recorded for these accounts yet.</p>}
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
  return <div className="rounded-lg border border-border p-3"><h4 className="font-heading text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">In-battle upgrades</h4>
    {error ? <p role="status" className="text-danger">Purchase history unavailable: {error}</p>
      : run ? <><p className="my-2 text-xs text-muted-foreground">Run #{run.id} · {run.ended_at == null ? "live" : "completed"}</p><RunPurchases data={purchases} startedAt={run.started_at} /></>
      : <p className="mt-2 text-muted-foreground">No run recorded yet.</p>}
  </div>;
}
