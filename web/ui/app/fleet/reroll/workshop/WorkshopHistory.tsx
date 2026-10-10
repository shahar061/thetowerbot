"use client";

import { Fragment, useState } from "react";
import type { WorkshopSelection } from "@/lib/buildRoute";
import { PlanGraph } from "./PlanGraph";
import { gameNumber } from "./workshopFormat";

const METHODS: Record<string, string> = {
  value: "Best value", weighted: "Weighted draw", priority: "Priority order",
  cheapest: "Cheapest", unlock: "Unlock",
};
const OUTCOMES: Record<string, string> = {
  bought: "Bought", free: "Free upgrade", buy_selected: "Buy selected",
  save_coins: "Saving coins", observe_price: "Reading price", unknown: "Outcome unavailable",
  needs_operator: "Needs attention", wait: "Waiting",
};

function stamp(at: number): React.JSX.Element {
  const date = new Date(at * 1000);
  return <time dateTime={date.toISOString()}>{date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  })}</time>;
}

function score(row: WorkshopSelection): string {
  if (row.score != null) return `${row.score.toFixed(1)} coins/pt`;
  if (row.selection === "weighted" && row.weight != null) {
    return `Weight ${row.weight}${row.odds != null ? ` · ${(row.odds * 100).toFixed(1)}% odds` : ""}`;
  }
  return "Score unavailable";
}

export function WorkshopHistory({ rows }: { rows: WorkshopSelection[] }): React.JSX.Element {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [limit, setLimit] = useState(10);
  return <section aria-label="Previous selections" className="rounded-xl border border-border bg-card">
    <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-3">
      <h2 className="text-sm font-semibold">Previous selections</h2>
      <p className="text-xs text-muted-foreground">Latest first · scores are from the original decision</p>
    </div>
    {rows.length === 0 ? <p className="p-4 text-sm text-muted-foreground">No previous selections recorded yet.</p>
      : <div className="overflow-x-auto"><table className="w-full text-left text-xs">
        <thead className="text-muted-foreground"><tr>
          {["Time", "Selected upgrade", "Score", "Selection method", "Outcome"].map(label =>
            <th key={label} scope="col" className="px-4 py-2 font-medium">{label}</th>)}
        </tr></thead>
        <tbody>{rows.slice(0, limit).map(row => <Fragment key={row.id}>
          <tr className="border-t border-border align-top">
            <td className="whitespace-nowrap px-4 py-3 tabular-nums">{stamp(row.selected_at)}</td>
            <td className="px-4 py-3"><p className="font-medium">{row.name}</p>
              {row.price != null && <p className="mt-1 text-muted-foreground">{gameNumber(row.price)} coins</p>}
              {row.plan && <button type="button" aria-expanded={expanded === row.id}
                aria-label={`${expanded === row.id ? "Hide" : "View"} decision graph for ${row.name}`}
                className="mt-2 text-primary hover:underline"
                onClick={() => setExpanded(current => current === row.id ? null : row.id)}>
                {expanded === row.id ? "Hide graph" : "View graph"}
              </button>}
            </td>
            <td className="whitespace-nowrap px-4 py-3 font-mono tabular-nums">{score(row)}</td>
            <td className="px-4 py-3">{METHODS[row.selection ?? ""] ?? "Method unavailable"}</td>
            <td className="px-4 py-3"><p className={row.purchased_at != null ? "text-live" : "text-muted-foreground"}>
              {OUTCOMES[row.outcome] ?? row.outcome.replaceAll("_", " ")}</p>
              {row.purchased_at != null && row.purchased_at !== row.selected_at &&
                <p className="mt-1 whitespace-nowrap text-muted-foreground">{stamp(row.purchased_at)}</p>}
              {row.reason && <p className="mt-1 max-w-xs text-muted-foreground">{row.reason}</p>}
            </td>
          </tr>
          {expanded === row.id && row.plan && <tr><td colSpan={5} className="border-t border-border p-4">
            <PlanGraph plan={row.plan} stale={false} />
          </td></tr>}
        </Fragment>)}</tbody>
      </table></div>}
    {rows.length > limit && <button type="button" className="m-4 text-xs text-primary hover:underline"
      onClick={() => setLimit(current => current + 10)}>Show older selections</button>}
  </section>;
}
