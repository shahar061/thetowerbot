import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import RerollPage from "./page";
import { addRerollMembers, fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases, fetchReroll, fetchRerollJournal, listRerolls, removeRerollMember, retireRerollMember, startNewReroll, startReroll, stopRerollInstance } from "@/lib/api";
import type { RerollMember, RerollPlan } from "@/lib/fleet";

const choose = vi.fn();
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ accounts: [{ key: "one", account_id: "100", instance: "Air_1", running: true }, { key: "two", account_id: "200", instance: "Air_2", running: true }], choose }) }));
vi.mock("@/lib/api", () => ({ fetchReroll: vi.fn(), fetchRerollJournal: vi.fn(), fetchAccountWorkshopPurchases: vi.fn(), fetchAccountRuns: vi.fn(), fetchAccountRunPurchases: vi.fn(), addRerollMembers: vi.fn(), removeRerollMember: vi.fn(), retireRerollMember: vi.fn(), startNewReroll: vi.fn(), stopRerollInstance: vi.fn(), listRerolls: vi.fn(), startReroll: vi.fn(), pauseReroll: vi.fn(), setRerollConcurrency: vi.fn() }));

// Named separately so a test that extends the plan keeps its full type -
// spreading `members[0].reroll_plan` inferred it as optional and lost it.
const plan: RerollPlan = { account_id: "100", stage: "opening", goal: "Reach Tier 1 Wave 20",
  state: "buy", item: "Damage", price: 10, wallet_coins: 25, lifetime_coins: null,
  reason: "Damage is affordable.", observed_at: 1 };

const members: RerollMember[] = [
  { name: "Air_1", endpoint: "127.0.0.1:5555", lease_id: "a", state: "running", account_id: "100", wave: 42, tier: 1, battle_cash: 0,
    reroll_plan: plan },
  { name: "Air_2", endpoint: "127.0.0.1:5556", lease_id: "b", state: "needs_choice", account_id: "200", uw_result: "Golden Tower offered" },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [] });
  vi.mocked(fetchRerollJournal).mockResolvedValue({ entries: [] });
  vi.mocked(listRerolls).mockResolvedValue({ runs: [] });
  vi.mocked(fetchAccountWorkshopPurchases).mockResolvedValue({ lines: [], balances: { coins: null, gems: null }, rehearsals: 0, next: null });
  vi.mocked(fetchAccountRuns).mockResolvedValue([]);
  vi.mocked(fetchAccountRunPurchases).mockResolvedValue({ purchases: [], totals: { count: 0, spent: 0, unpriced: 0, by_category: {} } });
});

test("worker cards collapse and scoped ledger shows confirmed purchases", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [{
    ...members[0], account_key: "worker:Air_1", lifetime_coins: 1200, workshop_upgrades_bought: 1,
    game_started: "2026-08-29", account_age_days: 20, recent_cps: .2,
  }] });
  vi.mocked(fetchAccountWorkshopPurchases).mockResolvedValue({ lines: [{
    id: 1, seq: 1, ts: 100, kind: "WORKSHOP_BUY", item: "Damage", category: "ATTACK",
    currency: "coins", delta: -20, price: 20, balance_after: 10, observed: 30,
    dry_run: 0, run_id: null, visit: 1, reason: null, detail: { verdict: "bought" },
  }], balances: { coins: 10, gems: null }, rehearsals: 0, next: null });
  render(<RerollPage />);
  expect(await screen.findByText("Shared Workshop ledger")).toBeInTheDocument();
  expect(await screen.findByText(/20 coins/)).toBeInTheDocument();
  expect(screen.getByText("20 days")).toBeInTheDocument();
  expect(screen.getByText("0.2")).toBeInTheDocument();
  expect(fetchAccountWorkshopPurchases).toHaveBeenCalledWith("worker:Air_1");
  const toggle = screen.getByRole("button", { name: "Collapse Air_1" });
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "Expand Air_1" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("Workshop upgrades bought").closest("#worker-Air_1")).toHaveAttribute("hidden");
  fireEvent.click(screen.getByRole("button", { name: "Collapse Shared Workshop ledger" }));
  expect(screen.getByRole("button", { name: "Expand Shared Workshop ledger" })).toHaveAttribute("aria-expanded", "false");
});

