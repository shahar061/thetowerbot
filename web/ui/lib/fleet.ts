import type { PushRunStatus } from "./types";

export type FleetClone = {
  instance?: string;
  state: "queued" | "staging" | "verifying" | "blocked" | "quarantined" | "dismissed" | "ready";
  reason: string;
  detail?: string | null;
  endpoint?: string;
  account_id?: string;
  evidence_ref?: string;
  registration_evidence_ref?: string;
  steps?: { at: number; state: string; reason: string; endpoint?: string }[];
};

export type FleetJob = {
  id: string;
  mode: "fresh" | "clone";
  source: string | null;
  requested_at: number;
  manager_result_url?: string;
  clones: FleetClone[];
};

export type FleetSnapshot = {
  capacity: { limit: number; used: number; available: number };
  sources: { instance: string; state: "parallel_session_qualified" | "blocked"; reason: string;
    evidence_at?: number; evidence_url?: string }[];
  jobs: FleetJob[];
  unavailable?: string;
};

export type FleetPreview = {
  mode: "fresh" | "clone";
  source: string | null;
  count: number;
  targets: string[];
  state: "eligible" | "blocked";
  reason?: string;
};

export type FleetSetup = {
  configured: boolean;
  settings: { capacity: number; name_prefix: string; qualification_id: string } | null;
  qualifications: { id: string; source_instance: string; evaluated_at?: number }[];
  host: { installed_prefix?: string; instance_count?: number; unavailable?: string };
};

export type RerollCandidate = { name: string; endpoint: string; state: string };
export type RerollPlan = { account_id: string; stage: string; goal: string; state: string;
  item: string | null; price: number | null; wallet_coins: number | null;
  lifetime_coins: number | null; reason: string; observed_at: number;
  upgrade_id?: string | null; price_source?: "observed" | "catalog_estimate" | null;
  confirmed_purchases?: Record<string, number>;
  projection_note?: string | null;
  next_purchases?: { account_id: string; position: number; upgrade_id: string;
    item: string; category: string; unlock: boolean; focus: string }[] };
export type FleetOverview = {
  account_id: string; lease_id: string; attempt_id: string; observed_at: number;
  health: { state: "progressing" | "waiting" | "recovering" | "attention" | "stopped" | "unknown";
    reason: string | null; last_completed_scan_at: number | null;
    last_progress_at: number | null; incidents_open: number | null };
  current_run: { id: number; tier: number | null; wave: number | null;
    speed: number | null; coins: number | null; observed_at: number } | null;
  last_completed_run: { id: number; tier: number | null; wave: number | null;
    coins: number | null; ended_at: number } | null;
  currency: { coins_lower: number | null; coins_upper: number | null; reserved: number | null;
    available_lower: number | null; gems: number | null };
  missions: { state: "unknown" | "pending" | "claiming" | "clear";
    reason: string | null; last_claim_at: number | null };
  strategy: { id: string; name: string; version: number } | null;
  source: { revision: string | null; hash: string | null };
  recovery: { mode: "off" | "shadow" | "assist"; model: string | null;
    configured: boolean; phase: string; last_outcome: string | null;
    cost_used_microusd: number | null; cost_reserved_microusd: number | null;
    /** Older servers omit these; the validator normalizes them to null. */
    blocker?: string | null; observed_at?: number | null } | null;
  unknown_count: number; blockers: string[];
  push_runs?: PushRunStatus | null;
};
export type RerollMember = { name: string; endpoint: string; lease_id: string; state: string;
  account_id?: string | null; account_key?: string | null; milestone?: string | null;
  tier?: number | null; wave?: number | null; run_duration_seconds?: number | null;
  best_tier_1_wave?: number | null; battle_cash?: number | null; run_coins?: number | null;
  game_screen?: string | null; current_run_id?: number | null;
  lifetime_coins?: number | null; lifetime_coins_incomplete?: boolean;
  game_started?: string | null; account_age_days?: number | null; recent_cps?: number | null;
  workshop_upgrades_bought?: number | null;
  recent_workshop_purchases?: { at: number; item: string; category: string; cost: number | null; reason: string | null }[];
  reroll_plan?: RerollPlan | null;
  wallet_gems?: number | null; wallet_stones?: number | null; wallet_medals?: number | null;
  uw_result?: string | null; observed_at?: number | null; error?: string | null;
  evidence?: string | null; recent_runs?: string[] | null;
  /** Set when a new reroll couldn't prove this bot stopped, so it was kept. */
  leave_error?: string | null;
  /** Deleted from the dashboard's device list; its worker still runs. */
  variant?: string | null; variant_name?: string | null;
  play_seconds_to_t1w20?: number | null; play_seconds_so_far?: number | null;
  hidden?: boolean;
  route_revision_applied?: number | null; route_error?: string | null;
  workshop_evaluation?: import("./buildRoute").RouteEvaluation | null;
  battle_evaluation?: import("./buildRoute").RouteEvaluation | null;
  resource_evaluation?: import("./buildRoute").ResourceEvaluation | null;
  overview?: FleetOverview;
};

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const number = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const nullableNumber = (value: unknown): boolean => value === null || number(value);
const nullableString = (value: unknown): boolean => value === null || typeof value === "string";

