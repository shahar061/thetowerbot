import type { BlockStep, CandidateRow, WorkshopPlanRecord } from "@/lib/buildRoute";
import { gameNumber } from "./workshopFormat";

const KIND_NAMES: Record<string, string> = {
  condition: "Condition", unlock: "Unlock", pool: "Upgrade pool", save_for: "Save for goal",
  while_saving: "While saving", fallback: "Fallback", budget: "Budget", native: "Built-in plan",
  buy: "Buy", wait: "Wait",
};

export function blockName(step: BlockStep): string {
  return step.label ?? KIND_NAMES[step.kind] ?? step.block_id;
}

/** The deepest matched block: containers on the path to it are matched too. */
export function matchedStep(steps: readonly BlockStep[]): BlockStep | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) if (steps[index].outcome === "matched") return steps[index];
  return null;
}

export function percentLabel(row: CandidateRow, selection: string | null, totalWeight: number): string {
  if (selection === "weighted" && row.odds !== null) return `${(row.odds * 100).toFixed(1)}%`;
  if (selection === "value") {
    if (row.chosen) return "100%";
    return row.weight !== null && totalWeight > 0 ? `${Math.round((row.weight / totalWeight) * 100)}%` : "";
  }
  return "";
}

export function linkWidth(row: CandidateRow, selection: string | null): number {
  if (selection === "weighted") return 2 + (row.odds ?? 0) * 22;
  return row.chosen ? 7 : 1.5;
}

export function candidateCaption(selection: string | null, drawRoll: number | null | undefined): string {
  if (selection === "weighted") return drawRoll == null ? "Choice kept from earlier this visit" : `Seeded draw · rolled ${drawRoll.toFixed(3)}`;
  if (selection === "value") return "Lowest price ÷ weight wins. No random draw.";
  if (selection === "priority") return "Considered in order. The first that fits is chosen.";
  if (selection === "cheapest") return "The cheapest eligible upgrade is chosen.";
  if (selection === "unlock") return "The cheapest unlock step is chosen.";
  return "";
}

export type RejectedLine = { subject: string | null; reason: string };

export function rejectedLines(lines: readonly string[], names: Record<string, string>): RejectedLine[] {
  const seen = new Set<string>();
  const result: RejectedLine[] = [];
  const add = (line: RejectedLine): void => {
    const key = `${line.subject}|${line.reason}`;
    if (!seen.has(key)) { seen.add(key); result.push(line); }
  };
  for (const line of lines) {
    const split = line.indexOf(": ");
    if (split < 0) { add({ subject: null, reason: line }); continue; }
    const prefix = line.slice(0, split);
    const rest = line.slice(split + 2);
    if (rest.startsWith("value ranking")) continue;
    if (names[prefix]) { add({ subject: names[prefix], reason: rest }); continue; }
    const first = rest.split(" ", 1)[0];
    if (names[first]) { add({ subject: names[first], reason: rest.slice(first.length + 1) }); continue; }
    add({ subject: null, reason: rest });
  }
  return result;
}

export function summarySentence(plan: WorkshopPlanRecord): string {
  const { decision, trace } = plan.evaluation;
  const step = matchedStep(trace.steps ?? []);
  const block = step ? blockName(step) : "The strategy";
  const candidates = trace.candidates ?? [];
  const chosen = candidates.find(row => row.chosen) ?? null;
  const valueNote = trace.selection === "value" && chosen?.score != null
    ? ` ${block} ranked it best value at ${chosen.score.toFixed(1)} coins per weight point. No random draw.` : "";
  if (plan.override === "workshop_paused") return decision?.reason ?? "Workshop paused while labs save coins.";
  if (!decision || !decision.item) return `Waiting: ${trace.reason}`;
  const price = decision.price === null ? "an unread price" : `${gameNumber(decision.price)} coins`;
  const tutorial = plan.override === "tutorial" ? " on the tutorial visit" : "";
  if (decision.state === "save_coins") {
    const saved = decision.wallet_coins !== null && decision.price !== null
      ? ` (${gameNumber(decision.wallet_coins)} / ${gameNumber(decision.price)} coins)` : "";
    return `Saving for ${decision.item}${saved}.${valueNote}`;
  }
  if (decision.state === "observe_price") return `Reading the price of ${decision.item} before deciding.`;
  if (decision.state !== "buy") return decision.reason;
  if (trace.selection === "weighted" && chosen?.odds != null)
    return `Buy ${decision.item} for ${price}${tutorial}. ${block} drew it at ${Math.round(chosen.odds * 100)}% odds from ${candidates.length} eligible upgrades.`;
  if (valueNote) return `Buy ${decision.item} for ${price}${tutorial}.${valueNote}`;
  return `Buy ${decision.item} for ${price}${tutorial}, chosen by ${block}.`;
}
