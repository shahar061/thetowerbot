"use client";

import { WorkshopViews } from "@/app/fleet/reroll/workshop/WorkshopViews";
import { useAccountSelection } from "@/lib/AccountSelection";
import type { RerollMember } from "@/lib/fleet";

export default function WorkshopPage(): React.JSX.Element {
  const { selected } = useAccountSelection();
  const name = selected?.instance ?? selected?.key ?? "Selected emulator";
  const member: RerollMember | null = selected ? {
    name, account_key: selected.key, account_id: selected.account_id,
    endpoint: "", lease_id: selected.key, state: selected.running ? "running" : "stopped",
  } : null;
  return <main className="space-y-6">
    <header>
      <p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Single emulator / workshop</p>
      <h1 className="text-3xl font-semibold tracking-tight">Workshop</h1>
      <p className="mt-2 text-sm text-muted-foreground">Upgrade levels, next-level coin prices, recent purchases and the Workshop plan for {name}.</p>
    </header>
    {member ? <WorkshopViews key={`${selected!.key}:${selected!.account_id}`} members={[member]} focusWorker={name} />
      : <p role="status" className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">Select an emulator to view its Workshop.</p>}
  </main>;
}
