// Mirrors of the Python payloads. Every event field here comes from the
// dataclasses in events.py: sinks/sse.py's to_payload() is dataclasses.asdict()
// plus a `type` discriminator, so these are the wire shapes exactly.

export interface EventBase {
  seq: number;
  ts: number;
}

export type RunPurpose = "farm" | "milestone";

export type BotEvent =
  | (EventBase & { type: "ScreenChanged"; prev: string; curr: string; confidence: number; scores: Record<string, number> })
  | (EventBase & { type: "ScanCompleted"; screen: string; duration_ms: number; wallet: number | null })
  | (EventBase & { type: "Tapped"; action: string; x: number; y: number; score: number; price: number | null; wallet: number | null })
  | (EventBase & { type: "BattlePurchased"; item: string; upgrade_id: string; price: number | null; value: number | null })
  | (EventBase & { type: "Skipped"; action: string; reason: string; detail: string })
  | (EventBase & {
      type: "SpeedAdjusted";
      direction: string;
      source: string;
      /** Both null for a manual nudge from the dashboard, which never reads
       * the widget - see splitEvent's own comment. */
      reading: number | null;
      target: number | null;
    })
  | (EventBase & { type: "RunStarted"; run_id: number; purpose?: RunPurpose })
  | (EventBase & { type: "RunEnded"; run_id: number; duration: number; wave: number | null; coins: number | null; tier: number | null; abandoned: boolean })
  | (EventBase & { type: "Navigated"; target: string })
  | (EventBase & { type: "UnknownScreen"; snapshot_path: string; best_anchor: string; best_score: number })
  | (EventBase & { type: "BotError"; message: string; traceback: string })
  | (EventBase & { type: "ControlChanged"; changed: Record<string, unknown>; source: string })
  | (EventBase & { type: "PageChanged"; prev_page: string; curr_page: string; confidence: number })
  | (EventBase & { type: "ShoppingStarted"; visit: number; dry_run: boolean })
  | (EventBase & { type: "ShoppingUnavailable"; reason: string })
  | (EventBase & { type: "Purchased"; item: string; category: string; price: number | null; coins_before: number | null; gems_before: number | null; dry_run: boolean })
  | (EventBase & { type: "PurchaseSkipped"; item: string; reason: string; detail: string; coins_before: number | null; gems_before: number | null })
  | (EventBase & { type: "ShoppingEnded"; visit: number; bought: number; spent: number; aborted: boolean; reason: string })
  /** The free gem that orbits the tower mid-battle. Published only when the
   * HUD gem counter actually rose across the tap - an unconfirmed one
   * arrives as ClaimUncertain instead. */
  | (EventBase & { type: "FloatingGemClaimed"; point: [number, number]; gems_before: number; gems_after: number; delta: number; run_id: number | null })
  | (EventBase & { type: "ClaimUncertain"; target: string; reason: string; detail: string });

/** A row from the `events` table, which carries columns plus a JSON blob. */
export interface StoredEvent {
  seq: number;
  run_id: number | null;
  ts: number;
  type: string;
  screen: string | null;
  action: string | null;
  reason: string | null;
  score: number | null;
  price: number | null;
  wallet: number | null;
  detail: Record<string, unknown>;
}

export interface CurrentRun {
  id: number;
  started_at: number;
  elapsed: number;
  taps: Record<string, number>;
}

export interface MatchBox {
  name: string;
  x: number;
  y: number;
  w: number;
  h: number;
  /** Where a tap actually lands - the buy square beside the label, not the
   * label's own (x, y) origin. Absolute frame coordinates, same as x/y. */
  tap_x: number;
  tap_y: number;
  score: number;
  tapped: boolean;
}

export interface BotStatus {
  running: boolean;
  /** Unix seconds when the current bot started, or null when stopped. */
  since: number | null;
  /** The last start failure - a dead emulator, usually. Cleared by a
   * successful start. */
  error: string | null;
}

export interface StrategyList {
  active: string;
  names: string[];
}

export interface StatusPayload {
  screen: string;
  uptime: number;
  scans: number;
  taps: Record<string, number>;
  skips: Record<string, number>;
  runs_completed: number;
  run: CurrentRun | null;
  wallet: number | null;
  last_error: string | null;
  tail: unknown[];
  dropped: number;
  boxes: MatchBox[];
  frame_size: { width: number; height: number } | null;
  bot: BotStatus;
  /** Added by runtime contract v1. Older servers omit this field; reads keep
   * working, while browser writes fail closed during their fresh preflight. */
  runtime?: RuntimeMetadata;
}

export type ReadinessMode = "stopped" | "paused" | "observing" | "automation_enabled";

export interface RuntimeMetadata {
  api_version: 1;
  backend: {
    revision: string | null;
    source_hash: string | null;
    started_at: number;
  };
  frontend: {
    source_hash: string | null;
    expected_backend_hash: string | null;
    built_at: number | null;
  };
  capabilities: string[];
  profile: string | null;
  device: {
    serial: string | null;
    game_version: string | null;
  };
  readiness: {
    mode: ReadinessMode;
    reasons: string[];
  };
}

