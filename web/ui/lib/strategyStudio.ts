import type { BuildRouteDocument } from "./buildRoute";

export type SpendingLane = "workshop" | "battle" | "gems" | "labs";
export type ProgramLane = "workshop" | "battle";
export type LevelCap = { base: number; per_level_of?: string; step?: number };
type BlockBase = { id: string; label?: string };
export type StrategyBlock =
  | (BlockBase & { type: "native"; policy: "opening" | "turtle"; phase: "starter" | "economy" | "objectives" | "fallback" | "battle" })
  | (BlockBase & { type: "buy"; upgrade_id: string })
  | (BlockBase & { type: "pool"; upgrade_ids: string[]; selection: "priority" | "weighted"; weights?: Record<string, number>;
      discount_pct?: number; reference_upgrade_id?: string; max_purchases?: number; count_scope?: "account" | "run";
      decay_pct?: number; weight_floor?: number; targets?: Record<string, number>; level_caps?: Record<string, LevelCap>;
      price_cap?: number; wallet_share_pct?: number })
  | (BlockBase & { type: "condition"; field: "best_tier_1_wave" | "wave" | "wallet" | "upgrade_value" | "def_abs_coverage";
      op: "gte" | "lte" | "gt" | "lt"; value: number; upgrade_id?: string; then: StrategyBlock[]; else: StrategyBlock[] })
  | (BlockBase & { type: "fallback"; blocks: StrategyBlock[] })
  | (BlockBase & { type: "budget"; metric: "utility_spent"; target: number; ceiling: number; blocks: StrategyBlock[] })
  | (BlockBase & { type: "save_for"; goal: StrategyBlock[] })
  | (BlockBase & { type: "while_saving"; upgrade_id?: string; blocks: StrategyBlock[] })
  | (BlockBase & { type: "wait" });

export type StrategyDefinition = {
  id: string; name: string; version: number; source_template: "opening" | "turtle" | "scratch"; builtin: boolean;
  baseline: BuildRouteDocument["baseline"];
};
export type StrategyLibrary = { revision: number; templates: StrategyDefinition[]; strategies: StrategyDefinition[] };
export type StrategyAssignment = { account_id: string; strategy_id: string; strategy_version: number; strategy_name: string;
  baseline: BuildRouteDocument["baseline"] };
export type SaveStrategyInput = { name: string; source_template: StrategyDefinition["source_template"]; baseline: BuildRouteDocument["baseline"]; strategy_id?: string };
export type StrategyWorker = { name: string; account_id?: string | null; hidden?: boolean };