test("empty pool offers candidates but rejects protected template", async () => {
  const run = { number: 2, name: "Reroll #2", status: "active" as const, started_at: "2026-09-23T09:00:00Z" };
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [
    { name: "Air_3", endpoint: "127.0.0.1:5557", state: "ready" },
    { name: "Air_6", endpoint: "127.0.0.1:5559", state: "protected_template" },
  ], members: [], run });
  vi.mocked(addRerollMembers).mockResolvedValue({ candidates: [], members: [] });
  render(<RerollPage />);
  expect(await screen.findByText(/No emulators in this reroll/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add emulators" }));
  expect(screen.getByText(/Unavailable: protected template/)).toBeInTheDocument();
  const boxes = screen.getAllByRole("checkbox");
  expect(boxes[1]).toBeDisabled();
  fireEvent.click(boxes[0]);
  fireEvent.click(screen.getByRole("button", { name: "Add selected" }));
  await waitFor(() => expect(addRerollMembers).toHaveBeenCalledWith(["Air_3"]));
});

test("running workers have distinct accounts, unknown metrics, and labelled journal", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  vi.mocked(fetchRerollJournal).mockResolvedValue({ entries: [
    { sequence: 2, at: 20, instance: "Air_2", level: "warning", kind: "milestone", message: "Choose UW", color: "red" },
    { sequence: 1, at: 10, instance: "Air_1", level: "info", kind: "action", message: "Run started", color: "blue" },
  ] });
  render(<RerollPage />);
  expect(await screen.findByText("Golden Tower offered")).toBeInTheDocument();
  expect(screen.getByText("Next Workshop decision")).toBeInTheDocument();
  expect(screen.getByText(/Workshop coins: 25/)).toBeInTheDocument();
  expect(screen.getAllByText("Battle cash")).toHaveLength(2);
  expect(screen.getAllByText("—").length).toBeGreaterThan(3);
  fireEvent.click(screen.getByRole("link", { name: "Open account 200" }));
  expect(choose).toHaveBeenCalledWith("two");
  const one = screen.getByText("[Air_1]");
  const two = screen.getByText("[Air_2]");
  expect(one.getAttribute("style")).not.toEqual(two.getAttribute("style"));
  fireEvent.change(screen.getByLabelText("Journal emulator"), { target: { value: "Air_2" } });
  expect(screen.queryByText("Run started")).not.toBeInTheDocument();
  expect(screen.getByText("Choose UW")).toBeInTheDocument();
});

test("worker shows ten ordered Workshop buys including defense unlocks", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [{
    ...members[0], reroll_plan: { ...plan,
      next_purchases: Array.from({ length: 10 }, (_, index) => ({
        account_id: "100", position: index + 1,
        upgrade_id: index === 0 ? "unlock_defense_upgrades" : "defense_absolute",
        item: index === 0 ? "Unlock Defense Upgrades" : "Defense Absolute",
        category: "DEFENSE", unlock: index === 0,
        focus: "Survive longer",
      })),
    },
  }] });
  render(<RerollPage />);
  const list = await screen.findByRole("list", { name: "Next 10 Workshop buys for Air_1" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(10);
  expect(within(list).getByText("Unlock Defense Upgrades")).toBeInTheDocument();
});

test("start failure is visible and four workers warn about resources", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  vi.mocked(startReroll).mockRejectedValue(new Error("supervisor unavailable"));
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Start all" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("supervisor unavailable");
  fireEvent.change(screen.getByLabelText("Concurrent workers"), { target: { value: "4" } });
  expect(screen.getByText(/Four concurrent emulators/)).toBeInTheDocument();
});

