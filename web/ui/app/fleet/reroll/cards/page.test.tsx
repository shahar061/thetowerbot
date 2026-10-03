import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import FleetCardsPage from "./page";
import { AssignmentStatus } from "./AssignmentStatus";
import type { CardsProjection, FleetCardsSnapshot } from "@/lib/cards";
const mocks = vi.hoisted(() => ({ fetchFleetCards: vi.fn(), fetchCardCatalog: vi.fn(), assignFleetCardProgram: vi.fn(), refreshFleetCards: vi.fn(), setFleetCardsAutomation: vi.fn(), selectFleetCardLoadout: vi.fn(), fleetCardAccountRequest: vi.fn(), fetchFleetStrategies: vi.fn() }));
vi.mock("@/lib/cards", async original => ({ ...await original<typeof import("@/lib/cards")>(), ...mocks }));
vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), fetchFleetStrategies: mocks.fetchFleetStrategies }));
const program = { version: 1 as const, gem_cap: 100, goals: [], loadouts: [{ id: "farm", name: "Farm", priority: [] }], selected_loadout_id: "farm" };
const projection = (id: string): CardsProjection => ({ account_id: id, program, program_revision: "v1", scope: { account_id: id, lease_id: "lease", generation: "g", epoch: 1 }, config_owner: "fleet", snapshot: null, fresh: false, policy: { enabled: false, gem_floor: 20, max_per_visit: 1, batch: "x1" }, active_budget: { cycle_id: "cycle", cap: 100, spent: 20, pending: 0 }, preconditions: { expected_account_id: id, expected_generation: "g", expected_epoch: 1, expected_program_revision: "v1" }, preview: { fresh: false, goals: [], loadouts: [] }, capabilities: { inventory: true, buy_one: false, buy_ten: false, buy_slot: false, assign: true, reasons: {} }, decision: { kind: "wait", reason: "disabled" }, recent_operations: [], read_only_reason: null });
const snapshot = (): FleetCardsSnapshot => ({ revision: 12, accounts: ["a", "b"].map(id => ({ account_id: id, worker: id === "a" ? "one" : null, cards: id === "a" ? projection(id) : { ...projection(id), preconditions: null, read_only_reason: "worker_unavailable" }, assignment: { account_id: id, worker: null, strategy_id: "saved", strategy_version: 3, program, overlay_id: id, published_revision: 12, applied_revision: id === "a" ? 12 : null, status: id === "a" ? "active" : "pending" } })) });
beforeEach(() => { vi.clearAllMocks(); mocks.fetchFleetCards.mockResolvedValue(snapshot()); mocks.fetchCardCatalog.mockResolvedValue({ cards: [], max_gem_slots: 21 }); mocks.fetchFleetStrategies.mockResolvedValue({ revision: 3, templates: [], strategies: [{ id: "saved", name: "Farm", version: 3, baseline: { cards: program } }] }); mocks.assignFleetCardProgram.mockResolvedValue({ revision: 13, results: [{ account_id: "a", status: "pending" }, { account_id: "b", status: "unavailable", reason: "offline" }] }); });
it("distinguishes exact worker acknowledgment", () => { render(<AssignmentStatus published={12} applied={11} />); expect(screen.getByText("Pending on worker")).toBeInTheDocument(); });
it("filters comparison rows and assigns an explicit immutable revision with partial results", async () => { render(<FleetCardsPage />); await screen.findByRole("button", { name: "Inspect a" }); fireEvent.change(screen.getByLabelText("Filter accounts"), { target: { value: "b" } }); expect(screen.queryByRole("button", { name: "Inspect a" })).not.toBeInTheDocument(); fireEvent.change(screen.getByLabelText("Filter accounts"), { target: { value: "" } }); fireEvent.click(screen.getByLabelText("Select a")); fireEvent.click(screen.getByLabelText("Select b")); fireEvent.change(screen.getByLabelText("Saved Cards program"), { target: { value: "saved@3" } }); fireEvent.click(screen.getByRole("button", { name: "Assign program revision" })); await waitFor(() => expect(mocks.assignFleetCardProgram).toHaveBeenCalled()); expect(mocks.assignFleetCardProgram.mock.calls[0][0]).toEqual({ expected_revision: 12, strategy_id: "saved", strategy_version: 3, targets: [{ account_id: "a", worker: "one" }, { account_id: "b" }] }); expect(await screen.findByText(/b: unavailable/)).toBeInTheDocument(); expect(mocks.setFleetCardsAutomation).not.toHaveBeenCalled(); expect(mocks.fleetCardAccountRequest).not.toHaveBeenCalled(); });
it("opens shared detail and fences apply when the saved program changes", async () => { render(<FleetCardsPage />); fireEvent.click(await screen.findByRole("button", { name: "Inspect a" })); expect(await screen.findByText("Execution & budget")).toBeInTheDocument(); const updated = snapshot(); updated.accounts[0].cards = { ...projection("a"), program_revision: "v2", program: { ...program, selected_loadout_id: null }, preconditions: { ...projection("a").preconditions!, expected_program_revision: "v2" } }; mocks.fetchFleetCards.mockResolvedValue(updated); fireEvent.click(screen.getByRole("button", { name: "Reload fleet" })); await screen.findAllByText(/Saved authority changed/); expect(screen.getByRole("button", { name: "Apply saved loadout" })).toBeDisabled(); expect(screen.getByRole("button", { name: "Reload latest plan" })).toBeEnabled(); });

