import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { AccountChoice } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { FleetLiveCard } from "./FleetLiveCard";

const member: RerollMember = { name: "Air_1", endpoint: "127.0.0.1:5555", lease_id: "lease",
  state: "running", account_id: "100", account_key: "worker:Air_1", best_tier_1_wave: 17,
  variant_name: "Balanced opening", workshop_upgrades_bought: 12,
  reroll_plan: { account_id: "100", stage: "opening", goal: "Reach T1 W20", state: "save",
    item: "Damage", price: 500, wallet_coins: 200, lifetime_coins: null, reason: "Save coins", observed_at: Date.now() / 1000 } };
const account: AccountChoice = { key: "worker:Air_1", account_id: "100", instance: "Air_1",
  running: true, kind: "worker", dashboard_url: "http://127.0.0.1:8766/" };

test("three fleet cards capture their own verified worker and preserve comparison rows", () => {
  render(<>{[1, 2, 3].map(i => <FleetLiveCard key={i}
    member={{ ...member, name: `Air_${i}`, account_id: `${i}00`, account_key: `worker:Air_${i}`,
      reroll_plan: { ...member.reroll_plan!, account_id: `${i}00` } }}
    account={{ ...account, instance: `Air_${i}`, key: `worker:Air_${i}`, account_id: `${i}00`, dashboard_url: `http://127.0.0.1:${8765 + i}/` }}
    onInspect={() => {}} />)}</>);
  const captures = screen.getAllByRole("img", { name: /Live screen of/ });
  expect(captures).toHaveLength(3);
  captures.forEach((capture, index) => {
    const url = new URL(capture.getAttribute("src")!);
    expect(url.port).toBe(String(8766 + index));
    expect(url.searchParams.get("scope")).toBe(`worker:Air_${index + 1}`);
    expect(url.searchParams.get("expected_account_id")).toBe(`${index + 1}00`);
  });
  expect(screen.getAllByText("Next workshop")).toHaveLength(3);
  expect(screen.getAllByText("Battle decision")).toHaveLength(3);
  expect(screen.getAllByText("300 coins short")).toHaveLength(3);
});

test("mismatched account identity never opens a capture or shows an old plan", () => {
  render(<FleetLiveCard member={{ ...member, reroll_plan: { ...member.reroll_plan!, account_id: "old" } }}
    account={{ ...account, account_id: "old" }} onInspect={() => {}} />);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByText(/Verified live account unavailable/)).toBeInTheDocument();
  expect(screen.queryByText("Damage")).not.toBeInTheDocument();
});

test("unknown prices and battle intent stay unknown and inspect keeps its account", () => {
  const inspect = vi.fn();
  render(<FleetLiveCard member={{ ...member, reroll_plan: { ...member.reroll_plan!, price: null } }}
    account={account} onInspect={inspect} />);
  expect(screen.getByText("Price not observed")).toBeInTheDocument();
  expect(screen.getByText("Live intent unavailable")).toBeInTheDocument();
  expect(screen.getByText("12 recorded workshop buys")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Inspect Air_1" }));
  expect(inspect).toHaveBeenCalledOnce();
});

test("an unavailable capture can retry without removing worker data", () => {
  render(<FleetLiveCard member={member} account={account} onInspect={() => {}} />);
  fireEvent.error(screen.getByRole("img", { name: "Live screen of Air_1" }));
  expect(screen.getByText(/Balanced opening/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry screen" }));
  expect(within(screen.getByRole("article", { name: "Air_1 live overview" })).getByRole("img")).toBeInTheDocument();
});
