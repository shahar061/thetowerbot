import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { AccountChoice } from "@/lib/api";
import type { FleetOverview, RerollMember } from "@/lib/fleet";
import type { LabsSnapshot } from "@/lib/labs";
import type { RouteEvaluation } from "@/lib/buildRoute";
import { FleetLiveCard } from "./FleetLiveCard";

const member: RerollMember = { name: "Air_1", endpoint: "127.0.0.1:5555", lease_id: "lease",
  state: "running", account_id: "100", account_key: "worker:Air_1", best_tier_1_wave: 17,
  variant_name: "Balanced opening", workshop_upgrades_bought: 12,
  reroll_plan: { account_id: "100", stage: "opening", goal: "Reach T1 W20", state: "save",
    item: "Damage", price: 500, wallet_coins: 200, lifetime_coins: null, reason: "Save coins", observed_at: Date.now() / 1000 } };
const account: AccountChoice = { key: "worker:Air_1", account_id: "100", instance: "Air_1",
  running: true, kind: "worker", dashboard_url: "http://127.0.0.1:8766/" };

test("three fleet cards capture their own verified worker and preserve comparison rows", () => {
  render(<>{[1, 2, 3].map(i => <FleetLiveCard key={i}
    member={{ ...member, name: `Air_${i}`, account_id: `${i}00`, account_key: `worker:Air_${i}`,
      reroll_plan: { ...member.reroll_plan!, account_id: `${i}00` } }}
    account={{ ...account, instance: `Air_${i}`, key: `worker:Air_${i}`, account_id: `${i}00`, dashboard_url: `http://127.0.0.1:${8765 + i}/` }}
    onInspect={() => {}} />)}</>);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  for (const i of [1, 2, 3]) fireEvent.click(screen.getByRole("button", { name: `Show screen of Air_${i}` }));
  const captures = screen.getAllByRole("img", { name: /Live screen of/ });
  expect(captures).toHaveLength(3);
  captures.forEach((capture, index) => {
    const url = new URL(capture.getAttribute("src")!);
    expect(url.port).toBe(String(8766 + index));
    expect(url.searchParams.get("scope")).toBe(`worker:Air_${index + 1}`);
    expect(url.searchParams.get("expected_account_id")).toBe(`${index + 1}00`);
  });
  expect(screen.getAllByText("Next workshop")).toHaveLength(3);
  expect(screen.getAllByText("Battle decision")).toHaveLength(3);
  expect(screen.getAllByText("300 coins short")).toHaveLength(3);
});

