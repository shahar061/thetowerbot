import type { ProgramLane, StrategyBlock } from "@/lib/strategyStudio";

export type BlockPreset = "condition" | "cheap" | "cap" | "weighted" | "buy" | "fallback" | "wait" | "budget" | "save_for" | "while_saving";
export type BlockTarget = { parent: string | null; branch: "root" | "then" | "else" | "blocks" | "goal"; index: number };
export const ROOT_END: BlockTarget = { parent: null, branch: "root", index: Number.MAX_SAFE_INTEGER };
export const BLOCK_PRESETS: { id: BlockPreset; label: string; detail: string; group: "Logic" | "Flow" | "Buy" }[] = [
  { id: "condition", label: "If / else", detail: "Branch on waves or balance", group: "Logic" },
  { id: "cheap", label: "Cheap pool", detail: "Buy one below a price limit", group: "Logic" },
  { id: "cap", label: "Purchase cap", detail: "Up to X buys per upgrade", group: "Logic" },
  { id: "weighted", label: "Weighted draw", detail: "Let weights evolve after buys", group: "Logic" },
  { id: "fallback", label: "First available", detail: "Try paths in order", group: "Flow" },
  { id: "budget", label: "Budget", detail: "Spend up to a utility allowance", group: "Flow" },
  { id: "save_for", label: "Save for goal", detail: "Save for one goal, let cheap buys continue", group: "Flow" },
  { id: "while_saving", label: "While saving", detail: "Only runs while a goal is saving", group: "Flow" },
  { id: "wait", label: "Save & wait", detail: "Stop buying on this path", group: "Flow" },
  { id: "buy", label: "Buy upgrade", detail: "One affordable purchase", group: "Buy" },
];
/** Budget counts Workshop utility spend, so the In-game palette omits it. */
export function presetsForLane(lane: ProgramLane): typeof BLOCK_PRESETS {
  return lane === "battle" ? BLOCK_PRESETS.filter(item => item.id !== "budget") : BLOCK_PRESETS;
}

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
    case "budget": return { id, type: "budget", metric: "utility_spent", target: 350, ceiling: 400,
      blocks: [{ id: `${id}.pool`, type: "pool", upgrade_ids: ["cash_per_wave"], selection: "priority", count_scope: "account" }] };
    case "save_for": return { id, type: "save_for", goal: [{ id: `${id}.goal`, type: "pool", upgrade_ids: [upgradeId], selection: "priority" }] };
    case "while_saving": return { id, type: "while_saving",
      blocks: [{ id: `${id}.pool`, type: "pool", upgrade_ids: ["damage"], selection: "priority", wallet_share_pct: 20, count_scope: scope }] };
  }
}
export const nativeDetails = {
  starter: ["Survival starter", "One cheap starter in Damage, Attack Speed, Health and Defense; early accounts only."],
  economy: ["Early economy", "Aim for 350 coins in utilities, capped at 400: Cash/Wave, Coins/Kill, then Cash Bonus."],
  objectives: ["objectives", "Native targets, unlock prerequisites and weighted priorities."],
  fallback: ["Cheap fallback", "Turtle: Defense Absolute at ≤80% of unaffordable Thorns, with no extra purchase cap. Otherwise utility/attack filler at ≤20% of wallet."],
  battle: ["Battle survival", "Current battle policy: safety first, with native upgrade targets and observed prices."],
} as const;

