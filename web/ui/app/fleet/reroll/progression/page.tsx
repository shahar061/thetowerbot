"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { fetchAccountRoadmap } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { useRerollWorkspace } from "../RerollWorkspace";
import { loadFleetRoadmaps, type WorkerRoadmap } from "./fleetRoadmap";
import { ProgressionAtlas } from "./ProgressionAtlas";

export default function ProgressionPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading progression atlas…</p>}><ProgressionContent /></Suspense>;
}

function ProgressionContent(): React.JSX.Element {
  const search = useSearchParams();
  const { pool, loading, error } = useRerollWorkspace();
  const members = useMemo(() => (pool?.members ?? []).filter((member) => !member.hidden), [pool]);
  const linkedWorker = members.find((member) => member.name === search.get("worker") && !!member.account_key && member.account_key === search.get("account") && !!member.account_id && member.account_id === search.get("identity"))?.name ?? null;
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const [result, setResult] = useState<{ identity: string; workers: WorkerRoadmap[]; complete: boolean } | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const scoped = JSON.parse(identity) as RerollMember[];
    async function refresh(): Promise<void> {
      const workers = await loadFleetRoadmaps(scoped, fetchAccountRoadmap, (worker) => {
        if (!active) return;
        setResult((current) => ({ identity, complete: false, workers: [
          ...(current?.identity === identity ? current.workers.filter((entry) => entry.member.name !== worker.member.name) : []), worker,
        ] }));
      });
      if (!active) return;
      setResult({ identity, workers, complete: true });
      timer = setTimeout(() => void refresh(), 15000);
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [identity, revision]);
  const workers = members.map((member): WorkerRoadmap => {
    const current = result?.identity === identity ? result.workers.find((worker) => worker.member.name === member.name) : undefined;
    return { member, roadmap: current?.roadmap ?? null, error: current?.error ?? null };
  });
  const fetching = result?.identity !== identity || !result.complete;
  return <main className="space-y-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / progression</p>
        <h1 className="text-3xl font-semibold tracking-tight">The progression atlas</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">Trace every unlock. Compare the evidence. Find the next meaningful objective for your fleet.</p></div>
      <button className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50" disabled={fetching} onClick={() => setRevision((value) => value + 1)}>Refresh evidence</button>
    </header>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {loading && !pool ? <p role="status">Loading fleet…</p> : !members.length ? <div className="rounded-xl border border-dashed border-border p-10 text-center text-muted-foreground">No visible emulators in this reroll. Add workers in Fleet Live to begin the atlas.</div> : <>
      {fetching && <p role="status" className="text-sm text-muted-foreground">Reading account-scoped milestone evidence…</p>}
      <ProgressionAtlas key={`${identity}:${linkedWorker ?? "fleet"}`} workers={workers} pending={fetching} initialWorker={linkedWorker} />
    </>}
  </main>;
}
