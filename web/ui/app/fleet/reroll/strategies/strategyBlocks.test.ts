import { expect, test } from "vitest";
import type { StrategyBlock } from "@/lib/strategyStudio";
import { BLOCK_PRESETS, GUIDE_BLOCKS, blockTitle, childGroups, findBlock, guideAnchor, insertBlock, locateBlock, makeBlock, presetsForLane, relativeWaveLimit, updateBlock } from "./strategyBlocks";

const tree: StrategyBlock[] = [{ id: "econ", type: "budget", metric: "utility_spent", target: 350, ceiling: 400, blocks: [
  { id: "goal", type: "save_for", goal: [{ id: "pool", type: "pool", upgrade_ids: ["cash_per_wave"], selection: "priority" }] }] },
  { id: "ws", type: "while_saving", upgrade_id: "thorns", blocks: [{ id: "w", type: "wait" }] }];

test("tree helpers reach blocks nested in budget and save_for", () => {
  expect(childGroups(tree[0]).map(group => group.label)).toEqual(["Within budget"]);
  expect(findBlock(tree, "pool")?.type).toBe("pool");
  expect(locateBlock(tree, "pool")).toEqual({ parent: "goal", branch: "goal", index: 0 });
  const removed = updateBlock(tree, "w", () => null);
  expect((removed[1] as Extract<StrategyBlock, { type: "while_saving" }>).blocks).toEqual([]);
  const inserted = insertBlock(tree, { id: "x", type: "wait" }, { parent: "ws", branch: "blocks", index: 0 });
  expect(findBlock(inserted, "x")).toBeTruthy();
  expect(findBlock(inserted, "pool")).toBeTruthy();
});

test("new presets build valid blocks", () => {
  expect(makeBlock("budget", "workshop")).toMatchObject({ type: "budget", metric: "utility_spent", target: 350, ceiling: 400 });
  expect(makeBlock("save_for", "workshop")).toMatchObject({ type: "save_for", goal: [{ type: "pool" }] });
  const budget = makeBlock("budget", "workshop");
  expect(budget).toMatchObject({ blocks: [{ id: `${budget.id}.pool`, type: "pool", upgrade_ids: ["cash_per_wave"],
    selection: "priority", count_scope: "account" }] });
  const saving = makeBlock("while_saving", "battle");
  expect(saving).toMatchObject({ type: "while_saving", blocks: [{ id: `${saving.id}.pool`, type: "pool", upgrade_ids: ["damage"],
    selection: "priority", wallet_share_pct: 20, count_scope: "run" }] });
  expect(makeBlock("while_saving", "workshop")).toMatchObject({ blocks: [{ count_scope: "account" }] });
  expect(BLOCK_PRESETS.map(item => item.id)).toEqual(expect.arrayContaining(["budget", "save_for", "while_saving"]));
});

test("every palette block type has a guide entry", () => {
  const types = new Set(GUIDE_BLOCKS.map(item => item.type));
  for (const preset of BLOCK_PRESETS) expect(types.has(guideAnchor(makeBlock(preset.id, "workshop")).replace("block-", "") as never)).toBe(true);
  expect(guideAnchor({ id: "n", type: "native", policy: "turtle", phase: "battle" })).toBe("block-native");
});

test("a block label overrides the generated title", () => {
  expect(blockTitle({ id: "x", type: "wait", label: "Keep coins" }, new Map())).toBe("Keep coins");
});

test("cheapest Battle pool and capped economy phase explain their purchase rules", () => {
  const combat: StrategyBlock = { id: "combat", type: "pool", selection: "cheapest", upgrade_ids: ["health"] };
  const economy: StrategyBlock = { id: "economy", type: "pool", selection: "cheapest", upgrade_ids: ["cash_bonus"],
    level_caps: { cash_bonus: { base: 20 } }, hold_until_capped: true };
  expect(blockTitle(combat, new Map())).toBe("Buy cheapest available");
  expect(blockTitle(economy, new Map())).toBe("Buy until purchase caps");
});

test("relative wave conditions title and limit", () => {
  const block: StrategyBlock = { id: "c", type: "condition", field: "wave", op: "lte",
    relative: { pct: 50, floor: 5, cap: 30 }, then: [], else: [] };
  expect(blockTitle(block, new Map())).toBe("If current wave ≤ 50% of best (5–30)");
  expect([null, 8, 24, 90].map(best => relativeWaveLimit({ pct: 50, floor: 5, cap: 30 }, best))).toEqual([5, 5, 12, 30]);
});

test("unlock and value presets are Workshop-only and build valid blocks", () => {
  expect(makeBlock("unlock", "workshop")).toMatchObject({ type: "unlock", upgrade_ids: ["knockback_chance", "orbs"], max_price: 20000, hold: true });
  expect(makeBlock("value", "workshop")).toMatchObject({ type: "pool", selection: "value",
    weights: { defense_percent: 12, health: 9, attack_speed: 10 } });
  expect(makeBlock("value", "workshop")).not.toHaveProperty("decay_pct");
  expect(presetsForLane("battle").map(item => item.id)).not.toEqual(expect.arrayContaining(["unlock"]));
  expect(presetsForLane("battle").map(item => item.id)).not.toEqual(expect.arrayContaining(["value"]));
  const names = new Map([["orbs", "Orbs"]]);
  expect(blockTitle(makeBlock("unlock", "workshop"), names)).toBe("Unlock missing skills");
  expect(blockTitle(makeBlock("value", "workshop"), names)).toBe("Buy best value per coin");
  expect(GUIDE_BLOCKS.map(item => item.type)).toContain("unlock");
});
