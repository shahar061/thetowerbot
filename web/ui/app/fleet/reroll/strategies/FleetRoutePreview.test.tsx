import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { FleetRoutePreview } from "./FleetRoutePreview";
import type { RerollMember } from "@/lib/fleet";
import { vi } from "vitest";
vi.mock("./routeCanvas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

const members: RerollMember[] = [
  { name: "Air_38", endpoint: "", lease_id: "a", state: "running", account_id: "account-a",
    wallet_gems: 75, battle_cash: 20, route_revision_applied: 2,
    reroll_plan: { account_id: "account-a", stage: "opening", goal: "Reach T1 W20", state: "buy",
      item: "Damage", upgrade_id: "damage", price: 10, wallet_coins: 50, lifetime_coins: null,
      reason: "Opening damage", observed_at: 100, confirmed_purchases: { damage: 2 },
      next_purchases: [{ account_id: "account-a", position: 1, upgrade_id: "cash_per_wave", item: "Cash/Wave", category: "UTILITY", unlock: false, focus: "Economy" }] } },
  { name: "Air_39", endpoint: "", lease_id: "b", state: "paused", account_id: "account-b",
    route_revision_applied: 1,
    reroll_plan: { account_id: "account-b", stage: "turtle", goal: "Reach T1 W60", state: "wait",
      item: "Defense Absolute", price: 80, wallet_coins: 40, lifetime_coins: null,
      reason: "Need more coins", observed_at: 100 } },
  { name: "Air_40", endpoint: "", lease_id: "c", state: "running", account_id: "account-c",
    reroll_plan: { account_id: "old-account", stage: "opening", goal: "Reach T1 W20", state: "buy",
      item: "Damage", price: 10, wallet_coins: 50, lifetime_coins: null,
      reason: "Old account", observed_at: 100 } },
];

test("compares verified next actions and revision state across the fleet", () => {
  render(<FleetRoutePreview members={members} savedRevision={2} />);
  const cards = screen.getAllByRole("article", { name: /Strategy for/ });
  expect(cards).toHaveLength(3);
  expect(within(cards[0]).getByText("Damage")).toBeInTheDocument();
  expect(within(cards[0]).getByText(/Next level: at least 3/)).toBeInTheDocument();
  expect(within(cards[0]).getByText(/10 coins · Price source unknown/)).toBeInTheDocument();
  expect(within(cards[0]).getByText(/Future price unknown/)).toBeInTheDocument();
  expect(within(cards[1]).getByText("40 / 80 coins")).toBeInTheDocument();
  expect(within(cards[1]).getByText("Pending until next spend")).toBeInTheDocument();
  expect(within(cards[2]).getAllByText("Unknown").length).toBeGreaterThan(0);
  expect(within(cards[2]).queryByText("Old account")).not.toBeInTheDocument();
});

test("one-emulator filter preserves fleet comparison access", () => {
  render(<FleetRoutePreview members={members} savedRevision={2} />);
  fireEvent.change(screen.getByLabelText("Emulator scope"), { target: { value: "Air_38" } });
  expect(screen.getAllByRole("article", { name: /Strategy for/ })).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("Emulator scope"), { target: { value: "fleet" } });
  expect(screen.getAllByRole("article", { name: /Strategy for/ })).toHaveLength(3);
});

test("explains why block strategies do not fabricate future purchases", () => {
  const member = structuredClone(members[0]);
  member.reroll_plan!.next_purchases = [];
  member.reroll_plan!.projection_note = "Future block choices depend on fresh prices and confirmed purchases";
  render(<FleetRoutePreview members={[member]} savedRevision={2} />);
  expect(screen.getByText(member.reroll_plan!.projection_note)).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: "Projected Workshop milestones" })).not.toBeInTheDocument();
});

test("shows a zero-spend price probe as observation rather than a selected buy", () => {
  const member = structuredClone(members[0]);
  member.reroll_plan = { ...member.reroll_plan!, state: "observe_price", stage: "strategy_observe", price: null,
    item: "Thorns", reason: "Inspect Thorns and Defense Absolute prices without spending", next_purchases: [] };
  render(<FleetRoutePreview members={[member]} savedRevision={2} />);
  expect(screen.getByText("Verify Workshop prices")).toBeInTheDocument();
  expect(screen.queryByText("Next selected buy")).not.toBeInTheDocument();
  expect(screen.getByText(/No spending during this inspection/)).toBeInTheDocument();
  expect(screen.getByText("Inspect Thorns and Defense Absolute prices without spending")).toBeInTheDocument();
});

