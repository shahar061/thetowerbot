"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { fetchAccountRunUpgrades } from "@/lib/api";
import { duration } from "@/lib/format";
import { deviceColor } from "@/lib/rerollState";
import type { RunUpgradesPayload } from "@/lib/types";
import { CATEGORY_COLOR } from "../workshop/workshopFormat";
import { killedLabel, totalCoins, type FleetRun } from "./runsView";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "done"; data: RunUpgradesPayload | null };

const title = (category: string): string => category[0] + category.slice(1).toLowerCase();

export function RunDetailPanel({ run, onClose }: { run: FleetRun; onClose: () => void }): React.JSX.Element {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  useEffect(() => {
    let live = true;
    setLoad({ state: "loading" });
    fetchAccountRunUpgrades(run.accountKey, run.id, run.accountId)
      .then(data => { if (live) setLoad({ state: "done", data }); })
      .catch((failure: unknown) => { if (live) setLoad({ state: "error", message: failure instanceof Error ? failure.message : "Upgrades unavailable" }); });
    return () => { live = false; };
  }, [run.accountKey, run.id, run.accountId]);

  const coins = totalCoins(run);
  const seconds = run.ended_at === null ? null : run.ended_at - run.started_at;
  return <aside aria-label="Run details" className="flex w-[32rem] shrink-0 flex-col overflow-hidden rounded-xl border border-border-strong bg-card">
    <header className="flex items-start justify-between gap-3 border-b px-4 pb-3 pt-4">
      <div className="space-y-1">
        <h2 className="text-base font-bold">Run #{run.id}</h2>
        <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <i className="size-[7px] rounded-full" style={{ backgroundColor: deviceColor(run.emulator) }} />{run.emulator} · {run.purpose === "milestone" ? "Milestone run" : "Farm run"}
        </p>
      </div>
      <button type="button" aria-label="Close run details" onClick={onClose} className="inline-flex size-8 items-center justify-center rounded-lg border"><X className="size-3.5" /></button>
    </header>
    <dl className="grid grid-cols-3 gap-2 p-4 text-xs">
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Wave · {run.tournament ? `Tournament · ${run.league ?? "unknown league"} · rank ${run.rank ?? "—"}` : `Tier ${run.tier ?? "—"}`}</dt><dd className="font-mono text-lg font-bold">{run.wave ?? "—"}</dd></div>
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Coins</dt><dd className="font-mono text-lg font-bold">{coins === null ? "—" : coins.toLocaleString()}</dd>
        <dd className="text-muted-foreground">{run.coins ?? "—"} earned + {run.ad_coins ?? "—"} ad</dd></div>
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Duration</dt><dd className="font-mono text-lg font-bold">{seconds === null ? "—" : duration(seconds)}</dd>
        {seconds !== null && run.wave ? <dd className="text-muted-foreground">{Math.round(seconds / run.wave)}s per wave</dd> : null}</div>
      <div className="col-span-3 flex items-center justify-between rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Killed by</dt><dd className="text-sm font-semibold">{killedLabel(run)}</dd></div>
    </dl>
    <div className="flex items-baseline justify-between px-4 pb-2">
      <h3 className="text-sm font-semibold">In-run upgrades at game over</h3>
      {load.state === "done" && load.data && <span className="text-[11px] text-muted-foreground">
        {load.data.totals.levels} levels bought · ${load.data.totals.spent.toLocaleString()} spent{load.data.totals.unpriced ? ` (+${load.data.totals.unpriced} unpriced)` : ""}</span>}
    </div>
    <div className="no-scrollbar flex-1 space-y-3 overflow-auto px-4 pb-4">
      {load.state === "loading" && <p role="status" className="text-sm text-muted-foreground">Loading upgrades…</p>}
      {load.state === "error" && <p role="alert" className="text-sm text-danger">{load.message}</p>}
      {load.state === "done" && !load.data && <p className="text-sm text-muted-foreground">No purchase record for this run.</p>}
      {load.state === "done" && load.data?.categories.map(category => {
        const color = CATEGORY_COLOR[category.name];
        const touched = category.items.filter(item => item.levels > 0).length;
        return <section key={category.name} aria-label={`${title(category.name)} upgrades`} className="space-y-2.5 rounded-xl border p-3"
          style={{ borderColor: `color-mix(in oklch, ${color}, transparent 55%)`, backgroundColor: `color-mix(in oklch, ${color}, transparent 93%)` }}>
          <header className="flex items-center justify-between text-xs font-semibold tracking-wide">
            <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-[2px]" style={{ backgroundColor: color }} />{category.name}</span>
            <span className="font-normal text-muted-foreground">{touched}/{category.items.length} bought</span>
          </header>
          <div className="grid grid-cols-2 gap-2">
            {category.items.map(item => <article key={item.upgrade_id} aria-label={item.name}
              className={`space-y-1.5 rounded-[10px] border bg-card p-2.5 ${item.levels === 0 ? "opacity-50" : ""}`}>
              <div className="flex items-baseline justify-between gap-1.5">
                <h4 className="truncate text-xs font-semibold">{item.name}</h4>
                <span className="font-mono text-[11px]"><b>{item.levels}</b><span className="text-muted-foreground">/{item.max_level.toLocaleString()}</span></span>
              </div>
              <div aria-hidden="true" className="h-1.5 overflow-hidden rounded" style={{ backgroundColor: `color-mix(in oklch, ${color}, transparent 82%)` }}>
                <div className="h-full rounded" style={{ width: `${item.levels === 0 ? 0 : Math.max(item.levels / item.max_level * 100, 2)}%`, backgroundColor: color }} />
              </div>
              <p className="text-[11px] text-muted-foreground">{item.levels === 0 ? "Not bought this run" : `+${item.levels} this run · $${item.spent.toLocaleString()}`}</p>
            </article>)}
          </div>
        </section>;
      })}
    </div>
  </aside>;
}
