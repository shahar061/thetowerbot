import type { FleetStateAccount, FleetStatePayload } from "@/lib/fleetState";

/** A complete account for tests: offline, nothing observed. Override what a test is about. */
export function makeAccount(overrides: Partial<FleetStateAccount> = {}): FleetStateAccount {
  const id = overrides.id ?? "Air_1";
  return {
    id, name: id, serial: "127.0.0.1:5555", online: false,
    stale_seconds: null, scan: null, error: null,
    strategy: null, next_buy: null, coin_split: null, best_wave: null,
    bot: { screen: null, now: null, live: false }, battle: null, balances: null, totals: null,
    decision: null, workshop: null, cards: null, labs: null, run_upgrades: null, runs: null,
    ...overrides,
  };
}

export function makePayload(accounts: FleetStateAccount[]): FleetStatePayload {
  return { generated_at: "2026-09-28T10:00:00+00:00", accounts };
}
