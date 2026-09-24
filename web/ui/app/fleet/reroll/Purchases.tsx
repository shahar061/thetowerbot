"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { RunPurchases } from "@/components/RunPurchases";
import { cn } from "@/lib/utils";
import { fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases } from "@/lib/api";
import type { LedgerLine, RunPurchasePayload, RunRow } from "@/lib/types";
import type { RerollMember } from "@/lib/fleet";
import { Button } from "@/components/ui/button";
import { deviceColor } from "@/lib/rerollState";
import { RerollCard } from "./RerollCard";

type Page = { lines: LedgerLine[]; next: number | null; loaded: boolean; error?: string };

export function SharedWorkshopLedger({ members }: { members: RerollMember[] }) {
  const [pages, setPages] = useState<Record<string, Page>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const sourceKey = JSON.stringify(members.filter(member => member.account_key && member.account_id)
    .map(member => ({ name: member.name, account_key: member.account_key, account_id: member.account_id, lease_id: member.lease_id })));
  const sources = useMemo(() => JSON.parse(sourceKey) as RerollMember[], [sourceKey]);
  const generation = useRef(0);
  const busy = useRef(new Set<string>());
  const identity = (member: RerollMember): string => `${member.account_key}:${member.account_id}`;
  useEffect(() => {
    const requestGeneration = ++generation.current;
    busy.current = new Set();
    let active = true;
    const refresh = async (): Promise<void> => {
      await Promise.all(sources.map(async member => {
        const key = identity(member);
        if (busy.current.has(key)) return;
        busy.current.add(key);
        setPending(current => ({ ...current, [key]: true }));
        try {
          const payload = await fetchAccountWorkshopPurchases(member.account_key!, undefined, member.account_id!);
          if (!active) return;
          setPages(current => {
            const previous = current[key];
            const rows = new Map<number, LedgerLine>();
            for (const line of [...(previous?.lines ?? []), ...payload.lines]) rows.set(line.id, line);
            return { ...current, [key]: { lines: [...rows.values()], next: previous?.loaded ? previous.next : payload.next, loaded: true } };
          });
        } catch (error) {
          if (active) setPages(current => ({ ...current, [key]: {
            lines: current[key]?.lines ?? [], next: current[key]?.next ?? null, loaded: current[key]?.loaded ?? false, error: (error as Error).message,
          } }));
        } finally {
          if (active && requestGeneration === generation.current) {
            busy.current.delete(key);
            setPending(current => ({ ...current, [key]: false }));
          }
        }
      }));
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { active = false; generation.current = requestGeneration + 1; window.clearInterval(timer); };
  }, [sources]);
  const loadOlder = async (member: RerollMember): Promise<void> => {
    const key = identity(member);
    const before = pages[key]?.next;
    if (before == null || busy.current.has(key)) return;
    const requestGeneration = generation.current;
    busy.current.add(key);
    setPending(current => ({ ...current, [key]: true }));
    try {
      const payload = await fetchAccountWorkshopPurchases(member.account_key!, before, member.account_id!);
      if (requestGeneration !== generation.current) return;
      setPages(current => {
        const rows = new Map<number, LedgerLine>();
        for (const line of [...(current[key]?.lines ?? []), ...payload.lines]) rows.set(line.id, line);
        return { ...current, [key]: { lines: [...rows.values()], next: payload.next, loaded: true } };
      });
    } catch (error) {
      if (requestGeneration === generation.current) setPages(current => ({ ...current, [key]: {
        ...current[key], error: (error as Error).message,
      } }));
    } finally {
      if (requestGeneration === generation.current) {
        busy.current.delete(key);
        setPending(current => ({ ...current, [key]: false }));
      }
    }
  };
  const rows = sources.flatMap(member => (pages[identity(member)]?.lines ?? [])
    .filter(line => line.detail?.verdict === "bought" || line.detail?.verdict === "free")
    .map(line => ({ member, line }))).sort((a, b) => b.line.ts - a.line.ts || b.line.id - a.line.id);
  return <RerollCard title="Shared Workshop ledger">
    <p className="text-sm text-muted-foreground">Confirmed upgrades bought by pool accounts, newest first. Loaded counts are not lifetime totals. Each emulator loads 100 records at a time; load older to see further back.</p>

    {/* Per-device loading state, not a per-device total. This list is
        paginated, so any sum here would be the sum of whatever happens to be
        loaded - a number that moves when you press "load older" is worse
        than no number. The device's own card carries the real count. */}
    <div className="flex flex-wrap gap-2">{sources.map(member => {
      const page = pages[identity(member)];
      const loaded = page?.lines.filter(line => line.detail?.verdict === "bought" || line.detail?.verdict === "free").length ?? 0;
      return <div key={member.name} className="flex items-center gap-2 rounded-md border border-border px-2 py-1 text-xs">
        <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} aria-hidden="true" />
        <span className="font-medium">{member.name} · Account {member.account_id}</span>
        <span className="font-mono text-faint-foreground">{loaded} loaded</span>
        {page?.next != null && <Button size="xs" variant="outline" disabled={pending[identity(member)]} onClick={() => void loadOlder(member)}>Load older for {member.name}</Button>}
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
        <span className="mr-2 text-xs text-muted-foreground">Account {member.account_id}</span>
        {line.item ?? "Unknown upgrade"}
        {line.category ? <span className="ml-2 font-mono text-[10px] uppercase text-faint-foreground">{line.category}</span> : null}
      </span>
      {/* An unread price is its own outcome, not a zero: the buy was
          confirmed, the row's number was not legible on the frame. */}
      <span className={cn("font-mono text-xs", line.price == null ? "text-warn" : "text-muted-foreground")}>
        {line.price == null ? "price unread" : `${line.price.toLocaleString()} coins`}
      </span>
    </li>)}</ol> : <p className="text-sm text-muted-foreground">{sources.some(member => !pages[identity(member)]) ? "Loading Workshop purchases…" : sources.some(member => pages[identity(member)]?.error) ? "Some purchase histories are unavailable." : "No confirmed Workshop purchases recorded for these accounts yet."}</p>}
  </RerollCard>;
}

