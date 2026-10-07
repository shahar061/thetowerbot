// Example plan records for tests (Tiramisu64_82 weighted draw, Tiramisu64_83 value ranking).
import type { RouteTrace, WorkshopPlanRecord } from "@/lib/buildRoute";

const baseTrace: RouteTrace = {
  matched_rule_id: "core", reason: "Eligible pool after price, cap and affordability filters",
  evidence_age_seconds: 1, price_source: "worker price evidence", variant: null, rejected: [],
  spend_ceiling: 480, branch_id: null, phase_id: null, phase_state: null, next_phase_id: null,
  transition_reason: null, eligible_odds: {}, draw_gate: null,
};

export function weightedPlan(overrides: Partial<WorkshopPlanRecord> = {}): WorkshopPlanRecord {
  return {
    account_id: "Tiramisu64_82", revision: 16, written_at: 1000,
    strategy: { id: "turtle", name: "Turtle", mode: "blocks" },
    budget: { wallet: 640, jar: 160, jar_kind: "lab_jar", lab_share_mode: "save_pct", spend_limit_pct: 100, ceiling: 480 },
    override: null,
    upgrade_names: { health: "Health", defense_absolute: "Defense Absolute", defense_percent: "Defense %",
      lifesteal: "Lifesteal", knockback_chance: "Knockback Chance" },
    evaluation: {
      account_id: "Tiramisu64_82", revision: 16, status: "projected", evidence_at: 990,
      decision: { account_id: "Tiramisu64_82", stage: "strategy", state: "buy", upgrade_id: "defense_absolute",
        item: "Defense Absolute", category: "DEFENSE", price: 198, wallet_coins: 640,
        reason: "Eligible pool after price, cap and affordability filters" },
      trace: {
        ...baseTrace,
        rejected: ["core: lifesteal over wallet share", "core: lifesteal over wallet share", "knockback_chance: blocked by Never Buy"],
        eligible_odds: { health: 0.4, defense_absolute: 0.3333, defense_percent: 0.2667 },
        steps: [
          { block_id: "unlock_da", label: "Unlock Defense Absolute", kind: "unlock", outcome: "done", note: "Already unlocked", depth: 0 },
          { block_id: "core", label: "Turtle core", kind: "pool", outcome: "matched", note: "", depth: 0 },
          { block_id: "rest", label: null, kind: "wait", outcome: "not_reached", note: "Not reached", depth: 0 },
        ],
        candidates: [
          { upgrade_id: "health", name: "Health", category: "DEFENSE", price: 212, weight: 30, odds: 0.4, score: null, chosen: false },
          { upgrade_id: "defense_absolute", name: "Defense Absolute", category: "DEFENSE", price: 198, weight: 25, odds: 0.3333, score: null, chosen: true },
          { upgrade_id: "defense_percent", name: "Defense %", category: "DEFENSE", price: 240, weight: 20, odds: 0.2667, score: null, chosen: false },
        ],
        selection: "weighted", draw_seed: "Tiramisu64_82:16:visit:3:core", draw_roll: 0.412,
      },
    },
    ...overrides,
  };
}

export function valuePlan(overrides: Partial<WorkshopPlanRecord> = {}): WorkshopPlanRecord {
  return {
    account_id: "Tiramisu64_83", revision: 16, written_at: 1000,
    strategy: { id: "blender", name: "blender", mode: "blocks" },
    budget: { wallet: 2940, jar: 410, jar_kind: "lab_jar", lab_share_mode: "save_pct", spend_limit_pct: 100, ceiling: 2530 },
    override: null,
    upgrade_names: { coins_per_kill_bonus: "Coins / Kill Bonus", health: "Health", attack_speed: "Attack Speed", orbs: "Orbs" },
    evaluation: {
      account_id: "Tiramisu64_83", revision: 16, status: "blocked", evidence_at: 990,
      decision: { account_id: "Tiramisu64_83", stage: "strategy", state: "save_coins", upgrade_id: "coins_per_kill_bonus",
        item: "Coins / Kill Bonus", category: "UTILITY", price: 5860, wallet_coins: 2530,
        reason: "Saving for Coins / Kill Bonus (2530/5860 coins)" },
      trace: {
        ...baseTrace, matched_rule_id: "bv2.value", reason: "Saving for Coins / Kill Bonus (2530/5860 coins)", spend_ceiling: 2530,
        rejected: ["bv2.value.goal: orbs target reached", "bv2.value: value ranking coins_per_kill_bonus 293.0, health 295.0"],
        steps: [
          { block_id: "bv2.orbs_unlock", label: "Unlock Orbs first", kind: "unlock", outcome: "done", note: "Already unlocked", depth: 0 },
          { block_id: "bv2.thorns", label: "Thorns while cheapest", kind: "pool", outcome: "skipped", note: "thorns not strictly cheaper than every reference", depth: 0 },
          { block_id: "bv2.value", label: "Save for the best value", kind: "save_for", outcome: "matched", note: "", depth: 0 },
          { block_id: "bv2.stop", label: null, kind: "wait", outcome: "not_reached", note: "Not reached", depth: 0 },
        ],
        candidates: [
          { upgrade_id: "coins_per_kill_bonus", name: "Coins / Kill Bonus", category: "UTILITY", price: 5860, weight: 20, odds: null, score: 293, chosen: true },
          { upgrade_id: "health", name: "Health", category: "DEFENSE", price: 4130, weight: 14, odds: null, score: 295, chosen: false },
          { upgrade_id: "attack_speed", name: "Attack Speed", category: "ATTACK", price: 6490, weight: 12, odds: null, score: 540.83, chosen: false },
        ],
        selection: "value", draw_seed: null, draw_roll: null,
      },
    },
    ...overrides,
  };
}