test("a device that has stopped for a person sorts first and can be isolated", async () => {
  // The pool is sent running-first; the page must not show it that way. On a
  // second monitor the card that needs a human is the only card that matters,
  // and it was previously wherever the supervisor happened to list it.
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  render(<RerollPage />);

  await screen.findByText("Needs your choice");
  const names = () => screen.getAllByRole("heading", { level: 3 }).map(node => node.textContent);
  expect(names()).toEqual(["Air_2", "Air_1"]);

  fireEvent.click(screen.getByRole("button", { name: /Needs you/ }));
  expect(names()).toEqual(["Air_2"]);
  expect(screen.getByRole("button", { name: /Needs you/ })).toHaveAttribute("aria-pressed", "true");

  fireEvent.click(screen.getByRole("button", { name: /^Running/ }));
  expect(names()).toEqual(["Air_1"]);
});

test("an unrecognised worker state counts as something to look at", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], name: "Air_9", state: "warp_core_breach", reroll_plan: null },
  ] });
  render(<RerollPage />);

  expect(await screen.findByText("warp core breach")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Needs you 1" })).toBeInTheDocument();
});

test("the plan says how many coins are missing, not just the two numbers", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], reroll_plan: { ...plan, state: "save_coins", price: 100, wallet_coins: 25 } },
  ] });
  render(<RerollPage />);

  expect(await screen.findByText("75 short")).toBeInTheDocument();
  expect(screen.getByText(/Workshop coins: 25/)).toBeInTheDocument();
});

test("an unread price leaves the affordability bar unknown rather than empty", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], reroll_plan: { ...plan, state: "observe_price", price: null } },
  ] });
  render(<RerollPage />);

  expect(await screen.findByText("not read yet")).toBeInTheDocument();
  // An empty bar would read as "no coins"; the account has 25 of them.
  expect(screen.getByRole("progressbar", { name: /Coins toward Damage on Air_1/ }))
    .toHaveAttribute("aria-valuetext", "not read yet");
});

test("the ladder reports the distance left to the operator hand-off", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], best_tier_1_wave: 42 },
  ] });
  render(<RerollPage />);

  expect(await screen.findByText("Reach Tier 1 Wave 60")).toBeInTheDocument();
  expect(screen.getByText("T1 W42 · 18 to go")).toBeInTheDocument();
});

const run = { number: 2, name: "Reroll #2", status: "active" as const, started_at: "2026-09-23T09:00:00Z" };

test("header names the active reroll and New reroll warns before choosing", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [{ name: "Air_3", endpoint: "e3", state: "ready" }], members, run });
  vi.mocked(startNewReroll).mockResolvedValue({ candidates: [], members: [], run: { ...run, number: 3, name: "Reroll #3" } });
  render(<RerollPage />);
  expect(await screen.findByText(/Reroll #2 · started/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "New reroll" }));
  expect(await screen.findByRole("alertdialog", { name: "Close Reroll #2?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_1/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_3/ }));
  fireEvent.click(screen.getByRole("button", { name: "Start Reroll #3 and retire 1" }));
  await waitFor(() => expect(startNewReroll).toHaveBeenCalledWith({ keep: ["Air_1"], add: ["Air_3"] }));
});

test("without an active reroll the page offers Start a reroll and skips the warning", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [{ name: "Air_3", endpoint: "e3", state: "ready" }], members: [], run: null });
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Start a reroll" }));
  expect(await screen.findByRole("alertdialog", { name: "Start a reroll" })).toBeInTheDocument();
});

