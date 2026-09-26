import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LabsReference, LabsRow, SlotPlan } from "@/lib/labs";
import { LabsMatrix } from "./LabsMatrix";

const AT = 100_000;
const reference: LabsReference = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true }],
  game_speed: [{ level: 1, coins: 300, seconds: 540, max_speed: 2 }], lab_slots: [{ slot: 2, gems: 100 }],
  card_slots: [{ slot: 2, gems: 50 }], card_gems: 20, labs_unlock_wave: 30,
  sources: [{ url: "https://the-tower-idle-tower-defense.fandom.com/wiki/Lab_Upgrades", checked: "2026-09-26" }] };
const plan = (slot1: Partial<SlotPlan>): LabsRow["plan"] => ({ wallet_coins: 20000, jar: 0, slots: [
  { slot: 1, now: { state: "idle", level: 3, completes_at: null, overdue_seconds: null, read_at: AT - 60, stale: false },
    next: { lab_id: "labs.game-speed", name: "Game Speed", level: 3, price: 12000, seconds: 35280 },
    covered: true, automated: true, why: ["labs.slot1: slots 1"], note: null, ...slot1 },
  ...[2, 3, 4, 5].map(slot => ({ slot, now: { state: "unknown" as const, level: null, completes_at: null, overdue_seconds: null, read_at: null, stale: false },
    next: { lab_id: "labs.coins-wave", name: "Coins / Wave", level: null, price: null, seconds: null },
    covered: null, automated: false, why: [], note: null }))],
  gems: { wallet: 60, price: 100, have: 60, need: 100, automated: true, why: [],
    next: { block_id: "gems.lab2", type: "unlock_lab_slot", label: "Lab slot 2", state: "current", price: 100, automated: true },
    steps: [{ block_id: "gems.lab2", type: "unlock_lab_slot", label: "Lab slot 2", state: "current", price: 100, automated: true },
      { block_id: "gems.lab3", type: "unlock_lab_slot", label: "Lab slot 3", state: "next", price: 400, automated: false }] } });
const row = (worker: string, slot1: Partial<SlotPlan> = {}): LabsRow => ({ worker, account_id: `acct-${worker}`,
  strategy_name: "Fleet baseline", read_at: AT - 60, wallet: { coins: 20000, gems: 60 }, plan: plan(slot1), state: "ok",
  reason: null, recent: [{ at: AT - 600, kind: "LAB", item: "Game Speed", category: "RESEARCH", currency: "coins", amount: 2500, reason: null }] });
const table = () => within(screen.getByRole("table", { name: "Lab slots per emulator" }));

describe("LabsMatrix", () => {
  it("colors only an idle, automated, covered slot amber", () => {
    render(<LabsMatrix rows={[row("Air_1"), row("Air_2", { covered: false }), row("Air_3", { automated: false })]}
      reference={reference} focus={null} at={AT} />);
    const tones = table().getAllByTestId("slot-1").map(cell => cell.dataset.tone);
    expect(tones).toEqual(["ready", "neutral", "neutral"]);
    expect(table().getAllByTestId("slot-2").every(cell => cell.dataset.tone === "neutral")).toBe(true);
    expect(table().getAllByText("Planned · not automated").length).toBeGreaterThan(0);
  });

  it("shows the catalog price and time and whether the wallet covers it", () => {
    render(<LabsMatrix rows={[row("Air_1")]} reference={reference} focus={null} at={AT} />);
    const cell = table().getByTestId("slot-1");
    expect(cell).toHaveTextContent("Game Speed L3");
    expect(cell).toHaveTextContent("12.00K coins · 9h 48m");
    expect(cell).toHaveTextContent("Covered");
    expect(table().getByTestId("gems")).toHaveTextContent("60 / 100 gems");
  });

  it("reads overdue research as should-have-finished", () => {
    render(<LabsMatrix rows={[row("Air_1", { now: { state: "researching", level: 3, completes_at: AT - 7200, overdue_seconds: 7200, read_at: AT - 9000, stale: false } })]}
      reference={reference} focus={null} at={AT} />);
    expect(table().getByTestId("slot-1")).toHaveTextContent("Should have finished ~2h ago");
  });

  it("hides planned steps on request and focuses one worker", () => {
    render(<LabsMatrix rows={[row("Air_1"), row("Air_2")]} reference={reference} focus="Air_2" at={AT} />);
    expect(table().getAllByRole("row", { name: /Air_/ })).toHaveLength(1);
    expect(table().getByRole("rowheader")).toHaveTextContent("Air_2");
    fireEvent.click(screen.getByLabelText("Hide not-automated"));
    expect(table().queryByText("Coins / Wave")).not.toBeInTheDocument();
    expect(table().getByTestId("slot-1")).toHaveTextContent("Game Speed L3");
  });

  it("expands a row into the gem path and recent activity, and says why a row is unknown", () => {
    render(<LabsMatrix rows={[row("Air_1"), { ...row("Air_9"), plan: null, state: "unknown", reason: "Worker is not registered to an account" }]}
      reference={reference} focus={null} at={AT} />);
    fireEvent.click(table().getByRole("button", { name: "Details for Air_1" }));
    const path = screen.getByRole("list", { name: "Gem path for Air_1" });
    expect(within(path).getAllByRole("listitem").map(item => item.dataset.state)).toEqual(["current", "next"]);
    expect(screen.getByRole("list", { name: "Recent lab and card activity for Air_1" })).toHaveTextContent("Game Speed · 2,500 coins");
    expect(table().getByText("Worker is not registered to an account")).toBeInTheDocument();
  });
});
