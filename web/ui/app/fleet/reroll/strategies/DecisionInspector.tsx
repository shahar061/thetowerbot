import type { RouteEvaluation } from "@/lib/buildRoute";

export type DecisionSelection = { worker: string; accountId: string; item: string;
  reason: string; revision: number | null; observedAt: number | null;
  evaluation: RouteEvaluation | null };

export function DecisionInspector({ selection, onClose }: {
  selection: DecisionSelection | null; onClose: () => void;
}): React.JSX.Element | null {
  if (!selection) return null;
  const trace = selection.evaluation?.trace;
  return <aside aria-label="Decision details" role="region" className="rounded-xl border border-primary/40 bg-card p-4 shadow-lg">
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-xs uppercase tracking-widest text-primary">Decision details</p>
        <h2 className="font-heading text-lg font-semibold">{selection.worker} · {selection.item}</h2>
        <p className="font-mono text-xs text-muted-foreground">Account {selection.accountId} · Revision {selection.revision ?? "unknown"}</p>
      </div>
      <button type="button" onClick={onClose} className="rounded-md border border-border px-2 py-1 text-sm">Close</button>
    </div>
    <p className="mt-3 text-sm">{selection.reason}</p>
    <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
      <div><dt className="text-muted-foreground">Matched rule</dt><dd>{trace?.matched_rule_id || "Current reroll planner"}</dd></div>
      <div><dt className="text-muted-foreground">Price source</dt><dd>{trace?.price_source || "Not supplied"}</dd></div>
      <div><dt className="text-muted-foreground">Evidence</dt><dd>{selection.observedAt ? new Date(selection.observedAt * 1000).toLocaleString() : "Unknown"}</dd></div>
      <div><dt className="text-muted-foreground">Variant</dt><dd>{trace?.variant ?? "No variant evidence"}</dd></div>
    </dl>
    {!!trace?.rejected.length && <p className="mt-3 text-xs text-muted-foreground">Not selected: {trace.rejected.join(", ")}</p>}
  </aside>;
}
