import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { weightedPlan } from "./planFixtures";
import { WorkshopViews } from "./WorkshopViews";

const fetchAccountWorkshopLevels = vi.hoisted(() => vi.fn());
const fetchAccountWorkshopPlan = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchAccountWorkshopLevels, fetchAccountWorkshopPlan }));

const member = (name: string) => ({
  name, account_key: `worker:${name}`, account_id: name, lease_id: name, endpoint: "", state: "running",
});

beforeEach(() => {
  fetchAccountWorkshopLevels.mockReset();
  fetchAccountWorkshopPlan.mockReset();
  fetchAccountWorkshopLevels.mockImplementation((_key: string, accountId: string) => Promise.resolve({ account_id: accountId, upgrades: [] }));
  fetchAccountWorkshopPlan.mockResolvedValue({ plan: weightedPlan(), age_seconds: 120, current_revision: 16, stale: false });
  window.history.replaceState(null, "", "/fleet/reroll/workshop/?view=cubes");
});
afterEach(() => { vi.useRealTimers(); });

describe("WorkshopViews", () => {
  it("opens on Upgrades and switches to the plan graph, keeping the layout view", async () => {
    render(<WorkshopViews members={[member("Tiramisu64_82")]} />);
    expect(screen.getByRole("tab", { name: "Upgrades" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "Plan graph" }));
    expect(await screen.findByRole("region", { name: "Candidates" })).toBeInTheDocument();
    expect(screen.getByText(/Decided 2m ago/)).toBeInTheDocument();
    const url = new URL(window.location.href);
    expect(url.searchParams.get("tab")).toBe("plan");
    expect(url.searchParams.get("view")).toBe("cubes");
  });

  it("moves between tabs with the arrow keys", () => {
    render(<WorkshopViews members={[member("Tiramisu64_82")]} />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "Upgrades" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Plan graph" })).toHaveAttribute("aria-selected", "true");
  });

  it("explains when an emulator has no saved plan yet", async () => {
    fetchAccountWorkshopPlan.mockResolvedValue({ plan: null });
    render(<WorkshopViews members={[member("Tiramisu64_82")]} initialTab="plan" />);
    expect(await screen.findByText("No Workshop decision recorded for Tiramisu64_82 yet. It appears after its next Workshop visit."))
      .toBeInTheDocument();
  });

  it("asks the picked emulator for its plan", async () => {
    render(<WorkshopViews members={[member("Tiramisu64_82"), member("Tiramisu64_83")]} initialTab="plan" />);
    await screen.findByRole("region", { name: "Candidates" });
    fireEvent.click(screen.getByRole("button", { name: "Tiramisu64_83" }));
    await vi.waitFor(() => expect(fetchAccountWorkshopPlan).toHaveBeenLastCalledWith("worker:Tiramisu64_83", "Tiramisu64_83"));
  });

  it("keeps the last graph when a refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<WorkshopViews members={[member("Tiramisu64_82")]} initialTab="plan" />);
    await screen.findByRole("region", { name: "Candidates" });
    fetchAccountWorkshopPlan.mockRejectedValueOnce(new Error("Worker unreachable"));
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByRole("alert").textContent).toContain("Worker unreachable");
    expect(screen.getByRole("region", { name: "Candidates" })).toBeInTheDocument();
  });

  it("shows a missing account id as a muted status, not an alert", async () => {
    render(<WorkshopViews members={[{ ...member("Tiramisu64_82"), account_id: null }]} initialTab="plan" />);
    const status = await screen.findByText("Waiting for a verified account");
    expect(status).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
