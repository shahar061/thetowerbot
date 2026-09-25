import type { BuildRoutePreview } from "@/lib/buildRoute";

function decisionLabel(value: BuildRoutePreview["members"][number]["current"]): string {
  const item = value.decision?.item;
  return item ?? value.trace?.reason ?? "No verified action";
}

export function RouteInspector({ preview }: { preview: BuildRoutePreview | null }): React.JSX.Element {
  return <aside aria-label="Route inspector" className="rounded-2xl border border-border bg-card p-4">
    <h3 className="font-heading text-lg font-semibold">Decision preview</h3>
    <p className="mt-1 text-xs text-muted-foreground">The server evaluates both routes against the same verified account facts. Unknown evidence stays unknown.</p>
    {!preview ? <p className="mt-4 text-sm text-muted-foreground">Preview the draft to compare it with the live route.</p> :
      <div className="mt-4 space-y-3">{preview.members.map(member => <section key={`${member.worker}:${member.account_id}`} className="rounded-xl border border-border bg-background/50 p-3">
        <h4 className="font-semibold">{member.worker}</h4>
        <p className="font-mono text-xs text-muted-foreground">{member.account_id}</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <div className="rounded-lg bg-card px-3 py-2 text-xs"><p>Current · {decisionLabel(member.current)}</p><p className="text-muted-foreground">{member.current.status}</p></div>
          <div className="rounded-lg bg-primary/10 px-3 py-2 text-xs"><p>Proposed · {decisionLabel(member.proposed)}</p><p className="text-muted-foreground">{member.proposed.status}</p></div>
        </div>
        {member.current_battle && member.proposed_battle && <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <p className="rounded-lg bg-card px-3 py-2 text-xs">Battle current · {decisionLabel(member.current_battle)} · {member.current_battle.trace?.phase_id ?? "phase unknown"}</p>
          <p className="rounded-lg bg-primary/10 px-3 py-2 text-xs">Battle proposed · {decisionLabel(member.proposed_battle)} · {member.proposed_battle.trace?.phase_id ?? "phase unknown"}</p>
        </div>}
        {member.current_resources && member.proposed_resources && <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <p className="rounded-lg bg-card px-3 py-2 text-xs">Resources current · {member.current_resources.gem_step.action.replaceAll("_", " ")} · {member.current_resources.lab_step.action.replaceAll("_", " ")}</p>
          <p className="rounded-lg bg-primary/10 px-3 py-2 text-xs">Resources proposed · {member.proposed_resources.gem_step.action.replaceAll("_", " ")} · {member.proposed_resources.lab_step.action.replaceAll("_", " ")}</p>
        </div>}
        {!!Object.keys(member.proposed.trace?.eligible_odds ?? {}).length && <p className="mt-2 text-xs text-muted-foreground">Eligible odds: {Object.entries(member.proposed.trace.eligible_odds).map(([id, odds]) => `${id} ${Math.round(odds * 100)}%`).join(" · ")}</p>}
      </section>)}</div>}
  </aside>;
}
