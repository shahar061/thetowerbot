import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkshopComparison } from "./WorkshopComparison";

const fetchAccountWorkshopSummary = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchAccountWorkshopSummary }));

const member = (name: string, accountId = name) => ({
  name, account_key: `worker:${name}`, account_id: accountId, lease_id: accountId,
  endpoint: "", state: "running",
});

beforeEach(() => {
  fetchAccountWorkshopSummary.mockReset();
  fetchAccountWorkshopSummary.mockImplementation((_key: string, accountId: string) => Promise.resolve({
    account_id: accountId,
    items: accountId === "Air_1"
      ? [{ category: "ATTACK", item: "Damage", count: 4 }, { category: "DEFENSE", item: "Thorns", count: 2 }, { category: "UTILITY", item: "Cash / Wave", count: 3 }]
      : [{ category: "ATTACK", item: "Damage", count: 1 }, { category: "DEFENSE", item: "Thorns", count: 5 }, { category: "UTILITY", item: "Coins / Kill", count: 6 }],
  }));
});

describe("WorkshopComparison", () => {
  it("compares confirmed buys by category, then drills into items for a selected category", async () => {
    render(<WorkshopComparison members={[member("Air_1"), member("Air_2")]} />);
    const section = within(await screen.findByRole("region", { name: "Workshop upgrade comparison" }));
    await section.findByText("9 confirmed buys");
    expect(section.getByRole("button", { name: /Attack/ })).toBeDefined();
    expect(section.getByRole("button", { name: /Defense/ })).toBeDefined();
    expect(section.getByRole("button", { name: /Utility/ })).toBeDefined();
    expect(section.getAllByText("Air_1").length).toBeGreaterThan(0);
    expect(section.getAllByText("Air_2").length).toBeGreaterThan(0);
    fireEvent.click(section.getByRole("button", { name: /Utility/ }));
    expect(section.getByRole("button", { name: /Utility/ }).getAttribute("aria-pressed")).toBe("true");
    expect(section.getByText("Cash / Wave")).toBeDefined();
    expect(section.getByText("Coins / Kill")).toBeDefined();
  });

  it("discards an old account response when the worker is replaced", async () => {
    let resolveOld!: (value: unknown) => void;
    fetchAccountWorkshopSummary.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
      .mockResolvedValue({ account_id: "new", items: [{ category: "ATTACK", item: "Damage", count: 8 }] });
    const view = render(<WorkshopComparison members={[member("Air_1", "old")]} />);
    view.rerender(<WorkshopComparison members={[member("Air_1", "new")]} />);
    await screen.findByText("8 confirmed buys");
    await act(async () => { resolveOld({ account_id: "old", items: [{ category: "ATTACK", item: "Damage", count: 100 }] }); });
    expect(screen.queryByText("100 confirmed buys")).toBeNull();
  });
});
