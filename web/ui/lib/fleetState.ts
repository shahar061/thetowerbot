/** GET /api/fleet/state: one column per emulator account. Mirrors fleet/state_view.py.
 *
 * Every number may be null. Null means nobody observed it, and the page shows
 * it as a dash or "price unknown" - never as 0. */

export type StateCategory = "attack" | "defense" | "utility";
export const STATE_CATEGORIES: readonly StateCategory[] = ["attack", "defense", "utility"];

export interface FleetStateBot { screen: string | null; now: string | null; live: boolean }
export interface FleetStateBattle {
  tier: number | null; wave: number | null; cash: number | null;
  elapsed_s: number | null; best_wave: number | null;
}
export interface FleetStateBalances { coins: number | null; gems: number | null; stones: null }
/** Lifetime coins and stones off the game's Stats screen, as of `observed_at`
 *  (coins topped up by later runs). The Stats screen has no gems row, so gems
 *  are only what this worker's own claims added. */
export interface FleetStateTotals {
  coins: number | null; coins_incomplete: boolean; stones: number | null;
  gems_claimed: number; observed_at: string | null;
}
export interface FleetStateDecision {
  phase: string; reason: string; upgrade_id: string | null;
  category: StateCategory | null; name: string | null; cost: number | null;
}
/** The highest wave any finished run reached, and the tier it was on. */
export interface FleetStateBestWave { wave: number; tier: number }
/** The Strategy Studio strategy assigned to this worker. */
export interface FleetStateStrategy { id: string; name: string; version: number }
/** The Workshop planner's next purchase (reroll-plan.json). `state` is one of
 *  `DECISIONS` in rerollState.ts; `price` is null until someone reads it.
 *  `projected`: the plan predates the strategy, so this is the current strategy
 *  evaluated over the worker's last Workshop facts, not the worker's own plan. */
export interface FleetStateNextBuy {
  state: string; upgrade_id: string | null; name: string | null; category: StateCategory | null;
  price: number | null; price_source: string | null; wallet: number | null;
  reason: string; goal: string | null; observed_at: string | null; projected?: boolean;
}
/** The lab the save_pct jar saves toward; `price` is that level's catalog price. */
export interface FleetStateJarTarget { lab_id: string; name: string; level: number | null; price: number | null }
/** How the coin wallet splits: `jar` is held for labs (0 outside save_pct, never
 *  above a known wallet); `workshop_budget` is (wallet − jar) × the spend limit. */
export interface FleetStateCoinSplit {
  wallet: number | null; jar: number; jar_target: FleetStateJarTarget | null;
  share_mode: string | null; share_pct: number | null;
  workshop_limit_pct: number | null; workshop_budget: number | null;
}
export interface WorkshopSkill {
  id: string; name: string; level: number | null; invested: number | null; bot_spent: number;
  next_cost: number | null; status: string; locked: boolean;
  max_level?: number; level_min?: number | null; level_max?: number | null;
  level_source?: "observed" | "confirmed_actions" | null;
  next_cost_source?: "observed" | "catalog_estimate" | "stat_ladder" | null;
  unlock_id?: string | null; next_unlock?: boolean;
}
export interface UnlockTarget { id: string; name: string; cost: number | null; upgrade_ids?: string[] }
export interface WorkshopCategory {
  unlocked: number; total: number; skills: WorkshopSkill[]; next_unlock: UnlockTarget | null;
}
export interface WorkshopRecent {
  ts: string | null; id: string | null; name: string; category: StateCategory | null;
  level: number | null; price: number | null;
}
export interface FleetStateWorkshop {
  totals: Record<StateCategory, number>;
  categories: Record<StateCategory, WorkshopCategory>;
  recent: WorkshopRecent[];
}
export interface FleetStateCards {
  slots: { equipped: number | null; capacity: number | null; next_slot_gems: number | null };
  items: { name: string; level: number | null; copies: number | null }[];
  gems_invested: number | null;
  recent: { ts: string | null; name: string; gems: number | null }[];
}
export interface LabJob { id: string; name: string; to_level: number | null; completes_at: string }
export interface LabLevelRow { id: string; name: string; level: number | null; next_cost: number | null }
export interface FleetStateLabs {
  slots: number | null; running: LabJob[]; levels: LabLevelRow[]; next: UnlockTarget | null;
  recent: { ts: string | null; name: string; price: number | null }[];
}
export interface FleetStateRunUpgrades {
  scope: "current" | "last"; total: number; by_category: Record<StateCategory, number>;
  items: { id: string; name: string; category: StateCategory | null; levels: number }[];
}
export interface FleetStateRun {
  tier: number | null; wave: number | null; coins: number | null; duration_s: number | null;
  ended_at: string | null; abandoned: boolean;
}
export interface FleetStateAccount {
  id: string; name: string; serial: string | null; online: boolean;
  stale_seconds: number | null; scan: number | null; error: string | null;
  strategy: FleetStateStrategy | null; next_buy: FleetStateNextBuy | null;
  coin_split: FleetStateCoinSplit | null;
  best_wave: FleetStateBestWave | null;
  bot: FleetStateBot; battle: FleetStateBattle | null; balances: FleetStateBalances | null;
  totals: FleetStateTotals | null;
  decision: FleetStateDecision | null; workshop: FleetStateWorkshop | null;
  cards: FleetStateCards | null; labs: FleetStateLabs | null;
  run_upgrades: FleetStateRunUpgrades | null; runs: FleetStateRun[] | null;
}
export interface FleetStatePayload { generated_at: string; accounts: FleetStateAccount[] }

/** What decides whether a column has to re-render: a new scan, or a change
 * in whether the worker answers. */
export function accountKey(account: FleetStateAccount): string {
  return JSON.stringify([account.scan, account.online, account.stale_seconds, account.error]);
}

/** The next accounts, with every unchanged account swapped for the previous
 * object - so a memoized column sees the same prop and skips its render. */
export function reuseUnchanged(previous: readonly FleetStateAccount[] | null,
  next: FleetStateAccount[]): FleetStateAccount[] {
  if (!previous) return next;
  const byId = new Map(previous.map(account => [account.id, account]));
  return next.map(account => {
    const old = byId.get(account.id);
    return old && accountKey(old) === accountKey(account) ? old : account;
  });
}
