"use client";

import { useState } from "react";
import type { RehearsedLabRow, StarterRolloutRow } from "@/lib/labs";

const STAGE_LABEL: Record<StarterRolloutRow["stage"], string> = {
  dry_run: "Dry run", canary: "Canary", fleet: "Fleet", halted: "Halted",
};
const STATUS_LABEL: Record<RehearsedLabRow["status"], string> = {
  rehearsed: "Rehearsed", needs_review: "Price needs review", unfindable: "Not found in picker", missing: "Missed",
};
const base = (path: string): string => path.split("/").pop() ?? path;

/** Where starting research stands per slot, and which labs have been rehearsed. */
export function StarterRolloutPanel({ rows, labs, onResetSlot, onLab }: {
  rows: StarterRolloutRow[];
  labs: RehearsedLabRow[];
  onResetSlot: (key: string) => Promise<void>;
  onLab: (labId: string, action: "reset" | "accept") => Promise<void>;
}): React.JSX.Element {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async (id: string, work: () => Promise<void>): Promise<void> => {
    setBusy(id);
    setError(null);
    try {
      await work();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Action failed");
    } finally {
      setBusy(null);
    }
  };
  return <section aria-label="Lab starter rollout" className="rounded-xl border border-border p-4">
    <h2 className="text-lg font-semibold">Lab starter rollout</h2>
    <p className="mt-1 text-sm text-muted-foreground">Starting research is rehearsed per slot, proven once by a canary, then used by the fleet. Each lab is rehearsed once before it is started.</p>
    {error && <p role="alert" className="mt-2 text-danger">Action failed: {error}</p>}
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="text-left text-muted-foreground">
          <th className="py-1 pr-3">Slot</th><th className="pr-3">Stage</th><th className="pr-3">Canary</th>
          <th className="pr-3">Dry runs</th><th className="pr-3">Evidence</th><th><span className="sr-only">Action</span></th>
        </tr></thead>
        <tbody>{rows.map(row => {
          const slot = row.key.replace("start:", "");
          return <tr key={row.key} className="border-t border-border align-top">
            <td className="py-2 pr-3">Lab {slot}</td>
            <td className="pr-3">{STAGE_LABEL[row.stage]}{row.halted_reason && <p className="text-danger">{row.halted_reason}</p>}</td>
            <td className="pr-3">{row.canary_worker ?? "—"}</td>
            <td className="pr-3">{row.dry_runs}</td>
            <td className="pr-3">{row.evidence.map(path => <p key={path} className="font-mono text-xs">{base(path)}</p>)}</td>
            <td>{row.stage === "halted" && <button type="button" disabled={busy === row.key}
              className="rounded border border-border px-2 py-1" aria-label={`Reset Lab ${slot} starts`}
              onClick={() => run(row.key, () => onResetSlot(row.key))}>Reset</button>}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
    {labs.length > 0 && <div className="mt-4 overflow-x-auto">
      <h3 className="font-medium">Rehearsed labs</h3>
      <table className="mt-2 w-full text-sm">
        <thead><tr className="text-left text-muted-foreground">
          <th className="py-1 pr-3">Lab</th><th className="pr-3">Status</th><th className="pr-3">Level</th>
          <th className="pr-3">Price seen</th><th className="pr-3">Catalog</th><th className="pr-3">Duration</th>
          <th><span className="sr-only">Action</span></th>
        </tr></thead>
        <tbody>{labs.map(lab => <tr key={lab.lab_id} className="border-t border-border align-top">
          <td className="py-2 pr-3">{lab.name}</td>
          <td className={`pr-3 ${lab.status === "rehearsed" ? "" : "text-danger"}`}>{STATUS_LABEL[lab.status]}{lab.misses > 0 && ` · ${lab.misses} misses`}</td>
          <td className="pr-3">{lab.level ?? "—"}</td>
          <td className={`pr-3 ${lab.mismatch ? "text-danger" : ""}`}>{lab.price ?? "—"}</td>
          <td className="pr-3">{lab.catalog_price ?? "none"}</td>
          <td className="pr-3">{lab.seconds !== null ? `${Math.round(lab.seconds)}s` : "—"}</td>
          <td>{(lab.status === "unfindable" || lab.status === "missing") && <button type="button"
              disabled={busy === lab.lab_id} className="rounded border border-border px-2 py-1"
              aria-label={`Search again for ${lab.name}`}
              onClick={() => run(lab.lab_id, () => onLab(lab.lab_id, "reset"))}>Search again</button>}
            {lab.status === "needs_review" && <button type="button" disabled={busy === lab.lab_id}
              className="rounded border border-border px-2 py-1" aria-label={`Accept price for ${lab.name}`}
              onClick={() => run(lab.lab_id, () => onLab(lab.lab_id, "accept"))}>Accept</button>}</td>
        </tr>)}</tbody>
      </table>
    </div>}
  </section>;
}
