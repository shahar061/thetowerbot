import type { BuildRoutePreview } from "@/lib/buildRoute";

function decisionLabel(value: BuildRoutePreview["members"][number]["current"]): string {
  const item = value.decision?.item;
  return item ?? value.trace?.reason ?? "No verified action";
}

function phaseName(phase: string | null): string {
  if (!phase) return "No native phase";
  return ({ starter: "Survival Starter", economy: "Early Economy", objectives: "Upgrade objectives",
    fallback: "Cheap fallback" } as Record<string, string>)[phase] ?? phase;
}

function evidenceAge(observedAt: number | null): string {
  if (observedAt === null) return "No observation yet";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - observedAt));
  return seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`;
}

function evidenceTone(status: "verified" | "stale" | "unknown"): string {
  return status === "verified" ? "border-emerald-500/30 bg-emerald-500/5" :
    status === "stale" ? "border-amber-500/30 bg-amber-500/5" : "border-border bg-card";
}

function EvidenceLanes({ evidence }: { evidence: NonNullable<BuildRoutePreview["members"][number]["evidence"]> }): React.JSX.Element {
  const lanes = [{ id: "workshop", label: "Workshop" }, { id: "battle", label: "In-game" },
    { id: "resources", label: "Gems & Labs" }] as const;
  return <div aria-label="Evidence by lane" className="mt-3 grid gap-2 sm:grid-cols-3">
    {lanes.map(({ id, label }) => {
      const lane = evidence[id];
      return <div key={id} className={`min-w-0 rounded-lg border px-3 py-2 text-xs ${evidenceTone(lane.status)}`}>
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium">{label}</span>
          <span className="capitalize text-muted-foreground">{lane.status}</span>
        </div>
        <p className="mt-1 text-muted-foreground">{evidenceAge(lane.observed_at)} · {lane.source}</p>
        {lane.reason && <p className="mt-1 break-words text-muted-foreground">{lane.reason}</p>}
      </div>;
    })}
  </div>;
}

function waitingForFirstObservations(member: BuildRoutePreview["members"][number]): boolean {
  const waiting = (value: typeof member.current | undefined): boolean =>
    value?.status === "unknown" && value.trace.reason.startsWith("Waiting for first verified");
  const resourcesUnknown = [member.current_resources, member.proposed_resources].every(value =>
    !value || (value.gem_step.status === "unknown" && value.lab_step.status === "unknown"));
  return waiting(member.current) && waiting(member.proposed) &&
    waiting(member.current_battle) && waiting(member.proposed_battle) && resourcesUnknown;
}

export function RouteInspector({ preview, members }: { preview: BuildRoutePreview | null;
  members: { name: string; account_id?: string | null; hidden?: boolean }[] }): React.JSX.Element {
  const current = new Map(members.filter(member => !member.hidden && member.account_id)
    .map(member => [member.name, member.account_id]));
  const visible = preview?.members.filter(member => current.get(member.worker) === member.account_id) ?? [];
  return <aside aria-label="Route inspector" className="rounded-2xl border border-border bg-card p-4">
    <h3 className="font-heading text-lg font-semibold">Decision preview</h3>
    <p className="mt-1 text-xs text-muted-foreground">The server evaluates both routes against the same verified account facts. Unknown evidence stays unknown.</p>
    {!preview ? <p className="mt-4 text-sm text-muted-foreground">Preview the draft to compare it with the live route.</p> :
      visible.length === 0 ? <p className="mt-4 text-sm text-muted-foreground">No current fleet emulators to compare.</p> :
      <div className="mt-4 space-y-3">{visible.map(member => <section key={`${member.worker}:${member.account_id}`} className="min-w-0 rounded-xl border border-border bg-background/50 p-3">
        <h4 className="font-semibold">{member.worker}</h4>
        <p className="break-all font-mono text-xs text-muted-foreground">{member.account_id}</p>
        {member.evidence && <EvidenceLanes evidence={member.evidence} />}
        {waitingForFirstObservations(member) ? <div className="mt-3 rounded-lg border border-border bg-card px-3 py-3 text-xs">
          <p className="font-medium text-foreground">Waiting for first observations</p>
          <p className="mt-1 break-words text-muted-foreground">Workshop, battle, and resource comparisons will appear after this emulator reports verified scans.</p>
        </div> : <>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <div className="min-w-0 break-words rounded-lg bg-card px-3 py-2 text-xs"><p>Current · {decisionLabel(member.current)}</p><p className="text-muted-foreground">{member.current.status}</p></div>
          <div className="min-w-0 break-words rounded-lg bg-primary/10 px-3 py-2 text-xs"><p>Proposed · {decisionLabel(member.proposed)}</p><p className="text-muted-foreground">{member.proposed.status}</p></div>
        </div>
        {(member.current.trace?.phase_state || member.proposed.trace?.phase_state) && <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {[{ label: "Current", value: member.current }, { label: "Proposed", value: member.proposed }].map(item => item.value.trace?.phase_state &&
            <div key={item.label} className="min-w-0 break-words rounded-lg border border-border bg-card px-3 py-2 text-xs">
              <p>{item.label} phase · {phaseName(item.value.trace.phase_id)} · {item.value.trace.phase_state}</p>
              {item.value.trace.transition_reason && <p className="mt-1 text-muted-foreground">{item.value.trace.transition_reason}</p>}
              {item.value.trace.next_phase_id && <p className="mt-1 text-muted-foreground">Next · {phaseName(item.value.trace.next_phase_id.split(".").at(-1) ?? null)}</p>}
            </div>)}
        </div>}
        {member.current_battle && member.proposed_battle && <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <p className="min-w-0 break-words rounded-lg bg-card px-3 py-2 text-xs">Battle current · {decisionLabel(member.current_battle)} · {member.current_battle.trace?.phase_id ?? "phase unknown"}</p>
          <p className="min-w-0 break-words rounded-lg bg-primary/10 px-3 py-2 text-xs">Battle proposed · {decisionLabel(member.proposed_battle)} · {member.proposed_battle.trace?.phase_id ?? "phase unknown"}</p>
        </div>}
        {member.current_resources && member.proposed_resources && <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <p className="min-w-0 break-words rounded-lg bg-card px-3 py-2 text-xs">Resources current · {member.current_resources.gem_step.action.replaceAll("_", " ")} · {member.current_resources.lab_step.action.replaceAll("_", " ")}</p>
          <p className="min-w-0 break-words rounded-lg bg-primary/10 px-3 py-2 text-xs">Resources proposed · {member.proposed_resources.gem_step.action.replaceAll("_", " ")} · {member.proposed_resources.lab_step.action.replaceAll("_", " ")}</p>
        </div>}
        {!!Object.keys(member.proposed.trace?.eligible_odds ?? {}).length && <p className="mt-2 text-xs text-muted-foreground">Eligible odds: {Object.entries(member.proposed.trace.eligible_odds).map(([id, odds]) => `${id} ${Math.round(odds * 100)}%`).join(" · ")}</p>}
        </>}
      </section>)}</div>}
  </aside>;
}
