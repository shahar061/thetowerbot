"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { labStarterAction, resetLabStarterRollout, resetLabUnlockRollout } from "@/lib/api";
import { useFleetLabs } from "../FleetLabsContext";
import { useRerollWorkspace } from "../RerollWorkspace";
import { FleetExportReports } from "@/components/game-export/GameExportReports";
import { StarterRolloutPanel } from "./StarterRolloutPanel";
import { UnlockRolloutPanel } from "./UnlockRolloutPanel";

export default function FleetLabsPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading labs…</p>}><LabsContent /></Suspense>;
}

function LabsContent(): React.JSX.Element {
  const focus = useSearchParams()?.get("worker") ?? null;
  const { snapshot, error, refresh } = useFleetLabs();
  const { pool, error: fleetError } = useRerollWorkspace();
  const reset = async (slot: number): Promise<void> => { await resetLabUnlockRollout(slot); await refresh(); };
  const resetSlot = async (key: string): Promise<void> => { await resetLabStarterRollout(key); await refresh(); };
  const labAction = async (labId: string, action: "reset" | "accept"): Promise<void> => {
    await labStarterAction(labId, action); await refresh(); };
  return <main className="space-y-6">
    <header><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / labs &amp; gems</p>
      <h1 className="text-3xl font-semibold tracking-tight">Every lab slot, every emulator.</h1>
      <p className="mt-2 text-sm text-muted-foreground">Research levels and active queues come from the official game export. Research and slot unlock automation remain controlled by the rollouts below.</p></header>
    {snapshot?.unlock_rollout && <UnlockRolloutPanel rows={snapshot.unlock_rollout} onReset={reset} />}
    {snapshot?.starter_rollout && <StarterRolloutPanel rows={snapshot.starter_rollout}
      labs={snapshot.rehearsed_labs ?? []} onResetSlot={resetSlot} onLab={labAction} />}
    {error && <p role="alert" className="text-danger">Labs unavailable: {error}</p>}
    {fleetError && <p role="alert" className="text-danger">Fleet unavailable: {fleetError}</p>}
    <FleetExportReports section="labs" members={pool?.members.filter(member => !member.hidden) ?? []} focusWorker={focus} />
  </main>;
}
