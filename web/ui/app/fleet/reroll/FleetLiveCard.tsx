"use client";

import Link from "next/link";
import { MonitorOff } from "lucide-react";
import { Meter } from "@/components/Meter";
import { StatusBadge } from "@/components/StatusBadge";
import type { AccountChoice } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { decisionFor, deviceColor, LADDER, ladderProgress, standingFor } from "@/lib/rerollState";
import { FleetCapture } from "./FleetCapture";

export function verifiedWorkerAccount(member: RerollMember, account?: AccountChoice): account is AccountChoice {
  return !!account?.running && !!account.dashboard_url && !!member.account_id
    && account.account_id === member.account_id && account.instance === member.name
    && (!member.account_key || account.key === member.account_key);
}

function age(at?: number | null): string {
  if (at == null || !Number.isFinite(at)) return "Observation time unknown";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - at));
  return `Telemetry ${seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`} ago`;
}

export function FleetLiveCard({ member, account, onInspect, actions }: {
  member: RerollMember; account?: AccountChoice; onInspect: () => void; actions?: React.ReactNode;
}): React.JSX.Element {
  const standing = standingFor(member.state, member.error);
  const plan = member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
  const progress = ladderProgress(member.best_tier_1_wave);
  const target = LADDER[progress.rung];
  const wallet = plan?.wallet_coins ?? null;
  const price = plan?.price ?? null;
  const measurable = wallet !== null && price !== null && price > 0;
  const decision = plan ? decisionFor(plan.state) : null;
  const query = new URLSearchParams({ worker: member.name, account: member.account_key ?? "", identity: member.account_id ?? "" });
  return <article aria-label={`${member.name} live overview`} className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-card shadow-sm">
    <div className="h-0.5" style={{ backgroundColor: deviceColor(member.name) }} />
    <div className="flex flex-1 flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-heading text-base font-semibold">{member.name}</h3>
        <StatusBadge state={standing.tone}>{standing.label}</StatusBadge>
      </div>
      <p className="-mt-2 truncate font-mono text-[10px] text-faint-foreground">{member.account_id ? `Account ${member.account_id}` : "Account identity pending"}</p>
      {verifiedWorkerAccount(member, account)
        ? <FleetCapture key={`${account.key}:${account.account_id}:${account.dashboard_url}:${member.lease_id}`} dashboardUrl={account.dashboard_url!} scope={account.key} instance={member.name} accountId={member.account_id!} />
        : <div role="status" className="flex h-[min(30vh,18rem)] min-h-48 flex-col items-center justify-center gap-3 rounded-lg border bg-well p-5 text-center text-xs text-muted-foreground"><MonitorOff className="size-6" aria-hidden="true" />Verified live account unavailable</div>}
      <Link href={`/fleet/reroll/strategies/#${encodeURIComponent(`strategy-${member.name}-${member.account_id}`)}`} className="truncate rounded-md bg-primary/10 px-2.5 py-1.5 text-xs font-medium text-primary">
        {member.variant_name ?? member.variant ?? "Strategy not observed"}{plan?.stage ? ` · ${plan.stage}` : ""}
      </Link>
      <section aria-label={`${member.name} workshop decision`} className="min-h-24 border-t pt-3">
        <div className="flex items-center justify-between gap-1"><h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Next workshop</h4>{decision && <span className="text-[10px] text-muted-foreground">{decision.label}</span>}</div>
        <p className="mt-1 text-sm font-semibold">{plan?.item ?? "Decision not observed"}</p>
        <Meter className="mt-2 h-1" label={`Workshop affordability for ${member.name}`} value={wallet ?? 0} max={price ?? 1} unknown={!measurable} tone="primary" />
        <div className="mt-1 flex justify-between gap-2 font-mono text-[10px] text-muted-foreground">
          <span>{wallet === null ? "Balance not observed" : `${wallet.toLocaleString()} coins`}</span>
          <span>{price === null ? "Price not observed" : `${price.toLocaleString()} price`}</span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">{measurable ? wallet >= price ? "Affordable · awaiting verified execution" : `${(price - wallet).toLocaleString()} coins short` : plan?.reason ?? "Waiting for an account-bound observation"}</p>
      </section>
      <section className="min-h-16 border-t pt-3">
        <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Battle decision</h4>
        <p className="mt-1 text-xs">Live intent unavailable</p>
        <p className="mt-1 font-mono text-[10px] text-muted-foreground">{member.tier == null ? "Tier —" : `T${member.tier}`} · {member.wave == null ? "Wave —" : `W${member.wave}`} · {member.battle_cash == null ? "Cash —" : `${member.battle_cash.toLocaleString()} cash`}</p>
      </section>
      <section className="min-h-24 border-t pt-3">
        <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Next milestone</h4>
        <Link href={`/fleet/reroll/progression/?${query}`} className="mt-1 block text-xs font-medium hover:text-primary">{target.goal}</Link>
        <Meter className="mt-2 h-1" label={`Reroll progress for ${member.name}`} value={progress.percent} max={100} unknown={progress.wave === null} tone="primary" />
        <p className="mt-1 text-[10px] text-muted-foreground">{progress.wave === null ? "No verified run yet" : progress.remaining === null ? `T1 W${progress.wave} · operator choice` : `T1 W${progress.wave} · ${progress.remaining} to go`}</p>
        <p className="mt-1 text-[10px] text-muted-foreground">{member.workshop_upgrades_bought == null ? "Workshop purchase count unknown" : `${member.workshop_upgrades_bought.toLocaleString()} recorded workshop buys`}</p>
      </section>
      {standing.needsYou && <div className="rounded-md border border-warn/30 bg-warn-surface p-2 text-xs text-warn"><p>{standing.hint}</p>{member.uw_result && <p className="mt-1">{member.uw_result}</p>}{member.error && <p className="mt-1 break-words">{member.error}</p>}</div>}
      {member.leave_error && <p className="rounded-md bg-warn-surface p-2 text-xs text-warn">Couldn&apos;t stop this bot when the reroll changed: {member.leave_error}</p>}
      <div className="mt-auto flex items-center justify-between gap-2 border-t pt-2.5">
        <span className="text-[10px] text-faint-foreground">{age(member.observed_at)}</span>
        <button type="button" aria-label={`Inspect ${member.name}`} onClick={onInspect} className="text-xs font-medium text-primary hover:underline">Inspect →</button>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
    </div>
  </article>;
}
