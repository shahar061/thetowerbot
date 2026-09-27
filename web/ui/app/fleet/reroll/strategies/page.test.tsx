import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import StrategiesPage from "./page";
import type { RerollSnapshot } from "@/lib/fleet";
import type { LabsRow, LabsSnapshot } from "@/lib/labs";
vi.mock("./routeCanvas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));
vi.mock("./studio.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

let pool: RerollSnapshot;
let query = new URLSearchParams();
let routeResponse: { revision: number; assignments: Record<string, unknown> };
let libraryResponse: { templates: unknown[]; strategies: unknown[] };
let labsState: { snapshot: LabsSnapshot | null; loading: boolean; error: string | null; refresh: () => Promise<void> };
vi.mock("next/navigation", () => ({ useSearchParams: () => query }));
vi.mock("@/lib/api", () => ({ fetchBuildRoute: () => Promise.resolve(routeResponse),
  fetchUpgrades: () => Promise.resolve([]), fetchFleetStrategies: () => Promise.resolve(libraryResponse) }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool, loading: false, error: null }) }));
vi.mock("../FleetLabsContext", () => ({ useFleetLabs: () => labsState }));
vi.mock("./StrategyStudio", () => ({ StrategyStudio: ({ labsSnapshot, labObservations, initialLane, initialStrategyId, assignedSnapshot, initialWorker, initialAccount, initialSlot, linkIdentity }: { labsSnapshot: LabsSnapshot | null; labObservations?: LabsRow[]; initialLane?: string; initialStrategyId?: string; assignedSnapshot?: { baseline: { labs: { steps: string[] } }; version: number }; initialWorker?: string; initialAccount?: string; initialSlot?: number; linkIdentity?: string }) =>
  <><p>{labsSnapshot?.workers[0]?.wallet.coins == null ? "Preview unavailable" : `Preview wallet ${labsSnapshot.workers[0].wallet.coins}`}</p>
    {labObservations?.flatMap(row => row.plan?.slots.map(slot => <p key={`${row.worker}-${slot.slot}`}>{slot.now.research_name}</p>) ?? [])}
    <p data-testid="initial-context">{JSON.stringify({ initialLane, initialStrategyId, initialWorker, initialAccount, initialSlot, linkIdentity, snapshotVersion: assignedSnapshot?.version, snapshotSteps: assignedSnapshot?.baseline.labs.steps })}</p></> }));
vi.mock("../Purchases", () => ({ WorkerBattlePurchases: ({ accountKey }: { accountKey?: string }) => <p>Evidence for {accountKey}</p> }));
beforeEach(() => {
  query = new URLSearchParams();
  routeResponse = { revision: 1, assignments: {} };
  libraryResponse = { templates: [], strategies: [] };
  labsState = { snapshot: null, loading: false, error: null, refresh: async () => {} };
  pool = { candidates: [], members: [
    { name: "Air_2", endpoint: "", lease_id: "b", state: "running", account_id: "200", account_key: "two", variant_name: "Turtle",
      reroll_plan: { account_id: "200", stage: "turtle", goal: "Reach T1 W60", state: "buy", item: "Defense Absolute", price: 10, wallet_coins: 50, lifetime_coins: null, reason: "Survive longer", observed_at: 100 } },
    { name: "Air_1", endpoint: "", lease_id: "a", state: "running", account_id: "100", account_key: "one", variant_name: "Balanced",
      reroll_plan: { account_id: "100", stage: "opening", goal: "Reach T1 W20", state: "buy", item: "Damage", price: 10, wallet_coins: 50, lifetime_coins: null, reason: "Opening damage", observed_at: 100 } },
  ] };
});

test("a Labs link selects the exact assigned version and account when current in the library", async () => {
  query = new URLSearchParams({ worker: "Air_1", account: "100", slot: "4" });
  routeResponse.assignments.Air_1 = { account_id: "100", strategy_id: "mine", strategy_name: "Mine", strategy_version: 2,
    baseline: { labs: { steps: ["assigned-v2"] } } };
  libraryResponse.strategies = [{ id: "mine", name: "Mine", version: 2 }];
  render(<StrategiesPage />);
  expect(await screen.findByTestId("initial-context")).toHaveTextContent('"initialStrategyId":"mine"');
  expect(screen.getByTestId("initial-context")).toHaveTextContent('"initialLane":"labs"');
  expect(screen.getByTestId("initial-context")).toHaveTextContent('"initialSlot":4');
  expect(screen.getByTestId("initial-context")).toHaveTextContent('"initialAccount":"100"');
});