/** Validate the optional new summary before a card uses it; old servers omit it. */
export function validatedOverview(member: unknown): FleetOverview | null {
  if (!object(member) || !object(member.overview)) return null;
  const row = member.overview;
  if (typeof member.account_id !== "string" || row.account_id !== member.account_id ||
      typeof member.lease_id !== "string" || row.lease_id !== member.lease_id ||
      typeof row.lease_id !== "string" || typeof row.attempt_id !== "string" || !number(row.observed_at) ||
      !object(row.health) || !["progressing", "waiting", "recovering", "attention", "stopped", "unknown"].includes(row.health.state as string) ||
      !nullableString(row.health.reason) || !nullableNumber(row.health.last_completed_scan_at) ||
      !nullableNumber(row.health.last_progress_at) || !nullableNumber(row.health.incidents_open)) return null;
  if (row.current_run !== null && (!object(row.current_run) || !number(row.current_run.id) ||
      !nullableNumber(row.current_run.tier) || !nullableNumber(row.current_run.wave) ||
      !nullableNumber(row.current_run.speed) || !nullableNumber(row.current_run.coins) ||
      !number(row.current_run.observed_at))) return null;
  if (row.last_completed_run !== null && (!object(row.last_completed_run) || !number(row.last_completed_run.id) ||
      !nullableNumber(row.last_completed_run.tier) || !nullableNumber(row.last_completed_run.wave) ||
      !nullableNumber(row.last_completed_run.coins) || !number(row.last_completed_run.ended_at))) return null;
  if (!object(row.currency)) return null;
  const currency = row.currency;
  if (!["coins_lower", "coins_upper", "reserved", "available_lower", "gems"]
      .every(key => key in currency && nullableNumber(currency[key]))) return null;
  if (!object(row.missions) || !["unknown", "pending", "claiming", "clear"].includes(row.missions.state as string) ||
      !nullableString(row.missions.reason) || !nullableNumber(row.missions.last_claim_at)) return null;
  if (row.strategy !== null && (!object(row.strategy) || typeof row.strategy.id !== "string" ||
      typeof row.strategy.name !== "string" || !number(row.strategy.version))) return null;
  if (!object(row.source) || !nullableString(row.source.revision) || !nullableString(row.source.hash)) return null;
  if (row.recovery !== null && (!object(row.recovery) || !["off", "shadow", "assist"].includes(row.recovery.mode as string) ||
      !nullableString(row.recovery.model) || typeof row.recovery.configured !== "boolean" ||
      typeof row.recovery.phase !== "string" || !nullableString(row.recovery.last_outcome) ||
      !nullableNumber(row.recovery.cost_used_microusd) || !nullableNumber(row.recovery.cost_reserved_microusd) ||
      !nullableString(row.recovery.blocker ?? null) || !nullableNumber(row.recovery.observed_at ?? null))) return null;
  if (!number(row.unknown_count) || !Array.isArray(row.blockers) ||
      !row.blockers.every(reason => typeof reason === "string")) return null;
  if (!("push_runs" in row)) return row as FleetOverview;
  const push = row.push_runs;
  const validPush = object(push) && push.account === row.account_id &&
    ["farm", "push"].includes(push.mode as string) &&
    ["farming", "selecting", "ready", "pushing", "returning"].includes(push.phase as string) &&
    number(push.every) && Number.isInteger(push.every) && push.every >= 0 &&
    number(push.farms_remaining) && Number.isInteger(push.farms_remaining) &&
    push.farms_remaining >= 0 && push.farms_remaining <= push.every &&
    [push.farm_tier, push.target_tier].every(tier => tier === null || number(tier) && Number.isInteger(tier) && tier > 0) &&
    nullableString(push.blocker);
  return { ...row, push_runs: validPush ? push : null } as FleetOverview;
}
export type RerollRun = { number: number; name: string; status: "active" | "closed";
  started_at: string; closed_at?: string | null };
export type RerollRunSummary = RerollRun & { member_count: number; left_count: number; members: string[] };
export type RetireResult = { name: string; worker?: "stopped" | "killed" | "gone";
  instance?: "stopped" | "already_stopped" | "missing"; error?: string };
export type RerollOperation = { kind: "new_run" | "remove" | "add"; state: "running" | "done" | "failed";
  started_at: string; target?: string | null; results: RetireResult[]; error?: string | null };
export type VariantRow = { id: string; name: string; caps: string; accounts: number;
  reached: number; median_seconds: number | null; fastest_seconds: number | null };
export type RerollSnapshot = { candidates: RerollCandidate[]; members: RerollMember[];
  concurrency_limit?: number; pressure?: { running: number; starting: number; limit: number; available: number };
  run?: RerollRun | null; operation?: RerollOperation | null;
  variant_comparison?: VariantRow[] };
export type RerollJournalEntry = { sequence: number; at: string | number; instance: string;
  level: string; kind: string; message: string; color: string };
export type RerollJournal = { entries: RerollJournalEntry[] };
