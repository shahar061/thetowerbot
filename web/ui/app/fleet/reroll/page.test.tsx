import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import RerollPage from "./page";
import { RerollWorkspaceProvider } from "./RerollWorkspace";
import { PastRerolls } from "./PastRerolls";
import { addRerollMembers, fetchAccountRunPurchases, fetchAccountRuns, fetchAccountWorkshopPurchases, fetchReroll, fetchRerollJournal, hideRerollMembers, listRerolls, removeRerollMember, restoreRerollMembers, startNewReroll, startReroll } from "@/lib/api";
import type { RerollMember, RerollPlan } from "@/lib/fleet";

const navigation = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(navigation.search) }));
const choose = vi.fn();
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ accounts: [{ key: "one", account_id: "100", instance: "Air_1", running: true }, { key: "two", account_id: "200", instance: "Air_2", running: true }], choose }) }));
vi.mock("@/lib/api", () => ({ fetchReroll: vi.fn(), fetchRerollJournal: vi.fn(), fetchAccountWorkshopPurchases: vi.fn(), fetchAccountRuns: vi.fn(), fetchAccountRunPurchases: vi.fn(), addRerollMembers: vi.fn(), removeRerollMember: vi.fn(), hideRerollMembers: vi.fn(), restoreRerollMembers: vi.fn(), startNewReroll: vi.fn(), listRerolls: vi.fn(), startReroll: vi.fn(), pauseReroll: vi.fn(), setRerollConcurrency: vi.fn() }));

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
  navigation.search = "";
  window.history.replaceState(null, "", "/fleet/reroll/");
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
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Inspect Air_1" }));
  expect(screen.getByText("20 days")).toBeInTheDocument();
  expect(screen.getByText("0.2")).toBeInTheDocument();
  const toggle = screen.getByRole("button", { name: "Collapse Air_1" });
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "Expand Air_1" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("Workshop upgrades bought").closest("#worker-Air_1")).toHaveAttribute("hidden");
  fireEvent.click(screen.getByRole("tab", { name: "Activity" }));
  expect(await screen.findByText(/20 coins/)).toBeInTheDocument();
  expect(fetchAccountWorkshopPurchases).toHaveBeenCalledWith("worker:Air_1", undefined, "100");
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
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByText(/No emulators in this reroll/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add emulators" }));
  expect(screen.getByText(/Unavailable: protected template/)).toBeInTheDocument();
  const boxes = screen.getAllByRole("checkbox");
  expect(boxes[1]).toBeDisabled();
  fireEvent.click(boxes[0]);
  fireEvent.click(screen.getByRole("button", { name: "Add selected" }));
  await waitFor(() => expect(addRerollMembers).toHaveBeenCalledWith(["Air_3"]));
});

test("running workers have distinct account summaries without switching global selection", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByText("Golden Tower offered")).toBeInTheDocument();
  expect(screen.getByText("Account 100")).toBeInTheDocument();
  expect(screen.getByText("Account 200")).toBeInTheDocument();
  expect(screen.getAllByText("Live intent unavailable")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Inspect Air_2" }));
  expect(choose).not.toHaveBeenCalled();
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
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Inspect Air_1" }));
  fireEvent.click(screen.getByRole("button", { name: "Show all 10 projections" }));
  const list = await screen.findByRole("list", { name: "Projected Workshop purchases for Air_1" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(10);
  expect(within(list).getByText("Unlock Defense Upgrades")).toBeInTheDocument();
});

test("start failure is visible and four workers warn about resources", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  vi.mocked(startReroll).mockRejectedValue(new Error("supervisor unavailable"));
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Start all" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("supervisor unavailable");
  fireEvent.change(screen.getByLabelText("Concurrent workers"), { target: { value: "4" } });
  expect(screen.getByText(/Four concurrent emulators/)).toBeInTheDocument();
});

test("fleet order stays stable and attention can be isolated", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);

  await screen.findByText("Needs your choice");
  const names = () => screen.getAllByRole("heading", { level: 3 }).map(node => node.textContent);
  expect(names()).toEqual(["Air_1", "Air_2"]);

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
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);

  expect(await screen.findByText("warp core breach")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Needs you 1" })).toBeInTheDocument();
});

test("the plan says how many coins are missing, not just the two numbers", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], reroll_plan: { ...plan, state: "save_coins", price: 100, wallet_coins: 25 } },
  ] });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);

  expect(await screen.findByText("75 coins short")).toBeInTheDocument();
  expect(screen.getByText("25 coins")).toBeInTheDocument();
});

