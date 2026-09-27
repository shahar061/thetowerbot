"use client";

import Link from "next/link";
import { useState } from "react";
import { MonitorOff } from "lucide-react";
import { Meter } from "@/components/Meter";
import { StatusBadge } from "@/components/StatusBadge";
import type { AccountChoice } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import type { LabsSnapshot } from "@/lib/labs";
import { duration } from "@/lib/labs";
import { decisionFor, deviceColor, LADDER, ladderProgress, standingFor } from "@/lib/rerollState";
import { overviewEvidence } from "./fleetOverview";
import { freshFleetEvidence } from "./FleetOverviewSummary";
import { FleetCapture } from "./FleetCapture";

export function verifiedWorkerAccount(member: RerollMember, account?: AccountChoice): account is AccountChoice {
  return !!account?.running && !!account.dashboard_url && !!member.account_id
    && account.account_id === member.account_id && account.instance === member.name
    && (!member.account_key || account.key === member.account_key);
}

function age(at?: number | null): string {
  if (at == null || !Number.isFinite(at)) return "Observation time unknown";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - at));
  return `Observed ${seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`} ago`;
}

/** Operator-facing meaning of a recovery blocker; unknown codes are shown as-is. */
const RECOVERY_HINTS: Record<string, string> = {
  unresolved: "An action's outcome is unknown; reconcile it with tools/reconcile_recovery.py",
  budget_exhausted: "Recovery call budget is used up",
  unsupported_model: "The configured model cannot take this request",
  image_unavailable: "No screen image could be prepared for the model",
  no_candidates: "No verified control to act on; no model call was made",
  credentials_missing: "Recovery key is not configured",
  shutdown_failed: "The previous recovery service did not shut down",
  action_denied: "The recovery allowance refused another action",
  storage_unavailable: "Recovery storage is unavailable",
};

export function recoveryBlockerText(blocker: string | null | undefined): string | null {
  if (!blocker) return null;
  return RECOVERY_HINTS[blocker] ? `${blocker}: ${RECOVERY_HINTS[blocker]}` : blocker;
}

function microusd(value: number | null | undefined): string {
  return value === null || value === undefined ? "unknown" : `$${(value / 1_000_000).toFixed(4)}`;
}

function value(number: number | null | undefined, suffix = ""): string {
  return number == null ? "Unknown" : `${number.toLocaleString()}${suffix}`;
}

const healthLabels = { progressing: "Progressing", waiting: "Expected wait", recovering: "Recovering",
  attention: "Needs attention", stopped: "Stopped", unknown: "Health unknown" } as const;

export function FleetLiveCard({ member, account, labsSnapshot = null, onInspect, actions }: {
  member: RerollMember; account?: AccountChoice; labsSnapshot?: LabsSnapshot | null;
  onInspect: () => void; actions?: React.ReactNode;
}): React.JSX.Element {
  const [screenOpen, setScreenOpen] = useState(false);
  const { overview: observedOverview, labs, appliedLabPlan, workshop, battle, resource } = overviewEvidence(member, labsSnapshot);
  const overview = observedOverview && freshFleetEvidence(observedOverview.observed_at) ? observedOverview : null;
  const staleOverview = observedOverview && !overview ? observedOverview : null;
  const standing = standingFor(member.state, member.error);
  const plan = member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
  const progress = ladderProgress(member.best_tier_1_wave);
  const target = LADDER[progress.rung];
  const query = new URLSearchParams({ worker: member.name, account: member.account_key ?? "", identity: member.account_id ?? "" });
  const health = overview?.health.state ?? "unknown";
  const statusTone = health === "progressing" ? "live" : health === "attention" || health === "recovering" ? "warn" : "idle";
  const blockers = overview?.blockers ?? [];
  // Only a fresh, scope-matched overview carries recovery status; otherwise unknown.
  const recovery = overview?.recovery ?? null;
  const slots = labs?.plan?.slots ?? [];
  const labsCurrent = labs?.freshness === "observed" && freshFleetEvidence(labs.read_at);
  const currentSlots = labsCurrent ? slots.filter(slot => slot.now.evidence_status === "current" && !slot.now.stale && freshFleetEvidence(slot.now.read_at)) : [];
  const owned = currentSlots.filter(slot => slot.now.owned === true);
  const unverifiedSlots = slots.length - currentSlots.length;
  const lastObservedOwned = slots.filter(slot => slot.now.owned === true).length;
  const nextSlot = slots.find(slot => slot.next) ?? null;
  const planCurrent = labsCurrent && freshFleetEvidence(appliedLabPlan?.evaluated_at);
  const soonest = currentSlots.filter(slot => slot.now.state === "researching" && slot.now.completes_at != null)
    .sort((a, b) => a.now.completes_at! - b.now.completes_at!)[0] ?? null;
  const gameSpeed = currentSlots.find(slot => slot.now.research_id === "labs.game-speed") ?? null;
  const current = health === "stopped" || !freshFleetEvidence(overview?.current_run?.observed_at) ? null : overview?.current_run;
  const last = observedOverview?.last_completed_run;
  const matchedBattle = overview && health !== "stopped" && freshFleetEvidence(battle?.evidence_at) && battle?.decision?.account_id === member.account_id ? battle : null;
  const matchedWorkshop = overview && health !== "stopped" && freshFleetEvidence(workshop?.evidence_at) && workshop?.decision?.account_id === member.account_id ? workshop : null;
  const matchedResource = overview && freshFleetEvidence(resource?.observed_at) ? resource : null;
  const nextAction = health === "stopped" ? "Worker stopped; no active action"
    : staleOverview ? "Next action not verified; latest worker observation stale"
    : matchedBattle?.decision?.reason ?? matchedWorkshop?.decision?.reason
      ?? matchedResource?.lab_step.reason ?? matchedResource?.gem_step.reason ?? "Next action not verified";
  const evaluatedWorkshop = matchedWorkshop?.decision ?? null;
  const workshopItem = matchedWorkshop ? evaluatedWorkshop?.item : plan?.item;
  const wallet = matchedWorkshop ? evaluatedWorkshop?.wallet_coins ?? null : plan?.wallet_coins ?? null;
  const price = matchedWorkshop ? evaluatedWorkshop?.price ?? null : plan?.price ?? null;
  const workshopState = matchedWorkshop ? evaluatedWorkshop?.state : plan?.state;
  const decision = workshopState ? decisionFor(workshopState) : null;
  const measurable = wallet !== null && price !== null && price > 0;
  const strategy = observedOverview?.strategy;
  const latest = labs?.recent?.[0] ?? null;
  const lastAction = latest ? `${latest.item ?? latest.kind} · ${age(latest.at)}` : "Unknown";

  return <article aria-label={`${member.name} live overview`} className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-card shadow-sm">
    <div className="h-1" style={{ backgroundColor: deviceColor(member.name) }} />
    <div className="flex flex-1 flex-col gap-4 p-3 sm:p-4">
      <header className="space-y-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-heading text-lg font-semibold">{member.name}</h3>
          <StatusBadge state={statusTone}>{healthLabels[health]}</StatusBadge>
        </div>
        <p className="break-words text-xs text-muted-foreground">{member.account_id ? `Account ${member.account_id}` : "Account identity unknown"} · {age(observedOverview?.observed_at)}</p>
        <p className="text-xs text-muted-foreground">Worker state: <span>{standing.label}</span></p>
        <p className="text-sm text-muted-foreground">{staleOverview
          ? `Last health report: ${healthLabels[staleOverview.health.state]} · ${age(staleOverview.observed_at)}; current health unknown`
          : overview?.health.reason ?? (health === "unknown" ? "Semantic health has not been verified" : standing.hint)}</p>
      </header>

      <section aria-label={`${member.name} objective and next action`} className="rounded-lg border bg-well/50 p-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Objective &amp; next action</h4>
        <p className="mt-1 text-sm font-medium">{plan?.goal ?? "Current objective unknown"}</p>
        {plan && <p className="text-xs text-muted-foreground">Planned objective · {age(plan.observed_at)}</p>}
        <p className="mt-1 text-sm">{nextAction}</p>
        <p className="mt-1 text-xs text-muted-foreground">Last confirmed action: {lastAction}</p>
        {blockers.length > 0 && <p className="mt-2 text-sm text-warn">Blocked: {blockers.join(" · ")}</p>}
        {staleOverview && staleOverview.blockers.length > 0 && <p className="mt-2 text-xs text-muted-foreground">Last observed blockers: {staleOverview.blockers.join(" · ")}</p>}
      </section>

      <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
        <section aria-label={`${member.name} current run`} className="min-w-0 rounded-lg border p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Current run</h4>
          {current ? <><p className="mt-1 text-sm font-semibold">{current.tier == null ? "Tier unknown" : `T${current.tier}`} {current.wave == null ? "Wave unknown" : `W${current.wave}`}</p>
            <p className="text-xs text-muted-foreground">Speed {value(current.speed)} · Coins {value(current.coins)} · Coins/hour unknown</p>
            <p className="text-xs text-muted-foreground">{age(current.observed_at)}</p></>
            : <><p className="mt-1 text-sm text-muted-foreground">No verified current run</p>
              {staleOverview?.current_run && <p className="mt-1 text-xs text-muted-foreground">Last observed open run: {staleOverview.current_run.tier == null ? "Tier unknown" : `T${staleOverview.current_run.tier}`} {staleOverview.current_run.wave == null ? "Wave unknown" : `W${staleOverview.current_run.wave}`} · {age(staleOverview.current_run.observed_at)}</p>}</>}
        </section>
        <section aria-label={`${member.name} last completed run`} className="min-w-0 rounded-lg border p-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Last completed run</h4>
          {last ? <><p className="mt-1 text-sm font-semibold">{last.tier == null ? "Tier unknown" : `T${last.tier}`} {last.wave == null ? "Wave unknown" : `W${last.wave}`}</p>
            <p className="text-xs text-muted-foreground">Coins {value(last.coins)} · {age(last.ended_at)}</p></>
            : <p className="mt-1 text-sm text-muted-foreground">No completed run observed</p>}
        </section>
      </div>

      <section aria-label={`${member.name} labs and gems`} className="border-t pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Labs &amp; gems</h4>
          <Link href="/fleet/reroll/labs/" className="rounded-md px-2 py-2 text-sm font-medium text-primary hover:underline">View labs →</Link></div>
        <p className="text-sm">{labs ? labsCurrent ? `${owned.length} currently verified owned · ${labs.unknown_slots ?? "Unknown"} slots unknown${unverifiedSlots ? ` · ${unverifiedSlots} slot observation${unverifiedSlots === 1 ? "" : "s"} unverified` : ""}`
          : `Slot ownership unverified · Last observed ${lastObservedOwned} owned · ${labs.unknown_slots ?? "Unknown"} slots unknown` : "Slot ownership unknown"}</p>
        <p className="text-sm text-muted-foreground">{soonest ? `Soonest expected completion: Slot ${soonest.slot} ${soonest.now.research_name ?? "research unread"} · ${soonest.now.completes_at! <= Date.now() / 1000 ? "past expected time; awaiting confirmation" : duration(soonest.now.completes_at! - Date.now() / 1000)}` : "Lab completion unknown"}</p>
        <p className="text-sm text-muted-foreground">{nextSlot?.next ? `Next queued research: ${nextSlot.next.name} · ${planCurrent && currentSlots.some(slot => slot.slot === nextSlot.slot) ? "applied plan" : "planning only"}` : "Next queued research unknown"}</p>
        {gameSpeed?.now.overdue_seconds != null && gameSpeed.now.overdue_seconds > 0 && <p className="text-sm text-warn">Game Speed completion due · {gameSpeed.why[0] ?? "awaiting confirmation"}</p>}
        <p className="text-sm text-muted-foreground">Gems {value(overview?.currency.gems)} · Next target {planCurrent ? appliedLabPlan?.gems.next?.label ?? "Unknown" : "Unknown"}</p>
        {labs && <p className="text-xs text-muted-foreground">Lab evidence {labsCurrent ? "current" : labs.freshness === "observed" ? "stale" : labs.freshness ?? "unknown"}{labs.reason ? ` · ${labs.reason}` : ""}</p>}
      </section>

      <section aria-label={`${member.name} workshop decision`} className="border-t pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Next workshop</h4>
          <span className="text-xs text-muted-foreground">{decision?.label ?? "Unknown"}</span></div>
        <p className="mt-1 text-sm font-semibold">{workshopItem ?? "Decision not observed"}</p>
        <Meter className="mt-2 h-1" label={`Workshop affordability for ${member.name}`} value={wallet ?? 0} max={price ?? 1} unknown={!measurable} tone="primary" />
        <div className="mt-1 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{wallet === null ? "Balance not observed" : `${wallet.toLocaleString()} coins`}</span>
          <span>{price === null ? "Price not observed" : `${price.toLocaleString()} price`}</span></div>
        <p className="mt-1 text-sm text-muted-foreground">{matchedWorkshop ? evaluatedWorkshop?.reason ?? "Evaluation reason unavailable" : measurable ? wallet >= price ? "Affordable in last plan · awaiting verified execution" : `${(price - wallet).toLocaleString()} coins short` : plan?.reason ?? "Waiting for account-bound observation"}</p>
        {matchedWorkshop ? <p className="text-xs text-muted-foreground">Route evaluation · {matchedWorkshop.status} · {age(matchedWorkshop.evidence_at)} · Price {matchedWorkshop.trace.price_source}</p>
          : plan && <p className="text-xs text-muted-foreground">Plan snapshot · {age(plan.observed_at)} · Price {plan.price_source ?? "quality unknown"}</p>}
        <p className="text-xs text-muted-foreground">Available after reserves: {value(overview?.currency.available_lower, " coins")} · Recorded buys: {value(member.workshop_upgrades_bought)}</p>
        {member.account_id && member.recent_workshop_purchases?.[0] && <p className="text-xs text-muted-foreground">Recent confirmed purchase: {member.recent_workshop_purchases[0].item} · {age(member.recent_workshop_purchases[0].at)}</p>}
        <Link href={`/fleet/reroll/workshop/?worker=${encodeURIComponent(member.name)}`} className="inline-flex min-h-11 items-center rounded-md px-2 text-sm font-medium text-primary hover:underline">View workshop</Link>
      </section>

      <section aria-label={`${member.name} battle decision`} className="border-t pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Battle decision</h4>
        <p className="mt-1 text-sm">{matchedBattle?.decision?.reason ?? "Live intent unavailable"}</p>
        {matchedBattle && <p className="text-xs text-muted-foreground">{matchedBattle.status} · {age(matchedBattle.evidence_at)}</p>}
      </section>

      <section aria-label={`${member.name} missions and strategy`} className="border-t pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Missions &amp; strategy</h4>
        <p className="mt-1 text-sm">Missions {overview?.missions.state ?? "unknown"}{overview?.missions.reason ? ` · ${overview.missions.reason}` : ""}</p>
        {staleOverview && <p className="text-xs text-muted-foreground">Last observed mission state: {staleOverview.missions.state} · {age(staleOverview.observed_at)}</p>}
        <p className="text-xs text-muted-foreground">Last claim {observedOverview?.missions.last_claim_at ? age(observedOverview.missions.last_claim_at) : "unknown"}</p>
        <Link href={`/fleet/reroll/strategies/#${encodeURIComponent(`strategy-${member.name}-${member.account_id}`)}`}
          className="mt-2 inline-flex min-h-11 items-center rounded-md bg-primary/10 px-3 text-sm font-medium text-primary">
          {strategy ? `${overview ? "" : "Last observed: "}${strategy.name} · v${strategy.version}` : "Assigned strategy unknown"}</Link>
        <p className="text-xs text-muted-foreground">Applied route revision {value(member.route_revision_applied)}</p>
      </section>

      <section aria-label={`${member.name} recovery`} className="border-t pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recovery</h4>
        {recovery ? <>
          <p className="mt-1 text-sm">{recovery.mode} · {recovery.phase}{recovery.last_outcome ? ` · last ${recovery.last_outcome}` : ""}</p>
          {recoveryBlockerText(recovery.blocker) && <p role="status" className="mt-1 break-words rounded-md bg-warn-surface p-2 text-sm text-warn">Blocked: {recoveryBlockerText(recovery.blocker)}</p>}
          <p className="text-xs text-muted-foreground">Cost used {microusd(recovery.cost_used_microusd)} · reserved {microusd(recovery.cost_reserved_microusd)}{recovery.observed_at ? ` · ${age(recovery.observed_at)}` : ""}</p>
        </> : <p className="mt-1 text-sm text-muted-foreground">Recovery status unknown</p>}
      </section>

      <section className="border-t pt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Next milestone</h4>
        <Link href={`/fleet/reroll/progression/?${query}`} className="mt-1 inline-flex min-h-11 items-center text-sm font-medium text-primary hover:underline">{target.goal}</Link>
        <Meter className="mt-2 h-1" label={`Reroll progress for ${member.name}`} value={progress.percent} max={100} unknown={progress.wave === null} tone="primary" />
        <p className="mt-1 text-xs text-muted-foreground">{progress.wave === null ? "No verified run yet" : progress.remaining === null ? `T1 W${progress.wave} · operator choice` : `T1 W${progress.wave} · ${progress.remaining} to go`}</p>
      </section>

      {standing.needsYou && <div className="rounded-md border border-warn/30 bg-warn-surface p-3 text-sm text-warn"><p>{standing.hint}</p>{member.uw_result && <p className="mt-1">{member.uw_result}</p>}{member.error && <p className="mt-1 break-words">{member.error}</p>}</div>}
      {member.leave_error && <p className="rounded-md bg-warn-surface p-3 text-sm text-warn">Couldn&apos;t stop this bot when the reroll changed: {member.leave_error}</p>}

      <details className="border-t pt-3"><summary className="flex min-h-11 cursor-pointer items-center text-sm font-medium">Recent activity</summary>
        <p className="text-sm text-muted-foreground">{latest ? `${latest.item ?? latest.kind} · ${age(latest.at)}${latest.reason ? ` · ${latest.reason}` : ""}` : "No account-bound activity observed"}</p>
        <Link href="/fleet/reroll/history/" className="inline-flex min-h-11 items-center text-sm text-primary hover:underline">View activity →</Link>
      </details>
      <section className="border-t pt-3"><button type="button" aria-expanded={screenOpen} aria-label={`${screenOpen ? "Hide" : "Show"} screen of ${member.name}`}
        onClick={() => setScreenOpen(open => !open)} className="min-h-11 rounded-md px-2 text-sm font-medium text-primary hover:underline">{screenOpen ? "Hide" : "Show"} device screen</button>
        {screenOpen && (verifiedWorkerAccount(member, account)
          ? <FleetCapture key={`${account.key}:${account.account_id}:${account.dashboard_url}:${member.lease_id}`} dashboardUrl={account.dashboard_url!} scope={account.key} instance={member.name} accountId={member.account_id!} />
          : <div role="status" className="flex min-h-48 flex-col items-center justify-center gap-3 rounded-lg border bg-well p-5 text-center text-sm text-muted-foreground"><MonitorOff className="size-6" aria-hidden="true" />Verified live account unavailable</div>)}
      </section>
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t pt-3">
        <span className="text-xs text-muted-foreground">{age(observedOverview?.observed_at)}</span>
        <button type="button" aria-label={`Inspect ${member.name}`} onClick={onInspect} className="min-h-11 rounded-md px-2 text-sm font-medium text-primary hover:underline">Inspect →</button>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
    </div>
  </article>;
}
