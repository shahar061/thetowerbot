"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { StatsPanels } from "@/components/StatsPanels";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchStats } from "@/lib/api";
import type { StatsPayload } from "@/lib/types";

export default function StatsPage() {
  const [stats, setStats] = useState<StatsPayload | null>(null);
  // A failed fetch used to land in the same `null` as "still loading", so an
  // unreachable bot said "Loading…" forever.
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    fetchStats()
      .then((s) => { setStats(s); setFailed(null); })
      .catch((e: Error) => setFailed(e.message));
  }, []);

  if (failed) {
    return (
      <div className="rounded-md border border-warn bg-warn-surface p-3">
        <p className="text-sm text-warn">Could not load stats.</p>
        <p className="mt-1 font-mono text-xs text-muted-foreground">{failed}</p>
      </div>
    );
  }
  if (!stats) return <Skeleton rows={5} />;
  if (!stats.runs.length) {
    // An explicit empty state, never zeros that look like real data.
    return <p className="text-sm text-muted-foreground">No stored runs yet. (Running with --no-store?)</p>;
  }

  return <div className="space-y-4"><PageHeader title="Stats" meta={`${stats.runs.length} runs`} /><StatsPanels stats={stats} /></div>;
}
