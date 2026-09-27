import type { FleetOverview, RerollMember } from "@/lib/fleet";
import { validatedOverview } from "@/lib/fleet";

const states: { id: FleetOverview["health"]["state"]; label: string }[] = [
  { id: "progressing", label: "Progressing" }, { id: "waiting", label: "Expected wait" },
  { id: "recovering", label: "Recovering" }, { id: "attention", label: "Needs attention" },
  { id: "stopped", label: "Stopped" }, { id: "unknown", label: "Health unknown" },
];

/** Match the backend's current-evidence window; account scope alone does not make an observation live. */
export const FLEET_EVIDENCE_TTL_SECONDS = 120;
export function freshFleetEvidence(at: number | null | undefined, now = Date.now() / 1000): boolean {
  return at != null && Number.isFinite(at) && at <= now && now - at <= FLEET_EVIDENCE_TTL_SECONDS;
}

function age(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

export function FleetOverviewSummary({ members }: { members: RerollMember[] }): React.JSX.Element {
  const counts = Object.fromEntries(states.map(state => [state.id, 0])) as Record<FleetOverview["health"]["state"], number>;
  let incidents = 0;
  let incidentsKnown = members.length > 0;
  let oldest: number | null = null;
  for (const member of members) {
    const overview = validatedOverview(member);
    const current = overview && freshFleetEvidence(overview.observed_at) ? overview : null;
    counts[current?.health.state ?? "unknown"] += 1;
    if (current?.health.incidents_open == null) incidentsKnown = false;
    else incidents += current.health.incidents_open;
    if (overview) oldest = Math.min(oldest ?? overview.observed_at, overview.observed_at);
  }
  return <section aria-label="Fleet health overview" className="rounded-xl border bg-card p-3 shadow-sm sm:p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="font-heading text-base font-semibold">Fleet at a glance</h2>
      <p className="text-sm text-muted-foreground">{members.length} {members.length === 1 ? "account" : "accounts"}{members.some(member => member.hidden) ? ` · ${members.filter(member => member.hidden).length} hidden` : ""}</p>
    </div>
    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {states.map(state => <div key={state.id} className="min-w-0 rounded-lg bg-well p-2.5">
        <strong data-count={state.id} className="block text-lg tabular-nums">{counts[state.id]}</strong>
        <span className="text-xs text-muted-foreground">{state.label}</span>
      </div>)}
    </div>
    <p className="mt-3 text-sm text-muted-foreground">{incidentsKnown ? `${incidents} unresolved incidents` : "Incident count unknown"}
      {" · "}{oldest === null ? "Evidence age unknown" : `Oldest evidence ${age(Math.max(0, Math.floor(Date.now() / 1000 - oldest)))} ago`}</p>
  </section>;
}
