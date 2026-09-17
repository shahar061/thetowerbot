"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchFleet } from "@/lib/api";
import type { FleetSnapshot } from "@/lib/fleet";

export default function FleetHistoryPage() {
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { void fetchFleet().then(setFleet).catch((failure: Error) => setError(failure.message)); }, []);
  return <div className="mx-auto flex max-w-6xl flex-col gap-4">
    <PageHeader title="Provisioning history" meta="Fleet · past jobs" action={<Link href="/fleet/reroll/" className="text-sm text-primary underline">Back to Reroll</Link>} />
    {error && <p role="alert">{error}</p>}
    <SectionCard title="Past jobs">
      {!fleet && !error && <p>Loading history…</p>}
      {fleet && !fleet.jobs.length && <p className="text-sm text-muted-foreground">No provisioning history.</p>}
      {fleet?.jobs.map(job => <article key={job.id} className="mt-3 rounded border p-3 text-sm"><h2 className="font-medium">{job.mode} · {job.source ?? "new instance"} · {new Date(job.requested_at * 1000).toLocaleString()}</h2><ol className="mt-2 space-y-1">{job.clones.map((clone, index) => <li key={index}>{clone.instance ?? `Instance ${index + 1}`} · {clone.state} · {clone.reason.replaceAll("_", " ")}</li>)}</ol></article>)}
    </SectionCard>
  </div>;
}
