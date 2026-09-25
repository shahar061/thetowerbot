import { render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import StrategiesPage from "./page";
import type { RerollSnapshot } from "@/lib/fleet";

let pool: RerollSnapshot;
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool, loading: false, error: null }) }));
vi.mock("../Purchases", () => ({ WorkerBattlePurchases: ({ accountKey }: { accountKey?: string }) => <p>Evidence for {accountKey}</p> }));
beforeEach(() => {
  pool = { candidates: [], members: [
    { name: "Air_2", endpoint: "", lease_id: "b", state: "running", account_id: "200", account_key: "two", variant_name: "Turtle",
      reroll_plan: { account_id: "200", stage: "turtle", goal: "Reach T1 W60", state: "buy", item: "Defense Absolute", price: 10, wallet_coins: 50, lifetime_coins: null, reason: "Survive longer", observed_at: 100 } },
    { name: "Air_1", endpoint: "", lease_id: "a", state: "running", account_id: "100", account_key: "one", variant_name: "Balanced",
      reroll_plan: { account_id: "100", stage: "opening", goal: "Reach T1 W20", state: "buy", item: "Damage", price: 10, wallet_coins: 50, lifetime_coins: null, reason: "Opening damage", observed_at: 100 } },
  ] };
});

test("compares effective account plans in stable worker order with separate battle evidence", () => {
  render(<StrategiesPage />);
  const columns = screen.getAllByRole("article");
  expect(within(columns[0]).getByRole("heading", { name: "Air_1" })).toBeInTheDocument();
  expect(within(columns[0]).getByText("Damage")).toBeInTheDocument();
  expect(within(columns[0]).getByText(/Phase: Opening/)).toBeInTheDocument();
  expect(within(columns[0]).getByText("Evidence for one")).toBeInTheDocument();
  expect(within(columns[1]).getByText("Defense Absolute")).toBeInTheDocument();
  expect(screen.getByText(/Live battle intent unavailable/)).toBeInTheDocument();
});

test("rejects a plan for a replaced account and omits hidden workers", () => {
  pool.members[0].reroll_plan!.account_id = "old";
  pool.members[1].hidden = true;
  render(<StrategiesPage />);
  expect(screen.getAllByRole("article")).toHaveLength(1);
  expect(screen.queryByText("Defense Absolute")).not.toBeInTheDocument();
  expect(screen.getByText(/Workshop plan unavailable for this account/)).toBeInTheDocument();
});
