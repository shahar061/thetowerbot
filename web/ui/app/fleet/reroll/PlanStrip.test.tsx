import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { PlanStrip } from "./PlanStrip";
import type { RerollPlan } from "@/lib/fleet";

const plan: RerollPlan = {
  account_id: "100", stage: "opening", goal: "Reach T1 W20", state: "wait_coins",
  item: "Damage", price: 100, wallet_coins: 20, lifetime_coins: null, reason: "Save coins", observed_at: 10,
  next_purchases: Array.from({ length: 10 }, (_, index) => ({ account_id: "100", position: index + 1,
    upgrade_id: `upgrade_${index}`, item: `Upgrade ${index + 1}`, category: "ATTACK", unlock: false, focus: "Survive" })),
};

test("starts with three projected purchases and expands to ten without claiming execution", () => {
  render(<PlanStrip plan={plan} worker="Air_1" />);
  const queue = screen.getByRole("list", { name: "Projected Workshop purchases for Air_1" });
  expect(within(queue).getAllByRole("listitem")).toHaveLength(3);
  expect(screen.getByText(/Only the first verified decision/)).toBeInTheDocument();
  expect(screen.getByText("80 short")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show all 10 projections" }));
  expect(within(queue).getAllByRole("listitem")).toHaveLength(10);
  fireEvent.click(screen.getByRole("button", { name: "Show first 3 projections" }));
  expect(within(queue).getAllByRole("listitem")).toHaveLength(3);
});

test("never presents another account's projected purchase", () => {
  render(<PlanStrip plan={{ ...plan, next_purchases: [{ ...plan.next_purchases![0], account_id: "old", item: "Old account buy" }] }} worker="Air_1" />);
  expect(screen.queryByText("Old account buy")).not.toBeInTheDocument();
});
