"use client";

import Link from "next/link";
import { useState } from "react";
import { deviceColor } from "@/lib/rerollState";
import type { RerollMember } from "@/lib/fleet";
import type { StrategyAssignment } from "@/lib/strategyStudio";
import { WorkerBattlePurchases } from "../Purchases";
import { DecisionInspector, type DecisionSelection } from "./DecisionInspector";
import styles from "./routeCanvas.module.css";

function phase(stage: string): string {
  return stage.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function stateLabel(state: string | null): string {
  if (state === "observed") return "Observed";
  if (state === "projected") return "Projected";
  if (state === "blocked") return "Blocked";
  return "Unknown";
}

// Workers republish their plan only from the main menu, so a plan goes quiet
// for a whole run. Keep showing it, but say how old it is.
function planAge(observedAt: number | null | undefined): string | null {
  if (typeof observedAt !== "number") return null;
  const seconds = Math.floor(Date.now() / 1000 - observedAt);
  if (seconds < 120) return null;
  return seconds < 3600 ? `as of ${Math.floor(seconds / 60)}m ago` : `as of ${Math.floor(seconds / 3600)}h ago`;
}

// Mirrors resolve_route: an assignment applies only to the account it was
// made for; after an account change the worker runs the legacy defaults.
function strategyLabel(member: RerollMember, assignments: Record<string, StrategyAssignment> | undefined): string | null {
  if (!assignments) return null;
  const assignment = assignments[member.name];
  if (!assignment) return "Fleet baseline";
  const name = `${assignment.strategy_name} v${assignment.strategy_version}`;
  return assignment.account_id === member.account_id ? name : `${name} · inactive, account changed`;
}

function boughtAgo(at: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - at) / 60));
  return minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
}

