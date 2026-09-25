import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { gameNumber, WorkshopMatrix } from "./WorkshopMatrix";

const fetchAccountWorkshopLevels = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchAccountWorkshopLevels }));

const member = (name: string) => ({
  name, account_key: `worker:${name}`, account_id: name, lease_id: name, endpoint: "", state: "running",
});
const row = (id: string, name: string, category: string, extra: object) => ({
  id, name, category, max_level: 99, value: null, raw_value: null, observed_at: Date.now() / 1000,
  status: "unseen", level_min: null, level_max: null, next_coins: null, ...extra,
});

beforeEach(() => {
  fetchAccountWorkshopLevels.mockReset();
  fetchAccountWorkshopLevels.mockImplementation((_key: string, accountId: string) => Promise.resolve({
    account_id: accountId,
    upgrades: accountId === "Air_1" ? [
      row("attack_speed", "Attack Speed", "ATTACK", { status: "exact", level_min: 11, level_max: 11, next_coins: 1050, raw_value: "1.55" }),
      row("health", "Health", "DEFENSE", { status: "ambiguous", level_min: 80, level_max: 82, max_level: 6000, next_coins: 1234567, raw_value: "12K" }),
      row("orbs", "Orbs", "DEFENSE", { status: "maxed", level_min: 4, level_max: 4, max_level: 4 }),
      row("range", "Range", "ATTACK", {}),
    ] : [
      row("attack_speed", "Attack Speed", "ATTACK", { status: "exact", level_min: 2, level_max: 2, next_coins: 92, raw_value: "1.10" }),
      row("health", "Health", "DEFENSE", {}),
      row("orbs", "Orbs", "DEFENSE", { status: "maxed", level_min: 4, level_max: 4, max_level: 4 }),
    ],
  }));
});

describe("WorkshopMatrix", () => {
  it("shows level out of max and next price per emulator, hiding upgrades maxed or unread everywhere", async () => {
    render(<WorkshopMatrix members={[member("Air_1"), member("Air_2")]} />);
    const table = within(await screen.findByRole("region", { name: "Workshop levels" }));
    const speed = await table.findByRole("row", { name: /Attack Speed/ });
    expect(speed.textContent).toContain("11 / 99");
    expect(speed.textContent).toContain("1.05K");
    expect(speed.textContent).toContain("2 / 99");
    expect(table.getByRole("row", { name: /Health/ }).textContent).toContain("80–82 / 6000");
    expect(table.queryByRole("row", { name: /Orbs/ })).toBeNull();
    expect(table.queryByRole("row", { name: /Range/ })).toBeNull();
    fireEvent.click(table.getByLabelText("Hide maxed & unread"));
    expect(table.getByRole("row", { name: /Range/ })).toBeDefined();
    expect(table.getByRole("row", { name: /Orbs/ }).textContent).toContain("MAX");
  });

  it("focuses one emulator and sorts by the cheapest next upgrade", async () => {
    render(<WorkshopMatrix members={[member("Air_1"), member("Air_2")]} />);
    const table = within(await screen.findByRole("region", { name: "Workshop levels" }));
    await table.findByRole("row", { name: /Attack Speed/ });
    fireEvent.change(table.getByLabelText("Sort upgrades"), { target: { value: "cheapest" } });
    const names = table.getAllByRole("rowheader").map(cell => cell.textContent);
    expect(names.indexOf("Attack Speed")).toBeLessThan(names.indexOf("Health"));
    fireEvent.click(table.getByRole("button", { name: "Air_2" }));
    expect(table.queryByRole("button", { name: "Air_1" })).toBeNull();
    expect(table.getByRole("row", { name: /Attack Speed/ }).textContent).toContain("read 1.10");
  });
});

it("prints prices the way the game does", () => {
  expect([gameNumber(331), gameNumber(1050), gameNumber(4_677_842_447_734)]).toEqual(["331", "1.05K", "4.68T"]);
});
