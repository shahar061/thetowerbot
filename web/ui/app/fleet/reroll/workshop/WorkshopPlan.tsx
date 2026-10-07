"use client";

import { useEffect, useState } from "react";
import { fetchAccountWorkshopPlan } from "@/lib/api";
import type { WorkshopPlanResponse } from "@/lib/buildRoute";
import type { RerollMember } from "@/lib/fleet";
import { cn } from "@/lib/utils";
import { PlanGraph } from "./PlanGraph";
import { ago } from "./workshopFormat";

type Loaded = { worker: string; response: WorkshopPlanResponse | null; error: string | null };

export function WorkshopPlan({ members, focusWorker = null }: { members: RerollMember[]; focusWorker?: string | null }): React.JSX.Element {
  const [picked, setPicked] = useState<string | null>(focusWorker);
  const member = members.find(item => item.name === picked) ?? members[0] ?? null;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const identity = JSON.stringify(member ? { name: member.name, account_key: member.account_key, account_id: member.account_id } : null);

  useEffect(() => {
    const scoped = JSON.parse(identity) as { name: string; account_key?: string | null; account_id?: string | null } | null;
    if (!scoped) return;
    let active = true;
    const load = async (): Promise<void> => {
      if (!scoped.account_key || !scoped.account_id) {
        if (active) setLoaded({ worker: scoped.name, response: null, error: "Waiting for a verified account" });
        return;
      }
      try {
        const response = await fetchAccountWorkshopPlan(scoped.account_key, scoped.account_id);
        if (active) setLoaded({ worker: scoped.name, response, error: null });
      } catch (failure) {
        const error = failure instanceof Error ? failure.message : "Workshop plan unavailable";
        // Keep the last good graph for this emulator; say the refresh failed.
        if (active) setLoaded(current => ({ worker: scoped.name, error,
          response: current?.worker === scoped.name ? current.response : null }));
      }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [identity]);

  const current = member && loaded?.worker === member.name ? loaded : null;
  const plan = current?.response?.plan ?? null;
  return <div className="flex flex-col gap-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div role="group" aria-label="Emulator" className="inline-flex flex-wrap gap-0.5 rounded-md border border-border p-0.5">
        {members.map(item => <button key={item.name} type="button" aria-pressed={item.name === member?.name} onClick={() => setPicked(item.name)}
          className={cn("rounded px-2.5 py-1 text-xs", item.name === member?.name ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:text-foreground")}>
          {item.name}
        </button>)}
      </div>
      {plan && <p className="text-xs text-muted-foreground">
        Decided {current?.response?.age_seconds != null ? ago(current.response.age_seconds) : "at an unknown time"} · {plan.strategy.name} · route rev {plan.revision}
      </p>}
    </div>
    {current?.error && <p role="alert" className="text-sm text-danger">{current.error}</p>}
    {!member ? null
      : !current ? <p role="status">Loading Workshop plan…</p>
      : plan ? <PlanGraph plan={plan} stale={current.response?.stale ?? false} />
      : !current.error && <p className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">
          No Workshop decision recorded for {member.name} yet. It appears after its next Workshop visit.</p>}
  </div>;
}
