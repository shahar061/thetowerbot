"use client";

import { useState } from "react";
import type { UnlockRolloutRow } from "@/lib/labs";

const STAGE_LABEL: Record<UnlockRolloutRow["stage"], string> = {
  dry_run: "Dry run", canary: "Canary", fleet: "Fleet", halted: "Halted",
};

/** One row per lab slot 2-5: where the bot's own unlock stands. Reset only on a halted slot. */
export function UnlockRolloutPanel({ rows, onReset }: {
  rows: UnlockRolloutRow[];
  onReset: (slot: number) => Promise<void>;
}): React.JSX.Element {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reset = async (slot: number): Promise<void> => {
    setBusy(slot);
    setError(null);
    try {
      await onReset(slot);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Reset failed");
    } finally {
      setBusy(null);
    }
  };
  return <section aria-label="Lab slot rollout" className="rounded-xl border border-border p-4">
    <h2 className="text-lg font-semibold">Lab slot rollout</h2>
    <p className="mt-1 text-sm text-muted-foreground">Each slot unlock is rehearsed, proven once by a canary, then used by the fleet. A halted slot waits for you.</p>
    {error && <p role="alert" className="mt-2 text-danger">Reset failed: {error}</p>}
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="text-left text-muted-foreground">
          <th className="py-1 pr-3">Slot</th><th className="pr-3">Stage</th><th className="pr-3">Canary</th>
          <th className="pr-3">Dry runs</th><th className="pr-3">Evidence</th><th><span className="sr-only">Action</span></th>
        </tr></thead>
        <tbody>{rows.map(row => <tr key={row.slot} className="border-t border-border align-top">
          <td className="py-2 pr-3">Lab {row.slot}{row.price !== null ? ` · ${row.price} gems` : " · price unknown"}</td>
          <td className="pr-3">{STAGE_LABEL[row.stage]}{row.halted_reason && <p className="text-danger">{row.halted_reason}</p>}</td>
          <td className="pr-3">{row.canary_worker ?? "—"}</td>
          <td className="pr-3">{row.dry_runs}</td>
          <td className="pr-3">{row.evidence.length
            ? <ul>{row.evidence.map(path => <li key={path} className="font-mono text-xs">{path.split("/").pop()}</li>)}</ul>
            : "—"}</td>
          <td>{row.stage === "halted" && <button type="button" disabled={busy !== null}
            onClick={() => void reset(row.slot)} aria-label={`Reset Lab ${row.slot} rollout`}
            className="min-h-11 rounded-md border border-border px-3 text-xs font-medium">Reset</button>}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>;
}