export function FleetRoutePreview({ members, savedRevision, assignments, showHistory = false }: {
  members: RerollMember[]; savedRevision: number | null; assignments?: Record<string, StrategyAssignment>; showHistory?: boolean;
}): React.JSX.Element {
  const [scope, setScope] = useState("fleet");
  const [selection, setSelection] = useState<DecisionSelection | null>(null);
  const visible = members.filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  const shown = scope === "fleet" ? visible : visible.filter(member => member.name === scope);
  const ready = visible.filter(member => {
    const evaluation = member.account_id && member.workshop_evaluation?.account_id === member.account_id ? member.workshop_evaluation : null;
    const plan = member.account_id && member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
    return (evaluation?.decision?.state ?? plan?.state) === "buy";
  }).length;
  const waiting = visible.filter(member => !member.account_id ||
    (member.workshop_evaluation?.account_id !== member.account_id && member.reroll_plan?.account_id !== member.account_id)).length;

  return <section aria-label="Fleet Build Route" className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card px-4 py-3">
      <div>
        <h2 className="font-heading font-semibold">Fleet purchase roads</h2>
        <p className="text-xs text-muted-foreground">The first tile is the current choice. Later tiles are milestone projections; their future price and level are not yet known.</p>
      </div>
      <label className="flex items-center gap-2 text-sm">Emulator scope
        <select aria-label="Emulator scope" className="rounded-md border border-border bg-background px-2 py-1" value={scope} onChange={event => setScope(event.target.value)}>
          <option value="fleet">Entire fleet</option>
          {visible.map(member => <option key={member.name} value={member.name}>{member.name}</option>)}
        </select>
      </label>
    </div>
    <div className="flex flex-wrap gap-2 text-xs" aria-label="Fleet road summary">
      <span className="rounded-full border border-border bg-card px-3 py-1">{visible.length} active emulators</span>
      <span className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1">{ready} ready to buy</span>
      <span className="rounded-full border border-border bg-card px-3 py-1">{waiting} waiting for account evidence</span>
    </div>
    {!visible.some(member => member.battle_evaluation?.status === "observed" &&
      member.battle_evaluation.account_id === member.account_id) &&
      <p className="text-xs text-muted-foreground">Live battle intent unavailable</p>}
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
      {shown.map(member => {
        const plan = member.account_id && member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
        const evaluation = member.workshop_evaluation?.account_id === member.account_id ? member.workshop_evaluation ?? null : null;
        const item = evaluation?.decision?.item ?? plan?.item ?? null;
        const inspectingPrices = (evaluation?.decision?.state ?? plan?.state) === "observe_price";
        const upgradeId = evaluation?.decision?.upgrade_id ?? plan?.upgrade_id ?? null;
        const confirmed = plan?.confirmed_purchases && upgradeId ? plan.confirmed_purchases[upgradeId] ?? 0 : null;
        const price = evaluation?.decision?.price ?? plan?.price ?? null;
        const priceSource = evaluation?.trace.price_source ?? plan?.price_source ?? null;
        const wallet = evaluation?.decision?.wallet_coins ?? plan?.wallet_coins ?? null;
        const reason = evaluation?.trace.reason ?? plan?.reason ?? "Waiting for verified Workshop observations.";
        const age = evaluation ? null : planAge(plan?.observed_at);
        const status = [stateLabel(evaluation?.status ?? (plan ? "projected" : null)), age].filter(Boolean).join(" · ");
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
            {strategyLabel(member, assignments) !== null && <p className="text-sm font-medium">Strategy: {strategyLabel(member, assignments)}</p>}
            <p className="text-xs text-muted-foreground">Variant: {member.variant_name ?? member.variant ?? "Not assigned"}</p>
            <p className="text-xs text-muted-foreground">Phase: {plan ? phase(plan.stage) : "Unknown"}</p>
            <p className="text-xs" aria-label={`Route status for ${member.name}`}>{revisionState}</p>
          </header>
          <section aria-label={`Workshop policy for ${member.name}`} className="space-y-2">
            <div className="flex items-center justify-between gap-2"><h4 className="text-sm font-semibold">Workshop policy</h4><span className="text-xs text-muted-foreground">{plan || evaluation ? status : "Unknown"}</span></div>
            {plan || evaluation ? <>
              <div className={styles.road}>
                <div className={`${styles.roadStep} rounded-xl border border-primary/40 bg-primary/10 p-3`}>
                  <span className="text-[10px] font-bold uppercase tracking-widest text-primary">{inspectingPrices ? "Verify Workshop prices" : "Next selected buy"}</span>
                  <p className="font-heading text-lg font-semibold">{item ?? "Waiting"}</p>
                  {inspectingPrices ? <><p className="text-xs">No spending during this inspection</p><p className="text-xs text-muted-foreground">{reason}</p></> : <>
                  <p className="font-mono text-xs">{wallet !== null && price !== null ? `${wallet} / ${price} coins` : "Price or wallet unknown"}</p>
                  <p className="text-xs text-muted-foreground">{price !== null ? `${price} coins · ${priceSource === "observed" ? "Observed price" : priceSource === "catalog_estimate" ? "Estimated price; checked before buying" : "Price source unknown; checked before buying"}` : "Next price unknown"}</p>
                  <p className="text-xs text-muted-foreground">{confirmed !== null ? `Next level: at least ${confirmed + 1} · ${confirmed} recorded purchases` : "Next level unknown · purchase history unavailable"}</p>
                  </>}
                </div>
                {!!plan?.next_purchases?.length && <ol className="mt-3 space-y-2" aria-label="Projected Workshop milestones">
                  {plan.next_purchases.map(step => <li key={`${step.position}:${step.upgrade_id}`} className={`${styles.roadStep} rounded-lg border border-border bg-background/40 p-3 text-xs`}>
                    <span className="font-mono text-[10px] text-muted-foreground">PROJECTED MILESTONE {step.position}</span>
                    <p className="font-semibold">{step.item}</p><p className="text-muted-foreground">{step.focus} · Future price unknown · {plan.confirmed_purchases?.[step.upgrade_id] ? `Recorded level at least ${plan.confirmed_purchases[step.upgrade_id]}` : "Level unknown"}</p>
                  </li>)}
                </ol>}
                {plan?.projection_note && <p className="mt-3 text-xs text-muted-foreground">{plan.projection_note}</p>}
              </div>
              {item && <button type="button" onClick={() => setSelection({ worker: member.name, accountId: member.account_id!, item, reason,
                revision: savedRevision, observedAt: evaluation?.evidence_at ?? plan?.observed_at ?? null, evaluation })}
                className="rounded-md border border-primary/50 px-2 py-1 text-xs text-primary">Why {item} for {member.name}?</button>}
            </> : <p className="text-sm text-muted-foreground">Workshop plan unavailable for this account. Waiting for matching observations.</p>}
          </section>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <section className="rounded-lg border border-border bg-background/40 p-2"><h4 className="font-medium">Battle</h4><p>{battle?.status === "observed" ? battle.decision?.item ?? "Waiting" : "Unknown"}</p><p className="text-muted-foreground">{battle?.trace.phase_id ?? battle?.trace.reason ?? "No current wave evidence"}</p><p>Cash {battle?.decision?.battle_cash ?? member.battle_cash ?? "—"}</p></section>
            <section className="rounded-lg border border-border bg-background/40 p-2"><h4 className="font-medium">Gems · Labs</h4>
              {resources ? <><p>Gems: {resources.gem_step.action.replaceAll("_", " ")} · {resources.gem_step.status}</p><p>Labs: {resources.lab_step.action.replaceAll("_", " ")} · {resources.lab_step.status}</p></> : <p>Path unknown</p>}
              <p>Gems {member.wallet_gems ?? "—"}</p></section>
          </div>
          {!!member.recent_workshop_purchases?.length && <section className="space-y-1">
            <h4 className="text-sm font-medium">Recent Workshop buys</h4>
            <ol aria-label={`Recent Workshop buys for ${member.name}`} className="space-y-1">
              {member.recent_workshop_purchases.map(buy => <li key={`${buy.at}:${buy.item}`} className="rounded-lg border border-border bg-background/40 px-2 py-1 text-xs">
                <div className="flex justify-between gap-2"><span className="font-medium">{buy.item}</span><span className="text-muted-foreground">{boughtAgo(buy.at)}</span></div>
                <p className="text-muted-foreground">{`${buy.cost ?? "?"} coins · ${buy.reason ?? "Reason not recorded"}`}</p>
              </li>)}
            </ol>
          </section>}
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