type BattlePurchaseScope = { accountKey: string | null | undefined; expectedAccountId?: string | null };

export function WorkerBattlePurchases({ accountKey, expectedAccountId }: BattlePurchaseScope) {
  // A keyed child discards every part of the previous account's state before
  // the next render; late responses are also rejected by effect cleanup.
  return <BattlePurchases key={`${accountKey ?? "unidentified"}:${expectedAccountId ?? ""}`} accountKey={accountKey} expectedAccountId={expectedAccountId} />;
}

function BattlePurchases({ accountKey, expectedAccountId }: BattlePurchaseScope) {
  const [run, setRun] = useState<RunRow | null>(null);
  const [purchases, setPurchases] = useState<RunPurchasePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!accountKey);
  useEffect(() => {
    if (!accountKey) return;
    let active = true;
    let polling = false;
    const refresh = async (): Promise<void> => {
      if (polling) return;
      polling = true;
      try {
        const runs = await fetchAccountRuns(accountKey, expectedAccountId ?? undefined);
        if (!active) return;
        const latest = runs[0] ?? null;
        const bought = latest ? await fetchAccountRunPurchases(accountKey, latest.id, expectedAccountId ?? undefined) : null;
        if (active) { setRun(latest); setPurchases(bought); setError(null); }
      } catch (failure) {
        if (active) setError((failure as Error).message);
      } finally {
        polling = false;
        if (active) setLoading(false);
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [accountKey, expectedAccountId]);
  return <div className="rounded-lg border border-border p-3"><h4 className="font-heading text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">In-battle upgrades</h4>
    {!accountKey ? <p className="mt-2 text-muted-foreground">Battle purchase history unavailable: account not verified.</p>
      : loading ? <p className="mt-2 text-muted-foreground">Loading battle purchase history…</p>
      : error ? <p role="status" className="text-danger">Purchase history unavailable: {error}</p>
      : run ? <><p className="my-2 text-xs text-muted-foreground">Run #{run.id} · {run.ended_at == null ? "live" : "completed"}</p><RunPurchases data={purchases} startedAt={run.started_at} /></>
      : <p className="mt-2 text-muted-foreground">No run recorded yet.</p>}
  </div>;
}
