"use client";

import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { deviceColor } from "@/lib/rerollState";
import { PlanStrip } from "../PlanStrip";
import { WorkerBattlePurchases } from "../Purchases";
import { VariantComparison } from "../VariantComparison";
import { useRerollWorkspace } from "../RerollWorkspace";

function phase(stage: string): string {
  return stage.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

export default function StrategiesPage() {
  const { pool, loading, error } = useRerollWorkspace();
  const members = [...(pool?.members ?? [])].filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  return <div className="flex flex-col gap-5">
    <PageHeader title="Strategies" meta={`${members.length} emulators · effective reroll plans`} />
    <p className="text-sm text-muted-foreground">Compare each account’s current Workshop decision and projected route. Battle purchases are recorded evidence; live battle intent is unavailable.</p>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {loading && !pool ? <p role="status">Loading fleet strategies…</p> : !members.length ? <p className="text-muted-foreground">No visible emulators in this reroll.</p> : null}
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
      {members.map(member => {
        const plan = member.account_id && member.reroll_plan?.account_id === member.account_id ? member.reroll_plan : null;
        return <article id={`strategy-${member.name}-${member.account_id}`} key={`${member.name}:${member.account_key}:${member.account_id}`} aria-label={`Strategy for ${member.name}`} className="min-w-0 scroll-mt-4 space-y-4 rounded-xl border border-border bg-card p-4 target:ring-2 target:ring-primary">
          <header className="space-y-1 border-b border-border pb-3">
            <h2 className="font-heading text-base font-semibold" style={{ color: deviceColor(member.name) }}>{member.name}</h2>
            <p className="font-mono text-xs text-muted-foreground">Account {member.account_id ?? "not verified"}</p>
            <p className="text-sm">Variant: {member.variant_name ?? member.variant ?? "Not assigned"}</p>
            <p className="text-sm">Phase: {plan ? phase(plan.stage) : "Unknown"}</p>
          </header>
          <section aria-label={`Workshop policy for ${member.name}`} className="space-y-2">
            <h3 className="font-medium">Workshop policy</h3>
            {plan ? <><PlanStrip plan={plan} worker={member.name} /><p className="text-xs text-faint-foreground">Plan observed: {new Date(plan.observed_at * 1000).toLocaleString()}</p></>
              : <p className="text-sm text-muted-foreground">Workshop plan unavailable for this account. Waiting for matching observations.</p>}
          </section>
          <section aria-label={`Battle evidence for ${member.name}`} className="space-y-2">
            <h3 className="font-medium">Battle evidence</h3>
            <p className="text-sm text-muted-foreground">Live battle intent is unavailable for this worker. Confirmed purchases below describe its latest recorded run.</p>
            <WorkerBattlePurchases accountKey={member.account_id ? member.account_key : null} expectedAccountId={member.account_id ?? undefined} />
          </section>
          {member.account_key && member.account_id && <Link className="inline-block text-sm text-primary underline" href={`/fleet/reroll/?worker=${encodeURIComponent(member.name)}&account=${encodeURIComponent(member.account_key)}&identity=${encodeURIComponent(member.account_id)}`}>Inspect {member.name}</Link>}
        </article>;
      })}
    </div>
    {!!pool?.variant_comparison?.length && <details className="rounded-xl border border-border bg-card p-4">
      <summary className="cursor-pointer font-medium">Compare opening variants</summary>
      <p className="my-3 text-sm text-muted-foreground">Timing includes accounts that reached Tier 1 Wave 20. Reached counts show incomplete attempts; these samples do not establish a winning strategy.</p>
      <VariantComparison rows={pool.variant_comparison} />
    </details>}
  </div>;
}
