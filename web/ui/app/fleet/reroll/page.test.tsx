import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import RerollPage from "./page";
import { addRerollMembers, fetchReroll, fetchRerollJournal, startReroll } from "@/lib/api";

const choose = vi.fn();
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ accounts: [{ key: "one", account_id: "100", instance: "Air_1", running: true }, { key: "two", account_id: "200", instance: "Air_2", running: true }], choose }) }));
vi.mock("@/lib/api", () => ({ fetchReroll: vi.fn(), fetchRerollJournal: vi.fn(), addRerollMembers: vi.fn(), removeRerollMember: vi.fn(), startReroll: vi.fn(), pauseReroll: vi.fn(), setRerollConcurrency: vi.fn() }));

const members = [
  { name: "Air_1", endpoint: "127.0.0.1:5555", lease_id: "a", state: "running", account_id: "100", wave: 42, tier: 1, battle_cash: 0,
    reroll_plan: { account_id: "100", stage: "opening", goal: "Reach Tier 1 Wave 20", state: "buy", item: "Damage", price: 10, wallet_coins: 25, lifetime_coins: null, reason: "Damage is affordable.", observed_at: 1 } },
  { name: "Air_2", endpoint: "127.0.0.1:5556", lease_id: "b", state: "needs_choice", account_id: "200", uw_result: "Golden Tower offered" },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members: [] });
  vi.mocked(fetchRerollJournal).mockResolvedValue({ entries: [] });
});

test("empty pool offers candidates but rejects protected template", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [
    { name: "Air_3", endpoint: "127.0.0.1:5557", state: "ready" },
    { name: "Air_6", endpoint: "127.0.0.1:5559", state: "protected_template" },
  ], members: [] });
  vi.mocked(addRerollMembers).mockResolvedValue({ candidates: [], members: [] });
  render(<RerollPage />);
  expect(await screen.findByText(/No emulators in the pool/)).toBeInTheDocument();
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

test("start failure is visible and four workers warn about resources", async () => {
  vi.mocked(fetchReroll).mockResolvedValue({ candidates: [], members });
  vi.mocked(startReroll).mockRejectedValue(new Error("supervisor unavailable"));
  render(<RerollPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Start all" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("supervisor unavailable");
  fireEvent.change(screen.getByLabelText("Concurrent workers"), { target: { value: "4" } });
  expect(screen.getByText(/Four concurrent emulators/)).toBeInTheDocument();
});