/** A row from the `runs` table. */
export interface RunRow {
  purpose?: RunPurpose;
  id: number;
  started_at: number;
  ended_at: number | null;
  wave: number | null;
  coins: number | null;
  tier: number | null;
  abandoned: number;
  scan_count: number;
  tap_count: number;
}

/** One in-run upgrade the autopilot bought and verified during a run.
 *
 * `price` is null when OCR could not read it - which is not a free upgrade,
 * and the UI must not render it as one. `category` is joined in by the
 * server from the upgrade catalog; the BattlePurchased event has none. */
export interface RunPurchase {
  seq: number;
  ts: number;
  item: string | null;
  upgrade_id: string | null;
  price: number | null;
  value: number | null;
  category: string | null;
}

export interface RunPurchaseTotals {
  count: number;
  /** Sums only the prices that were read. See `unpriced`. */
  spent: number;
  /** How many buys had an unreadable price, and so are missing from `spent`. */
  unpriced: number;
  by_category: Record<string, number>;
}

export interface RunPurchasePayload {
  purchases: RunPurchase[];
  totals: RunPurchaseTotals;
}

export interface Snapshot {
  name: string;
  ts: number;
  url: string;
}

export interface ActionRule {
  name: string;
  template: string;
  enabled: boolean;
  threshold: number;
  brightness_ratio: number;
}

export interface ShoppingRule {
  name: string;
  /** The identity: matched, normalised, against the row names OCR reads off
   * the page. Nothing else addresses a row. */
  category: "ATTACK" | "DEFENSE" | "UTILITY";
  enabled: boolean;
  target?: number | null;
}

export interface CardPolicy {
  enabled: boolean;
  gem_floor: number;
  max_per_visit: number;
  batch: "x1" | "x10";
}

export interface AdvisorSource {
  name: string;
  version: string;
  account_name: string;
  exported_at: number;
  account_snapshot_at: number;
  url?: string | null;
}
export interface AdvisorRecommendation {
  id: string;
  path: "health" | "damage" | "economy";
  system: "workshop" | "lab" | "ultimate_weapon" | "enhancement" | "other";
  upgrade: string;
  upgrade_id?: string | null;
  current_value: number | null;
  target_value: number | null;
  value_kind: "level" | "stat";
  cost: number | null;
  currency: "coins" | "gems" | "stones" | "medals" | "time" | "other";
  benefit: number | null;
  can_stage: boolean;
  blocked_reason: string | null;
}
export interface AdvisorSnapshot {
  import_id: string | null;
  profile: string;
  imported_at: number | null;
  source: AdvisorSource | null;
  missing_inputs: string[];
  stale: boolean;
  recommendations: AdvisorRecommendation[];
}
export interface AdvisorDraftResult { draft: Strategy; added: boolean; message: string }

/** Mirrors strategy.py's Shopping.to_dict(). `enabled` and `armed` are two
 * switches: enabled+unarmed reads and reports without tapping. */
export interface Shopping {
  enabled: boolean;
  armed: boolean;
  visit_every_n_runs: number;
  max_taps_per_visit: number;
  workshop: ShoppingRule[];
  cards: CardPolicy;
  coin_reserve?: number;
  /** `null` is unlimited; `0` is the opposite - spend nothing. */
  coin_budget?: number | null;
  /** A share of the balance the visit opened with, 0 to 1. `null` is none.
   *  Applied alongside coin_budget; whichever is tighter decides. */
  coin_budget_pct?: number | null;
  allow_unlocks?: boolean;
}

export type UpgradeCategory = "ATTACK" | "DEFENSE" | "UTILITY";
export interface Upgrade { id: string; name: string; category: UpgradeCategory; aliases: string[]; unlock: boolean }
export interface UpgradeRule { upgrade_id: string; enabled: boolean; target?: number | null }
export interface AutopilotPolicy {
  enabled: boolean;
  preset: string;
  rules: UpgradeRule[];
  economy_until_wave: number;
  survival_buffer: number;
  cash_reserve: number;
  max_scrolls: number;
  purpose?: RunPurpose;
}
export interface AutopilotPreset { name: string; rules: UpgradeRule[] }
export interface UpgradeObservation {
  upgrade_id: string; context: string; category: UpgradeCategory; name: string;
  value: number | null; price: number | null; status: string; observed_at: number;
}
export interface AutopilotSnapshot {
  can_control?: boolean;
  phase: string; reason: string; next_upgrade_id: string | null; category: string | null;
  observations: UpgradeObservation[];
  combat: Record<string, number | null>;
  updated_at: number | null;
  verified_purchases: number;
  last_purchase: { upgrade_id?: string; name?: string; price?: number; [key: string]: unknown } | null;
  tier_comparison?: { tiers: { tier: number; runs: number; coins_per_hour: number; median_wave: number }[]; recommended_tier: number | null; reason: string } | null;
}
export interface AutopilotCommand { action: "category" | "buy" | "scan"; category?: UpgradeCategory; upgrade_id?: string }

