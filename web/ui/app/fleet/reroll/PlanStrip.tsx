"use client";

import { useState } from "react";
import { Meter } from "@/components/Meter";
import { StatusBadge } from "@/components/StatusBadge";
import { decisionFor } from "@/lib/rerollState";
import type { RerollPlan } from "@/lib/fleet";
import { cn } from "@/lib/utils";

/** The catalog's three categories (`upgrades.CATEGORIES`), given three of the
 *  chart hues. Categories are a taxonomy, not a machine state, so they must
 *  not borrow the reserved green/amber/red - a DEFENSE buy is not a warning. */
const CATEGORY = {
  ATTACK: { dot: "bg-chart-2", text: "text-chart-2" },
  DEFENSE: { dot: "bg-chart-1", text: "text-chart-1" },
  UTILITY: { dot: "bg-chart-3", text: "text-chart-3" },
} as const;

function categoryTone(category: string) {
  return CATEGORY[category as keyof typeof CATEGORY] ?? { dot: "bg-muted-foreground", text: "text-muted-foreground" };
}

const number = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : value.toLocaleString();

/**
 * What this worker is about to spend coins on, and why it has not yet.
 *
 * The page used to render this as four sentences of muted grey, which meant
 * the single most actionable fact on the card - "it is 2,760 coins short" -
 * was the same weight as the boilerplate around it. The shape here is: the
 * item, big; the gap to affording it, as a bar; and only then the prose.
 */
export function PlanStrip({ plan, worker }: { plan: RerollPlan; worker: string }) {
  const [expanded, setExpanded] = useState(false);
  const projections = (plan.next_purchases ?? []).filter(step => step.account_id === plan.account_id).slice(0, 10);
  const decision = decisionFor(plan.state);
  const price = plan.price;
  const wallet = plan.wallet_coins;
  // Only a price AND a balance make a gap measurable. Either one missing is
  // what `observe_price` / `observe_balance` exist to say, and the bar goes
  // hatched rather than empty so it cannot be misread as "no coins".
  const measurable = price !== null && price > 0 && wallet !== null;
  const shortfall = measurable ? Math.max(0, price - wallet) : null;

  return (
    <div className="rounded-lg border border-border-strong bg-well/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* h4: the device card's own name is the h3 on this card, and the
            page outline is what a screen reader navigates the pool by. */}
        <h4 className="font-heading text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Next Workshop decision
        </h4>
        <StatusBadge state={decision.tone}>{decision.label}</StatusBadge>
      </div>

      <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-base font-medium">{plan.item ?? "Operator review"}</span>
        <span className="font-mono text-xs text-faint-foreground">{plan.goal}</span>
      </div>

      <div className="mt-2.5">
        <Meter
          label={`Coins toward ${plan.item ?? "the next decision"} on ${worker}`}
          value={wallet ?? 0}
          max={price ?? 1}
          unknown={!measurable}
          tone={shortfall === 0 ? "live" : "chart"}
        />
        <div className="mt-1 flex flex-wrap items-baseline justify-between gap-x-3 font-mono text-[11px]">
          {/* Kept as one plain sentence: it is the line an operator reads out
              loud when asking why a worker has not bought anything. */}
          <span className="text-muted-foreground">
            Workshop coins: {number(wallet)} · Price: {number(price)} · Lifetime coins: {number(plan.lifetime_coins)}
          </span>
          {shortfall === null ? (
            <span className="text-warn">not read yet</span>
          ) : shortfall === 0 ? (
            <span className="text-live">affordable</span>
          ) : (
            <span className="text-warn">{shortfall.toLocaleString()} short</span>
          )}
        </div>
      </div>

      <p className="mt-2 text-sm text-muted-foreground">{plan.reason}</p>
      <p className="mt-1 text-xs text-faint-foreground">{decision.hint}</p>

      {projections.length ? (
        <div className="mt-3 border-t border-border pt-3">
          <h4 className="font-heading text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Projected Workshop purchases</h4>
          {projections.length > 3 && <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            className="flex w-full items-center justify-between gap-2 text-left"
          >
            <span className="font-mono text-[10px] text-faint-foreground">
              {expanded ? "Show first 3 projections" : `Show all ${projections.length} projections`}
            </span>
          </button>}

          <div>
            <p className="mt-2 text-xs text-muted-foreground">
              Projected order. Only the first verified decision is executable; every buy still
              needs a fresh price, balance and screen check before it happens.
            </p>
            <ol
              aria-label={`Projected Workshop purchases for ${worker}`}
              className="mt-2 flex flex-col gap-1"
            >
              {projections.slice(0, expanded ? 10 : 3).map((step, index) => {
                const tone = categoryTone(step.category);
                const now = index === 0;
                return (
                  <li
                    key={step.position}
                    className={cn(
                      "flex items-start gap-2.5 rounded-md border px-2 py-1.5",
                      now ? "border-live/50 bg-live-surface" : "border-transparent bg-card",
                    )}
                  >
                    <span className="mt-0.5 w-4 shrink-0 text-right font-mono text-[11px] text-faint-foreground">
                      {step.position}
                    </span>
                    <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", tone.dot)} aria-hidden="true" />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[13px] font-medium">{step.item}</span>
                        {step.unlock ? (
                          <span className="rounded bg-primary/12 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-primary">
                            Unlock
                          </span>
                        ) : null}
                        {now ? (
                          <span className="rounded bg-live/15 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-live">
                            Next
                          </span>
                        ) : null}
                      </span>
                      <span className="mt-0.5 block text-[11px] text-muted-foreground">
                        <span className={tone.text}>{step.category}</span> · {step.focus}
                      </span>
                    </span>
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      ) : null}
    </div>
  );
}
