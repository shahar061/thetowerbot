"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { BlockStep, CandidateRow, WorkshopPlanRecord } from "@/lib/buildRoute";
import { cn } from "@/lib/utils";
import { CATEGORY_COLOR, gameNumber } from "./workshopFormat";
import { blockName, candidateCaption, linkWidth, matchedStep, percentLabel, rejectedLines, summarySentence } from "./workshopPlanFormat";

type Link = { d: string; width: number; color: string; dashed: boolean; opacity: number };

const OUTCOME_ICON: Record<BlockStep["outcome"], string> = { done: "✓", skipped: "↷", matched: "●", not_reached: "·" };
const OUTCOME_LABEL: Record<BlockStep["outcome"], string> = { done: "done", skipped: "skipped", matched: "matched", not_reached: "not reached" };
const coins = (value: number | null | undefined): string => (value === null || value === undefined ? "—" : gameNumber(value));

function Heading({ step, children }: { step: number; children: React.ReactNode }): React.JSX.Element {
  return <h3 className="flex items-baseline gap-2 font-mono text-[10.5px] uppercase tracking-[.18em] text-faint-foreground">
    <span className="text-primary">{step}</span>{children}
  </h3>;
}

function OddsStrip({ rows }: { rows: CandidateRow[] }): React.JSX.Element {
  return <div role="img" aria-label={`Odds: ${rows.map(row => `${row.name} ${Math.round((row.odds ?? 0) * 100)}%`).join(", ")}`}
    className="flex h-[22px] gap-0.5 overflow-hidden rounded-[5px]">
    {rows.map(row => <span key={row.upgrade_id} style={{ flex: row.odds ?? 0, background: CATEGORY_COLOR[row.category] }}
      className={cn("flex min-w-0 items-center justify-center overflow-hidden whitespace-nowrap font-mono text-[10.5px] font-semibold text-background",
        row.chosen ? "outline-2 -outline-offset-2 outline-foreground" : "opacity-55")}>
      {Math.round((row.odds ?? 0) * 100)}%
    </span>)}
  </div>;
}

