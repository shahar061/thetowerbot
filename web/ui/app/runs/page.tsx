"use client";
import { SelectedExportReport } from "@/components/game-export/GameExportReports";

import { ArrowLeft } from "lucide-react";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { EventFeed } from "@/components/EventFeed";
import { PageHeader } from "@/components/PageHeader";
import { RunPurchases } from "@/components/RunPurchases";
import { RunTable } from "@/components/RunTable";
import { StatTile } from "@/components/StatTile";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/ui/section-card";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchRunEvents, fetchRunPurchases, fetchRuns } from "@/lib/api";
import { duration } from "@/lib/format";
import type { BotEvent, RunPurchasePayload, RunRow, StoredEvent } from "@/lib/types";

function Runs() {
  const router = useRouter();
  const params = useSearchParams();
  // Static export cannot prerender /runs/[id] - the ids do not exist at build
  // time - so the id rides in the query string instead. Same deep link, one
  // exported route, no SPA fallback needed in FastAPI.
  const selected = params.get("id");

  const [runs, setRuns] = useState<RunRow[]>([]);
  // Distinguishes "haven't heard back yet" from "heard back, no such run":
  // `runs` alone can't tell the two apart, since both start out empty.
  // Without this, /runs/?id=999 briefly - and then permanently, since the
  // fetch always resolves to the same empty match - looks identical to a
  // real run with no stored events.
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [events, setEvents] = useState<StoredEvent[]>([]);
  // null is "no record came back", which is not the same as "bought nothing":
  // `events` is pruned at 30 days while `runs` never is, so an old run keeps
  // its row long after its purchases are gone. RunPurchases draws the two
  // differently on purpose.
  const [purchases, setPurchases] = useState<RunPurchasePayload | null>(null);

  useEffect(() => {
    fetchRuns(200)
      .then(setRuns)
      .catch(() => setRuns([]))
      .finally(() => setRunsLoaded(true));
  }, []);

  useEffect(() => {
    if (!selected) return setEvents([]);
    fetchRunEvents(Number(selected)).then(setEvents).catch(() => setEvents([]));
  }, [selected]);

  useEffect(() => {
    if (!selected) return setPurchases(null);
    fetchRunPurchases(Number(selected)).then(setPurchases).catch(() => setPurchases(null));
  }, [selected]);

  const run = runs.find((r) => String(r.id) === selected);

  if (selected) {
    return (
      <div className="flex flex-col gap-4">
        <Button
          variant="ghost" size="sm" onClick={() => router.push("/runs/")}
          className="self-start"
        >
          <ArrowLeft />
          all runs
        </Button>
        <h1 className="font-mono text-lg font-semibold">Run #{selected}</h1>
        {run ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            {/* The shared tile, rather than the byte-identical copy that used
                to live here alongside the one in the status strip. */}
            <StatTile label="wave" value={run.wave ?? "—"} />
            <StatTile label="coins" value={run.coins ?? "—"} />
            <StatTile label="tier" value={run.tier ?? "—"} />
            <StatTile label="taps" value={run.tap_count} />
            <StatTile
              label="length"
              value={run.ended_at ? duration(run.ended_at - run.started_at) : "live"}
              sub={`${run.scan_count} scans`}
            />
          </div>
        ) : runsLoaded ? (
          // Otherwise indistinguishable from a real run with no stored
          // events: no tiles and an empty feed either way.
          <p className="text-sm text-muted-foreground">No such run.</p>
        ) : null}
        {run || !runsLoaded ? (
          <SectionCard title="Purchases — in run">
            <RunPurchases data={purchases} startedAt={run?.started_at ?? 0} />
          </SectionCard>
        ) : null}
        {run || !runsLoaded ? (
          <SectionCard title="Events">
            <EventFeed
              events={events.map((row) => ({ ...row, ...row.detail }) as unknown as BotEvent)}
              // A stored run is one fixed array, not a stream; without this
              // the feed reads the swap as the bot restarting.
              live={false}
            />
          </SectionCard>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Runs"
        meta={runsLoaded ? `${runs.length} stored` : undefined}
      />
      <SectionCard title="Run history">
        {runsLoaded ? (
          <RunTable runs={runs} onSelect={(id) => router.push(`/runs/?id=${id}`)} />
        ) : (
          <Skeleton rows={6} />
        )}
      </SectionCard>
    </div>
  );
}

function BotRunsPage() {
  return (
    <Suspense fallback={<Skeleton rows={6} />}>
      <Runs />
    </Suspense>
  );
}

export default function RunsPage(): React.JSX.Element {
  return <main className="space-y-5"><PageHeader title="Battle history" meta="Official completed game reports" /><SelectedExportReport section="runs" />
    <details className="rounded-xl border p-4"><summary className="cursor-pointer font-medium">Bot run audit and action receipts</summary><p className="my-3 text-sm text-muted-foreground">Historical worker sessions and confirmed actions are separate from imported game battle reports.</p><BotRunsPage /></details></main>;
}