test("a running operation shows a banner and disables starting another", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run,
    operation: { kind: "new_run", state: "running", started_at: "x", results: [{ name: "Air_2", worker: "stopped" }] },
    stop_failures: [{ name: "Air_9", error: "window stuck" }] });
  render(<RerollPage />);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent(/Starting a new reroll/);
  expect(screen.getByRole("button", { name: "New reroll" })).toBeDisabled();
  // The local `busy` flag clears as soon as its own request returns, but the
  // supervisor's operation can stay running for minutes - every control that
  // could start a second concurrent action must key off `operation.state`
  // too, not just the request-in-flight flag.
  for (const button of screen.getAllByRole("button", { name: "Retire" })) expect(button).toBeDisabled();
  for (const button of screen.getAllByRole("button", { name: "Remove" })) expect(button).toBeDisabled();
  expect(screen.getByRole("button", { name: "Shut down Air_9" })).toBeDisabled();
});

test("retire asks first, then calls the retire route", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run });
  vi.mocked(retireRerollMember).mockResolvedValue({ candidates: [], members: [], run });
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Retire" }));
  expect(await screen.findByRole("alertdialog", { name: "Retire Air_1?" })).toBeInTheDocument();
  expect(retireRerollMember).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retire Air_1" }));
  await waitFor(() => expect(retireRerollMember).toHaveBeenCalledWith("Air_1"));
});

test("remove asks first, says it can come back, then calls the remove route", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run });
  vi.mocked(removeRerollMember).mockResolvedValue({ candidates: [], members: [], run });
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
  const dialog = await screen.findByRole("alertdialog", { name: "Remove Air_1 from the reroll?" });
  expect(dialog).toHaveTextContent(/add it back/);
  expect(removeRerollMember).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Remove Air_1" }));
  await waitFor(() => expect(removeRerollMember).toHaveBeenCalledWith("Air_1"));
  expect(retireRerollMember).not.toHaveBeenCalled();
});

test("a running removal shows its own progress text", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "remove", state: "running", started_at: "2026-09-23T09:00:00Z", target: "Air_1", results: [] } });
  render(<RerollPage />);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent("Removing Air_1 from the reroll…");
});

test("adding shows that stopped emulators boot to be checked", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "add", state: "running", started_at: "2026-09-23T09:00:00Z", target: "Air_3", results: [] } });
  render(<RerollPage />);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent(/Adding Air_3… a stopped emulator boots first/);
});

test("a rejected add explains an already-opened Tower", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "add", state: "failed", started_at: "2026-09-23T09:00:00Z", target: "Air_3", results: [], error: "tower_already_opened: Air_3" } });
  render(<RerollPage />);
  const banner = await screen.findByRole("status", { name: "Reroll operation" });
  expect(banner).toHaveTextContent("Last operation failed: tower_already_opened: Air_3");
  expect(banner).toHaveTextContent(/never opened/);
});

test("a retired emulator still running offers a shutdown retry", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [], run,
    stop_failures: [{ name: "Air_9", error: "window stuck" }] });
  vi.mocked(stopRerollInstance).mockResolvedValue({ candidates: [], members: [], run });
  render(<RerollPage />);
  expect(await screen.findByText(/Air_9 was retired but its emulator is still running/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Shut down Air_9" }));
  await waitFor(() => expect(stopRerollInstance).toHaveBeenCalledWith("Air_9"));
});

test("past rerolls list closed runs with links to their archives", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run });
  vi.mocked(listRerolls).mockResolvedValue({ runs: [
    { number: 2, name: "Reroll #2", status: "active", started_at: "2026-09-23T09:00:00Z", member_count: 2, retired_count: 0, members: ["Air_1", "Air_2"] },
    { number: 1, name: "Reroll #1", status: "closed", started_at: "2026-09-01T09:00:00Z", closed_at: "2026-09-23T09:00:00Z", member_count: 2, retired_count: 1, members: ["Air_1", "Air_0"] }] });
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Expand Past rerolls" }));
  expect(screen.getByRole("link", { name: "Reroll #1" })).toHaveAttribute("href", "/archives/?run=1");
});