function conditionLabel(block: Extract<StrategyBlock, { type: "condition" }>, names: Map<string, string>): string {
  if (block.field === "upgrade_value") return `${names.get(block.upgrade_id ?? "") ?? block.upgrade_id} value`;
  return ({ best_tier_1_wave: "best T1 wave", wave: "current wave", wallet: "balance", def_abs_coverage: "Def. Abs coverage" })[block.field];
}
export function blockTitle(block: StrategyBlock, names: Map<string, string>): string {
  if (block.label) return block.label;
  switch (block.type) {
    case "native": return block.phase === "objectives" ? `${block.policy === "turtle" ? "Turtle" : "Opening"} objectives` : nativeDetails[block.phase][0];
    case "buy": return `Buy ${names.get(block.upgrade_id) ?? block.upgrade_id}`;
    case "condition": return `If ${conditionLabel(block, names)} ${({ gte: "≥", lte: "≤", gt: ">", lt: "<" })[block.op]} ${block.value}`;
    case "fallback": return "First available path";
    case "budget": return `Budget · ${block.target}/${block.ceiling} utility coins`;
    case "save_for": return "Save for goal";
    case "while_saving": return block.upgrade_id ? `While saving for ${names.get(block.upgrade_id) ?? block.upgrade_id}` : "While saving";
    case "wait": return "Save & wait";
    case "pool": return block.selection === "weighted" ? "Draw with evolving weights" : block.discount_pct !== undefined ? "Buy one from a cheap pool" : "Capped upgrade pool";
  }
}
export function blockDetail(block: StrategyBlock): string {
  if (block.type === "native") return nativeDetails[block.phase][1];
  if (block.type === "pool") return [block.discount_pct !== undefined ? `At least ${block.discount_pct}% cheaper` : "Filter eligible upgrades",
    block.max_purchases !== undefined ? `Max ${block.max_purchases} confirmed buys / upgrade` : "One purchase, then evaluate again",
    block.selection === "weighted" ? `${block.decay_pct ?? 0}% weight reduction / buy` : "First eligible item",
    block.price_cap !== undefined ? `≤ ${block.price_cap} coins` : null,
    block.wallet_share_pct !== undefined ? `≤ ${block.wallet_share_pct}% of wallet` : null].filter(Boolean).join(" · ");
  if (block.type === "condition") return "Known facts choose the branch. Unknown facts stop this decision.";
  if (block.type === "fallback") return "Try each child until one can buy or explicitly waits.";
  if (block.type === "budget") return "Runs its blocks until utility spend reaches the target; never spends past the ceiling.";
  if (block.type === "save_for") return "Buys the goal when affordable. Otherwise saves for it while later blocks may buy cheaply.";
  if (block.type === "while_saving") return "Skipped unless a goal above is saving for coins.";
  if (block.type === "wait") return "End this decision without spending. Try again when facts change.";
  return "Requires verified price, unlock and enough available currency.";
}
export function childGroups(block: StrategyBlock): { branch: "then" | "else" | "blocks" | "goal"; label: string; blocks: StrategyBlock[] }[] {
  switch (block.type) {
    case "condition": return [{ branch: "then", label: "Then", blocks: block.then }, { branch: "else", label: "Else", blocks: block.else }];
    case "fallback": return [{ branch: "blocks", label: "Paths", blocks: block.blocks }];
    case "budget": return [{ branch: "blocks", label: "Within budget", blocks: block.blocks }];
    case "save_for": return [{ branch: "goal", label: "Goal", blocks: block.goal }];
    case "while_saving": return [{ branch: "blocks", label: "While saving", blocks: block.blocks }];
    default: return [];
  }
}
function withChildren(block: StrategyBlock, branch: string, blocks: StrategyBlock[]): StrategyBlock {
  return childGroups(block).some(group => group.branch === branch) ? { ...block, [branch]: blocks } as StrategyBlock : block;
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
    return [childGroups(block).reduce((current, group) => withChildren(current, group.branch, updateBlock(group.blocks, id, transform)), block)];
  });
}
export function insertBlock(blocks: StrategyBlock[], block: StrategyBlock, target: BlockTarget): StrategyBlock[] {
  const insert = (list: StrategyBlock[]): StrategyBlock[] => { const next = [...list]; next.splice(target.index, 0, block); return next; };
  if (!target.parent) return insert(blocks);
  return updateBlock(blocks, target.parent, parent => {
    const group = childGroups(parent).find(item => item.branch === target.branch);
    return group ? withChildren(parent, group.branch, insert(group.blocks)) : parent;
  });
}
export function locateBlock(blocks: StrategyBlock[], id: string, parent: string | null = null, branch: BlockTarget["branch"] = "root"): BlockTarget | null {
  for (const [index, block] of blocks.entries()) {
    if (block.id === id) return { parent, branch, index };
    for (const group of childGroups(block)) { const found = locateBlock(group.blocks, id, block.id, group.branch); if (found) return found; }
  }
  return null;
}
export type GuideBlockType = "buy" | "pool" | "condition" | "fallback" | "budget" | "save_for" | "while_saving" | "wait" | "native";
export const GUIDE_BLOCKS: { type: GuideBlockType; title: string; summary: string }[] = [
  { type: "buy", title: "Buy upgrade", summary: "Buys one upgrade if it is unlocked, affordable and not on Never Buy. Otherwise passes." },
  { type: "pool", title: "Upgrade pool", summary: "Filters a list of upgrades by caps, targets and price limits, then buys the first eligible one or draws by weight." },
  { type: "condition", title: "If / else", summary: "Checks one fact and follows Then or Else. Unknown facts pause the decision." },
  { type: "fallback", title: "First available", summary: "Tries each path in order until one buys or waits." },
  { type: "budget", title: "Budget", summary: "Runs its blocks until utility spend reaches a target, never past the ceiling." },
  { type: "save_for", title: "Save for goal", summary: "Buys a goal when affordable; otherwise saves for it and lets later blocks buy cheaply." },
  { type: "while_saving", title: "While saving", summary: "Runs only while a goal above is saving, optionally only for one upgrade." },
  { type: "wait", title: "Save & wait", summary: "Ends the decision without spending." },
  { type: "native", title: "Legacy built-in", summary: "An older sealed policy block kept so saved copies still work." },
];
export function guideAnchor(block: StrategyBlock): string {
  return `block-${block.type}`;
}
