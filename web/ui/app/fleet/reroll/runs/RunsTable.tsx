import { Trophy } from "lucide-react";
import { duration } from "@/lib/format";
import { deviceColor } from "@/lib/rerollState";
import type { RecordState } from "@/lib/types";
import { killedLabel, recordTip, totalCoins, type FleetRun } from "./runsView";

export const runKey = (run: FleetRun): string => `${run.accountKey}#${run.id}`;

function Badge({ state, tip }: { state: RecordState | undefined; tip: string | null }): React.JSX.Element | null {
  if (!state || !tip) return null;
  const standing = state === "standing";
  return <span aria-label={tip} title={tip} data-record={state}
    className={`inline-flex items-center gap-0.5 rounded-full px-1.5 font-sans text-[10px] font-bold ${standing
      ? "bg-[oklch(0.82_0.14_85)] text-[oklch(0.22_0.03_85)]" : "border border-[oklch(0.82_0.14_85)] text-[oklch(0.82_0.14_85)]"}`}>
    <Trophy aria-hidden="true" className="size-2.5" />HS
  </span>;
}

const COLUMNS = "grid grid-cols-[4rem_8rem_5.5rem_2.75rem_6rem_7rem_5rem_minmax(0,1fr)_4rem] items-center gap-2.5";

export function RunsTable({ runs, selected, onSelect, now }: {
  runs: FleetRun[]; selected: string | null; onSelect: (run: FleetRun) => void; now: number;
}): React.JSX.Element {
  return <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card">
    <div className={`${COLUMNS} border-b px-4 py-2.5 text-[11px] font-semibold tracking-wide text-muted-foreground`}>
      <span>RUN</span><span>EMULATOR</span><span>ENDED</span><span>TIER</span><span>WAVE</span><span>COINS</span>
      <span>DURATION</span><span>KILLED BY</span><span className="text-right">BUYS</span>
    </div>
    <div className="no-scrollbar flex-1 overflow-auto">
      {runs.map(run => {
        const active = selected === runKey(run);
        const coins = totalCoins(run);
        const plain = run.ended_at === null || run.abandoned || !run.killed_by;
        return <button key={runKey(run)} type="button" aria-label={`Run #${run.id} on ${run.emulator}`} aria-pressed={active}
          onClick={() => onSelect(run)}
          className={`${COLUMNS} min-h-12 w-full border-b px-4 text-left text-[13px] ${active ? "bg-primary/15 shadow-[inset_3px_0_0_var(--primary)]" : "hover:bg-muted/50"}`}>
          <span className="font-mono text-muted-foreground">#{run.id}</span>
          <span className="inline-flex items-center gap-1.5 truncate"><i className="size-[7px] shrink-0 rounded-full" style={{ backgroundColor: deviceColor(run.emulator) }} />{run.emulator}</span>
          <span className="text-muted-foreground">{run.ended_at === null ? "live" : `${duration(Math.max(0, now - run.ended_at))} ago`}</span>
          <span className="font-mono">{run.tier === null ? "—" : `T${run.tier}`}</span>
          <span className="inline-flex items-center gap-1.5 font-mono">{run.wave ?? "—"}<Badge state={run.wave_record} tip={recordTip("wave", run)} /></span>
          <span className="inline-flex items-center gap-1.5 font-mono">{coins === null ? "—" : coins.toLocaleString()}<Badge state={run.coin_record} tip={recordTip("coin", run)} /></span>
          <span className="font-mono">{run.ended_at === null ? "—" : duration(run.ended_at - run.started_at)}</span>
          <span className={`truncate ${plain ? "italic text-muted-foreground" : ""}`}>{killedLabel(run)}</span>
          <span className="text-right font-mono text-muted-foreground">{run.buys ?? "—"}</span>
        </button>;
      })}
    </div>
  </div>;
}
