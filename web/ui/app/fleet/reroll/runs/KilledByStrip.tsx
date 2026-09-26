import type { RunRow } from "@/lib/types";
import { killedBy } from "./runsView";

const PALETTE = ["var(--ws-defense)", "var(--ws-attack)", "var(--ws-utility)", "oklch(0.75 0.13 85)", "var(--muted-foreground)"];

export function KilledByStrip({ runs }: { runs: RunRow[] }): React.JSX.Element {
  const { counts, excluded } = killedBy(runs);
  return <section aria-label="Killed by" className="space-y-2.5 rounded-xl border bg-card p-4">
    <div className="flex items-baseline justify-between">
      <h2 className="text-sm font-semibold">Killed by</h2>
      <span className="text-xs text-muted-foreground">{runs.length} runs · {excluded} abandoned or unreadable</span>
    </div>
    {counts.length === 0 ? <p className="text-xs text-muted-foreground">No finished run has a Killed By reading yet.</p> : <>
      <div className="flex h-3.5 gap-[2px] overflow-hidden rounded">
        {counts.map((c, i) => <div key={c.name} title={`${c.name}: ${c.count} runs`} className="h-full"
          style={{ flexGrow: c.count, backgroundColor: PALETTE[i % PALETTE.length] }} />)}
      </div>
      <ul className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        {counts.map((c, i) => <li key={c.name} className="inline-flex items-center gap-1.5">
          <i className="size-2.5 rounded-[3px]" style={{ backgroundColor: PALETTE[i % PALETTE.length] }} />{c.name}
          <b className="font-mono text-foreground">{c.count}</b><span>{c.share}%</span>
        </li>)}
      </ul>
    </>}
  </section>;
}