test("an older assignment opens its embedded baseline for copying, never the latest library version", async () => {
  query = new URLSearchParams({ worker: "Air_1", account: "100", slot: "3" });
  routeResponse.assignments.Air_1 = { account_id: "100", strategy_id: "mine", strategy_name: "Mine", strategy_version: 1,
    baseline: { labs: { steps: ["assigned-v1"] } } };
  libraryResponse.strategies = [{ id: "mine", name: "Mine", version: 2, baseline: { labs: { steps: ["latest-v2"] } } }];
  render(<StrategiesPage />);
  expect(await screen.findByTestId("initial-context")).toHaveTextContent('"snapshotVersion":1');
  expect(screen.getByTestId("initial-context")).toHaveTextContent("assigned-v1");
  expect(screen.getByTestId("initial-context")).not.toHaveTextContent("latest-v2");
});

test("a replaced account cannot select the old worker assignment", async () => {
  query = new URLSearchParams({ worker: "Air_1", account: "old", slot: "2" });
  routeResponse.assignments.Air_1 = { account_id: "old", strategy_id: "mine", strategy_name: "Mine", strategy_version: 1,
    baseline: { labs: { steps: ["old"] } } };
  render(<StrategiesPage />);
  expect(await screen.findByText(/Planning link no longer matches/)).toBeInTheDocument();
  expect(screen.getByTestId("initial-context")).not.toHaveTextContent("snapshotVersion");
});

test("a linked assigned snapshot is withdrawn when the worker account changes", async () => {
  query = new URLSearchParams({ worker: "Air_1", account: "100", slot: "2" });
  routeResponse.assignments.Air_1 = { account_id: "100", strategy_id: "mine", strategy_name: "Mine", strategy_version: 1,
    baseline: { labs: { steps: ["assigned-v1"] } } };
  libraryResponse.strategies = [{ id: "mine", name: "Mine", version: 2 }];
  const view = render(<StrategiesPage />);
  expect(await screen.findByTestId("initial-context")).toHaveTextContent('"snapshotVersion":1');
  pool = { ...pool, members: [{ ...pool.members[0] }, { ...pool.members[1], account_id: "replacement" }] };
  view.rerender(<StrategiesPage />);
  expect(screen.getByText(/Planning link no longer matches/)).toBeInTheDocument();
  expect(screen.getByTestId("initial-context")).not.toHaveTextContent("snapshotVersion");
});

test("a mounted page resolves a second Labs link for another account and assigned version", async () => {
  query = new URLSearchParams({ worker: "Air_1", account: "100", slot: "2" });
  routeResponse.assignments.Air_1 = { account_id: "100", strategy_id: "mine", strategy_name: "Mine", strategy_version: 1,
    baseline: { labs: { steps: ["assigned-one"] } } };
  routeResponse.assignments.Air_2 = { account_id: "200", strategy_id: "other", strategy_name: "Other", strategy_version: 2,
    baseline: { labs: { steps: ["assigned-two"] } } };
  libraryResponse.strategies = [{ id: "mine", version: 3 }, { id: "other", version: 3 }];
  const view = render(<StrategiesPage />);
  expect(await screen.findByTestId("initial-context")).toHaveTextContent("assigned-one");
  const firstIdentity = JSON.parse(screen.getByTestId("initial-context").textContent ?? "{}").linkIdentity;
  query = new URLSearchParams({ worker: "Air_2", account: "200", slot: "5" });
  view.rerender(<StrategiesPage />);
  expect(screen.getByTestId("initial-context")).toHaveTextContent("assigned-two");
  expect(screen.getByTestId("initial-context")).toHaveTextContent('"initialAccount":"200"');
  expect(screen.getByTestId("initial-context")).toHaveTextContent('"initialSlot":5');
  expect(JSON.parse(screen.getByTestId("initial-context").textContent ?? "{}").linkIdentity).not.toBe(firstIdentity);
});

