"use client";

import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Meter } from "@/components/Meter";
import { StatTile } from "@/components/StatTile";
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

const STATUS_EDGE: Record<ObjectiveStatus, string> = {
  ready: "border-l-live",
  blocked: "border-l-border-strong",
  done: "border-l-primary",
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

/** Sort key for "soonest first". The two non-numeric horizons are pushed past
 *  every real wait rather than treated as zero - an unmeasured objective is
 *  not an imminent one, and a measured dead end is the furthest thing there
 *  is. Ordering them any other way would put the two kinds of "no number"
 *  at the top of a list whose whole promise is "this one first". */
function horizonKey(horizon: HorizonPayload): number {
  if (horizon.kind === "hours") return horizon.hours;
  if (horizon.kind === "unknown") return Number.MAX_SAFE_INTEGER - 1;
  return Number.MAX_SAFE_INTEGER;
}

function CandidateRow({
  candidate,
  rank,
  topScore,
}: {
  candidate: DirectorCandidate;
  /** Position in the director's own ranking, kept through re-sorts so the
   *  page can be reordered without ever losing what the ranking said. */
  rank: number;
  topScore: number;
}) {
  const held = candidate.held_by.length > 0;
  return (
    <li
      className={cn(
        "rounded-lg border border-l-4 p-3 transition-colors",
        held ? "border-warn bg-warn-surface/30" : cn("border-border", STATUS_EDGE[candidate.status]),
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-muted font-mono text-[10px] font-bold text-muted-foreground">
          {rank}
        </span>
        <span className="font-mono text-sm font-medium">{candidate.objective_id}</span>
        <StatusChip status={candidate.status} />
        <HorizonChip horizon={candidate.hours_to_afford} />
        {candidate.price !== null ? (
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">
            {candidate.price} {candidate.currency}
          </span>
        ) : null}
      </div>

      {/* The score, as a length. A column of `score 0.412` numbers is a
          column nobody compares; the bar is the comparison, and the number
          stays beside it for anyone checking the ranking against the log. */}
      {candidate.score !== null ? (
        <div className="mt-2 flex items-center gap-2">
          <Meter
            label={`Score for ${candidate.objective_id}`}
            value={candidate.score}
            max={topScore || 1}
            tone={held ? "muted" : candidate.status === "ready" ? "live" : "muted"}
            className="h-1"
          />
          <span className="shrink-0 font-mono text-[10px] text-faint-foreground">
            score {candidate.score.toFixed(3)}
          </span>
        </div>
      ) : null}

      <p className="mt-1.5 text-sm text-muted-foreground">{candidate.why}</p>

      {candidate.blocked_by.length ? (
        <p className="mt-1 font-mono text-xs text-muted-foreground">
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

type Lens = "all" | "ready" | "blocked" | "held";
type Order = "rank" | "soonest";

export default function DirectorPage() {
  const [data, setData] = useState<DirectorPlanPayload | null>(null);
  // A failed fetch must never fall into the same empty state as a healthy
  // plan - the Errors and Ledger pages make the same distinction, and a
  // dashboard reporting "nothing to do" because it could not reach the bot
  // is the most dangerous thing this page could say.
  const [failed, setFailed] = useState<string | null>(null);
  const [lens, setLens] = useState<Lens>("all");
  const [order, setOrder] = useState<Order>("rank");

  useEffect(() => {
    fetchDirector()
      .then((payload) => { setData(payload); setFailed(null); })
      .catch((e: Error) => setFailed(e.message));
  }, []);

  const candidates = useMemo(() => data?.candidates ?? [], [data]);

  // Ranks are assigned once, from the order the director sent, and travel
  // with each candidate through every filter and re-sort below. The list is
  // "the top five plus every held candidate", so a held objective's rank is
  // not its index here - discarding it would be discarding the ranking.
  const ranked = useMemo(
    () => candidates.map((candidate, index) => ({ candidate, rank: index + 1 })),
    [candidates],
  );

  const census = useMemo(() => ({
    total: ranked.length,
    ready: ranked.filter(({ candidate }) => candidate.status === "ready" && !candidate.held_by.length).length,
    blocked: ranked.filter(({ candidate }) => candidate.status === "blocked").length,
    held: ranked.filter(({ candidate }) => candidate.held_by.length > 0).length,
  }), [ranked]);

  const shown = useMemo(() => {
    const matches = ({ candidate }: { candidate: DirectorCandidate }) => {
      if (lens === "ready") return candidate.status === "ready" && !candidate.held_by.length;
      if (lens === "blocked") return candidate.status === "blocked";
      if (lens === "held") return candidate.held_by.length > 0;
      return true;
    };
    const list = ranked.filter(matches);
    if (order === "soonest") {
      return [...list].sort((a, b) =>
        horizonKey(a.candidate.hours_to_afford) - horizonKey(b.candidate.hours_to_afford) || a.rank - b.rank);
    }
    return list;
  }, [ranked, lens, order]);

  const topScore = useMemo(
    () => Math.max(0, ...candidates.map((candidate) => candidate.score ?? 0)),
    [candidates],
  );

  const LENSES: { id: Lens; label: string; count: number; tone: string }[] = [
    { id: "all", label: "All", count: census.total, tone: "text-foreground" },
    { id: "ready", label: "Ready", count: census.ready, tone: "text-live" },
    { id: "blocked", label: "Blocked", count: census.blocked, tone: "text-muted-foreground" },
    { id: "held", label: "Held", count: census.held, tone: "text-warn" },
  ];

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
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

      <SectionCard title="Recommendation" tone={failed ? "warn" : data?.top ? "live" : undefined}>
        {failed ? (
          <div className="rounded-md border border-warn bg-warn-surface p-3">
            <p className="text-sm text-warn">Could not load the director&apos;s plan.</p>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{failed}</p>
            <p className="mt-2 text-xs text-muted-foreground">
              This is not the same as having nothing to do — the dashboard could not reach the bot.
            </p>
          </div>
        ) : data ? (
          <div className="flex flex-col gap-3">
            {data.top ? (
              // The one thing to do next, set at the size of a decision
              // rather than as the first row of a list.
              <div className="rounded-lg border border-live/50 bg-live-surface/40 p-4">
                <p className="font-heading text-[10px] font-semibold uppercase tracking-[0.14em] text-live">
                  Do this next
                </p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <span className="font-mono text-lg font-medium tracking-tight">{data.top.objective_id}</span>
                  <StatusChip status={data.top.status} />
                  <HorizonChip horizon={data.top.hours_to_afford} />
                  {data.top.price !== null ? (
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {data.top.price} {data.top.currency}
                    </span>
                  ) : null}
                </div>
                <p className="mt-2 text-sm">{data.top.why}</p>
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-border-strong p-4">
                <p className="font-heading text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
                  No recommendation
                </p>
                <p className="mt-1.5 text-sm text-muted-foreground">
                  Nothing on the plan is both ready and unheld, so the director names no next move.
                </p>
              </div>
            )}

            {/* The census (ready/blocked/held) and the income state
                (CurrencyRates.reason) live in `reason` whether or not a
                `top` pick exists - see director.py's `_plan_reason` and
                `_census`. Rendering it only in the no-top branch, as this
                page used to, threw both away on every day there WAS a top
                pick - which is most days - leaving a reader with no way to
                tell "income has never been measured" from "measured and
                healthy" purely from the recommendation card. */}
            <p className="text-sm text-muted-foreground">{data.reason}</p>

            {census.total ? (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <StatTile label="Ranked" value={census.total} />
                <StatTile label="Ready" value={census.ready} tone={census.ready ? "live" : "none"} />
                <StatTile label="Blocked" value={census.blocked} />
                <StatTile
                  label="Held for you"
                  value={census.held}
                  tone={census.held ? "warn" : "none"}
                  sub={census.held ? "waiting on a decision" : undefined}
                  subTone={census.held ? "warn" : "none"}
                />
              </div>
            ) : null}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
      </SectionCard>

      <SectionCard
        title="Ranked objectives"
        action={
          census.total ? (
            <div className="flex flex-wrap items-center gap-1">
              {LENSES.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={lens === option.id}
                  onClick={() => setLens(option.id)}
                  className={cn(
                    "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                    lens === option.id
                      ? "border-primary bg-primary/12 text-foreground"
                      : "border-border text-muted-foreground hover:bg-muted",
                  )}
                >
                  {option.label}{" "}
                  <span className={cn("font-mono", option.count ? option.tone : "text-faint-foreground")}>
                    {option.count}
                  </span>
                </button>
              ))}
              <button
                type="button"
                aria-pressed={order === "soonest"}
                onClick={() => setOrder((value) => (value === "rank" ? "soonest" : "rank"))}
                title="Reorder by measured time to afford. The director's own rank stays on every row."
                className={cn(
                  "ml-1 rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                  order === "soonest"
                    ? "border-primary bg-primary/12 text-foreground"
                    : "border-border text-muted-foreground hover:bg-muted",
                )}
              >
                {/* Named as a sort, not as a fifth filter chip - it sits in
                    the same row and would otherwise read as one. */}
                <span className="text-faint-foreground">sort</span>{" "}
                {order === "soonest" ? "soonest first" : "by rank"}
              </button>
            </div>
          ) : null
        }
      >
        {shown.length ? (
          <ul className="flex flex-col gap-2">
            {shown.map(({ candidate, rank }) => (
              <CandidateRow
                key={candidate.objective_id}
                candidate={candidate}
                rank={rank}
                topScore={topScore}
              />
            ))}
          </ul>
        ) : census.total ? (
          <p className="text-sm text-muted-foreground">No objective matches this filter.</p>
        ) : failed ? null : (
          <p className="text-sm text-muted-foreground">Nothing to rank yet.</p>
        )}
      </SectionCard>
    </div>
  );
}
