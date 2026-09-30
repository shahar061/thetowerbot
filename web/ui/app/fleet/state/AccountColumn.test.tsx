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

function show(account: FleetStateAccount, shownCount = 1, open = { [`${account.id}:workshop-bars`]: false }) {
  return render(<AccountColumn account={account} accent="red" changedAt={NOW} shownCount={shownCount}
    open={open} onToggleSection={vi.fn()} />);
}

test("the bot's next buy is highlighted and unknown prices never become numbers", () => {
  show(makeAccount({
    online: true, scan: 18442, bot: { screen: "IN_RUN", now: "Shopping", live: true },
    decision: { phase: "buying", reason: "cheapest", upgrade_id: "damage", category: "attack", name: "Damage", cost: null },
    next_buy: { state: "buy", upgrade_id: "damage", name: "Damage", category: "attack", price: null,
      price_source: null, wallet: null, reason: "cheapest", goal: null, observed_at: null },
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

test("Workshop hides future locked groups and uses its own queue instead of the battle decision", () => {
  show(makeAccount({
    decision: { phase: "buying", reason: "battle", upgrade_id: "wall_health", category: "defense", name: "Wall Health", cost: null },
    next_buy: { state: "save_coins", upgrade_id: "health", name: "Health", category: "defense", price: 55,
      price_source: "observed", wallet: 30, reason: "saving", goal: null, observed_at: null },
    workshop: { ...workshop, categories: { ...workshop.categories, defense: {
      unlocked: 1, total: 18, skills: [skill("health", "Health"),
        skill("thorns", "Thorns", { locked: true, level: null }),
        skill("lifesteal", "Lifesteal", { locked: true, level: null }),
        skill("wall_health", "Wall Health", { locked: true, level: null })],
      next_unlock: { id: "unlock_thorns", name: "Unlock Thorns", cost: 500, upgrade_ids: ["thorns"] },
    } } },
  }));
  const table = screen.getByRole("region", { name: "Defense workshop" });
  expect(within(table).getByText("NEXT").closest("tr")).toHaveTextContent("Health");
  expect(within(table).getByText("Thorns").closest("tr")).toHaveTextContent("locked");
  expect(within(table).queryByText("Lifesteal")).toBeNull();
  expect(within(table).queryByText("Wall Health")).toBeNull();
});

test("Workshop bars retain unknown levels and label estimated prices", () => {
  const account = makeAccount({ workshop: { ...workshop, categories: { ...workshop.categories,
    attack: category([skill("damage", "Damage", { level: 3, next_cost_source: "catalog_estimate" }),
      skill("attack_speed", "Attack Speed", { level: null, next_cost: null })]),
  } } });
  show(account, 1, {});
  const chart = screen.getByRole("region", { name: "Attack workshop levels" });
  expect(within(chart).getByRole("meter", { name: "Damage level" })).toHaveAttribute("aria-valuenow", "3");
  expect(within(chart).queryByRole("meter", { name: "Attack Speed level" })).toBeNull();
  expect(chart).toHaveTextContent("Level unknown");
  expect(chart).toHaveTextContent("Estimated");
  expect(chart).toHaveTextContent("price unknown");
});

test("a battle decision alone never highlights a Workshop purchase", () => {
  show(makeAccount({ workshop,
    decision: { phase: "buying", reason: "battle", upgrade_id: "damage", category: "attack", name: "Damage", cost: null },
  }));
  expect(screen.queryByText("NEXT")).toBeNull();
});

test("an offline worker shows its stale age, its error, and a dash for every unknown", () => {
  show(makeAccount({ stale_seconds: 42, error: "Worker database is missing" }));
  expect(screen.getByText("stale · 42s")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Worker database is missing");
  expect(screen.getByText("OFFLINE")).toBeInTheDocument();
  expect(screen.getAllByText("Unavailable")).toHaveLength(5);  // totals, workshop, cards, labs, runs
  expect(screen.getByText("No run recorded yet")).toBeInTheDocument();
});

test("lifetime totals show the Stats reading's age and label gems as bot claims", () => {
  show(makeAccount({ online: true, totals: {
    coins: 1.86e12, coins_incomplete: true, stones: 450, gems_claimed: 320,
    observed_at: "2026-09-28T07:00:00+00:00" } }));
  const section = screen.getByText("Lifetime totals").closest("section")!;
  expect(within(section).getByText("1.86T")).toBeInTheDocument();
  expect(within(section).getByText("some runs unread")).toBeInTheDocument();
  expect(within(section).getByText("claimed by bot")).toBeInTheDocument();
  expect(section).toHaveTextContent("Stats read 3h ago");
});

test("totals before any Stats read are dashes, never zero", () => {
  show(makeAccount({ online: true, totals: {
    coins: null, coins_incomplete: false, stones: null, gems_claimed: 0, observed_at: null } }));
  const section = screen.getByText("Lifetime totals").closest("section")!;
  expect(within(section).getAllByText("—")).toHaveLength(2);
  expect(within(section).queryByText("some runs unread")).toBeNull();
  expect(section).toHaveTextContent("Stats screen not read yet");
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
  expect(screen.getByText("Workshop", { selector: "summary" }).closest("details")!.open).toBe(true);
});

test("the header names the strategy and the queue leads with the next Workshop buy and its price", () => {
  show(makeAccount({
    online: true, strategy: { id: "s1", name: "Coin Rush", version: 3 },
    best_wave: { wave: 5020, tier: 11 },
    next_buy: { state: "save_coins", upgrade_id: "damage", name: "Damage", category: "attack",
      price: 1.2e8, price_source: "observed", wallet: 6e7, reason: "", goal: "Reach T1 W20", observed_at: null },
  }));
  expect(screen.getByText("Coin Rush")).toBeInTheDocument();
  expect(screen.getByText("v3")).toBeInTheDocument();
  expect(screen.getByText(/Reach T1 W20/)).toBeInTheDocument();
  expect(screen.getByText("5,020")).toBeInTheDocument();
  expect(screen.getByText("T11")).toBeInTheDocument();
  const next = screen.getByText("Workshop", { selector: ".qk" }).closest(".fs-q")!;
  expect(next).toHaveTextContent("Damage");
  expect(next).toHaveTextContent("Saving coins");
  expect(next).toHaveTextContent("120.00M");
  expect(within(next as HTMLElement).getByRole("meter")).toHaveAttribute("aria-valuenow", "50");
});

test("a live wave past the best wave is flagged as a new best", () => {
  show(makeAccount({
    online: true, bot: { screen: "IN_RUN", now: null, live: true }, best_wave: { wave: 400, tier: 3 },
    battle: { tier: 3, wave: 412, cash: null, elapsed_s: null, best_wave: 400 },
  }));
  expect(screen.getByText("New best")).toBeInTheDocument();
  expect(screen.getByText("400")).toBeInTheDocument();
});