export function PlanGraph({ plan, stale }: { plan: WorkshopPlanRecord; stale: boolean }): React.JSX.Element {
  const { budget, evaluation } = plan;
  const { decision, trace } = evaluation;
  const steps = trace.steps ?? [];
  const candidates = trace.candidates ?? [];
  const selection = trace.selection ?? null;
  const matched = matchedStep(steps);
  const rejected = rejectedLines(trace.rejected, plan.upgrade_names);
  const chosen = candidates.find(row => row.chosen) ?? null;
  // The engine compares a price to the spend ceiling, not to the full wallet.
  const spendable = budget.ceiling ?? decision?.wallet_coins ?? null;
  const totalWeight = candidates.reduce((sum, row) => sum + (row.weight ?? 0), 0);
  const maxOdds = Math.max(0, ...candidates.map(row => row.odds ?? 0));
  const bestScore = Math.min(Infinity, ...candidates.map(row => row.score ?? Infinity));
  const override = plan.override === "workshop_paused" ? "Workshop paused" : plan.override === "tutorial" ? "Tutorial visit" : null;
  const buying = decision?.state === "buy" && plan.override !== "workshop_paused";
  const pill = override ?? (decision === null ? "Waiting" : buying ? "Buy" : decision.state === "save_coins" ? "Save coins" : decision.state.replaceAll("_", " "));
  const pillClass = buying ? "bg-live-surface text-live" : "bg-warn-surface text-warn";
  const graph = useRef<HTMLDivElement>(null);
  const [links, setLinks] = useState<Link[]>([]);

  useLayoutEffect(() => {
    const root = graph.current;
    if (!root) return;
    const rows = plan.evaluation.trace.candidates ?? [];
    const kind = plan.evaluation.trace.selection ?? null;
    const buys = plan.evaluation.decision?.state === "buy" && plan.override !== "workshop_paused";
    const chosenColor = buys ? "var(--live)" : "var(--warn)";
    const draw = (): void => {
      if (window.matchMedia?.("(max-width: 767px)").matches) { setLinks([]); return; }
      const frame = root.getBoundingClientRect();
      const box = (element: Element) => {
        const rect = element.getBoundingClientRect();
        return { left: rect.left - frame.left, right: rect.right - frame.left, top: rect.top - frame.top,
          middle: (rect.top + rect.bottom) / 2 - frame.top };
      };
      const curve = (x1: number, y1: number, x2: number, y2: number): string => {
        const dx = Math.max(24, (x2 - x1) / 2);
        return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
      };
      const next: Link[] = [];
      const budgetNode = root.querySelector("[data-node=budget]");
      const matchedNode = root.querySelector("[data-node=matched]");
      const decisionNode = root.querySelector("[data-node=decision]");
      const from = matchedNode ? box(matchedNode) : null;
      if (budgetNode && from) {
        const start = box(budgetNode);
        if (from.left > start.right) next.push({ d: curve(start.right, start.middle, from.left, from.middle), width: 2, color: "var(--border-strong)", dashed: false, opacity: 1 });
      }
      root.querySelectorAll<HTMLElement>("[data-candidate]").forEach(element => {
        const row = rows.find(candidate => candidate.upgrade_id === element.dataset.candidate);
        if (!row) return;
        const end = box(element);
        if (from && end.left > from.right) next.push({ d: curve(from.right, from.middle, end.left, end.middle), width: linkWidth(row, kind),
          color: row.chosen ? chosenColor : CATEGORY_COLOR[row.category] ?? "var(--border-strong)", dashed: false, opacity: row.chosen ? 0.9 : 0.35 });
        if (row.chosen && decisionNode) {
          const target = box(decisionNode);
          if (target.left > end.right) next.push({ d: curve(end.right, end.middle, target.left, target.top + 40), width: 3, color: chosenColor, dashed: !buys, opacity: 1 });
        }
      });
      setLinks(next);
    };
    draw();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(draw);
    observer.observe(root);
    return () => observer.disconnect();
  }, [plan]);

  return <div className="flex flex-col gap-4">
    {stale && <p role="status" className="rounded-md border border-warn bg-warn-surface px-3 py-2 text-sm text-warn">
      Strategy changed since this decision. The graph updates after this emulator&apos;s next Workshop visit.</p>}
    <div className={cn("flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3", stale && "opacity-60")}>
      <span className={cn("rounded-full px-2.5 py-0.5 font-mono text-[11px] font-semibold uppercase tracking-[.12em]", pillClass)}>{pill}</span>
      <p className="min-w-0 flex-1 text-[15px]">{summarySentence(plan)}</p>
    </div>
    <div ref={graph} className={cn("relative grid grid-cols-1 items-start gap-x-16 gap-y-7 rounded-xl border border-border bg-well p-4 md:grid-cols-2 lg:grid-cols-[190px_250px_minmax(0,1fr)_210px]", stale && "opacity-60")}>
      <svg aria-hidden="true" className="pointer-events-none absolute inset-0 size-full overflow-visible">
        {links.map((link, index) => <path key={index} d={link.d} fill="none" stroke={link.color} strokeWidth={link.width}
          strokeOpacity={link.opacity} strokeLinecap="round" strokeDasharray={link.dashed ? "6 5" : undefined} />)}
      </svg>

      <section aria-label="Budget" className="relative z-10 flex min-w-0 flex-col gap-2">
        <Heading step={1}>Budget</Heading>
        <div data-node="budget" className="rounded-lg border border-border bg-card p-3 text-xs">
          <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">Wallet</dt><dd className="text-right font-mono tabular-nums">{coins(budget.wallet)}</dd>
            <dt className="text-muted-foreground">{budget.jar_kind === "jit_hold" ? "Held for labs" : "Lab coin jar"}</dt>
            <dd className="text-right font-mono tabular-nums text-muted-foreground">− {coins(budget.jar)}</dd>
            <dt className="text-muted-foreground">Spend limit</dt><dd className="text-right font-mono tabular-nums text-muted-foreground">× {budget.spend_limit_pct}%</dd>
            <dt className="mt-0.5 border-t border-border pt-1.5 font-semibold">Spend ceiling</dt>
            <dd className="mt-0.5 border-t border-border pt-1.5 text-right font-mono font-semibold tabular-nums text-[var(--currency-coins)]">{coins(budget.ceiling)}</dd>
          </dl>
          <p className="mt-2 text-[11px] text-faint-foreground">Lab share: {budget.lab_share_mode.replaceAll("_", " ")}</p>
        </div>
      </section>

      <section aria-label="Strategy blocks" className="relative z-10 flex min-w-0 flex-col gap-2">
        <Heading step={2}>Strategy blocks</Heading>
        {steps.length === 0
          ? <div data-node="matched" className="rounded-lg border border-dashed border-border-strong bg-card p-3 text-xs text-muted-foreground">
              {plan.strategy.mode === "blocks"
                ? "No blocks were evaluated for this decision."
                : <>This strategy doesn&apos;t use blocks ({plan.strategy.mode.replaceAll("_", " ")}).</>}</div>
          : <ol className="flex flex-col gap-2">
              {steps.map((step, index) => <li key={`${step.block_id}-${index}`} data-node={step === matched ? "matched" : undefined}
                aria-label={`${blockName(step)}: ${OUTCOME_LABEL[step.outcome]}`} style={{ marginLeft: step.depth * 12 }}
                className={cn("grid grid-cols-[18px_1fr] gap-x-2 gap-y-0.5 rounded-lg border border-border bg-card px-2.5 py-2 text-[12.5px]",
                  step.outcome === "matched" && "border-primary/60",
                  step === matched && "border-primary bg-primary/10 ring-3 ring-primary/15",
                  step.outcome === "not_reached" && "border-dashed opacity-45")}>
                <span className={cn("text-center font-mono text-xs leading-[18px] text-faint-foreground",
                  step.outcome === "done" && "text-live", step.outcome === "matched" && "text-primary")}>{OUTCOME_ICON[step.outcome]}</span>
                <span className={cn("font-medium", (step.outcome === "done" || step.outcome === "skipped") && "text-muted-foreground")}>{blockName(step)}</span>
                <span className="col-start-2 text-[11.5px] leading-snug text-faint-foreground">
                  {step === matched ? (step.note || trace.reason) : step.note}
                  {step === matched && selection && <><br /><span className="mt-1 inline-block rounded bg-primary/12 px-1.5 font-mono text-[10.5px] text-primary">selection: {selection}</span></>}
                </span>
              </li>)}
            </ol>}
      </section>

      <section aria-label="Candidates" className="relative z-10 flex min-w-0 flex-col gap-2">
        <Heading step={3}>{selection === "weighted" ? "Draw odds" : selection === "value" ? "Value ranking" : "Candidates"}</Heading>
        {selection === "weighted" && candidates.length > 0 && <OddsStrip rows={candidates} />}
        {candidateCaption(selection, trace.draw_roll) && <p className="text-xs text-muted-foreground">{candidateCaption(selection, trace.draw_roll)}</p>}
        {candidates.length === 0
          ? <p className="text-xs text-muted-foreground">No candidates recorded for this decision.</p>
          : <ul className="flex flex-col gap-2">
              {candidates.map((row, index) => {
                const bar = selection === "weighted" && maxOdds > 0 ? ((row.odds ?? 0) / maxOdds) * 100
                  : selection === "value" && row.score !== null && Number.isFinite(bestScore) ? (bestScore / row.score) * 100 : null;
                const tone = buying ? "text-live" : "text-warn";
                return <li key={row.upgrade_id} data-candidate={row.upgrade_id} aria-label={row.name}
                  className={cn("grid grid-cols-[8px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-1 rounded-lg border border-border bg-card px-3 py-2",
                    row.chosen && (buying ? "border-live ring-3 ring-live-surface" : "border-warn ring-3 ring-warn-surface"))}>
                  <span className="size-2 rounded-[2px]" style={{ background: CATEGORY_COLOR[row.category] ?? "var(--border-strong)" }} />
                  <span className="truncate text-[13px] font-medium">
                    {selection === "value" && <span className="mr-1 font-mono text-faint-foreground">#{index + 1}</span>}
                    {row.name}
                    {row.chosen && <span className={cn("ml-1.5 text-[10.5px] font-semibold uppercase tracking-[.08em]", tone)}>
                      {selection === "weighted" ? "drawn" : buying ? "picked" : "saving"}</span>}
                  </span>
                  <span className={cn("text-right font-mono text-[15px] font-semibold tabular-nums", row.chosen && tone)}>{percentLabel(row, selection, totalWeight)}</span>
                  {bar !== null && <span className="col-span-2 col-start-2 h-1.5 overflow-hidden rounded-full bg-muted">
                    <i className={cn("block h-full rounded-full", row.chosen ? (buying ? "bg-live" : "bg-warn") : "bg-border-strong")} style={{ width: `${bar}%` }} /></span>}
                  <span className="col-span-2 col-start-2 flex flex-wrap gap-x-3 font-mono text-[11px] tabular-nums text-faint-foreground">
                    {selection === "weighted" && row.weight !== null && <span>weight {row.weight}</span>}
                    {selection === "value" && row.score !== null && <span>{row.score.toFixed(1)} coins/pt</span>}
                    <span>{coins(row.price)} coins</span>
                    {selection === "value" && <span>{row.chosen ? "chosen" : "weight share"}</span>}
                  </span>
                </li>;
              })}
            </ul>}
        {rejected.length > 0 && <div className="mt-2">
          <h4 className="font-mono text-[10.5px] uppercase tracking-[.18em] text-faint-foreground">Not selected · {rejected.length}</h4>
          <ul>
            {rejected.map(line => <li key={`${line.subject}|${line.reason}`} className="grid grid-cols-[14px_minmax(0,1fr)] gap-x-2 border-t border-dashed border-border px-1 py-1.5 text-xs">
              <span className="text-center font-mono text-danger">×</span>
              <span className="min-w-0">
                {line.subject && <span className="text-muted-foreground line-through decoration-faint-foreground">{line.subject} </span>}
                <span className="text-faint-foreground">{line.reason}</span>
              </span>
            </li>)}
          </ul>
        </div>}
      </section>

      <section aria-label="Decision" className="relative z-10 flex min-w-0 flex-col gap-2">
        <Heading step={4}>Decision</Heading>
        <div data-node="decision" className={cn("rounded-lg border bg-card p-3.5", buying ? "border-live" : "border-warn")}>
          <span className={cn("rounded-full px-2.5 py-0.5 font-mono text-[11px] font-semibold uppercase tracking-[.12em]", pillClass)}>{pill}</span>
          <p className="mt-2.5 text-lg font-semibold tracking-tight">{decision?.item ?? (matched ? blockName(matched) : "Nothing to buy")}</p>
          {decision?.price != null && <p className="font-mono text-[13px] tabular-nums text-[var(--currency-coins)]">{coins(decision.price)} coins</p>}
          {decision?.price != null && spendable != null && <div className="mt-3">
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[var(--currency-coins)]" style={{ width: `${Math.min(100, (spendable / Math.max(1, decision.price)) * 100)}%` }} />
            </div>
            <div className="mt-1 flex justify-between font-mono text-[11px] tabular-nums text-faint-foreground">
              <span>spendable {coins(spendable)}</span>
              <span>{spendable >= decision.price ? "affordable" : `${coins(decision.price - spendable)} short`}</span>
            </div>
          </div>}
          {override && chosen && <p className="mt-2 text-xs text-muted-foreground">Underlying pick: {chosen.name}</p>}
          <p className="mt-3 border-t border-border pt-2.5 text-xs text-muted-foreground">Reason: {decision?.reason ?? trace.reason}</p>
        </div>
      </section>
    </div>
  </div>;
}
