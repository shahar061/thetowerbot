export type RouteTrace = {
  matched_rule_id: string;
  reason: string;
  evidence_age_seconds: number | null;
  price_source: string;
  variant: string | null;
  rejected: string[];
  spend_ceiling: number | null;
  branch_id: string | null;
  phase_id: string | null;
  eligible_odds: Record<string, number>;
  draw_gate: number | null;
};

export type RouteDecision = {
  account_id: string;
  stage: string;
  state: string;
  upgrade_id: string | null;
  item: string | null;
  category: string | null;
  price: number | null;
  wallet_coins: number | null;
  battle_cash?: number | null;
  reason: string;
};

export type ResourceStep = { action: string; status: "supported" | "planned" | "blocked" | "unknown"; reason: string };
export type ResourceEvaluation = { account_id: string; revision: number;
  observed_at: number; gem_step: ResourceStep; lab_step: ResourceStep };

export type RouteEvaluation = {
  account_id: string;
  revision: number | null;
  status: "observed" | "projected" | "blocked" | "unknown";
  decision: RouteDecision | null;
  trace: RouteTrace;
  evidence_at: number | null;
};

export type BuildRouteDocument = {
  schema: 1;
  revision: number;
  authored_at: number | null;
  baseline: {
    workshop: {
      id: string;
      mode: "legacy_planner" | "priorities";
      priority_ids: string[];
      banned_upgrade_ids: string[];
      coin_spend_limit_pct: number;
      draw_chance_pct: number;
      weights: Record<string, number>;
    };
    battle: { mode: "legacy_policy" | "phases"; branches: {
      id: string; min_best_tier_1_wave: number | null; phases: {
        id: string; start_wave: number; end_wave: number | null; priority_ids: string[];
        cash_spend_limit_pct: number; draw_chance_pct: number; weights: Record<string, number>;
        emergency_survival: boolean;
      }[];
    }[] };
    gems: { lab_slot2_reserve: number; spend_limit_pct: number; steps: string[] };
    labs: { slot1_research: string; steps: string[] };
  };
  overrides: Record<string, { account_id: string; patches: Record<string, Record<string, unknown>> }>;
  dependencies: Record<string, string[]>;
};

export type BuildRouteRevisions = { revisions: BuildRouteDocument[] };

export type BuildRouteRebindPreview = { worker: string; old_account_id: string;
  new_account_id: string; patched_rules: string[];
  old_effective: BuildRouteDocument["baseline"] & { override_state: string };
  new_effective: BuildRouteDocument["baseline"] & { override_state: string } };

export type BuildRoutePreview = {
  saved_revision: number;
  proposed_revision: number;
  members: { worker: string; account_id: string;
    current: RouteEvaluation; proposed: RouteEvaluation;
    current_battle?: RouteEvaluation; proposed_battle?: RouteEvaluation;
    current_resources?: { gem_step: ResourceStep; lab_step: ResourceStep };
    proposed_resources?: { gem_step: ResourceStep; lab_step: ResourceStep } }[];
};