test("decision inspector explains the selected evidence without inventing battle intent", () => {
  render(<FleetRoutePreview members={members} savedRevision={2} />);
  fireEvent.click(screen.getByRole("button", { name: "Why Damage for Air_38?" }));
  expect(screen.getByRole("region", { name: "Decision details" })).toHaveTextContent("Opening damage");
  expect(screen.getByRole("region", { name: "Decision details" })).toHaveTextContent("Revision 2");
  expect(screen.getByText("Live battle intent unavailable")).toBeInTheDocument();
});

test("keeps the last selected buy visible and labels its age once the worker goes quiet", () => {
  vi.useFakeTimers({ now: 1_000 * 1_000 });
  try {
    const member = structuredClone(members[1]);
    member.reroll_plan!.observed_at = 1_000 - 7 * 60;
    render(<FleetRoutePreview members={[member]} savedRevision={2} />);
    expect(screen.getByText("Defense Absolute")).toBeInTheDocument();
    expect(screen.getByText("Projected · as of 7m ago")).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

test("names each emulator's assigned strategy and flags one left behind by an account change", () => {
  const assignments = {
    Air_38: { account_id: "account-a", strategy_id: "eco", strategy_version: 2, strategy_name: "Turtle Eco Wall", baseline: {} },
    Air_39: { account_id: "someone-else", strategy_id: "old", strategy_version: 1, strategy_name: "Stale", baseline: {} },
  } as unknown as Parameters<typeof FleetRoutePreview>[0]["assignments"];
  render(<FleetRoutePreview members={members} savedRevision={2} assignments={assignments} />);
  const cards = screen.getAllByRole("article", { name: /Strategy for/ });
  expect(within(cards[0]).getByText("Strategy: Turtle Eco Wall v2")).toBeInTheDocument();
  expect(within(cards[1]).getByText("Strategy: Stale v1 · inactive, account changed")).toBeInTheDocument();
  expect(within(cards[2]).getByText("Strategy: Fleet baseline")).toBeInTheDocument();
});

test("a three-line strip links to the Labs and Workshop pages and says unknown without data", () => {
  const member = structuredClone(members[0]);
  member.recent_workshop_purchases = [{ at: Date.now() / 1000 - 125, item: "Coins / Kill Bonus", category: "UTILITY", cost: 126, reason: "Random draw (71%)" }];
  render(<FleetRoutePreview members={[member]} savedRevision={2} />);
  const strip = screen.getByRole("region", { name: "Labs, gems and last buy for Air_38" });
  expect(strip).toHaveTextContent("Labs: unknown");
  expect(strip).toHaveTextContent("Gems: unknown · 75 gems");
  expect(strip).toHaveTextContent("Last buy: Coins / Kill Bonus · 126 coins · 2m ago");
  expect(within(strip).getByRole("link", { name: "Labs" })).toHaveAttribute("href", "/fleet/reroll/labs/?worker=Air_38");
  expect(within(strip).getByRole("link", { name: "Last buy" })).toHaveAttribute("href", "/fleet/reroll/workshop/?worker=Air_38");
  expect(screen.queryByRole("list", { name: "Recent Workshop buys for Air_38" })).not.toBeInTheDocument();
  expect(screen.queryByText("Gems · Labs")).not.toBeInTheDocument();
});

test("the strip reads the worker's resource evaluation", () => {
  const member = structuredClone(members[0]);
  member.resource_evaluation = { account_id: "account-a", revision: 2, observed_at: 100,
    gem_step: { action: "unlock_lab_slot_2", status: "blocked", reason: "Save 25 more gems" },
    lab_step: { action: "research_game_speed", status: "supported", reason: "Game Speed available for 300 coins" } };
  render(<FleetRoutePreview members={[member]} savedRevision={2} />);
  const strip = screen.getByRole("region", { name: "Labs, gems and last buy for Air_38" });
  expect(strip).toHaveTextContent("Labs: Game Speed available for 300 coins · Automated");
  expect(strip).toHaveTextContent("Gems: Save 25 more gems · Waiting");
  expect(strip).toHaveTextContent("Last buy: none recorded");
});
