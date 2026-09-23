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
  next_purchases?: { account_id: string; position: number; upgrade_id: string;
    item: string; category: string; unlock: boolean; focus: string }[] };
export type RerollMember = { name: string; endpoint: string; lease_id: string; state: string;
  account_id?: string | null; account_key?: string | null; milestone?: string | null;
  tier?: number | null; wave?: number | null; run_duration_seconds?: number | null;
  best_tier_1_wave?: number | null; battle_cash?: number | null; run_coins?: number | null;
  game_screen?: string | null; current_run_id?: number | null;
  lifetime_coins?: number | null; lifetime_coins_incomplete?: boolean;
  game_started?: string | null; account_age_days?: number | null; recent_cps?: number | null;
  workshop_upgrades_bought?: number | null;
  reroll_plan?: RerollPlan | null;
  wallet_gems?: number | null; wallet_stones?: number | null; wallet_medals?: number | null;
  uw_result?: string | null; observed_at?: number | null; error?: string | null;
  evidence?: string | null; recent_runs?: string[] | null;
  /** Set when a new reroll couldn't prove this bot stopped, so it was kept. */
  leave_error?: string | null;
};
export type RerollRun = { number: number; name: string; status: "active" | "closed";
  started_at: string; closed_at?: string | null };
export type RerollRunSummary = RerollRun & { member_count: number; left_count: number; members: string[] };
export type RetireResult = { name: string; worker?: "stopped" | "killed" | "gone";
  instance?: "stopped" | "already_stopped" | "missing"; error?: string };
export type RerollOperation = { kind: "new_run" | "remove" | "add"; state: "running" | "done" | "failed";
  started_at: string; target?: string | null; results: RetireResult[]; error?: string | null };
export type RerollSnapshot = { candidates: RerollCandidate[]; members: RerollMember[];
  concurrency_limit?: number; pressure?: { running: number; starting: number; limit: number; available: number };
  run?: RerollRun | null; operation?: RerollOperation | null };
export type RerollJournalEntry = { sequence: number; at: string | number; instance: string;
  level: string; kind: string; message: string; color: string };
export type RerollJournal = { entries: RerollJournalEntry[] };
