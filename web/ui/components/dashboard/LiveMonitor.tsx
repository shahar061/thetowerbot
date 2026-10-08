"use client";

import { useCallback, useEffect, useState } from "react";
import { DeviceView } from "@/components/DeviceView";
import { AutopilotStatus } from "@/components/AutopilotStatus";
import { EventFeed } from "@/components/EventFeed";
import { MiniBarList } from "@/components/MiniBarList";
import { RunPurchases } from "@/components/RunPurchases";
import { RunTable } from "@/components/RunTable";
import { SnapshotStrip } from "@/components/SnapshotStrip";
import { StatBar } from "@/components/StatBar";
import { WaveSparkline } from "@/components/WaveSparkline";
import { Card } from "@/components/ui/card";
import { SectionCard } from "@/components/ui/section-card";
import { fetchRunEvents, fetchRunPurchases, fetchRuns, fetchStatus, fetchUnknown } from "@/lib/api";
import { useEventStream } from "@/lib/useEventStream";
import type { BotEvent, RunPurchasePayload, RunRow, Snapshot, StatusPayload } from "@/lib/types";

/** Re-run `load` now and every `ms` thereafter, until unmounted. */
function usePoll(load: () => Promise<void>, ms: number) {
  useEffect(() => {
    let alive = true;
    const tick = () => {
      // A poll failing is expected while the bot restarts; swallow it and
      // keep the last good state rather than blanking the page. The SSE
      // `connected` indicator already tells the reader the bot is unreachable.
      if (alive) void load().catch(() => {});
    };
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
  }, [load, ms]);
}

export function LiveMonitor() {
  const { events, connected } = useEventStream();
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [shots, setShots] = useState<Snapshot[]>([]);
  // Non-null means the feed is showing one stored run instead of the live stream.
  const [history, setHistory] = useState<{ id: number; events: BotEvent[] } | null>(null);
  const [purchases, setPurchases] = useState<RunPurchasePayload | null>(null);
  const run = status?.run ?? null;
  // The id alone, not the run object: /api/status hands back a fresh object
  // every two seconds, and a callback depending on it would restart the
  // interval below on every status poll - turning a 5s purchase poll into a
  // 2s one.
  const runId = run?.id ?? null;

  usePoll(useCallback(async () => setStatus(await fetchStatus()), []), 2000);
  // Keyed on the open run's id so the interval restarts when a run does, and
  // stops asking between runs - there is no id to ask about then. Slower than
  // the status poll because a run buys an upgrade every several seconds at
  // best, not every two.
  usePoll(
    useCallback(async () => {
      if (runId === null) return setPurchases(null);
      setPurchases(await fetchRunPurchases(runId));
    }, [runId]),
    5000,
  );
  usePoll(useCallback(async () => setRuns(await fetchRuns(30)), []), 15000);
  usePoll(useCallback(async () => setShots(await fetchUnknown()), []), 60000);

  async function showRun(id: number) {
    // A failed fetch here is otherwise an unhandled rejection on click; swallow
    // it the same way usePoll does and leave the feed showing whatever it had.
    const stored = await fetchRunEvents(id).catch(() => null);
    if (!stored) return;
    // Stored rows are columns plus a detail blob; flatten them back into the
    // shape describe() takes, so one renderer serves live and history both.
    // Blob last: it holds no column's name except `detail` itself, whose
    // string value is the one the renderer wants back.
    setHistory({ id, events: stored.map((row) => ({ ...row, ...row.detail }) as unknown as BotEvent) });
  }

  const waves = runs.filter((r) => r.wave != null).map((r) => r.wave as number).reverse();

  return (
    <div className="flex flex-col gap-4">
      <StatBar status={status} connected={connected} runs={runs} />

      {/* The device frame is the hero: a bright 9:16 video is the most
          information-dense object on the page, and it spent its life capped at
          320px in one third of a row, smaller than the sparkline beside it. */}
      <div className="grid gap-4 xl:grid-cols-[minmax(300px,400px)_1fr]">
        {/* Sticky: the frame is the thing you keep half an eye on, and it
            should not scroll away while you read the feed beside it. */}
        <Card size="sm" className="gap-0 self-start py-0 xl:sticky xl:top-4">
          <DeviceView boxes={status?.boxes ?? []} size={status?.frame_size ?? null} />
        </Card>

        <div className="flex flex-col gap-4">
          <AutopilotStatus />
          {/* Only while a run is open: between runs there is no "this run" to
              report on, and a stale card would read as the current one. The
              same view over a finished run lives on /runs/?id=N. */}
          {run ? (
            <SectionCard title="Purchases — this run">
              <RunPurchases data={purchases} startedAt={run.started_at} />
            </SectionCard>
          ) : null}
          {/* Both halves come from /api/status. `skips` is keyed by reason and
              had never been rendered anywhere - it is the field that answers
              "the bot is running but nothing is happening, why?". */}
          <SectionCard title="Activity — this session">
            <div className="grid gap-6 sm:grid-cols-2">
              <MiniBarList
                title="taps"
                counts={status?.taps ?? {}}
                empty="No taps yet this session."
              />
              <MiniBarList
                title="skips by reason"
                counts={status?.skips ?? {}}
                highlightLeader
                empty="Nothing skipped yet."
              />
            </div>
          </SectionCard>

          <SectionCard title="Waves">
            <WaveSparkline waves={waves} />
          </SectionCard>

          {/* The feed sits beside the device rather than under it: a 9:16 hero
              is tall, and anything shorter in the next column leaves a dead
              half-screen. This is also where the reader's eyes actually live. */}
          <SectionCard
            title="Events"
            tone={history ? "warn" : undefined}
            action={
              history ? (
                <button
                  onClick={() => setHistory(null)}
                  className="rounded-md border px-2 py-0.5 text-xs normal-case transition-colors hover:border-border-strong hover:bg-muted active:translate-y-px"
                >
                  back to live
                </button>
              ) : null
            }
          >
            {history ? (
              <p className="mb-2 rounded-md bg-warn-surface px-2 py-1 font-mono text-[11px] text-warn">
                Replaying run #{history.id} · {history.events.length} events — the live stream keeps
                running underneath.
              </p>
            ) : null}
            <EventFeed events={history ? history.events : events} live={!history} />
          </SectionCard>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard title="Run history">
          <RunTable runs={runs} onSelect={showRun} />
        </SectionCard>

        <SectionCard title="Unknown screens">
          <SnapshotStrip shots={shots} />
        </SectionCard>
      </div>
    </div>
  );
}
