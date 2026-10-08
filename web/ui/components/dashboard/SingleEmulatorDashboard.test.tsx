import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { makeAccount } from "@/app/fleet/state/fixtures";
import { SingleEmulatorDashboard } from "./SingleEmulatorDashboard";

const state = vi.hoisted(() => ({
  selected: { key: "worker:solo", account_id: "SOLO", instance: "Solo", kind: "worker", running: false, dashboard_url: null },
  fetchState: vi.fn(), fetchLedger: vi.fn(),
}));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ selected: state.selected }) }));
vi.mock("@/lib/api", () => ({ fetchSingleAccountState: state.fetchState, fetchLedger: state.fetchLedger }));
vi.mock("./LiveMonitor", () => ({ LiveMonitor: () => <p>Device monitor mounted</p> }));
vi.mock("./LabSavings", () => ({ LabSavings: () => <p>Lab savings controls</p> }));
vi.mock("@/components/EmulatorRecovery", () => ({ EmulatorRecovery: () => <p>Available emulators</p> }));
beforeEach(() => {
  vi.clearAllMocks();
  state.selected = { key: "worker:solo", account_id: "SOLO", instance: "Solo", kind: "worker", running: false, dashboard_url: null };
  state.fetchState.mockResolvedValue({ account_id: "SOLO", generated_at: "2026-10-08T10:00:00Z", account: makeAccount({ name: "Solo" }) });
  state.fetchLedger.mockResolvedValue({ account_id: "SOLO", balances: {}, lines: [], rehearsals: 0, next: null });
});

test("offline account still shows all state panels and no device monitor", async () => {
  render(<SingleEmulatorDashboard />);
  expect(await screen.findByText("Lifetime totals")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Account overview" })).toBeInTheDocument();
  for (const label of ["Workshop", "Cards", "Labs", "In-run upgrades"]) {
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  }
  expect(screen.getByText("Lab savings controls")).toBeInTheDocument();
  expect(screen.queryByText("Device monitor mounted")).not.toBeInTheDocument();
});

test("refresh failures preserve saved evidence with an explicit stale warning", async () => {
  render(<SingleEmulatorDashboard />);
  await screen.findByText("Lifetime totals");
  state.fetchState.mockRejectedValue(new Error("Connection lost"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh overview" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
  expect(screen.getByText("Lifetime totals")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Last successful snapshot");
});

test("a response for another account is rejected rather than rendered", async () => {
  state.fetchState.mockResolvedValue({ account_id: "OTHER", generated_at: "2026-10-08T10:00:00Z", account: makeAccount({ name: "Other account" }) });
  render(<SingleEmulatorDashboard />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Account changed");
  expect(screen.queryByText("Other account")).not.toBeInTheDocument();
});

test("ledger failures do not hide the account overview", async () => {
  state.fetchLedger.mockRejectedValue(new Error("Ledger unavailable"));
  render(<SingleEmulatorDashboard />);
  await screen.findByText("Lifetime totals");
  await waitFor(() => expect(screen.getByText(/Ledger unavailable/)).toBeInTheDocument());
});

test("reusing an emulator for a different account clears the previous snapshot", async () => {
  const view = render(<SingleEmulatorDashboard />);
  await screen.findByText("Lifetime totals");
  state.selected = { ...state.selected, account_id: "REPLACEMENT" };
  state.fetchState.mockRejectedValue(new Error("Reconnecting"));
  state.fetchLedger.mockRejectedValue(new Error("Reconnecting"));
  view.rerender(<SingleEmulatorDashboard />);
  await screen.findByText(/Could not refresh account/);
  expect(screen.queryByText("Lifetime totals")).not.toBeInTheDocument();
});
