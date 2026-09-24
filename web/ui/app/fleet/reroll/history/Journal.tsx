"use client";

import { useState } from "react";
import type { RerollJournalEntry } from "@/lib/fleet";
import { deviceColor } from "@/lib/rerollState";
import { cn } from "@/lib/utils";
import { RerollCard } from "../RerollCard";

function observed(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
export function Journal({ entries, worker, onWorker }: { entries: RerollJournalEntry[]; worker: string; onWorker: (name: string) => void }) {
  const [level, setLevel] = useState("all");
  const [diagnostics, setDiagnostics] = useState(false);
  const names = [...new Set(entries.map(entry => entry.instance))].sort();
  const visible = entries.filter(entry => (worker === "all" || entry.instance === worker)
    && (level === "all" || entry.level === level)
    && (diagnostics || entry.kind !== "diagnostic"))
    .sort((a, b) => new Date(typeof a.at === "number" ? a.at * 1000 : a.at).getTime()
      - new Date(typeof b.at === "number" ? b.at * 1000 : b.at).getTime() || a.sequence - b.sequence);
  const severities = { info: 0, warning: 0, error: 0 } as Record<string, number>;
  for (const entry of entries) if (entry.kind !== "diagnostic") severities[entry.level] = (severities[entry.level] ?? 0) + 1;

  return <RerollCard
    title="Shared journal"
    action={<div className="flex items-center gap-2 font-mono text-[10px]">
      {/* The severity census is the panel's headline: an error the filter is
          currently hiding has to be visible from the collapsed header. */}
      {severities.error ? <span className="rounded bg-danger-surface px-1.5 py-0.5 text-danger">{severities.error} error</span> : null}
      {severities.warning ? <span className="rounded bg-warn-surface px-1.5 py-0.5 text-warn">{severities.warning} warning</span> : null}
      <span className="text-faint-foreground">{visible.length} shown</span>
    </div>}
  >
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-1.5 text-muted-foreground">Emulator <select aria-label="Journal emulator" value={worker} onChange={event => onWorker(event.target.value)} className="rounded-md border bg-background px-2 py-1 text-foreground"><option value="all">All</option>{names.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      <label className="flex items-center gap-1.5 text-muted-foreground">Severity <select aria-label="Journal severity" value={level} onChange={event => setLevel(event.target.value)} className="rounded-md border bg-background px-2 py-1 text-foreground"><option value="all">All</option>{["info", "warning", "error"].map(item => <option key={item} value={item}>{item}</option>)}</select></label>
      <label className="flex items-center gap-1.5 text-muted-foreground"><input type="checkbox" checked={diagnostics} onChange={event => setDiagnostics(event.target.checked)} /> Diagnostics</label>
    </div>
    {visible.length ? <ol className="max-h-96 min-w-0 space-y-1 overflow-y-auto">{visible.map(entry => <li
      key={entry.sequence}
      className={cn("grid min-w-0 grid-cols-[auto_1fr] gap-x-2.5 rounded-md border-l-2 bg-well/60 px-2.5 py-1.5 text-sm",
        entry.level === "error" ? "border-l-danger" : entry.level === "warning" ? "border-l-warn" : "border-l-border-strong")}
    >
      <time className="font-mono text-[11px] leading-5 text-faint-foreground">{observed(entry.at)}</time>
      <div className="min-w-0 break-words [overflow-wrap:anywhere]">
        <span className="mr-2 font-semibold" style={{ color: entry.color || deviceColor(entry.instance) }}>[{entry.instance}]</span>
        <span className={cn("mr-2 font-mono text-[10px] uppercase",
          entry.level === "error" ? "text-danger" : entry.level === "warning" ? "text-warn" : "text-faint-foreground")}>{entry.level}</span>
        <span className="mr-2 font-mono text-[10px] text-faint-foreground">{entry.kind}</span>
        <span className="mr-2 text-xs text-muted-foreground">Account not recorded</span>
        <span>{entry.message}</span>
      </div>
    </li>)}</ol> : <p className="text-sm text-muted-foreground">No journal entries match these filters.</p>}
  </RerollCard>;
}