/** Mirrors strategy.py's Claims. */
export interface Claims {
  enabled: boolean;
  /** Bounded by strategy.py's MIN_CLAIM_HOURS/MAX_CLAIM_HOURS (0.1 - 168). */
  missions_every_hours: number;
  milestones_on_new_best: boolean;
}

/** Mirrors strategy.py's Strategy.to_dict(). */
export interface Strategy {
  name: string;
  autopilot?: AutopilotPolicy;
  actions: ActionRule[];
  affordability: string;
  interval: number;
  click_cooldown: number;
  auto_navigate: boolean;
  max_runs: number | null;
  navigation_cooldown: number;
  screen_confirmations: number;
  tap_jitter_px: number;
  timing_jitter: number;
  tap_delay: number;
  /** null means "leave the in-battle speed alone". */
  target_speed: number | null;
  shopping: Shopping;
  /** Optional for the same reason `autopilot` is: a profile served by a
   * backend older than the claim scheduler carries no such key. */
  claims?: Claims;
}

export interface ControlPayload {
  paused: boolean;
  strategy: Strategy;
  affordability_available: string[];
  /** Non-null when this machine's header glyph atlas cannot support a
   * balance read, which makes ShoppingSession.begin() decline every visit
   * forever regardless of the policy - see build_shopping(). Lets the
   * Strategy page say why enabling and arming shopping produces total
   * silence, instead of doing nothing with no explanation. */
  shopping_disabled_reason: string | null;
  /** The in-battle speeds this build has readout templates for. Grows when
   * tools/harvest_speed_glyphs.py captures another one. */
  speed_values: number[];
}

export interface RunStat {
  id: number;
  started_at: number;
  ended_at: number;
  wave: number | null;
  coins: number | null;
  tier: number | null;
  tap_count: number;
  scan_count: number;
  duration: number;
}

export interface StatsPayload {
  runs: RunStat[];
  taps: { action: string; count: number }[];
  screens: { screen: string; count: number }[];
}

/** A row from the `ledger` table - the account's permanent non-battle
 *  history. Distinct from StoredEvent, which is the 30-day event log. */
export interface LedgerLine {
  id: number;
  /** The source event's bus seq, or null for a derived UNEXPLAINED line. */
  seq: number | null;
  ts: number;
  kind: string;
  item: string | null;
  category: string | null;
  currency: string | null;
  /** What actually moved. 0 means "provably nothing" (a skip, a rehearsal);
   *  null means "an unknown amount" (an unreadable price). */
  delta: number | null;
  price: number | null;
  balance_after: number | null;
  observed: number | null;
  dry_run: number;
  run_id: number | null;
  visit: number | null;
  reason: string | null;
  detail: Record<string, unknown>;
}

export interface LedgerPayload {
  lines: LedgerLine[];
  balances: { coins: number | null; gems: number | null };
  rehearsals: number;
  next: number | null;
}

/** `objectives.classify`'s three-way answer, carried through unchanged. */
export type ObjectiveStatus = "done" | "ready" | "blocked";

/** director.py's own JSON boundary correction, spelled out as a discriminated
 *  union rather than a raw number: `math.inf` is not valid JSON (Python's
 *  `json.dumps` emits the bare, non-standard token `Infinity` for it, which
 *  `JSON.parse` rejects outright), and even where it round-trips, a plain
 *  number can never tell "we have not measured this" (`kind: "unknown"`)
 *  apart from "measured: not at this rate, ever" (`kind: "infinite"`) apart
 *  from a real wait (`kind: "hours"`). A reader must switch on `kind` before
 *  ever touching `.hours` - see director.py's `_horizon_payload`. */
export type HorizonPayload =
  | { kind: "unknown" }
  | { kind: "infinite" }
  | { kind: "hours"; hours: number };

/** One knowledge-pack citation, with the source link a human needs to check
 *  it - `source_url` is null only for a ref the committed pack does not
 *  (yet) contain, which director.py's own tests keep unreachable today. */
export interface KnowledgeRefPayload {
  id: string;
  source_url: string | null;
}

/** One ranked, explained objective - director.Candidate's JSON shape.
 *  `blocked_by`, `held_by` and `knowledge_refs` are kept as separate lists
 *  rather than folded into `why`: "blocked" (a prerequisite), "held" (a
 *  deliberate gate) and "cited" (why the objective exists at all) must stay
 *  visibly distinct on the page, exactly as they do in director.py. */
export interface DirectorCandidate {
  objective_id: string;
  status: ObjectiveStatus;
  score: number | null;
  hours_to_afford: HorizonPayload;
  price: number | null;
  currency: string | null;
  blocked_by: string[];
  held_by: string[];
  why: string;
  knowledge_refs: KnowledgeRefPayload[];
}

/** director.Plan's JSON shape - the whole /api/director response.
 *  `candidates` is the ranked list's top five plus every held candidate
 *  beyond that, in rank order: a hold is a decision waiting on a human and
 *  is never truncated away, however far down the ranking it sits. */
export interface DirectorPlanPayload {
  observed_at: number | null;
  revision_id: number | null;
  reason: string;
  top: DirectorCandidate | null;
  candidates: DirectorCandidate[];
}
