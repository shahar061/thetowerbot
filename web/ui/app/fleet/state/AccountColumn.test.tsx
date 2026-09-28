import { render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { FleetStateAccount, FleetStateWorkshop, WorkshopSkill } from "@/lib/fleetState";
import { AccountColumn } from "./AccountColumn";
import { makeAccount } from "./fixtures";

const NOW = Date.parse("2026-09-28T10:00:00Z");

function skill(id: string, name: string, overrides: Partial<WorkshopSkill> = {}): WorkshopSkill {
  return { id, name, level: 480, invested: 2.1e9, bot_spent: 1.7e9, next_cost: 1.2e8, status: "exact", locked: false, ...overrides };
}
const category = (skills: WorkshopSkill[]) => ({ unlocked: skills.length, total: skills.length, skills, next_unlock: null });
const workshop: FleetStateWorkshop = {
  totals: { attack: 480, defense: 0, utility: 0 },
  categories: {
    attack: { ...category([skill("damage", "Damage"), skill("attack_speed", "Attack Speed", { next_cost: null })]),
      next_unlock: { id: "unlock_multishot", name: "Unlock Multishot", cost: null } },
    defense: category([]), utility: category([]),
  },
  recent: [],
};

function show(account: FleetStateAccount, shownCount = 1) {
  return render(<AccountColumn account={account} accent="red" changedAt={NOW} shownCount={shownCount}
    open={{}} onToggleSection={vi.fn()} />);
}

test("the bot's next buy is highlighted and unknown prices never become numbers", () => {
  show(makeAccount({
    online: true, scan: 18442, bot: { screen: "IN_RUN", now: "Shopping", live: true },
    decision: { phase: "buying", reason: "cheapest", upgrade_id: "damage", category: "attack", name: "Damage", cost: null },
    balances: { coins: 3.84e9, gems: null, stones: null }, workshop,
  }));
  const table = screen.getByRole("region", { name: "Attack workshop" });
  const next = within(table).getByText("NEXT").closest("tr")!;
  expect(next).toHaveTextContent("Damage");
  expect(within(table).getByText("Attack Speed").closest("tr")).toHaveTextContent("price unknown");
  expect(screen.getByText("Unlock Multishot").parentElement).toHaveTextContent("price unknown");
  expect(screen.getByText("LIVE")).toBeInTheDocument();
  expect(screen.getByText(/#18,442/)).toBeInTheDocument();
  expect(screen.getByText("3.84B")).toBeInTheDocument();
  expect(screen.getByText("not tracked yet")).toBeInTheDocument();
});

test("an offline worker shows its stale age, its error, and a dash for every unknown", () => {
  show(makeAccount({ stale_seconds: 42, error: "Worker database is missing" }));
  expect(screen.getByText("stale · 42s")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Worker database is missing");
  expect(screen.getByText("OFFLINE")).toBeInTheDocument();
  expect(screen.getAllByText("Unavailable")).toHaveLength(4);  // workshop, cards, labs, runs
  expect(screen.getByText("No run recorded yet")).toBeInTheDocument();
});

test("a new account with zero runs says so instead of inventing a wave", () => {
  show(makeAccount({ online: true, bot: { screen: "MAIN_MENU", now: null, live: false }, runs: [] }));
  expect(screen.getByText("No battle")).toBeInTheDocument();
  expect(screen.getAllByText("No runs yet")).toHaveLength(2);  // HUD last run, and the runs table
});

test("a lab job that finishes between polls reads done, not a negative timer", () => {
  show(makeAccount({ labs: { slots: 2, levels: [], next: null, recent: [],
    running: [{ id: "labs.damage", name: "Damage", to_level: 12, completes_at: "2020-01-01T00:00:00Z" }] } }));
  expect(screen.getByText("done")).toBeInTheDocument();
  expect(screen.getByText("1 slot free")).toBeInTheDocument();
});

test("cards and labs start open with one column and closed with three", () => {
  const account = makeAccount({ id: "Air_1" });
  const first = show(account, 1);
  const titles = () => ["Cards", "Labs"].map(title => screen.getByText(title).closest("details")!);
  expect(titles().map(d => d.open)).toEqual([true, true]);
  first.unmount();
  show(account, 3);
  expect(titles().map(d => d.open)).toEqual([false, false]);
  expect(screen.getByText("Workshop").closest("details")!.open).toBe(true);
});
