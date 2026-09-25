import { expect, test } from "vitest";
import type { StrategyBlock } from "@/lib/strategyStudio";
import { BLOCK_PRESETS, GUIDE_BLOCKS, blockTitle, childGroups, findBlock, guideAnchor, insertBlock, locateBlock, makeBlock, updateBlock } from "./strategyBlocks";

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
  expect(makeBlock("while_saving", "battle")).toMatchObject({ type: "while_saving", blocks: [] });
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
