import { expect, test } from "vitest";
import { fleetScope, overviewEvidence, validatedLabsSnapshot } from "./fleetOverview";
import type { FleetOverview, RerollMember } from "@/lib/fleet";
import type { LabPlan, LabsRow, LabsSnapshot } from "@/lib/labs";

const member = { name: "Air_1", endpoint: "", state: "running", account_id: "new-account", lease_id: "lease-2",
  route_revision_applied: 7,
  workshop_evaluation: { account_id: "new-account", revision: 7 },
  battle_evaluation: { account_id: "old-account", revision: 7 },
  resource_evaluation: { account_id: "new-account", revision: 6 },
} as RerollMember;
const row: LabsRow = { worker: "Air_1", account_id: "new-account", strategy_name: null, read_at: null,
  wallet: { coins: null, gems: null }, plan: null, state: "unknown", reason: null,
  recent: [], unknown_slots: 5, freshness: "unknown", blockers: [] };
const snapshot: LabsSnapshot = { workers: [row], automated: [], reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [],
  card_gems: 0, labs_unlock_wave: 40, sources: [] } };

test("lab evidence requires the current worker and account", () => {
  expect(overviewEvidence(member, snapshot).labs).toEqual(row);
  const evidence = overviewEvidence(member, { ...snapshot, workers: [{ ...row, worker: member.name, account_id: "old-account" }] });
  expect(evidence.labs).toBeNull();
});

test("route evaluations require account and applied revision", () => {
  const evidence = overviewEvidence(member, snapshot);
  expect(evidence.workshop).toBe(member.workshop_evaluation);
  expect(evidence.battle).toBeNull();
  expect(evidence.resource).toBeNull();
});

test("observed labs survive a missing or newer applied strategy revision", () => {
  const plan: LabPlan = { account_id: "new-account", strategy_revision: 8, evaluated_at: 100, wallet_coins: 40, jar: 5,
    gems: { wallet: null, next: null, price: null, have: null, need: null, automated: false, why: [], steps: [] },
    slots: [{ slot: 1, now: { state: "researching", level: 4, completes_at: 200, overdue_seconds: null, read_at: 100,
      stale: false, research_id: "labs.game-speed", research_name: "Game Speed", owned: true, evidence_status: "historical" },
      next: null, covered: null, automated: false, why: [], note: null,
      capabilities: { observe: true, plan: true, execute: false } }] };
  const populated = { ...snapshot, workers: [{ ...row, plan }] };
  for (const applied of [7, undefined]) {
    const evidence = overviewEvidence({ ...member, route_revision_applied: applied }, populated);
    expect(evidence.labs?.plan?.slots[0].now.research_name).toBe("Game Speed");
    expect(evidence.appliedLabPlan).toBeNull();
  }
  expect(overviewEvidence({ ...member, route_revision_applied: 8 }, populated).appliedLabPlan).toBe(plan);
});

test("runtime validation rejects malformed lab ownership and capability evidence", () => {
  expect(validatedLabsSnapshot(snapshot)).toEqual(snapshot);
  expect(validatedLabsSnapshot({ ...snapshot, workers: [row, row] })).toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, freshness: undefined }] })).toBeNull();
  const plan: LabPlan = { account_id: "new-account", strategy_revision: 7, evaluated_at: 100, wallet_coins: null, jar: 0,
    gems: { wallet: null, next: null, price: null, have: null, need: null, automated: false, why: [], steps: [] },
    slots: [{ slot: 1, now: { state: "unknown", level: null, completes_at: null, overdue_seconds: null, read_at: null,
      stale: false, research_id: null, research_name: null, owned: null, evidence_status: "unknown" },
      next: null, covered: null, automated: false, why: [], note: null,
      capabilities: { observe: false, plan: true, execute: false } }] };
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan }] })).not.toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan: { ...plan, account_id: "other" } }] })).toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan: { ...plan, slots: [{ ...plan.slots[0], now: { ...plan.slots[0].now, owned: "yes" } }] } }] })).toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan: { ...plan, slots: [{ ...plan.slots[0], capabilities: { observe: true, plan: true, execute: "yes" } }] } }] })).toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan: { ...plan, slots: [{ ...plan.slots[0], capabilities: undefined }] } }] })).toBeNull();
  expect(validatedLabsSnapshot({ ...snapshot, workers: [{ ...row, plan: { ...plan, scope: { account_id: "other", lease_id: "lease-2", generation: "g", epoch: 1 } } }] })).toBeNull();
  expect(overviewEvidence(member, { ...snapshot, workers: [{ ...row, plan: { ...plan, scope: { account_id: "new-account", lease_id: "old-lease", generation: "g", epoch: 1 } } }] }).labs).toBeNull();
});

test("fleet request identity changes when only the validated attempt changes", () => {
  const overview: FleetOverview = { account_id: "new-account", lease_id: "lease-2", attempt_id: "first", observed_at: 100,
    health: { state: "stopped", reason: null, last_completed_scan_at: null, last_progress_at: null, incidents_open: null },
    current_run: null, last_completed_run: null,
    currency: { coins_lower: null, coins_upper: null, reserved: null, available_lower: null, gems: null },
    missions: { state: "unknown", reason: null, last_claim_at: null }, strategy: null,
    source: { revision: null, hash: null }, recovery: null, unknown_count: 1, blockers: [] };
  expect(fleetScope([{ ...member, overview: { ...overview, health: { ...overview.health, state: "stopped" } } }])).not.toBe(
    fleetScope([{ ...member, overview: { ...overview, attempt_id: "second", health: { ...overview.health, state: "stopped" } } }]));
});
