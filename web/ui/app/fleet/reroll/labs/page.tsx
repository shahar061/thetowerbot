"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useFleetLabs } from "../FleetLabsContext";
import { useRerollWorkspace } from "../RerollWorkspace";
import { overviewEvidence } from "../fleetOverview";
import { LabsMatrix } from "./LabsMatrix";

export default function FleetLabsPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading labs…</p>}><LabsContent /></Suspense>;
}

function LabsContent(): React.JSX.Element {
  const focus = useSearchParams()?.get("worker") ?? null;
  const { snapshot, error, loading } = useFleetLabs();
  const { pool, error: fleetError } = useRerollWorkspace();
  const rows = pool && snapshot ? pool.members.flatMap(member => {
    const row = overviewEvidence(member, snapshot).labs;
    return row ? [row] : [];
  }) : [];
  return <main className="space-y-6">
    <header><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / labs &amp; gems</p>
      <h1 className="text-3xl font-semibold tracking-tight">Every lab slot, every emulator.</h1>
      <p className="mt-2 text-sm text-muted-foreground">What each slot is doing, the next lab it plans with its price, and the next gem step. Only Game Speed in Lab 1 and the Lab 2 unlock are automated today.</p></header>
    {error && <p role="alert" className="text-danger">Labs unavailable: {error}</p>}
    {fleetError && <p role="alert" className="text-danger">Fleet unavailable: {fleetError}</p>}
    {loading && !snapshot ? <p role="status">Loading labs…</p>
      : snapshot && !rows.length ? <p className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">No account-matched lab observations available.</p>
      : snapshot && <LabsMatrix rows={rows} reference={snapshot.reference} focus={focus} />}
  </main>;
}
