"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchDirector } from "@/lib/api";
import { clock } from "@/lib/format";
import type { DirectorCandidate, DirectorPlanPayload, HorizonPayload, ObjectiveStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATUS_TONE: Record<ObjectiveStatus, string> = {
  ready: "bg-live-surface text-live",
  blocked: "bg-muted text-muted-foreground",
  done: "bg-primary/12 text-primary",
};

function StatusChip({ status }: { status: ObjectiveStatus }) {
  return (
    <span className={cn("rounded px-1.5 py-0.5 font-mono text-[10px] uppercase", STATUS_TONE[status])}>
      {status}
    </span>
  );
}

/** The horizon, rendered so "we don't know" and "we measured: never" can
 *  never be mistaken for one another - or for a real number of hours. See
 *  director.py's `_horizon_payload` for why this distinction exists at all:
 *  a `None` horizon means "go gather data", `math.inf` means "measured, at
 *  the current rate this will never be affordable" - two different facts
 *  that a naive render (or a bare `Infinity` on the wire) would collapse. */
function HorizonChip({ horizon }: { horizon: HorizonPayload }) {
  if (horizon.kind === "unknown") {
    return (
      <span
        title="No horizon has ever been measured for this objective - not a missing number, an unmeasured one."
        className="rounded border border-dashed border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
      >
        unknown
      </span>
    );
  }
  if (horizon.kind === "infinite") {
    return (
      <span
        title="Measured: at the current rate this will never be affordable. A measured dead end, not a missing observation."
        className="rounded bg-danger-surface px-1.5 py-0.5 font-mono text-[10px] text-danger"
      >
        never (measured)
      </span>
    );
  }
  return (
    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-foreground">
      {horizon.hours.toFixed(2)}h
    </span>
  );
}

function CandidateRow({ candidate }: { candidate: DirectorCandidate }) {
  const held = candidate.held_by.length > 0;
  return (
    <li className={cn("rounded-md border p-3", held ? "border-warn" : "border-border")}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-medium">{candidate.objective_id}</span>
        <StatusChip status={candidate.status} />
        <HorizonChip horizon={candidate.hours_to_afford} />
        {candidate.score !== null ? (
          <span className="font-mono text-[10px] text-muted-foreground">score {candidate.score.toFixed(3)}</span>
        ) : null}
        {candidate.price !== null ? (
          <span className="font-mono text-[10px] text-muted-foreground">
            {candidate.price} {candidate.currency}
          </span>
        ) : null}
      </div>

      <p className="mt-1.5 text-sm text-muted-foreground">{candidate.why}</p>

      {candidate.blocked_by.length ? (
        <p className="mt-1 text-xs text-muted-foreground">
          Blocked by: {candidate.blocked_by.join(", ")}
        </p>
      ) : null}

      {/* The whole point of the page: a hold is a decision waiting on a
          human, and the reason it exists must be readable in full, not
          collapsed into a boolean. */}
      {held ? (
        <ul className="mt-1.5 flex flex-col gap-1.5">
          {candidate.held_by.map((reason, index) => (
            <li key={index} className="rounded-md border border-warn bg-warn-surface p-2 text-xs text-warn">
              {reason}
            </li>
          ))}
        </ul>
      ) : null}

      {candidate.knowledge_refs.length ? (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px]">
          {candidate.knowledge_refs.map((ref) =>
            ref.source_url ? (
              <a
                key={ref.id}
                href={ref.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline-offset-2 hover:underline"
              >
                {ref.id}
              </a>
            ) : (
              <span key={ref.id} className="text-faint-foreground">
                {ref.id}
              </span>
            ),
          )}
        </div>
      ) : null}
    </li>
  );
}

export default function DirectorPage() {
  const [data, setData] = useState<DirectorPlanPayload | null>(null);
  // A failed fetch must never fall into the same empty state as a healthy
  // plan - the Errors and Ledger pages make the same distinction, and a
  // dashboard reporting "nothing to do" because it could not reach the bot
  // is the most dangerous thing this page could say.
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    fetchDirector()
      .then((payload) => { setData(payload); setFailed(null); })
      .catch((e: Error) => setFailed(e.message));
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Director"
        meta={
          data
            ? `revision ${data.revision_id ?? "none"} · ${
                data.observed_at !== null ? clock(data.observed_at) : "no observation yet"
              }`
            : undefined
        }
      />

      <SectionCard title="Recommendation" tone={failed ? "warn" : undefined}>
        {failed ? (
          <div className="rounded-md border border-warn bg-warn-surface p-3">
            <p className="text-sm text-warn">Could not load the director&apos;s plan.</p>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{failed}</p>
            <p className="mt-2 text-xs text-muted-foreground">
              This is not the same as having nothing to do — the dashboard could not reach the bot.
            </p>
          </div>
        ) : data ? (
          <div className="flex flex-col gap-2">
            {data.top ? (
              <div className="rounded-md border border-live p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-medium">{data.top.objective_id}</span>
                  <StatusChip status={data.top.status} />
                  <HorizonChip horizon={data.top.hours_to_afford} />
                </div>
                <p className="mt-1.5 text-sm">{data.top.why}</p>
              </div>
            ) : null}
            {/* The census (ready/blocked/held) and the income state
                (CurrencyRates.reason) live in `reason` whether or not a
                `top` pick exists - see director.py's `_plan_reason` and
                `_census`. Rendering it only in the no-top branch, as this
                page used to, threw both away on every day there WAS a top
                pick - which is most days - leaving a reader with no way to
                tell "income has never been measured" from "measured and
                healthy" purely from the recommendation card. */}
            <p className="text-sm text-muted-foreground">{data.reason}</p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
      </SectionCard>

      <SectionCard
        title="Ranked objectives"
        action={
          data?.candidates.length ? (
            <span className="font-mono text-xs text-muted-foreground">{data.candidates.length}</span>
          ) : null
        }
      >
        {data?.candidates.length ? (
          <ul className="flex flex-col gap-2">
            {data.candidates.map((candidate) => (
              <CandidateRow key={candidate.objective_id} candidate={candidate} />
            ))}
          </ul>
        ) : failed ? null : (
          <p className="text-sm text-muted-foreground">Nothing to rank yet.</p>
        )}
      </SectionCard>
    </div>
  );
}
