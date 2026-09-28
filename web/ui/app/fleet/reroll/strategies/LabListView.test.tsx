import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabsReference, LabsRow } from "@/lib/labs";
import { isLabList } from "@/lib/labs";
import { LabListView } from "./LabListView";

type Labs = BuildRouteDocument["baseline"]["labs"];
const labs: Labs = { slot1_research: "game_speed", steps: [], mode: "blocks", blocks: [
  { id: "labs.list", type: "lab_list", entries: [
    { id: "gs", lab_id: "labs.game-speed", to_level: 7, tier: "S+", pin_slot: 1 },
    { id: "ckb", lab_id: "labs.coins-kill-bonus", to_level: 30, tier: "A" }] }] };
const reference = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true },
  { id: "labs.coins-kill-bonus", name: "Coins / Kill Bonus", max_level: 99, priced: true }],
  game_speed: [], lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] } as LabsReference;
const now = { state: "idle", level: null, completes_at: null, overdue_seconds: null, read_at: null, stale: false } as const;
const observed = { worker: "w1", account_id: "a", strategy_name: null, read_at: null, wallet: { coins: 20000, gems: 0 },
  state: "ok", reason: null, recent: [], plan: { wallet_coins: 20000, jar: 0, gems: { wallet: 0, next: null, price: null,
    have: null, need: null, automated: false, why: [], steps: [] },
    saving: { reserve: 18650, workshop_budget: 0, wallet: 18650, coins_per_hour: 7500, why: ["Slot 1 Game Speed L4: needs 50k, due in 4.2h"],
      targets: [{ slot: 1, lab_id: "labs.game-speed", name: "Game Speed", level: 4, price: 50000, needed_at: 0, ready_at: 0, covered: true }] },
    slots: [{ slot: 1, now, covered: true, automated: false, why: [], note: "Start manually", role: "filler",
      next: { lab_id: "labs.coins-kill-bonus", name: "Coins / Kill Bonus", level: 6, price: 1350, seconds: 4800 },
      saving_for: { lab_id: "labs.game-speed", name: "Game Speed", level: 4, price: 50000, seconds: 122520 } }] } } as unknown as LabsRow;

test("detects a ranked list lane", () => {
  expect(isLabList(labs)).toBe(true);
});

test("shows ranked entries with tier and pin", () => {
  render(<LabListView labs={labs} reference={reference} observed={null} />);
  const rows = screen.getAllByRole("row");
  expect(within(rows[1]).getByText("Game Speed")).toBeInTheDocument();
  expect(within(rows[1]).getByText("Slot 1")).toBeInTheDocument();
  expect(within(rows[2]).getByText("A")).toBeInTheDocument();
  expect(screen.getByText(/editing arrives with the planner/i)).toBeInTheDocument();
});

test("shows a filler slot with what it saves for and the saving line", () => {
  render(<LabListView labs={labs} reference={reference} observed={observed} />);
  expect(screen.getByText(/Coins \/ Kill Bonus L6 · filler/)).toBeInTheDocument();
  expect(screen.getByText(/saving for Game Speed L4/)).toBeInTheDocument();
  expect(screen.getByText(/Start manually/)).toBeInTheDocument();
  expect(screen.getByText(/needs 50k, due in 4.2h/)).toBeInTheDocument();
});
