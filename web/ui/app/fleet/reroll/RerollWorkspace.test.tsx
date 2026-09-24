import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { RerollWorkspaceProvider, useRerollWorkspace } from "./RerollWorkspace";
import type { RerollSnapshot } from "@/lib/fleet";
const fetchPool = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchReroll: fetchPool }));
vi.mock("@/lib/fleetRedirect", () => ({ rerollCoordinatorUrl: () => null }));
const snapshot = { members: [], candidates: [], concurrency_limit: 2 } as unknown as RerollSnapshot;
function Probe() {
  const { pool, loading, error, refresh, setPool } = useRerollWorkspace();
  return <><p>{loading ? "loading" : "ready"}</p><p>{pool?.concurrency_limit ?? "empty"}</p><p>{error}</p>
    <button onClick={() => void refresh()}>Refresh</button>
    <button onClick={() => setPool({ ...snapshot, concurrency_limit: 8 })}>Action result</button></>;
}
beforeEach(() => { vi.useFakeTimers(); fetchPool.mockReset(); });
afterEach(() => { vi.useRealTimers(); });

test("initial and manual refresh share an in-flight request; polling never overlaps", async () => {
  let resolve!: (value: RerollSnapshot) => void;
  fetchPool.mockImplementationOnce(() => new Promise<RerollSnapshot>(done => { resolve = done; }));
  const view = render(<RerollWorkspaceProvider><Probe /></RerollWorkspaceProvider>);
  expect(screen.getByText("loading")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Refresh"));
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(fetchPool).toHaveBeenCalledTimes(1);
  await act(async () => { resolve(snapshot); });
  expect(screen.getByText("2")).toBeInTheDocument();
  expect(screen.getByText("ready")).toBeInTheDocument();
  fetchPool.mockResolvedValue(snapshot);
  await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
  expect(fetchPool).toHaveBeenCalledTimes(2);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchPool).toHaveBeenCalledTimes(2);
});

test("errors retain the last snapshot and a manual refresh recovers", async () => {
  fetchPool.mockResolvedValueOnce(snapshot).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ ...snapshot, concurrency_limit: 3 });
  render(<RerollWorkspaceProvider><Probe /></RerollWorkspaceProvider>);
  await act(async () => {});
  await act(async () => { fireEvent.click(screen.getByText("Refresh")); });
  expect(screen.getByText("offline")).toBeInTheDocument();
  expect(screen.getByText("2")).toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByText("Refresh")); });
  expect(screen.queryByText("offline")).not.toBeInTheDocument();
  expect(screen.getByText("3")).toBeInTheDocument();
});

test("late snapshot cannot overwrite a completed fleet action", async () => {
  let resolve!: (value: RerollSnapshot) => void;
  fetchPool.mockImplementationOnce(() => new Promise<RerollSnapshot>(done => { resolve = done; }));
  render(<RerollWorkspaceProvider><Probe /></RerollWorkspaceProvider>);
  fireEvent.click(screen.getByText("Action result"));
  await act(async () => { resolve(snapshot); });
  expect(screen.getByText("8")).toBeInTheDocument();
});


test("a late response from an unmounted workspace cannot populate a fresh mount", async () => {
  let resolveOld!: (value: RerollSnapshot) => void;
  fetchPool.mockImplementationOnce(() => new Promise<RerollSnapshot>(done => { resolveOld = done; }))
    .mockResolvedValueOnce({ ...snapshot, concurrency_limit: 4 });
  const old = render(<RerollWorkspaceProvider><Probe /></RerollWorkspaceProvider>);
  old.unmount();
  render(<RerollWorkspaceProvider><Probe /></RerollWorkspaceProvider>);
  await act(async () => {});
  await act(async () => { resolveOld(snapshot); });
  expect(screen.getByText("4")).toBeInTheDocument();
  expect(fetchPool).toHaveBeenCalledTimes(2);
});
