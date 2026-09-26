import { describe, expect, it } from "vitest";
import type { BuildRouteDocument } from "./buildRoute";
import { appendChild, containerFor, DEFAULT_RULES, duration, findResourceBlock, isAutomated, laneProblems, legacyGemBlocks,
  legacyLabBlocks, moveWithin, newResourceBlock, nowText, rulesOf, slotTone, splitPreview, withRules,
  type AutomatedBlock, type GemBlock, type LabBlock, type SlotPlan } from "./labs";

const baseline = {
  workshop: { id: "workshop.default", mode: "blocks", blocks: [], priority_ids: [], banned_upgrade_ids: [],
    coin_spend_limit_pct: 40, draw_chance_pct: 0, weights: {} },
  battle: { mode: "blocks", branches: [], blocks: [] },
  gems: { lab_slot2_reserve: 100, spend_limit_pct: 70, steps: ["unlock_lab_slot_2"] },
  labs: { slot1_research: "game_speed", steps: ["research_game_speed"] },
} as BuildRouteDocument["baseline"];
const AUTOMATED: AutomatedBlock[] = [{ lane: "gems", type: "unlock_lab_slot", slot: 2 },
  { lane: "labs", type: "research", lab_id: "labs.game-speed", slot: 1 }];
const slot = (patch: Partial<SlotPlan>): SlotPlan => ({ slot: 1, now: { state: "idle", level: 3, completes_at: null,
  overdue_seconds: null, read_at: 900, stale: false }, next: { lab_id: "labs.game-speed", name: "Game Speed", level: 3,
  price: 12000, seconds: 35280 }, covered: true, automated: true, why: [], note: null, ...patch });

describe("rules", () => {
  it("fills missing rules from the moved fields and dual-writes on change", () => {
    const rules = rulesOf(baseline);
    expect(rules.coins.workshop_spend_limit_pct).toBe(40);
    expect(rules.gems.spend_limit_pct).toBe(70);
    expect(rules.coins.lab_share.mode).toBe("when_affordable");
    const next = withRules(baseline, { ...rules, coins: { ...rules.coins, workshop_spend_limit_pct: 25 } });
    expect(next.workshop.coin_spend_limit_pct).toBe(25);
    expect(next.rules?.coins.workshop_spend_limit_pct).toBe(25);
    expect(DEFAULT_RULES.labs.auto_start && DEFAULT_RULES.gems.auto_unlock_lab_slots).toBe(true);
  });

  it("previews how the wallet splits between the jar and Workshop", () => {
    const saving = { ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "save_pct" as const, pct: 20 } } };
    expect(splitPreview(saving, 1000, 200, 2500)).toEqual({ jar: 200, workshop: 800, price: 2500, progress: 0.08, paused: false });
    expect(splitPreview(DEFAULT_RULES, 1000, 200, 2500)?.workshop).toBe(1000);
    const first = { ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "labs_first" as const, pct: 25 } } };
    expect(splitPreview(first, 1000, 0, 2500)).toMatchObject({ workshop: 0, paused: true });
    expect(splitPreview(saving, null, 0, 2500)).toBeNull();
  });

  it("never pauses or holds a jar once auto_start is turned off", () => {
    const first = { ...DEFAULT_RULES, labs: { ...DEFAULT_RULES.labs, auto_start: false },
      coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "labs_first" as const, pct: 25 } } };
    expect(splitPreview(first, 1000, 200, 2500)).toEqual({ jar: 0, workshop: 1000, price: 2500, progress: 0, paused: false });
  });
});

describe("slots", () => {
  it("is amber only when idle, automated and covered", () => {
    expect(slotTone(slot({}))).toBe("ready");
    expect(slotTone(slot({ covered: false }))).toBe("neutral");
    expect(slotTone(slot({ covered: null }))).toBe("neutral");
    expect(slotTone(slot({ automated: false }))).toBe("neutral");
    expect(slotTone(slot({ now: { ...slot({}).now, state: "researching" } }))).toBe("neutral");
  });

  it("never prints a negative timer", () => {
    const researching = { state: "researching" as const, level: 3, completes_at: 10_000, overdue_seconds: null, read_at: 900, stale: false };
    expect(nowText(researching, 10_000 - 7200)).toBe("Researching L3 · 2h left");
    expect(nowText(researching, 10_000 + 7200)).toBe("Should have finished ~2h ago");
    expect(nowText({ ...researching, state: "owned_unread", stale: true }, 0)).toBe("Owned · research not read · stale read");
    expect([duration(540), duration(2_199_960), duration(7200)]).toEqual(["9m", "25d 11h", "2h"]);
  });

  it("reads the automated set from the server, never its own", () => {
    const research: LabBlock = { id: "r", type: "research", lab_id: "labs.game-speed", to_level: 7 };
    expect(isAutomated("labs", research, [1], AUTOMATED)).toBe(true);
    expect(isAutomated("labs", research, [2], AUTOMATED)).toBe(false);
    expect(isAutomated("labs", research, [1], [])).toBe(false);
    expect(isAutomated("gems", { id: "g", type: "unlock_lab_slot", slot: 2 }, [], AUTOMATED)).toBe(true);
    expect(isAutomated("gems", { id: "g", type: "unlock_lab_slot", slot: 3 }, [], AUTOMATED)).toBe(false);
  });
});

