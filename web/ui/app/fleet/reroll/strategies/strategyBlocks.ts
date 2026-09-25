import type { ProgramLane, StrategyBlock } from "@/lib/strategyStudio";

export type BlockPreset = "condition" | "cheap" | "cap" | "weighted" | "buy" | "fallback" | "wait";
export type BlockTarget = { parent: string | null; branch: "root" | "then" | "else" | "blocks"; index: number };
export const ROOT_END: BlockTarget = { parent: null, branch: "root", index: Number.MAX_SAFE_INTEGER };
export const BLOCK_PRESETS: { id: BlockPreset; label: string; detail: string; group: "Logic" | "Flow" | "Buy" }[] = [
  { id: "condition", label: "If / else", detail: "Branch on waves or balance", group: "Logic" },
  { id: "cheap", label: "Cheap pool", detail: "Buy one below a price limit", group: "Logic" },
  { id: "cap", label: "Purchase cap", detail: "Up to X buys per upgrade", group: "Logic" },
  { id: "weighted", label: "Weighted draw", detail: "Let weights evolve after buys", group: "Logic" },
  { id: "fallback", label: "First available", detail: "Try paths in order", group: "Flow" },
  { id: "wait", label: "Save & wait", detail: "Stop buying on this path", group: "Flow" },
  { id: "buy", label: "Buy upgrade", detail: "One affordable purchase", group: "Buy" },
];

export function makeBlock(preset: BlockPreset, lane: ProgramLane, upgradeId = "defense_absolute"): StrategyBlock {
  const id = `block.${crypto.randomUUID()}`;
  const scope = lane === "workshop" ? "account" : "run";
  switch (preset) {
    case "condition": return { id, type: "condition", field: "best_tier_1_wave", op: "gte", value: 50, then: [], else: [] };
    case "fallback": return { id, type: "fallback", blocks: [] };
    case "wait": return { id, type: "wait" };
    case "buy": return { id, type: "buy", upgrade_id: upgradeId };
    case "cheap": return { id, type: "pool", upgrade_ids: ["defense_absolute", "cash_per_wave", "damage"], selection: "priority", discount_pct: 20, reference_upgrade_id: "priority" };
    case "cap": return { id, type: "pool", upgrade_ids: [upgradeId], selection: "priority", max_purchases: 8, count_scope: scope };
    case "weighted": return { id, type: "pool", upgrade_ids: ["defense_absolute", "cash_per_wave", "damage"], selection: "weighted",
      weights: { defense_absolute: 8, cash_per_wave: 4, damage: 2 }, decay_pct: 20, weight_floor: 1, count_scope: scope };
  }
}
export const nativeDetails = {
  starter: ["Survival starter", "One cheap starter in Damage, Attack Speed, Health and Defense; early accounts only."],
  economy: ["Early economy", "Aim for 350 coins in utilities, capped at 400: Cash/Wave, Coins/Kill, then Cash Bonus."],
  objectives: ["objectives", "Native targets, unlock prerequisites and weighted priorities."],
  fallback: ["Cheap fallback", "Turtle: Defense Absolute at ≤80% of unaffordable Thorns, with no extra purchase cap. Otherwise utility/attack filler at ≤20% of wallet."],
  battle: ["Battle survival", "Current battle policy: safety first, with native upgrade targets and observed prices."],
} as const;

export function blockTitle(block: StrategyBlock, names: Map<string, string>): string {
  switch (block.type) {
    case "native": return block.phase === "objectives" ? `${block.policy === "turtle" ? "Turtle" : "Opening"} objectives` : nativeDetails[block.phase][0];
    case "buy": return `Buy ${names.get(block.upgrade_id) ?? block.upgrade_id}`;
    case "condition": return `If ${block.field === "best_tier_1_wave" ? "best T1 wave" : block.field === "wave" ? "current wave" : "balance"} ${block.op === "gte" ? "≥" : "≤"} ${block.value}`;
    case "fallback": return "First available path";
    case "wait": return "Save & wait";
    case "pool": return block.selection === "weighted" ? "Draw with evolving weights" : block.discount_pct !== undefined ? "Buy one from a cheap pool" : "Capped upgrade pool";
  }
}
export function blockDetail(block: StrategyBlock): string {
  if (block.type === "native") return nativeDetails[block.phase][1];
  if (block.type === "pool") return [block.discount_pct !== undefined ? `At least ${block.discount_pct}% cheaper` : "Filter eligible upgrades",
    block.max_purchases !== undefined ? `Max ${block.max_purchases} confirmed buys / upgrade` : "One purchase, then evaluate again",
    block.selection === "weighted" ? `${block.decay_pct ?? 0}% weight reduction / buy` : "First eligible item"].join(" · ");
  if (block.type === "condition") return "Known facts choose the branch. Unknown facts stop this decision.";
  if (block.type === "fallback") return "Try each child until one can buy or explicitly waits.";
  if (block.type === "wait") return "End this decision without spending. Try again when facts change.";
  return "Requires verified price, unlock and enough available currency.";
}
export function childGroups(block: StrategyBlock): { branch: "then" | "else" | "blocks"; label: string; blocks: StrategyBlock[] }[] {
  if (block.type === "condition") return [{ branch: "then", label: "Then", blocks: block.then }, { branch: "else", label: "Else", blocks: block.else }];
  if (block.type === "fallback") return [{ branch: "blocks", label: "Paths", blocks: block.blocks }];
  return [];
}
export function findBlock(blocks: StrategyBlock[], id: string | null): StrategyBlock | undefined {
  for (const block of blocks) {
    if (block.id === id) return block;
    for (const child of childGroups(block)) {
      const found = findBlock(child.blocks, id);
      if (found) return found;
    }
  }
}
export function updateBlock(blocks: StrategyBlock[], id: string, transform: (block: StrategyBlock) => StrategyBlock | null): StrategyBlock[] {
  return blocks.flatMap(block => {
    if (block.id === id) { const next = transform(block); return next ? [next] : []; }
    if (block.type === "condition") return [{ ...block, then: updateBlock(block.then, id, transform), else: updateBlock(block.else, id, transform) }];
    if (block.type === "fallback") return [{ ...block, blocks: updateBlock(block.blocks, id, transform) }];
    return [block];
  });
}
export function insertBlock(blocks: StrategyBlock[], block: StrategyBlock, target: BlockTarget): StrategyBlock[] {
  const insert = (list: StrategyBlock[]): StrategyBlock[] => { const next = [...list]; next.splice(target.index, 0, block); return next; };
  if (!target.parent) return insert(blocks);
  return updateBlock(blocks, target.parent, parent => {
    if (parent.type === "condition" && (target.branch === "then" || target.branch === "else")) return { ...parent, [target.branch]: insert(parent[target.branch]) };
    if (parent.type === "fallback" && target.branch === "blocks") return { ...parent, blocks: insert(parent.blocks) };
    return parent;
  });
}
export function locateBlock(blocks: StrategyBlock[], id: string, parent: string | null = null, branch: BlockTarget["branch"] = "root"): BlockTarget | null {
  for (const [index, block] of blocks.entries()) {
    if (block.id === id) return { parent, branch, index };
    for (const group of childGroups(block)) { const found = locateBlock(group.blocks, id, block.id, group.branch); if (found) return found; }
  }
  return null;
}
