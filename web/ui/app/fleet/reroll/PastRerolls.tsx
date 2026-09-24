"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listRerolls } from "@/lib/api";
import type { RerollRunSummary } from "@/lib/fleet";
import { RerollCard } from "./RerollCard";

const day = (value?: string | null) => value ? new Date(value).toLocaleDateString() : "—";

/** Read-only history. A closed reroll is never reopened; its accounts are
 *  reached through Archives, filtered to that run. */
export function PastRerolls({ refreshKey }: { refreshKey: number | undefined }) {
  const [runs, setRuns] = useState<RerollRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void listRerolls().then(value => { if (active) { setRuns(value.runs); setError(null); } })
      .catch((failure: Error) => { if (active) setError(failure.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refreshKey]);
  return <RerollCard title="Past rerolls">
    {loading ? <p role="status">Loading past rerolls…</p> : error ? <p role="alert" className="text-danger">Past rerolls unavailable: {error}</p> : !runs.length ? <p className="text-muted-foreground">No past rerolls recorded yet.</p> : <div className="overflow-x-auto">
    <table className="w-full text-left text-sm">
      <thead className="text-xs text-muted-foreground"><tr>
        <th className="py-1">Reroll</th><th>Started</th><th>Closed</th><th>Emulators</th><th>Left</th>
      </tr></thead>
      <tbody>{runs.map(run => <tr key={run.number} className="border-t border-border">
        <td className="py-1.5"><Link href={`/archives/?run=${run.number}`} className="text-primary underline">{run.name}</Link>{run.status === "active" ? " · active" : ""}</td>
        <td>{day(run.started_at)}</td><td>{day(run.closed_at)}</td>
        <td><span>{run.member_count}</span><span className="ml-2 text-xs text-muted-foreground">{run.members.join(", ")}</span></td><td>{run.left_count}</td>
      </tr>)}</tbody>
    </table>
    </div>}
  </RerollCard>;
}
