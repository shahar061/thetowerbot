import type { BuildRouteDocument } from "./buildRoute";

export type SpendingLane = "workshop" | "battle" | "gems" | "labs";
export type ProgramLane = "workshop" | "battle";
export type StrategyBlock =
  | { id: string; type: "native"; policy: "opening" | "turtle"; phase: "starter" | "economy" | "objectives" | "fallback" | "battle" }
  | { id: string; type: "buy"; upgrade_id: string }
  | { id: string; type: "pool"; upgrade_ids: string[]; selection: "priority" | "weighted"; weights?: Record<string, number>;
      discount_pct?: number; reference_upgrade_id?: string; max_purchases?: number; count_scope?: "account" | "run";
      decay_pct?: number; weight_floor?: number }
  | { id: string; type: "condition"; field: "best_tier_1_wave" | "wave" | "wallet"; op: "gte" | "lte"; value: number;
      then: StrategyBlock[]; else: StrategyBlock[] }
  | { id: string; type: "fallback"; blocks: StrategyBlock[] }
  | { id: string; type: "wait" };

export type StrategyDefinition = {
  id: string; name: string; version: number; source_template: "opening" | "turtle" | "scratch"; builtin: boolean;
  baseline: BuildRouteDocument["baseline"];
};
export type StrategyLibrary = { revision: number; templates: StrategyDefinition[]; strategies: StrategyDefinition[] };
export type StrategyAssignment = { account_id: string; strategy_id: string; strategy_version: number; strategy_name: string;
  baseline: BuildRouteDocument["baseline"] };
export type SaveStrategyInput = { name: string; source_template: StrategyDefinition["source_template"]; baseline: BuildRouteDocument["baseline"]; strategy_id?: string };
export type StrategyWorker = { name: string; account_id?: string | null; hidden?: boolean };