describe("blocks", () => {
  it("translates legacy steps exactly as the server does", () => {
    expect(legacyGemBlocks(["unlock_lab_slot_2", "cards", "unlock_lab_slot_4", "unlock_lab_slot_3"])).toEqual([
      { id: "legacy.gems.unlock_lab_slot_2", type: "unlock_lab_slot", slot: 2 },
      { id: "legacy.gems.cards", type: "buy_cards", purpose: "card_missions" },
      { id: "legacy.gems.unlock_lab_slot_4", type: "unlock_lab_slot", slot: 3 },
      { id: "legacy.gems.unlock_lab_slot_3", type: "unlock_lab_slot", slot: 4 }]);
    expect(legacyLabBlocks(["research_game_speed", "slot2_research"], 7)).toEqual([
      { id: "legacy.labs.slot1", type: "slot_track", slots: [1], children: [
        { id: "legacy.labs.game_speed", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
      { id: "legacy.labs.slot2", type: "slot_track", slots: [2], children: [
        { id: "legacy.labs.slot2.pool", type: "lab_pool", lab_ids: ["labs.coins-wave", "labs.cash-bonus", "labs.coins-kill-bonus"], selection: "cheapest" }] }]);
  });

  it("adds, finds and moves blocks inside tracks and branches", () => {
    let blocks = legacyLabBlocks(["research_game_speed"], 7) as LabBlock[];
    expect(containerFor(blocks, "legacy.labs.game_speed")).toEqual({ id: "legacy.labs.slot1", branch: "children" });
    const wait = newResourceBlock("wait", new Set(["wait.1"]));
    expect(wait.id).toBe("wait.2");
    blocks = appendChild(blocks, "legacy.labs.slot1", "children", wait) as LabBlock[];
    expect(findResourceBlock(blocks, "wait.2")).toEqual(wait);
    blocks = moveWithin(blocks, "wait.2", -1) as LabBlock[];
    expect(blocks[0].type === "slot_track" && blocks[0].children.map(child => child.id)).toEqual(["wait.2", "legacy.labs.game_speed"]);
  });
});

describe("laneProblems", () => {
  it("flags gem slots that unlock out of order and until_cards with no cards", () => {
    const blocks: GemBlock[] = [
      { id: "g2", type: "unlock_lab_slot", slot: 2 },
      { id: "g4", type: "unlock_lab_slot", slot: 4 },
      { id: "g3", type: "unlock_lab_slot", slot: 3 },
      { id: "empty", type: "buy_cards", purpose: "until_cards", cards: [] },
      { id: "missions", type: "buy_cards", purpose: "card_missions" },
    ];
    expect(laneProblems("gems", blocks, DEFAULT_RULES)).toEqual({
      g3: "Lab slots must unlock in increasing order.",
      empty: "Pick at least one card.",
    });
  });

  it("flags a slot track with no slots and a slot claimed by two tracks", () => {
    const blocks: LabBlock[] = [
      { id: "slot1", type: "slot_track", slots: [1], children: [
        { id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
      { id: "empty", type: "slot_track", slots: [], children: [] },
      { id: "dupe", type: "slot_track", slots: [1, 3], children: [] },
    ];
    expect(laneProblems("labs", blocks, DEFAULT_RULES)).toEqual({
      empty: "A slot track needs at least one slot.",
      dupe: "Lab slot 1 is already claimed by another track.",
    });
  });

  it("flags a lab pool looser than the strategy rule, nested in a track or a condition branch", () => {
    const tightRules = { ...DEFAULT_RULES, labs: { ...DEFAULT_RULES.labs,
      pool: { selection: "cheapest" as const, max_price_pct_of_wallet: 10, max_seconds: 1800 } } };
    const blocks: LabBlock[] = [
      { id: "slot1", type: "slot_track", slots: [1], children: [
        { id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
      { id: "slot2", type: "slot_track", slots: [2], children: [
        { id: "pool.seconds", type: "lab_pool", lab_ids: ["labs.coins-wave"], max_seconds: 3600 }] },
      { id: "slot3", type: "slot_track", slots: [3], children: [
        { id: "cond", type: "condition", field: "best_tier_1_wave", cmp: "gte", value: 30, then: [
          { id: "pool.pct", type: "lab_pool", lab_ids: ["labs.coins-wave"], max_price_pct_of_wallet: 50 }], else: [] }] },
    ];
    expect(laneProblems("labs", blocks, tightRules)).toEqual({
      "pool.seconds": "This pool's max duration is looser than the strategy rule.",
      "pool.pct": "This pool's max price is looser than the strategy rule.",
    });
    expect(laneProblems("labs", blocks, DEFAULT_RULES)).toEqual({});
  });
});
