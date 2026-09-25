import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { FleetRoutePreview } from "./FleetRoutePreview";
import type { RerollMember } from "@/lib/fleet";

const members: RerollMember[] = [
  { name: "Air_38", endpoint: "", lease_id: "a", state: "running", account_id: "account-a",
    wallet_gems: 75, battle_cash: 20, route_revision_applied: 2,
    reroll_plan: { account_id: "account-a", stage: "opening", goal: "Reach T1 W20", state: "buy",
      item: "Damage", price: 10, wallet_coins: 50, lifetime_coins: null,
      reason: "Opening damage", observed_at: 100 } },
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

test("decision inspector explains the selected evidence without inventing battle intent", () => {
  render(<FleetRoutePreview members={members} savedRevision={2} />);
  fireEvent.click(screen.getByRole("button", { name: "Why Damage for Air_38?" }));
  expect(screen.getByRole("region", { name: "Decision details" })).toHaveTextContent("Opening damage");
  expect(screen.getByRole("region", { name: "Decision details" })).toHaveTextContent("Revision 2");
  expect(screen.getByText("Live battle intent unavailable")).toBeInTheDocument();
});