test("an unread price leaves the affordability bar unknown rather than empty", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], reroll_plan: { ...plan, state: "observe_price", price: null } },
  ] });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);

  expect(await screen.findByText("Price not observed")).toBeInTheDocument();
  // An empty bar would read as "no coins"; the account has 25 of them.
  expect(screen.getByRole("progressbar", { name: "Workshop affordability for Air_1" }))
    .toHaveAttribute("aria-valuetext", "not read yet");
});

test("the ladder reports the distance left to the operator hand-off", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    { ...members[0], best_tier_1_wave: 42 },
  ] });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);

  expect(await screen.findByText("Reach Tier 1 Wave 60")).toBeInTheDocument();
  expect(screen.getByText("T1 W42 · 18 to go")).toBeInTheDocument();
});

const run = { number: 2, name: "Reroll #2", status: "active" as const, started_at: "2026-09-23T09:00:00Z" };

test("header names the active reroll and New reroll warns before choosing", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [{ name: "Air_3", endpoint: "e3", state: "ready" }], members, run });
  vi.mocked(startNewReroll).mockResolvedValue({ candidates: [], members: [], run: { ...run, number: 3, name: "Reroll #3" } });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByText(/Reroll #2 · started/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "New reroll" }));
  expect(await screen.findByRole("alertdialog", { name: "Close Reroll #2?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_1/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Air_3/ }));
  fireEvent.click(screen.getByRole("button", { name: "Start Reroll #3 and stop 1" }));
  await waitFor(() => expect(startNewReroll).toHaveBeenCalledWith({ keep: ["Air_1"], add: ["Air_3"] }));
});

test("without an active reroll the page offers Start a reroll and skips the warning", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [{ name: "Air_3", endpoint: "e3", state: "ready" }], members: [], run: null });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Start a reroll" }));
  expect(await screen.findByRole("alertdialog", { name: "Start a reroll" })).toBeInTheDocument();
});

test("a running operation shows a banner and disables starting another", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run,
    operation: { kind: "new_run", state: "running", started_at: "x", results: [{ name: "Air_2", worker: "stopped" }] } });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent(/Starting a new reroll… stopping the bots/);
  expect(screen.getByRole("button", { name: "New reroll" })).toBeDisabled();
  // The local `busy` flag clears as soon as its own request returns, but the
  // supervisor's operation can stay running for minutes - every control that
  // could start a second concurrent action must key off `operation.state`
  // too, not just the request-in-flight flag.
  fireEvent.click(screen.getByRole("button", { name: "Inspect Air_1" }));
  for (const button of screen.getAllByRole("button", { name: "Remove" })) expect(button).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Retire" })).not.toBeInTheDocument();
});

test("a bot that couldn't be stopped when the reroll changed says so on its card", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [{ ...members[0], leave_error: "identity_changed" }], run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByText("Couldn't stop this bot when the reroll changed: identity_changed")).toBeInTheDocument();
});

test("remove asks first, says it can come back, then calls the remove route", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run });
  vi.mocked(removeRerollMember).mockResolvedValue({ candidates: [], members: [], run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Inspect Air_1" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  const dialog = await screen.findByRole("alertdialog", { name: "Remove Air_1 from the reroll?" });
  expect(dialog).toHaveTextContent(/add it back/);
  expect(removeRerollMember).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Remove Air_1" }));
  await waitFor(() => expect(removeRerollMember).toHaveBeenCalledWith("Air_1"));
});

test("a running removal shows its own progress text", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "remove", state: "running", started_at: "2026-09-23T09:00:00Z", target: "Air_1", results: [] } });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent("Removing Air_1 from the reroll…");
});

test("adding shows that stopped emulators boot to be checked", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "add", state: "running", started_at: "2026-09-23T09:00:00Z", target: "Air_3", results: [] } });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("status", { name: "Reroll operation" })).toHaveTextContent(/Adding Air_3… a stopped emulator boots first/);
});

test("a rejected add explains an already-opened Tower", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0]], run,
    operation: { kind: "add", state: "failed", started_at: "2026-09-23T09:00:00Z", target: "Air_3", results: [], error: "tower_already_opened: Air_3" } });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  const banner = await screen.findByRole("status", { name: "Reroll operation" });
  expect(banner).toHaveTextContent("Last operation failed: tower_already_opened: Air_3");
  expect(banner).toHaveTextContent(/never opened/);
});

