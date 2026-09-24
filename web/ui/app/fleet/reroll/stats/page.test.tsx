import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FleetStatsPage from "./page";

const { fetchAccountStats, workspace } = vi.hoisted(() => ({
  fetchAccountStats: vi.fn(),
  workspace: { pool: { members: [] as { name: string; account_key: string; account_id: string; lease_id: string; state: string }[] }, loading: false, error: null },
}));
vi.mock("@/lib/api", () => ({ fetchAccountStats }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => workspace }));
const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `account-${name}`, lease_id: name, state: "running" });
const payload = (account: string, seconds: number) => ({ account_id: account,
  runs: [{ id: 1, started_at: 0, ended_at: 60, duration: 60, wave: 20, tier: 1, coins: 100, tap_count: 3, scan_count: 4 }], taps: [], screens: [],
  summary: { total_runs: 230, best_tier_1_wave: 65, play_seconds: 10000 },
  benchmarks: [20, 30, 60, 100].map(wave => ({ tier: 1, wave, run_id: wave < 100 ? 1 : null, reached_at: wave < 100 ? 100 : null,
    play_seconds: wave < 100 ? seconds : null, elapsed_seconds: wave < 100 ? seconds + 600 : null })),
});
beforeEach(() => {
  fetchAccountStats.mockReset();
  workspace.pool.members = [member("Air_1"), member("Air_2")];
});

describe("FleetStatsPage", () => {
  it("compares full-history benchmarks and reuses per-emulator charts and milestone progress", async () => {
    fetchAccountStats.mockImplementation((_key, account) => Promise.resolve(payload(account, account === "account-Air_1" ? 120 : 240)));
    render(<FleetStatsPage />);
    await screen.findAllByText("Fastest recorded");
    const card = within(screen.getByRole("article", { name: "Stats for Air_1" }));
    expect(card.getByText("65")).toBeDefined();
    expect(card.getByText("230 completed runs")).toBeDefined();
    expect(card.getByRole("progressbar", { name: "Reroll progress for Air_1" })).toBeDefined();
    expect(card.getByText("Wave per run")).toBeDefined();
    expect(screen.getAllByText("Not reached")).toHaveLength(2);
    expect(screen.getAllByText("2m 0s")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Elapsed time" }));
    expect(screen.getAllByText("12m 0s")).toHaveLength(3);
    fireEvent.click(card.getByRole("button", { name: "Run length, taps & screen events" }));
    expect(card.getByText("Run length (s)")).toBeDefined();
  });

  it("distinguishes unavailable account history from unreached milestones", async () => {
    fetchAccountStats.mockImplementation((_key, account) => account === "account-Air_1"
      ? Promise.resolve(payload(account, 120)) : Promise.reject(new Error("Worker offline")));
    render(<FleetStatsPage />);
    await screen.findByText("Worker offline");
    expect(screen.getAllByText("Unavailable")).toHaveLength(4);
    expect(screen.getAllByText("Not reached")).toHaveLength(1);
    expect(screen.queryByText("Fastest recorded")).toBeNull();
  });

  it("ignores late responses for a replaced account", async () => {
    workspace.pool.members = [member("Air_1")];
    let resolveOld!: (value: unknown) => void;
    fetchAccountStats.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
      .mockResolvedValue({ ...payload("new-account", 300), summary: { total_runs: 1, best_tier_1_wave: 10, play_seconds: 300 } });
    const view = render(<FleetStatsPage />);
    workspace.pool.members = [{ ...member("Air_1"), account_id: "new-account", lease_id: "new-lease" }];
    view.rerender(<FleetStatsPage />);
    await screen.findByText("1 completed runs");
    await act(async () => { resolveOld(payload("account-Air_1", 120)); });
    expect(screen.queryByText("230 completed runs")).toBeNull();
    expect(screen.getAllByText("5m 0s").length).toBeGreaterThan(0);
  });
});
