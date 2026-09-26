"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { fetchFleetLabs } from "@/lib/api";
import type { LabsSnapshot } from "@/lib/labs";
import { LabsMatrix } from "./LabsMatrix";

export default function FleetLabsPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading labs…</p>}><LabsContent /></Suspense>;
}

function LabsContent(): React.JSX.Element {
  const focus = useSearchParams()?.get("worker") ?? null;
  const [snapshot, setSnapshot] = useState<LabsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    const load = (): void => { fetchFleetLabs().then(next => { if (active) { setSnapshot(next); setError(null); } },
      failure => { if (active) setError(failure instanceof Error ? failure.message : "Labs unavailable"); }); };
    load();
    const timer = window.setInterval(load, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return <main className="space-y-6">
    <header><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / labs &amp; gems</p>
      <h1 className="text-3xl font-semibold tracking-tight">Every lab slot, every emulator.</h1>
      <p className="mt-2 text-sm text-muted-foreground">What each slot is doing, the next lab it plans with its price, and the next gem step. Only Game Speed in Lab 1 and the Lab 2 unlock are automated today.</p></header>
    {error && <p role="alert" className="text-danger">Labs unavailable: {error}</p>}
    {!snapshot && !error ? <p role="status">Loading labs…</p>
      : snapshot && !snapshot.workers.length ? <p className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">No visible emulators in this fleet.</p>
      : snapshot && <LabsMatrix rows={snapshot.workers} reference={snapshot.reference} focus={focus} />}
  </main>;
}
