import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { SharedWorkshopLedger, WorkerBattlePurchases } from "./Purchases";
import { fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import type { LedgerPayload, RunRow } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchAccountRuns: vi.fn(), fetchAccountRunPurchases: vi.fn(), fetchAccountWorkshopPurchases: vi.fn() }));
vi.mock("@/components/RunPurchases", () => ({ RunPurchases: () => <p>Battle purchase evidence</p> }));
const member: RerollMember = { name: "Air_1", account_id: "100", account_key: "one", endpoint: "", lease_id: "a", state: "running" };
const empty: LedgerPayload = { lines: [], balances: { coins: null, gems: null }, rehearsals: 0, next: null };
beforeEach(() => { vi.clearAllMocks(); vi.mocked(fetchAccountWorkshopPurchases).mockResolvedValue(empty); });
afterEach(() => { vi.useRealTimers(); });

test("battle requests do not overlap and stop when their view unmounts", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchAccountRuns).mockReturnValue(new Promise(() => {}));
  const view = render(<WorkerBattlePurchases accountKey="one" />);
  expect(screen.getByText(/Loading battle purchase history/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(fetchAccountRuns).toHaveBeenCalledTimes(1);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(fetchAccountRuns).toHaveBeenCalledTimes(1);
});

test("clears previous battle evidence immediately when account identity changes", async () => {
  vi.mocked(fetchAccountRuns).mockResolvedValueOnce([{ id: 7, started_at: 1, ended_at: null } as RunRow]);
  vi.mocked(fetchAccountRunPurchases).mockResolvedValue({ purchases: [], totals: { count: 0, spent: 0, unpriced: 0, by_category: {} } });
  const view = render(<WorkerBattlePurchases accountKey="one" />);
  expect(await screen.findByText(/Run #7/)).toBeInTheDocument();
  vi.mocked(fetchAccountRuns).mockReturnValue(new Promise(() => {}));
  view.rerender(<WorkerBattlePurchases accountKey="two" />);
  expect(screen.queryByText(/Run #7/)).not.toBeInTheDocument();
  expect(screen.getByText(/Loading battle purchase history/)).toBeInTheDocument();
});

test("ledger retains account provenance and cursor when loading older records", async () => {
  vi.mocked(fetchAccountWorkshopPurchases).mockResolvedValueOnce({ ...empty, next: 9 });
  render(<SharedWorkshopLedger members={[member]} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load older for Air_1" }));
  expect(fetchAccountWorkshopPurchases).toHaveBeenNthCalledWith(1, "one", undefined, "100");
  expect(fetchAccountWorkshopPurchases).toHaveBeenLastCalledWith("one", 9, "100");
  expect(screen.getByText(/Account 100/)).toBeInTheDocument();
});

test("ledger does not overlap a slow refresh and stops after unmount", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchAccountWorkshopPurchases).mockReturnValue(new Promise(() => {}));
  const view = render(<SharedWorkshopLedger members={[member]} />);
  expect(screen.getByText(/Loading Workshop purchases/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(fetchAccountWorkshopPurchases).toHaveBeenCalledTimes(1);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(fetchAccountWorkshopPurchases).toHaveBeenCalledTimes(1);
});

test("an older-page response cannot repopulate a worker after its account is replaced", async () => {
  let resolveOlder!: (value: LedgerPayload) => void;
  vi.mocked(fetchAccountWorkshopPurchases)
    .mockResolvedValueOnce({ ...empty, next: 9 })
    .mockImplementationOnce(() => new Promise(resolve => { resolveOlder = resolve; }))
    .mockResolvedValueOnce(empty);
  const view = render(<SharedWorkshopLedger members={[member]} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load older for Air_1" }));
  view.rerender(<SharedWorkshopLedger members={[{ ...member, account_id: "200" }]} />);
  await act(async () => { resolveOlder({ ...empty, lines: [{
    id: 1, seq: 1, ts: 100, kind: "WORKSHOP_BUY", item: "Old account purchase", category: "ATTACK",
    currency: "coins", delta: -20, price: 20, balance_after: 10, observed: 30,
    dry_run: 0, run_id: null, visit: 1, reason: null, detail: { verdict: "bought" },
  }] }); });
  expect(screen.queryByText("Old account purchase")).not.toBeInTheDocument();
  expect(screen.getByText(/Account 200/)).toBeInTheDocument();
});

test("leaving battle evidence does not start a purchase request after the runs request completes", async () => {
  let resolveRuns!: (value: RunRow[]) => void;
  vi.mocked(fetchAccountRuns).mockImplementation(() => new Promise(resolve => { resolveRuns = resolve; }));
  const view = render(<WorkerBattlePurchases accountKey="one" />);
  view.unmount();
  await act(async () => { resolveRuns([{ id: 7, started_at: 1, ended_at: null } as RunRow]); });
  expect(fetchAccountRunPurchases).not.toHaveBeenCalled();
});

test("a recovered first ledger read retains its older-record cursor", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchAccountWorkshopPurchases)
    .mockRejectedValueOnce(new Error("temporarily offline"))
    .mockResolvedValueOnce({ ...empty, next: 9 });
  render(<SharedWorkshopLedger members={[member]} />);
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByText("temporarily offline")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.getByRole("button", { name: "Load older for Air_1" })).toBeInTheDocument();
});

test("battle evidence binds the run list and purchases to the same expected identity", async () => {
  vi.mocked(fetchAccountRuns).mockResolvedValue([{ id: 7, started_at: 1, ended_at: null } as RunRow]);
  vi.mocked(fetchAccountRunPurchases).mockResolvedValue({ purchases: [], totals: { count: 0, spent: 0, unpriced: 0, by_category: {} } });
  const view = render(<WorkerBattlePurchases accountKey="one" expectedAccountId="100" />);
  expect(await screen.findByText(/Run #7/)).toBeInTheDocument();
  expect(fetchAccountRuns).toHaveBeenCalledWith("one", "100");
  expect(fetchAccountRunPurchases).toHaveBeenCalledWith("one", 7, "100");
  vi.mocked(fetchAccountRuns).mockReturnValue(new Promise(() => {}));
  view.rerender(<WorkerBattlePurchases accountKey="one" expectedAccountId="200" />);
  expect(screen.queryByText(/Run #7/)).not.toBeInTheDocument();
});

test("a rejected account change between run and purchase reads shows no evidence", async () => {
  vi.mocked(fetchAccountRuns).mockResolvedValue([{ id: 7, started_at: 1, ended_at: null } as RunRow]);
  vi.mocked(fetchAccountRunPurchases).mockRejectedValue(new Error("Account identity changed"));
  render(<WorkerBattlePurchases accountKey="one" expectedAccountId="100" />);
  expect(await screen.findByRole("status")).toHaveTextContent("Account identity changed");
  expect(fetchAccountRunPurchases).toHaveBeenCalledWith("one", 7, "100");
  expect(screen.queryByText("Battle purchase evidence")).not.toBeInTheDocument();
});
