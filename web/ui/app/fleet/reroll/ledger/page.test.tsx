import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FleetLedgerPage from "./page";

const { fetchAccountLedger, workspace } = vi.hoisted(() => ({
  fetchAccountLedger: vi.fn(),
  workspace: { pool: { members: [] as { name: string; account_key: string; account_id: string; lease_id: string }[] }, loading: false, error: null },
}));
vi.mock("@/lib/api", () => ({ fetchAccountLedger }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => workspace }));

const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `account-${name}`, lease_id: name });
const line = (item: string, id = 5) => ({ id, seq: id, ts: id, kind: "WORKSHOP_BUY", item,
  category: "UTILITY", currency: "coins", delta: -75, price: 75, balance_after: 100,
  observed: 175, dry_run: 0, run_id: null, visit: 1, reason: null, detail: {} });
const payload = (account: string, item: string, next: number | null = null, id = 5) => ({
  account_id: account, lines: [line(item, id)], balances: { coins: 100, gems: null }, rehearsals: 0, next,
});
beforeEach(() => {
  fetchAccountLedger.mockReset();
  workspace.pool.members = [member("Air_1"), member("Air_2")];
});

describe("FleetLedgerPage", () => {
  it("keeps colliding event IDs separate and paginates each account independently", async () => {
    fetchAccountLedger.mockImplementation((_key, account, options) => Promise.resolve(
      payload(account, `${account}-${options.before ? "older" : "latest"}`, options.before ? null : account === "account-Air_1" ? 5 : 8, options.before ? 1 : 5),
    ));
    render(<FleetLedgerPage />);
    await screen.findByText("account-Air_2-latest");
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Load older entries" }));
    await screen.findByText("account-Air_2-older");
    expect(fetchAccountLedger).toHaveBeenCalledWith("worker:Air_1", "account-Air_1", { includeRehearsals: false, before: 5 });
    expect(fetchAccountLedger).toHaveBeenCalledWith("worker:Air_2", "account-Air_2", { includeRehearsals: false, before: 8 });
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(5);
    expect(screen.queryByRole("button", { name: "Load older entries" })).toBeNull();
  });

  it("shows healthy account history alongside failures and rejects mismatched identities", async () => {
    fetchAccountLedger.mockImplementation((_key, account) => Promise.resolve(payload(account === "account-Air_2" ? "replaced" : account, account)));
    render(<FleetLedgerPage />);
    await screen.findByRole("alert");
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("alert").textContent).toMatch(/Account changed/);
  });

  it("discards late filter results and forwards all active filters", async () => {
    workspace.pool.members = [member("Air_1")];
    let resolveOld!: (value: unknown) => void;
    fetchAccountLedger.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
      .mockResolvedValue(payload("account-Air_1", "Filtered purchase"));
    render(<FleetLedgerPage />);
    fireEvent.change(screen.getByLabelText("Transaction type"), { target: { value: "WORKSHOP_BUY" } });
    await screen.findByText("Filtered purchase");
    await act(async () => { resolveOld(payload("account-Air_1", "Stale purchase")); });
    expect(screen.queryByText("Stale purchase")).toBeNull();
    fireEvent.change(screen.getByLabelText("Currency"), { target: { value: "coins" } });
    fireEvent.click(screen.getByLabelText("Include rehearsals"));
    await waitFor(() => expect(fetchAccountLedger).toHaveBeenLastCalledWith("worker:Air_1", "account-Air_1", {
      kind: "WORKSHOP_BUY", currency: "coins", includeRehearsals: true,
    }));
  });

  it("removes previous account history immediately when an emulator is replaced", async () => {
    workspace.pool.members = [member("Air_1")];
    fetchAccountLedger.mockResolvedValueOnce(payload("account-Air_1", "Old account purchase"))
      .mockImplementation(() => new Promise(() => {}));
    const view = render(<FleetLedgerPage />);
    await screen.findByText("Old account purchase");
    workspace.pool.members = [{ ...member("Air_1"), account_id: "new-account", lease_id: "new-lease" }];
    view.rerender(<FleetLedgerPage />);
    expect(screen.queryByText("Old account purchase")).toBeNull();
    expect(fetchAccountLedger).toHaveBeenLastCalledWith("worker:Air_1", "new-account", { includeRehearsals: false });
  });
});
