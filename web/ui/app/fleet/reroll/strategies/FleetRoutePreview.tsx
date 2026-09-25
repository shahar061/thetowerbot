"use client";

import Link from "next/link";
import { useState } from "react";
import { deviceColor } from "@/lib/rerollState";
import type { RerollMember } from "@/lib/fleet";
import { WorkerBattlePurchases } from "../Purchases";
import { DecisionInspector, type DecisionSelection } from "./DecisionInspector";

function phase(stage: string): string {
  return stage.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function stateLabel(state: string | null): string {
  if (state === "observed") return "Observed";
  if (state === "projected") return "Projected";
  if (state === "blocked") return "Blocked";
  return "Unknown";
}

export function FleetRoutePreview({ members, savedRevision, showHistory = false }: {
  members: RerollMember[]; savedRevision: number | null; showHistory?: boolean;
}): React.JSX.Element {
  const [scope, setScope] = useState("fleet");
  const [selection, setSelection] = useState<DecisionSelection | null>(null);
  const visible = members.filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  const shown = scope === "fleet" ? visible : visible.filter(member => member.name === scope);

  return <section aria-label="Fleet Build Route" className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card px-4 py-3">
      <div>
        <h2 className="font-heading font-semibold">Fleet preview</h2>
        <p className="text-xs text-muted-foreground">Compare the next supported action for every verified account.</p>
      </div>
      <label className="flex items-center gap-2 text-sm">Emulator scope
        <select aria-label="Emulator scope" className="rounded-md border border-border bg-background px-2 py-1" value={scope} onChange={event => setScope(event.target.value)}>
          <option value="fleet">Entire fleet</option>
          {visible.map(member => <option key={member.name} value={member.name}>{member.name}</option>)}
        </select>
      </label>
    </div>
    {!visible.some(member => member.battle_evaluation?.status === "observed" &&
      member.battle_evaluation.account_id === member.account_id) &&
      <p className="text-xs text-muted-foreground">Live battle intent unavailable</p>}
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
      {shown.map(member => {
        const plan = member.account_id && member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
        const evaluation = member.workshop_evaluation?.account_id === member.account_id ? member.workshop_evaluation ?? null : null;
        const item = evaluation?.decision?.item ?? plan?.item ?? null;
        const price = evaluation?.decision?.price ?? plan?.price ?? null;
        const wallet = evaluation?.decision?.wallet_coins ?? plan?.wallet_coins ?? null;
        const reason = evaluation?.trace.reason ?? plan?.reason ?? "Waiting for verified Workshop observations.";
        const status = stateLabel(evaluation?.status ?? (plan ? "projected" : null));
        const revision = evaluation?.revision ?? member.route_revision_applied ?? null;
        const revisionState = member.route_error ? "Route blocked" : member.state === "paused" ? "Pending until next spend" :
          savedRevision === null ? "Route revision unknown" : revision === savedRevision ? "Applied" : "Route revision pending";
        const battle = member.battle_evaluation?.account_id === member.account_id ? member.battle_evaluation : null;
        const resources = member.resource_evaluation?.account_id === member.account_id ? member.resource_evaluation : null;
        return <article id={`strategy-${member.name}-${member.account_id}`} key={`${member.name}:${member.account_id}`} aria-label={`Strategy for ${member.name}`}
          className="min-w-0 space-y-4 rounded-xl border border-border bg-card p-4">
          <header className="space-y-1 border-b border-border pb-3">
            <h3 className="font-heading text-base font-semibold" style={{ color: deviceColor(member.name) }}>{member.name}</h3>
            <p className="font-mono text-xs text-muted-foreground">Account {member.account_id ?? "not verified"}</p>
            <p className="text-xs text-muted-foreground">Variant: {member.variant_name ?? member.variant ?? "Not assigned"}</p>
            <p className="text-xs text-muted-foreground">Phase: {plan ? phase(plan.stage) : "Unknown"}</p>
            <p className="text-xs" aria-label={`Route status for ${member.name}`}>{revisionState}</p>
          </header>
          <section aria-label={`Workshop policy for ${member.name}`} className="space-y-2">
            <div className="flex items-center justify-between gap-2"><h4 className="text-sm font-semibold">Workshop policy</h4><span className="text-xs text-muted-foreground">{plan ? status : "Unknown"}</span></div>
            {plan ? <>
              <p className="font-heading text-lg font-semibold">{item ?? "Waiting"}</p>
              <p className="font-mono text-xs">{wallet !== null && price !== null ? `${wallet} / ${price} coins` : "Price or wallet unknown"}</p>
              {item && <button type="button" onClick={() => setSelection({ worker: member.name, accountId: member.account_id!, item, reason,
                revision: savedRevision, observedAt: evaluation?.evidence_at ?? plan.observed_at, evaluation })}
                className="rounded-md border border-primary/50 px-2 py-1 text-xs text-primary">Why {item} for {member.name}?</button>}
              {!!plan.next_purchases?.length && <div className="flex flex-wrap gap-1" aria-label="Projected Workshop milestones">
                {plan.next_purchases.map(step => <span key={`${step.position}:${step.upgrade_id}`} className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">{step.item} · Projected</span>)}
              </div>}
            </> : <p className="text-sm text-muted-foreground">Workshop plan unavailable for this account. Waiting for matching observations.</p>}
          </section>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <section className="rounded-lg border border-border bg-background/40 p-2"><h4 className="font-medium">Battle</h4><p>{battle?.status === "observed" ? battle.decision?.item ?? "Waiting" : "Unknown"}</p><p className="text-muted-foreground">{battle?.trace.phase_id ?? battle?.trace.reason ?? "No current wave evidence"}</p><p>Cash {battle?.decision?.battle_cash ?? member.battle_cash ?? "—"}</p></section>
            <section className="rounded-lg border border-border bg-background/40 p-2"><h4 className="font-medium">Gems · Labs</h4>
              {resources ? <><p>Gems: {resources.gem_step.action.replaceAll("_", " ")} · {resources.gem_step.status}</p><p>Labs: {resources.lab_step.action.replaceAll("_", " ")} · {resources.lab_step.status}</p></> : <p>Path unknown</p>}
              <p>Gems {member.wallet_gems ?? "—"}</p></section>
          </div>
          {showHistory && <section aria-label={`Battle evidence for ${member.name}`} className="space-y-1">
            <h4 className="text-sm font-medium">Battle evidence</h4>
            <p className="text-xs text-muted-foreground">Confirmed purchases below describe the latest recorded run.</p>
            <WorkerBattlePurchases accountKey={member.account_id ? member.account_key : null} expectedAccountId={member.account_id ?? undefined} />
          </section>}
          {member.account_key && member.account_id && <Link className="inline-block text-xs text-primary underline" href={`/fleet/reroll/?worker=${encodeURIComponent(member.name)}&account=${encodeURIComponent(member.account_key)}&identity=${encodeURIComponent(member.account_id)}`}>Inspect {member.name}</Link>}
        </article>;
      })}
    </div>
    <DecisionInspector selection={selection} onClose={() => setSelection(null)} />
  </section>;
}
