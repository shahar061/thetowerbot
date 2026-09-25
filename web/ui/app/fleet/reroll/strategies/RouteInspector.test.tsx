import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import type { BuildRoutePreview, RouteEvaluation } from "@/lib/buildRoute";
import type { RerollMember } from "@/lib/fleet";
import { RouteInspector } from "./RouteInspector";

function evaluation(account_id: string): RouteEvaluation {
  return {
    account_id, revision: 1, status: "unknown", decision: null, evidence_at: null,
    trace: { matched_rule_id: "", reason: "No fresh facts", evidence_age_seconds: null,
      price_source: "unknown", variant: null, rejected: [], spend_ceiling: null,
      branch_id: null, phase_id: null, phase_state: null, next_phase_id: null,
      transition_reason: null, eligible_odds: {}, draw_gate: null },
  };
}

const members: RerollMember[] = [
  { name: "Air_39", endpoint: "", lease_id: "", state: "paused", account_id: "new-account" },
  { name: "Air_40", endpoint: "", lease_id: "", state: "running", account_id: "hidden-account", hidden: true },
];

const preview: BuildRoutePreview = {
  saved_revision: 1, proposed_revision: 2,
  members: [
    { worker: "Air_18", account_id: "retired-account", current: evaluation("retired-account"), proposed: evaluation("retired-account") },
    { worker: "Air_39", account_id: "new-account", current: evaluation("new-account"), proposed: evaluation("new-account") },
    { worker: "Air_39", account_id: "old-account", current: evaluation("old-account"), proposed: evaluation("old-account") },
    { worker: "Air_40", account_id: "hidden-account", current: evaluation("hidden-account"), proposed: evaluation("hidden-account") },
  ],
};

test("decision preview shows only current visible accounts, including paused members", () => {
  render(<RouteInspector preview={preview} members={members} />);
  expect(screen.getAllByRole("heading", { name: "Air_39" })).toHaveLength(1);
  expect(screen.getByText("new-account")).toBeInTheDocument();
  expect(screen.queryByText("retired-account")).not.toBeInTheDocument();
  expect(screen.queryByText("old-account")).not.toBeInTheDocument();
  expect(screen.queryByText("hidden-account")).not.toBeInTheDocument();
});

test("decision preview explains when no current fleet account can be compared", () => {
  render(<RouteInspector preview={preview} members={[]} />);
  expect(screen.getByText("No current fleet emulators to compare.")).toBeInTheDocument();
});

test("shows evidence health and provenance independently for each lane", () => {
  const lanePreview: BuildRoutePreview = { ...preview, members: [{ worker: "Air_39",
    account_id: "new-account", current: evaluation("new-account"), proposed: evaluation("new-account"),
    evidence: {
      workshop: { status: "verified", source: "account revision + price memory + ledger",
        observed_at: 1_790_351_800, reason: null },
      battle: { status: "unknown", source: "route snapshot", observed_at: null,
        reason: "Waiting for first verified battle observation" },
      resources: { status: "stale", source: "account-bound lab cadence",
        observed_at: 1_790_351_700, reason: "Observation is stale" },
    },
  }] };
  render(<RouteInspector preview={lanePreview} members={members} />);
  const lanes = screen.getByLabelText("Evidence by lane");
  expect(lanes).toBeInTheDocument();
  expect(lanes.textContent).toContain("account revision + price memory + ledger");
  expect(lanes.textContent).toContain("Waiting for first verified battle observation");
  expect(lanes.textContent).toContain("Observation is stale");
});

test("shows the active workshop phase and the reason the next phase has not started", () => {
  const current = evaluation("new-account");
  current.trace.phase_id = "economy";
  current.trace.phase_state = "waiting";
  current.trace.next_phase_id = "opening.objectives";
  current.trace.transition_reason = "Waiting for an affordable utility upgrade to reach the allocation.";
  const phasePreview: BuildRoutePreview = { ...preview, members: [{ worker: "Air_39",
    account_id: "new-account", current, proposed: current }] };
  render(<RouteInspector preview={phasePreview} members={members} />);
  expect(screen.getAllByText(/Early Economy · waiting/)).toHaveLength(2);
  expect(screen.getAllByText(/Waiting for an affordable utility upgrade/)).toHaveLength(2);
});

test("unobserved accounts show one compact waiting state", () => {
  const workshop = { ...evaluation("new-account"),
    trace: { ...evaluation("new-account").trace, reason: "Waiting for first verified Workshop observation" } };
  const battle = { ...evaluation("new-account"),
    trace: { ...evaluation("new-account").trace, reason: "Waiting for first verified battle observation" } };
  const pending: BuildRoutePreview = { ...preview, members: [{
    worker: "Air_39", account_id: "new-account", current: workshop, proposed: workshop,
    current_battle: battle, proposed_battle: battle,
    current_resources: { gem_step: { action: "unknown", status: "unknown", reason: "Waiting for observation" },
      lab_step: { action: "unknown", status: "unknown", reason: "Waiting for observation" } },
    proposed_resources: { gem_step: { action: "unknown", status: "unknown", reason: "Waiting for observation" },
      lab_step: { action: "unknown", status: "unknown", reason: "Waiting for observation" } },
  }] };
  render(<RouteInspector preview={pending} members={members} />);
  expect(screen.getByText("Waiting for first observations")).toBeInTheDocument();
  expect(screen.getByText(/Workshop, battle, and resource comparisons will appear/)).toBeInTheDocument();
  expect(screen.queryByText(/Current · Waiting for first verified Workshop observation/)).not.toBeInTheDocument();
});
