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
  useEffect(() => { void listRerolls().then(value => setRuns(value.runs)).catch(() => setRuns([])); }, [refreshKey]);
  if (!runs.length) return null;
  return <RerollCard title="Past rerolls" defaultCollapsed>
    <table className="w-full text-left text-sm">
      <thead className="text-xs text-muted-foreground"><tr>
        <th className="py-1">Reroll</th><th>Started</th><th>Closed</th><th>Emulators</th><th>Left</th>
      </tr></thead>
      <tbody>{runs.map(run => <tr key={run.number} className="border-t border-border">
        <td className="py-1.5"><Link href={`/archives/?run=${run.number}`} className="text-primary underline">{run.name}</Link>{run.status === "active" ? " · active" : ""}</td>
        <td>{day(run.started_at)}</td><td>{day(run.closed_at)}</td>
        <td>{run.member_count}</td><td>{run.left_count}</td>
      </tr>)}</tbody>
    </table>
  </RerollCard>;
}