it("reports offline refresh separately without submitting that account", async () => {
  mocks.refreshFleetCards.mockResolvedValue({ results: [{ account_id: "a", worker: "one", status: "accepted" }] });
  render(<FleetCardsPage />); await screen.findByLabelText("Select a");
  fireEvent.click(screen.getByLabelText("Select a")); fireEvent.click(screen.getByLabelText("Select b"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh selected accounts" }));
  await screen.findByText(/b: unavailable/);
  expect(mocks.refreshFleetCards.mock.calls[0][0]).toHaveLength(1);
  expect(mocks.refreshFleetCards.mock.calls[0][0][0]).toMatchObject({ expected_account_id: "a", worker: "one", kind: "refresh" });
});
it("retries the exact account command and invalidates retries on lease change", async () => {
  mocks.fleetCardAccountRequest.mockRejectedValue(new TypeError("interrupted"));
  render(<FleetCardsPage />); fireEvent.click(await screen.findByRole("button", { name: "Inspect a" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry original request" }));
  await waitFor(() => expect(mocks.fleetCardAccountRequest).toHaveBeenCalledTimes(2));
  expect(mocks.fleetCardAccountRequest.mock.calls[1][0]).toEqual(mocks.fleetCardAccountRequest.mock.calls[0][0]);
  const changed = snapshot(); changed.accounts[0].cards.scope = { ...changed.accounts[0].cards.scope!, lease_id: "replacement" };
  mocks.fetchFleetCards.mockResolvedValue(changed); fireEvent.click(screen.getByRole("button", { name: "Reload fleet" }));
  await screen.findAllByText(/Saved authority changed/);
  expect(screen.queryByRole("button", { name: "Retry original request" })).not.toBeInTheDocument();
});
it("does not show an old account's late command response in the next detail", async () => {
  let finish!: (value: unknown) => void;
  mocks.fleetCardAccountRequest.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<FleetCardsPage />); fireEvent.click(await screen.findByRole("button", { name: "Inspect a" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
  fireEvent.click(screen.getByRole("button", { name: "Inspect b" }));
  await act(async () => { finish({ account_id: "a", worker: "one", status: "unavailable", reason: "old account result" }); });
  expect(screen.queryByText("old account result")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh observations" })).toBeDisabled();
});
it("retries uncertain bulk refresh using original keys and discards on selection change", async () => {
  mocks.refreshFleetCards.mockResolvedValue({ results: [{ account_id: "a", worker: "one", status: "unavailable", reason: "timeout" }] });
  render(<FleetCardsPage />); fireEvent.click(await screen.findByLabelText("Select a"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh selected accounts" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry original selected request" }));
  await waitFor(() => expect(mocks.refreshFleetCards).toHaveBeenCalledTimes(2));
  expect(mocks.refreshFleetCards.mock.calls[1][0]).toEqual(mocks.refreshFleetCards.mock.calls[0][0]);
  fireEvent.click(screen.getByLabelText("Select b"));
  expect(screen.queryByRole("button", { name: "Retry original selected request" })).not.toBeInTheDocument();
});
it("serializes selected loadout publications with the returned route revision", async () => {
  mocks.selectFleetCardLoadout.mockResolvedValueOnce({ revision: 13, assignment: { ...snapshot().accounts[0].assignment, status: "pending" } }).mockRejectedValueOnce(new Error("offline"));
  render(<FleetCardsPage />); fireEvent.click(await screen.findByLabelText("Select a")); fireEvent.click(screen.getByLabelText("Select b"));
  fireEvent.change(screen.getByLabelText("Selected loadout"), { target: { value: "farm" } });
  fireEvent.click(screen.getByRole("button", { name: "Publish loadout selection" }));
  await screen.findByText(/b: unavailable/);
  expect(mocks.selectFleetCardLoadout.mock.calls[0][0]).toEqual({ expected_revision: 12, account_id: "a", loadout_id: "farm" });
  expect(mocks.selectFleetCardLoadout.mock.calls[1][0]).toEqual({ expected_revision: 13, account_id: "b", loadout_id: "farm" });
});
it("detail automation targets only its account and never copies effective policy", async () => {
  mocks.setFleetCardsAutomation.mockResolvedValue({ results: [{ account_id: "a", worker: "one", status: "accepted", result: {} }] });
  render(<FleetCardsPage />); fireEvent.click(await screen.findByRole("button", { name: "Inspect a" }));
  fireEvent.click(screen.getByRole("button", { name: "Enable Cards for a" }));
  await waitFor(() => expect(mocks.setFleetCardsAutomation).toHaveBeenCalled());
  expect(mocks.setFleetCardsAutomation.mock.calls[0][0]).toEqual([{ ...projection("a").preconditions, worker: "one", enabled: true }]);
  expect(mocks.fleetCardAccountRequest).not.toHaveBeenCalled();
});