test("mismatched account identity never opens a capture or shows an old plan", () => {
  render(<FleetLiveCard member={{ ...member, reroll_plan: { ...member.reroll_plan!, account_id: "old" } }}
    account={{ ...account, account_id: "old" }} onInspect={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Show screen of Air_1" }));
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByText(/Verified live account unavailable/)).toBeInTheDocument();
  expect(screen.queryByText("Damage")).not.toBeInTheDocument();
});

test("unknown prices and battle intent stay unknown and inspect keeps its account", () => {
  const inspect = vi.fn();
  render(<FleetLiveCard member={{ ...member, reroll_plan: { ...member.reroll_plan!, price: null } }}
    account={account} onInspect={inspect} />);
  expect(screen.getByText("Price not observed")).toBeInTheDocument();
  expect(screen.getByText("Live intent unavailable")).toBeInTheDocument();
  expect(screen.getByText(/Recorded buys: 12/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Inspect Air_1" }));
  expect(inspect).toHaveBeenCalledOnce();
});

test("an unavailable capture can retry without removing worker data", () => {
  render(<FleetLiveCard member={member} account={account} onInspect={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Show screen of Air_1" }));
  fireEvent.error(screen.getByRole("img", { name: "Live screen of Air_1" }));
  expect(screen.getByText("Reach T1 W20")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry screen" }));
  expect(within(screen.getByRole("article", { name: "Air_1 live overview" })).getByRole("img")).toBeInTheDocument();
});

const now = Date.now() / 1000;
const overview: FleetOverview = {
  account_id: "100", lease_id: "lease", attempt_id: "attempt", observed_at: now - 30,
  health: { state: "stopped", reason: "Worker stopped", last_completed_scan_at: now - 30,
    last_progress_at: now - 60, incidents_open: null },
  current_run: null, last_completed_run: { id: 7, tier: 1, wave: 200, coins: 900, ended_at: now - 300 },
  currency: { coins_lower: null, coins_upper: null, reserved: null, available_lower: null, gems: null },
  missions: { state: "pending", reason: "Battle route has no safe mission entry", last_claim_at: null },
  strategy: { id: "strategy-1", name: "Balanced opening", version: 4 },
  source: { revision: "12", hash: "abc" }, recovery: null, unknown_count: 3,
  blockers: ["Awaiting safe navigation"],
};

const labs: LabsSnapshot = { workers: [{ worker: "Air_1", account_id: "100", strategy_name: "Balanced opening",
  read_at: now - 25, wallet: { coins: null, gems: null }, state: "ok", reason: null,
  recent: [], unknown_slots: 4, freshness: "observed", blockers: [],
  plan: { account_id: "100", strategy_revision: 12, wallet_coins: null, jar: 0, evaluated_at: now - 25,
    slots: [{ slot: 1, now: { state: "researching", level: 2, completes_at: now + 3600, overdue_seconds: null,
      read_at: now - 25, stale: false, research_id: "labs.game-speed", research_name: "Game Speed",
      owned: true, evidence_status: "current" },
      next: { lab_id: "labs.game-speed", name: "Game Speed", level: 3, price: 100, seconds: 3600 },
      covered: true, automated: false, why: ["Waiting for lab completion"], note: null,
      capabilities: { observe: true, plan: true, execute: false } }],
    gems: { wallet: null, next: null, price: null, have: null, need: null, automated: false, why: [], steps: [] } },
  }], automated: [], reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [], card_gems: 0,
    labs_unlock_wave: 0, sources: [] } };

test("a stopped worker keeps completed run values historical and capture unmounted", () => {
  render(<FleetLiveCard member={{ ...member, state: "stopped", tier: 1, wave: 200, overview }}
    account={account} labsSnapshot={labs} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 current run" })).toHaveTextContent("No verified current run");
  expect(screen.getByRole("region", { name: "Air_1 last completed run" })).toHaveTextContent("W200");
  expect(screen.getByRole("article", { name: "Air_1 live overview" })).toHaveTextContent("Stopped");
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show screen of Air_1" }));
  expect(screen.getByRole("img", { name: "Live screen of Air_1" })).toBeInTheDocument();
});

test("labs keep unknown slot ownership and pending mission reason visible", () => {
  render(<FleetLiveCard member={{ ...member, overview, route_revision_applied: 12 }}
    account={account} labsSnapshot={labs} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 labs and gems" })).toHaveTextContent("4 slots unknown");
  expect(screen.getByRole("region", { name: "Air_1 labs and gems" })).toHaveTextContent("Game Speed");
  expect(screen.getByRole("region", { name: "Air_1 missions and strategy" })).toHaveTextContent("Battle route has no safe mission entry");
  expect(screen.getByText(/Balanced opening · v4/)).toBeInTheDocument();
});

const battle: RouteEvaluation = { account_id: "100", revision: 12, status: "observed",
  decision: { account_id: "100", stage: "battle", state: "wait", upgrade_id: null,
    item: null, category: null, price: null, wallet_coins: null, reason: "Waiting for safe wave" },
  trace: { matched_rule_id: "battle.wait", reason: "Current phase waits", evidence_age_seconds: 2,
    price_source: "none", variant: null, rejected: [], spend_ceiling: null, branch_id: null,
    phase_id: null, phase_state: "waiting", next_phase_id: null, transition_reason: null,
    eligible_odds: {}, draw_gate: null }, evidence_at: now - 5 };

test("matched battle evaluation gives a reason while account and revision mismatches are withdrawn", () => {
  const activeOverview = { ...overview, health: { ...overview.health, state: "waiting" as const } };
  const view = render(<FleetLiveCard member={{ ...member, overview: activeOverview, route_revision_applied: 12,
    battle_evaluation: battle }} account={account} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 objective and next action" })).toHaveTextContent("Waiting for safe wave");
  expect(screen.getByText(/Balanced opening · v4/)).toBeInTheDocument();
  view.rerender(<FleetLiveCard member={{ ...member, overview: activeOverview, route_revision_applied: 12,
    battle_evaluation: { ...battle, account_id: "old", decision: { ...battle.decision!, account_id: "old", reason: "Old account action" } } }}
    account={account} onInspect={() => {}} />);
  expect(screen.queryByText("Old account action")).not.toBeInTheDocument();
  view.rerender(<FleetLiveCard member={{ ...member, overview: activeOverview, route_revision_applied: 13,
    battle_evaluation: battle }} account={account} onInspect={() => {}} />);
  expect(screen.queryByText("Waiting for safe wave")).not.toBeInTheDocument();
});

test("a stopped worker never presents an old evaluated decision as its active action", () => {
  render(<FleetLiveCard member={{ ...member, overview, route_revision_applied: 12,
    battle_evaluation: battle }} account={account} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 objective and next action" })).toHaveTextContent(
    "Worker stopped; no active action");
  expect(screen.getByRole("region", { name: "Air_1 battle decision" })).not.toHaveTextContent(
    "Waiting for safe wave");
});

test("a Game Speed completion due states why it is still pending", () => {
  const dueLabs: LabsSnapshot = { ...labs, workers: [{ ...labs.workers[0], plan: {
    ...labs.workers[0].plan!, slots: [{ ...labs.workers[0].plan!.slots[0],
      now: { ...labs.workers[0].plan!.slots[0].now, overdue_seconds: 180, completes_at: now - 180 },
      why: ["Waiting for safe menu before increasing speed"] }],
  } }] };
  render(<FleetLiveCard member={{ ...member, overview, route_revision_applied: 12 }}
    account={account} labsSnapshot={dueLabs} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 labs and gems" })).toHaveTextContent(
    "Game Speed completion due · Waiting for safe menu before increasing speed");
});

test("old progressing evidence withdraws current health, run and next action", () => {
  const old = { ...overview, observed_at: now - 7200,
    health: { ...overview.health, state: "progressing" as const, reason: "Old progress" },
    current_run: { id: 8, tier: 1, wave: 99, speed: 3, coins: null, observed_at: now - 7200 } };
  render(<FleetLiveCard member={{ ...member, state: "running", overview: old,
    route_revision_applied: 12, battle_evaluation: battle }} account={account} onInspect={() => {}} />);
  expect(screen.getByText("Health unknown")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Air_1 current run" })).toHaveTextContent("No verified current run");
  expect(screen.getByRole("region", { name: "Air_1 current run" })).toHaveTextContent("Last observed open run: T1 W99");
  expect(screen.getByRole("region", { name: "Air_1 objective and next action" })).not.toHaveTextContent("Waiting for safe wave");
  expect(screen.getByText(/Last health report: Progressing/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Air_1 missions and strategy" })).toHaveTextContent("Missions unknown");
  expect(screen.getByRole("link", { name: /Last observed: Balanced opening · v4/ })).toBeInTheDocument();
});

test("Workshop uses one matched evaluation for item, wallet, price and reason", () => {
  const evaluation: RouteEvaluation = { ...battle,
    decision: { ...battle.decision!, stage: "workshop", state: "buy", item: "Defense",
      price: 75, wallet_coins: 100, reason: "Ready for Defense" } };
  render(<FleetLiveCard member={{ ...member, overview: { ...overview,
    health: { ...overview.health, state: "waiting" } }, route_revision_applied: 12,
    workshop_evaluation: evaluation }} account={account} onInspect={() => {}} />);
  const workshop = screen.getByRole("region", { name: "Air_1 workshop decision" });
  expect(workshop).toHaveTextContent("Defense");
  expect(workshop).toHaveTextContent("75 price");
  expect(workshop).toHaveTextContent("100 coins");
  expect(workshop).toHaveTextContent("Ready for Defense");
  expect(workshop).not.toHaveTextContent("300 coins short");
  expect(within(workshop).getByRole("link", { name: "View workshop" })).toHaveAttribute(
    "href", "/fleet/reroll/workshop/?worker=Air_1");
});

test("stale lab rows cannot claim current ownership or a live completion estimate", () => {
  const staleLabs: LabsSnapshot = { ...labs, workers: [{ ...labs.workers[0],
    freshness: "stale", read_at: now - 7200 }] };
  render(<FleetLiveCard member={{ ...member, overview, route_revision_applied: 12 }}
    account={account} labsSnapshot={staleLabs} onInspect={() => {}} />);
  const region = screen.getByRole("region", { name: "Air_1 labs and gems" });
  expect(region).not.toHaveTextContent("currently verified owned");
  expect(region).not.toHaveTextContent("Soonest expected completion");
  expect(region).toHaveTextContent("Last observed");
});

test("an observed lab row cannot promote a stale slot to current", () => {
  const staleSlotLabs: LabsSnapshot = { ...labs, workers: [{ ...labs.workers[0], plan: {
    ...labs.workers[0].plan!, slots: [{ ...labs.workers[0].plan!.slots[0],
      now: { ...labs.workers[0].plan!.slots[0].now, stale: true } }],
  } }] };
  render(<FleetLiveCard member={{ ...member, overview, route_revision_applied: 12 }}
    account={account} labsSnapshot={staleSlotLabs} onInspect={() => {}} />);
  const region = screen.getByRole("region", { name: "Air_1 labs and gems" });
  expect(region).toHaveTextContent("0 currently verified owned");
  expect(region).toHaveTextContent("1 slot observation unverified");
  expect(region).not.toHaveTextContent("Soonest expected completion");
});

test("per-worker recovery status shows mode, phase, blocker with its fix and costs", () => {
  const recovering = { ...overview, health: { ...overview.health, state: "attention" as const },
    recovery: { mode: "assist" as const, model: "openai/gpt-5.4-nano", configured: true,
      phase: "paused", last_outcome: "unknown", cost_used_microusd: 12_000,
      cost_reserved_microusd: 0, blocker: "unresolved", observed_at: now - 10 } };
  const view = render(<FleetLiveCard member={{ ...member, overview: recovering }} account={account} onInspect={() => {}} />);
  const section = screen.getByRole("region", { name: "Air_1 recovery" });
  expect(section).toHaveTextContent("assist · paused · last unknown");
  expect(within(section).getByRole("status")).toHaveTextContent(/unresolved: .*tools\/reconcile_recovery\.py/);
  expect(section).toHaveTextContent("Cost used $0.0120 · reserved $0.0000");
  for (const [blocker, text] of [["budget_exhausted", /budget is used up/], ["unsupported_model", /model cannot take/]] as const) {
    view.rerender(<FleetLiveCard member={{ ...member, overview: { ...recovering, recovery: { ...recovering.recovery, blocker } } }}
      account={account} onInspect={() => {}} />);
    expect(within(screen.getByRole("region", { name: "Air_1 recovery" })).getByRole("status")).toHaveTextContent(text);
  }
  view.rerender(<FleetLiveCard member={{ ...member, overview: { ...recovering, recovery: null } }} account={account} onInspect={() => {}} />);
  expect(screen.getByRole("region", { name: "Air_1 recovery" })).toHaveTextContent("Recovery status unknown");
});
