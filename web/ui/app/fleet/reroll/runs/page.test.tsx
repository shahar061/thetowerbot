import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FleetRunsPage from "./page";

const { fetchAccountRunHistory, fetchAccountRunUpgrades, workspace } = vi.hoisted(() => ({
  fetchAccountRunHistory: vi.fn(),
  fetchAccountRunUpgrades: vi.fn(),
  workspace: { pool: { members: [] as { name: string; account_key: string; account_id: string; lease_id: string }[] }, loading: false, error: null },
}));
vi.mock("@/lib/api", () => ({ fetchAccountRunHistory, fetchAccountRunUpgrades }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => workspace }));

const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `acc-${name}`, lease_id: name });
const run = (id: number, over: object = {}) => ({
  id, started_at: 0, ended_at: 1000 + id, wave: 10, coins: 50, tier: 1, abandoned: 0, scan_count: 0, tap_count: 0,
  killed_by: "Tank", ad_coins: 0, total_coins: 50, buys: 3, wave_record: null, coin_record: null, ...over,
});

beforeEach(() => {
  fetchAccountRunHistory.mockReset();
  fetchAccountRunUpgrades.mockReset();
  workspace.pool.members = [member("Air_1"), member("Air_2")];
});

describe("FleetRunsPage", () => {
  it("lists runs newest first with standing and broken HS badges", async () => {
    fetchAccountRunHistory.mockImplementation((key: string) => Promise.resolve(key === "worker:Air_1"
      ? [run(2, { wave_record: "standing", wave_prev: { run_id: 1, value: 9 } }), run(1, { wave_record: "broken", wave_broken_by: 2 })]
      : [run(5, { ended_at: 2000, killed_by: null })]));
    render(<FleetRunsPage />);
    const rows = await screen.findAllByRole("button", { name: /^Run #/ });
    expect(rows.map(r => r.getAttribute("aria-label"))).toEqual(["Run #5 on Air_2", "Run #2 on Air_1", "Run #1 on Air_1"]);
    expect(within(rows[0]).getByText("Unreadable")).toBeInTheDocument();
    expect(within(rows[1]).getByLabelText(/Wave record at T1 · beat #1 \(9\) · still standing/)).toHaveAttribute("data-record", "standing");
    expect(within(rows[2]).getByLabelText(/broken by #2/)).toHaveAttribute("data-record", "broken");
    expect(fetchAccountRunHistory).toHaveBeenCalledWith("worker:Air_1", "acc-Air_1");
  });

  it("opens the side panel with levels over max for the clicked run", async () => {
    fetchAccountRunHistory.mockResolvedValue([run(2)]);
    fetchAccountRunUpgrades.mockResolvedValue({
      categories: [{ name: "DEFENSE", items: [{ upgrade_id: "health", name: "Health", levels: 5, max_level: 6000, spent: 40, unpriced: 0 }] }],
      totals: { levels: 5, spent: 40, unpriced: 0 },
    });
    workspace.pool.members = [member("Air_1")];
    render(<FleetRunsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Run #2 on Air_1" }));
    const panel = await screen.findByRole("complementary", { name: "Run details" });
    expect(await within(panel).findByText("5")).toBeInTheDocument();
    expect(within(panel).getByText("/6,000")).toBeInTheDocument();
    expect(fetchAccountRunUpgrades).toHaveBeenCalledWith("worker:Air_1", 2, "acc-Air_1");
    fireEvent.click(within(panel).getByRole("button", { name: "Close run details" }));
    expect(screen.queryByRole("complementary", { name: "Run details" })).toBeNull();
  });

  it("closes the side panel when the selected run's emulator is toggled off", async () => {
    fetchAccountRunHistory.mockResolvedValue([run(2)]);
    fetchAccountRunUpgrades.mockResolvedValue(null);
    workspace.pool.members = [member("Air_1")];
    render(<FleetRunsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Run #2 on Air_1" }));
    expect(await screen.findByRole("complementary", { name: "Run details" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Air_1", pressed: true }));

    expect(screen.queryByRole("complementary", { name: "Run details" })).toBeNull();
  });

  it("keeps other emulators when one fails and says no purchase record when there is none", async () => {
    fetchAccountRunHistory.mockImplementation((key: string) => key === "worker:Air_1"
      ? Promise.reject(new Error("worker offline")) : Promise.resolve([run(9)]));
    fetchAccountRunUpgrades.mockResolvedValue(null);
    render(<FleetRunsPage />);
    expect(await screen.findByText(/Air_1: worker offline/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Run #9 on Air_2" }));
    expect(await screen.findByText("No purchase record for this run.")).toBeInTheDocument();
  });
});