test("past rerolls list closed runs with links to their archives", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run });
  vi.mocked(listRerolls).mockResolvedValue({ runs: [
    { number: 2, name: "Reroll #2", status: "active", started_at: "2026-09-23T09:00:00Z", member_count: 2, left_count: 0, members: ["Air_1", "Air_2"] },
    { number: 1, name: "Reroll #1", status: "closed", started_at: "2026-09-01T09:00:00Z", closed_at: "2026-09-23T09:00:00Z", member_count: 2, left_count: 1, members: ["Air_1", "Air_0"] }] });
  render(<PastRerolls refreshKey={2} />);
  expect(await screen.findByRole("link", { name: "Reroll #1" })).toHaveAttribute("href", "/archives/?run=1");
});

test("a hidden device is left off the list but stays under Start all", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [members[0], { ...members[1], hidden: true }], run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("button", { name: "Inspect Air_1" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Inspect Air_2" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^All/ })).toHaveTextContent("1");
  expect(screen.getByRole("button", { name: "Start all" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: /^Hidden/ }));
  expect(await screen.findByRole("button", { name: "Inspect Air_2" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Restore Air_2" }));
  await waitFor(() => expect(restoreRerollMembers).toHaveBeenCalledWith(["Air_2"]));
});

test("only a device that isn't ready or running has the delete icon", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [
    ...members, { ...members[0], name: "Air_3", endpoint: "127.0.0.1:5557", state: "ready" },
    { ...members[0], name: "Air_4", endpoint: "127.0.0.1:5558", state: "paused" }], run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("button", { name: "Delete Air_2 from the list" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Delete Air_4 from the list" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete Air_1 from the list" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete Air_3 from the list" })).not.toBeInTheDocument();
});

test("delete removes one card from the list without asking", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Delete Air_2 from the list" }));
  await waitFor(() => expect(hideRerollMembers).toHaveBeenCalledWith(["Air_2"]));
  expect(removeRerollMember).not.toHaveBeenCalled();
});

test("delete all asks first and deletes only the shown cards that have the icon", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));
  const dialog = await screen.findByRole("alertdialog", { name: "Delete 1 device from the list?" });
  expect(dialog).toHaveTextContent(/keep running/);
  expect(hideRerollMembers).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Delete 1" }));
  await waitFor(() => expect(hideRerollMembers).toHaveBeenCalledWith(["Air_2"]));
});

test("delete all is off when every shown card is ready or running", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members, run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: /^Running/ }));
  expect(screen.getByRole("button", { name: "Delete all" })).toBeDisabled();
});

test("restore all brings back every hidden card", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: members.map(member => ({ ...member, hidden: true })), run });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  fireEvent.click(await screen.findByRole("button", { name: /^Hidden/ }));
  fireEvent.click(screen.getByRole("button", { name: "Restore all" }));
  await waitFor(() => expect(vi.mocked(restoreRerollMembers).mock.calls[0]?.[0].toSorted()).toEqual(["Air_1", "Air_2"]));
});


test("fleet wall keeps details off the landing page and does not fetch the journal", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("heading", { name: "Fleet Live" })).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Inspect Air_1" })).toBeInTheDocument();
  expect(screen.queryByText("Shared Workshop ledger")).not.toBeInTheDocument();
  expect(fetchRerollJournal).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Inspect Air_1" }));
  expect(screen.getByRole("region", { name: "Inspect Air_1" })).toBeInTheDocument();
  expect(screen.getAllByRole("article", { name: /live overview/ })).toHaveLength(2);
  expect(choose).not.toHaveBeenCalled();
});


test("same-route query navigation closes the inspector and stale attempt links cannot select a replacement", async () => {
  navigation.search = "worker=Air_1&account=&identity=100";
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  const view = render(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(await screen.findByRole("region", { name: "Inspect Air_1" })).toBeInTheDocument();
  navigation.search = "";
  view.rerender(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(screen.queryByRole("region", { name: "Inspect Air_1" })).not.toBeInTheDocument();
  navigation.search = "worker=Air_1&account=&identity=previous";
  view.rerender(<RerollWorkspaceProvider><RerollPage /></RerollWorkspaceProvider>);
  expect(screen.getByText(/This account attempt is no longer active/)).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Inspect Air_1" })).not.toBeInTheDocument();
});
