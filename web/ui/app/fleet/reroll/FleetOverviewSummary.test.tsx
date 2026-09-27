import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import type { FleetOverview, RerollMember } from "@/lib/fleet";
import { FleetOverviewSummary } from "./FleetOverviewSummary";

const now = Math.floor(Date.now() / 1000);
const overview: FleetOverview = {
  account_id: "100", lease_id: "a", attempt_id: "1", observed_at: now - 30,
  health: { state: "waiting", reason: "Expected timer", last_completed_scan_at: now - 30,
    last_progress_at: now - 30, incidents_open: null },
  current_run: null, last_completed_run: null,
  currency: { coins_lower: null, coins_upper: null, reserved: null, available_lower: null, gems: null },
  missions: { state: "unknown", reason: null, last_claim_at: null }, strategy: null,
  source: { revision: null, hash: null }, recovery: null, unknown_count: 2, blockers: [],
};
const member: RerollMember = { name: "Air_1", endpoint: "host", lease_id: "a", state: "running",
  account_id: "100", overview };

test("semantic counts leave missing health and incidents unknown despite running lifecycle", () => {
  render(<FleetOverviewSummary members={[member, { ...member, name: "Air_2", account_id: "200",
    lease_id: "b", overview: undefined }]} />);
  expect(screen.getByText("1", { selector: '[data-count="waiting"]' })).toBeInTheDocument();
  expect(screen.getByText("1", { selector: '[data-count="unknown"]' })).toBeInTheDocument();
  expect(screen.getByText(/Incident count unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Oldest evidence 30s ago/)).toBeInTheDocument();
});

test("an old progressing observation is counted as unknown, with its age retained", () => {
  render(<FleetOverviewSummary members={[{ ...member, overview: { ...overview,
    observed_at: now - 7200, health: { ...overview.health, state: "progressing", incidents_open: 3 } } }]} />);
  expect(screen.getByText("0", { selector: '[data-count="progressing"]' })).toBeInTheDocument();
  expect(screen.getByText("1", { selector: '[data-count="unknown"]' })).toBeInTheDocument();
  expect(screen.getByText(/Incident count unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Oldest evidence 2h ago/)).toBeInTheDocument();
});
