import { render, screen } from "@testing-library/react";
import { describe as group, expect, it, vi } from "vitest";
import StatsPage from "./page";

const { fetchStats } = vi.hoisted(() => ({ fetchStats: vi.fn() }));

vi.mock("@/components/game-export/GameExportReports", () => ({ SelectedExportReport: () => <p>Official imported battle statistics</p> }));
vi.mock("@/lib/api", () => ({ fetchStats }));

group("StatsPage", () => {
  it("shows an explicit empty state under --no-store rather than zeroed charts", async () => {
    fetchStats.mockResolvedValue({ runs: [], taps: [], screens: [] });
    render(<StatsPage />);

    await screen.findByText(/No stored runs yet/);
  });

  it("renders all four panels, and an explicit empty state for a panel with no data of its own", async () => {
    fetchStats.mockResolvedValue({
      runs: [
        { id: 1, started_at: 0, ended_at: 30, wave: 5, coins: 100, tier: 1, tap_count: 2, scan_count: 3, duration: 30 },
      ],
      taps: [],
      screens: [{ screen: "MENU", count: 4 }],
    });
    render(<StatsPage />);

    await screen.findByText("Wave per run");
    expect(screen.getByText("Run length (s)")).toBeDefined();
    expect(screen.getByText("Taps by action")).toBeDefined();
    expect(screen.getByText("Events by screen")).toBeDefined();
    // The taps series is empty even though runs exist - must not render a
    // blank/zeroed chart in its place.
    expect(screen.getByText("No taps recorded yet.")).toBeDefined();
  });
});
