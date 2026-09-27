import { expect, test } from "vitest";
import { collectIds, DEFAULT_RULES, laneProblems, type LabBlock, type LabsReference } from "@/lib/labs";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { detachLabSlot, editLabSlot, labSlotBlocks, trackForSlot } from "./labSlotDraft";

const reference: LabsReference = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true }],
  game_speed: Array.from({ length: 7 }, (_, i) => ({ level: i + 1, coins: 10, seconds: 30, max_speed: 2 })),
  lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] };
const labs: BuildRouteDocument["baseline"]["labs"] = { slot1_research: "game_speed", steps: ["research_game_speed"], mode: "blocks", blocks: [
  { id: "one", type: "slot_track", slots: [1], children: [{ id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
  { id: "shared", type: "slot_track", slots: [3, 4, 5], paused: false, slot_policies: { "3": { paused: true }, "4": { on_blocked: "skip" } }, children: [
    { id: "condition", type: "condition", field: "best_tier_1_wave", cmp: "gte", value: 30,
      then: [{ id: "pool", type: "lab_pool", lab_ids: ["labs.attack-speed"] }], else: [{ id: "wait", type: "wait" }] },
  ] },
] };
test("detaching slot 3 preserves slots 4/5 and recursively refreshes clone IDs", () => {
  const next = detachLabSlot(labs, 3, reference);
  const original = trackForSlot(labs.blocks!, 3)!;
  const rest = trackForSlot(next.blocks!, 4)!;
  expect(rest).toEqual({ ...original, slots: [4, 5], slot_policies: { "4": { on_blocked: "skip" } } });
  const detached = trackForSlot(next.blocks!, 3)!;
  expect(detached.slots).toEqual([3]);
  expect(detached.slot_policies).toEqual({ "3": { paused: true } });
  expect(detached.children[0]).toMatchObject({ type: "condition", then: [{ type: "lab_pool", lab_ids: ["labs.attack-speed"] }], else: [{ type: "wait" }] });
  const oldIds = collectIds(labs.blocks!);
  for (const id of collectIds([detached])) expect(oldIds.has(id)).toBe(false);
  expect(Object.keys(laneProblems("labs", next.blocks!, DEFAULT_RULES))).toHaveLength(0);
  expect(labs.blocks![1]).toEqual(original);
});
test("shared tracks require explicit detachment before a per-slot edit", () => {
  expect(editLabSlot(labs, 3, reference, track => ({ ...track, paused: true }))).toBe(labs);
});
test("legacy conversion waits for catalog and preserves the legacy pool", () => {
  const legacy = { slot1_research: "game_speed", steps: ["research_game_speed", "slot2_research"] };
  expect(labSlotBlocks(legacy, null)).toBeNull();
  expect(editLabSlot(legacy, 2, null, track => ({ ...track, paused: true }))).toBe(legacy);
  const next = editLabSlot(legacy, 2, reference, track => ({ ...track, paused: true }));
  expect(trackForSlot(next.blocks!, 2)?.children[0].type).toBe("lab_pool");
  expect(trackForSlot(next.blocks!, 1)?.children[0]).toMatchObject({ lab_id: "labs.game-speed", to_level: 7 });
});
test("slot 1 cannot lose or move Game Speed, but later targets can be queued", () => {
  expect(editLabSlot(labs, 1, reference, track => ({ ...track, children: [] }))).toBe(labs);
  const target: LabBlock = { id: "later", type: "research", lab_id: "labs.attack-speed", to_level: 10 };
  const next = editLabSlot(labs, 1, reference, track => ({ ...track, children: [...track.children, target] }));
  expect(trackForSlot(next.blocks!, 1)?.children).toHaveLength(2);
});
