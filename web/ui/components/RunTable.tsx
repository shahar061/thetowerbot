import type { RunRow } from "@/lib/types";

export function RunTable({ runs, onSelect }: { runs: RunRow[]; onSelect: (id: number) => void }) {
  if (!runs.length) {
    return <p className="text-sm text-muted-foreground">No stored runs. (Running with --no-store?)</p>;
  }
  return (
    // Bounded and scrolled rather than as long as the data: this sits beside
    // the unknown-screen gallery in a two-column grid, and 30 runs left to
    // grow stretch the whole row and push everything under it off the fold.
    // 80 (320px) is about ten rows plus the header - enough that the recent
    // history reads at a glance, short enough that the card stays a card.
    <div className="max-h-80 overflow-y-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-background text-muted-foreground">
          <tr>{["#", "wave", "coins", "tier", "taps", "length"].map((h) => (
            <th key={h} className="border-b py-1 text-left font-medium">{h}</th>
          ))}</tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} onClick={() => onSelect(run.id)} className="cursor-pointer hover:bg-accent/50">
              <td className="border-b py-1 tabular-nums">{run.id}</td>
              <td className="border-b py-1 tabular-nums">{run.wave ?? "-"}</td>
              <td className="border-b py-1 tabular-nums">{run.coins ?? "-"}</td>
              <td className="border-b py-1 tabular-nums">{run.tournament ? `Tournament · ${run.league ?? "unknown league"} · rank ${run.rank ?? "—"}` : run.tier ?? "-"}</td>
              <td className="border-b py-1 tabular-nums">{run.tap_count}</td>
              <td className="border-b py-1 tabular-nums">
                {run.ended_at ? Math.round(run.ended_at - run.started_at) + "s" : "live"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