test("a failed labs refresh shows retained-data status and withdraws the wallet preview", async () => {
  pool.members[1].route_revision_applied = 7;
  labsState = { ...labsState, snapshot: { workers: [{ worker: "Air_1", account_id: "100", strategy_name: null, read_at: 100,
    wallet: { coins: 123, gems: null }, plan: { account_id: "100", strategy_revision: 7, evaluated_at: 100,
      wallet_coins: 123, jar: 0, slots: [], gems: { wallet: null, next: null, price: null, have: null, need: null,
        automated: false, why: [], steps: [] } }, state: "unknown", reason: null, recent: [],
    unknown_slots: 5, freshness: "historical", blockers: [] }], automated: [],
    reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [], card_gems: 0, labs_unlock_wave: 40, sources: [] } } };
  const view = render(<StrategiesPage />);
  expect(await screen.findByText("Preview wallet 123")).toBeInTheDocument();
  labsState = { ...labsState, error: "labs offline" };
  view.rerender(<StrategiesPage />);
  expect(screen.getByRole("alert", { name: /Labs/ })).toHaveTextContent("labs offline");
  expect(screen.getByText("Preview unavailable")).toBeInTheDocument();
  expect(screen.queryByText("Preview wallet 123")).not.toBeInTheDocument();
});

test("compares effective account plans in stable worker order with separate battle evidence", () => {
  render(<StrategiesPage />);
  fireEvent.click(screen.getByRole("tab", { name: /Fleet roads/ }));
  const columns = screen.getAllByRole("article");
  expect(within(columns[0]).getByRole("heading", { name: "Air_1" })).toBeInTheDocument();
  expect(within(columns[0]).getByText("Damage")).toBeInTheDocument();
  expect(within(columns[0]).getByText(/Phase: Opening/)).toBeInTheDocument();
  expect(within(columns[0]).getByText("Evidence for one")).toBeInTheDocument();
  expect(within(columns[1]).getByText("Defense Absolute")).toBeInTheDocument();
  expect(screen.getByText(/Live battle intent unavailable/)).toBeInTheDocument();
});

test("rejects a plan for a replaced account and omits hidden workers", () => {
  pool.members[0].reroll_plan!.account_id = "old";
  pool.members[1].hidden = true;
  render(<StrategiesPage />);
  fireEvent.click(screen.getByRole("tab", { name: /Fleet roads/ }));
  expect(screen.getAllByRole("article")).toHaveLength(1);
  expect(screen.queryByText("Defense Absolute")).not.toBeInTheDocument();
  expect(screen.getByText(/Workshop plan unavailable for this account/)).toBeInTheDocument();
});

test("slot observations survive an unapplied plan revision but never account replacement", async () => {
  pool.members[0].route_revision_applied = 2;
  labsState.snapshot = { automated: [], reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] },
    workers: [{ worker: "Air_2", account_id: "200", read_at: 100, strategy_name: null, state: "ok", reason: null, recent: [], wallet: { coins: 55, gems: null },
      plan: { account_id: "200", strategy_revision: 1, wallet_coins: 55, jar: 0,
        gems: { wallet: null, next: null, price: null, have: null, need: null, automated: false, why: [], steps: [] },
        slots: [{ slot: 2, now: { state: "researching", research_name: "Observed Attack Speed", level: 2, completes_at: null, overdue_seconds: null, read_at: 100, stale: true },
          next: null, covered: null, automated: false, why: [], note: null }] } }] };
  const view = render(<StrategiesPage />);
  expect(await screen.findByText("Observed Attack Speed")).toBeInTheDocument();
  expect(screen.getByText("Preview unavailable")).toBeInTheDocument();
  pool = { ...pool, members: [{ ...pool.members[0], account_id: "replacement" }, pool.members[1]] };
  view.rerender(<StrategiesPage />);
  expect(screen.queryByText("Observed Attack Speed")).not.toBeInTheDocument();
});
