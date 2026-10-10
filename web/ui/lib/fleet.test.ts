import { describe, expect, it } from "vitest";
import { validatedOverview } from "./fleet";

const overview = {
  account_id: "42", lease_id: "lease-1", attempt_id: "attempt-1", observed_at: 1000,
  health: { state: "stopped", reason: "Worker is stopped", last_completed_scan_at: null,
    last_progress_at: null, incidents_open: null },
  current_run: null,
  last_completed_run: { id: 7, tier: 1, wave: 200, coins: 123, ended_at: 900 },
  currency: { coins_lower: null, coins_upper: null, reserved: null,
    available_lower: null, gems: null },
  missions: { state: "unknown", reason: "Mission evidence unavailable", last_claim_at: null },
  strategy: null, source: { revision: null, hash: null }, recovery: null,
  unknown_count: 4, blockers: ["Worker is stopped"],
};

describe("fleet overview response", () => {
  it("shows push status only for matching well-formed account evidence", () => {
    const push = { account: "42", mode: "push", phase: "pushing", every: 10,
      farms_remaining: 0, farm_tier: 1, target_tier: 2, blocker: null };
    const member = (push_runs: object) => ({ account_id: "42", lease_id: "lease-1", overview: { ...overview, push_runs } });
    expect(validatedOverview(member(push))?.push_runs).toEqual(push);
    expect(validatedOverview(member({ ...push, account: "other" }))?.push_runs).toBeNull();
    expect(validatedOverview(member({ ...push, farms_remaining: -1 }))?.push_runs).toBeNull();
  });
  it("accepts an old member without optional overview", () => {
    expect(validatedOverview({ name: "Air_2", account_id: "42" })).toBeNull();
  });

  it("requires matching account and complete nullable evidence fields", () => {
    expect(validatedOverview({ account_id: "42", lease_id: "lease-1", overview })).toEqual(overview);
    expect(validatedOverview({ account_id: "other", lease_id: "lease-1", overview })).toBeNull();
    expect(validatedOverview({ account_id: "42", lease_id: "other", overview })).toBeNull();
    expect(validatedOverview({ account_id: "42", lease_id: "lease-1", overview: { ...overview,
      current_run: { id: 7, tier: 1, wave: 200 } } })).toBeNull();
  });

  it("carries a recovery blocker and observation time, tolerating servers that omit them", () => {
    const recovery = { mode: "assist", model: "m", configured: true, phase: "paused",
      last_outcome: "unknown", cost_used_microusd: 0, cost_reserved_microusd: 0 };
    const member = (row: object) => ({ account_id: "42", lease_id: "lease-1", overview: { ...overview, recovery: row } });
    expect(validatedOverview(member({ ...recovery, blocker: "unresolved", observed_at: 990 }))?.recovery?.blocker).toBe("unresolved");
    expect(validatedOverview(member(recovery))).not.toBeNull();
    expect(validatedOverview(member({ ...recovery, blocker: 7 }))).toBeNull();
  });
});
