import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FleetLabsProvider, useFleetLabs } from "./FleetLabsContext";
import type { RerollSnapshot } from "@/lib/fleet";
import type { LabsRow, LabsSnapshot } from "@/lib/labs";
import { FleetLiveCard } from "./FleetLiveCard";

const fetchLabs = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchFleetLabs: fetchLabs }));
let pool: RerollSnapshot;
vi.mock("./RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool }) }));
const row: LabsRow = { worker: "Air_1", account_id: "a", strategy_name: null, read_at: null,
  wallet: { coins: null, gems: null }, plan: null, state: "unknown", reason: null,
  recent: [], unknown_slots: 5, freshness: "unknown", blockers: [] };
const snapshot: LabsSnapshot = { workers: [row], automated: [], reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [],
  card_gems: 0, labs_unlock_wave: 40, sources: [] } };
function Probe({ name }: { name: string }) {
  const { snapshot: labs, loading, error, refresh } = useFleetLabs();
  return <div data-testid={name}>{loading ? "loading" : "ready"} {labs?.workers[0]?.account_id ?? "none"} {labs?.workers[0]?.reason ?? ""} {error ?? ""}
    <button onClick={() => void refresh()}>Refresh {name}</button></div>;
}
beforeEach(() => {
  vi.useFakeTimers(); fetchLabs.mockReset();
  pool = { candidates: [], members: [{ name: "Air_1", endpoint: "", state: "running", account_id: "a", lease_id: "one",
    overview: { attempt_id: "attempt-one" } } as RerollSnapshot["members"][number]] };
});
afterEach(() => { vi.useRealTimers(); });

test("three cards share one nonoverlapping request and polling cleans up", async () => {
  let resolve!: (value: typeof snapshot) => void;
  fetchLabs.mockImplementationOnce(() => new Promise(done => { resolve = done; })).mockResolvedValue(snapshot);
  const view = render(<FleetLabsProvider><Probe name="one" /><Probe name="two" /><Probe name="three" /></FleetLabsProvider>);
  expect(fetchLabs).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText("Refresh two"));
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(fetchLabs).toHaveBeenCalledTimes(1);
  await act(async () => { resolve(snapshot); });
  expect(screen.getByTestId("three")).toHaveTextContent("a");
  await act(async () => {});
  expect(fetchLabs).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(fetchLabs).toHaveBeenCalledTimes(3);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(fetchLabs).toHaveBeenCalledTimes(3);
});

test("late old-account response cannot populate a changed account", async () => {
  let resolveOld!: (value: typeof snapshot) => void;
  fetchLabs.mockImplementationOnce(() => new Promise(done => { resolveOld = done; }))
    .mockResolvedValueOnce({ ...snapshot, workers: [{ ...row, account_id: "b" }] });
  const view = render(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  pool = { ...pool, members: [{ ...pool.members[0], account_id: "b", lease_id: "two",
    overview: { ...pool.members[0].overview!, attempt_id: "attempt-two" } }] };
  view.rerender(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  await act(async () => {});
  expect(screen.getByTestId("one")).toHaveTextContent("b");
  await act(async () => { resolveOld(snapshot); });
  expect(screen.getByTestId("one")).toHaveTextContent("b");
});

test("failed labs refresh retains scoped data and foreground return retries", async () => {
  fetchLabs.mockResolvedValueOnce(snapshot).mockRejectedValueOnce(new Error("labs offline")).mockResolvedValueOnce(snapshot);
  render(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  await act(async () => {});
  await act(async () => { fireEvent.click(screen.getByText("Refresh one")); });
  expect(screen.getByTestId("one")).toHaveTextContent("a labs offline");
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
  expect(fetchLabs).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("one")).not.toHaveTextContent("labs offline");
});

test("a labs failure leaves the fleet health card available", async () => {
  fetchLabs.mockRejectedValue(new Error("labs offline"));
  render(<FleetLabsProvider><FleetLiveCard member={pool.members[0]} onInspect={() => {}} /><Probe name="one" /></FleetLabsProvider>);
  await act(async () => {});
  expect(screen.getByRole("article", { name: "Air_1 live overview" })).toBeInTheDocument();
  expect(screen.getByTestId("one")).toHaveTextContent("labs offline");
});

test("an unmounted provider cannot deliver its late response to a fresh mount", async () => {
  let resolveOld!: (value: typeof snapshot) => void;
  fetchLabs.mockImplementationOnce(() => new Promise(done => { resolveOld = done; }))
    .mockResolvedValueOnce({ ...snapshot, workers: [{ ...row, account_id: "a", reason: "fresh" }] });
  const old = render(<FleetLabsProvider><Probe name="old" /></FleetLabsProvider>);
  old.unmount();
  render(<FleetLabsProvider><Probe name="new" /></FleetLabsProvider>);
  await act(async () => {});
  await act(async () => { resolveOld(snapshot); });
  expect(screen.getByTestId("new")).toHaveTextContent("a");
  expect(screen.getByTestId("new")).toHaveTextContent("fresh");
  expect(fetchLabs).toHaveBeenCalledTimes(2);
});

test("a second assignment during the queued read schedules a third nonoverlapping read", async () => {
  const resolvers: Array<(value: typeof snapshot) => void> = [];
  fetchLabs.mockImplementation(() => new Promise(done => { resolvers.push(done); }));
  render(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  fireEvent.click(screen.getByText("Refresh one"));
  expect(fetchLabs).toHaveBeenCalledTimes(1);
  await act(async () => { resolvers[0](snapshot); });
  expect(fetchLabs).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByText("Refresh one"));
  await act(async () => { resolvers[1]({ ...snapshot, workers: [{ ...row, reason: "before second assignment" }] }); });
  expect(fetchLabs).toHaveBeenCalledTimes(3);
  await act(async () => { resolvers[2]({ ...snapshot, workers: [{ ...row, reason: "after second assignment" }] }); });
  expect(screen.getByTestId("one")).toHaveTextContent("after second assignment");
});

test("an old attempt response is rejected even when account and lease stay the same", async () => {
  const overview = { account_id: "a", lease_id: "one", attempt_id: "old", observed_at: 100,
    health: { state: "stopped" as const, reason: null, last_completed_scan_at: null, last_progress_at: null, incidents_open: null },
    current_run: null, last_completed_run: null,
    currency: { coins_lower: null, coins_upper: null, reserved: null, available_lower: null, gems: null },
    missions: { state: "unknown" as const, reason: null, last_claim_at: null }, strategy: null,
    source: { revision: null, hash: null }, recovery: null, unknown_count: 1, blockers: [] };
  pool = { ...pool, members: [{ ...pool.members[0], overview }] };
  let resolveOld!: (value: typeof snapshot) => void;
  fetchLabs.mockImplementationOnce(() => new Promise(done => { resolveOld = done; }))
    .mockResolvedValueOnce({ ...snapshot, workers: [{ ...row, reason: "new attempt" }] });
  const view = render(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  pool = { ...pool, members: [{ ...pool.members[0], overview: { ...overview, attempt_id: "new" } }] };
  view.rerender(<FleetLabsProvider><Probe name="one" /></FleetLabsProvider>);
  await act(async () => {});
  await act(async () => { resolveOld({ ...snapshot, workers: [{ ...row, reason: "old attempt" }] }); });
  expect(screen.getByTestId("one")).toHaveTextContent("new attempt");
  expect(screen.getByTestId("one")).not.toHaveTextContent("old attempt");
});
