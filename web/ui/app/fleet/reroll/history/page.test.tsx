import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import HistoryPage from "./page";
import { fetchRerollJournal, listRerolls } from "@/lib/api";

vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool: { members: [], run: { number: 1 } }, loading: false, error: null }) }));
vi.mock("../Purchases", () => ({ SharedWorkshopLedger: () => <p>Workshop ledger content</p> }));
vi.mock("@/lib/api", () => ({ fetchRerollJournal: vi.fn(), listRerolls: vi.fn() }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchRerollJournal).mockResolvedValue({ entries: [] });
  vi.mocked(listRerolls).mockResolvedValue({ runs: [] });
});
afterEach(() => { vi.useRealTimers(); });

test("activity preserves worker filtering and marks unknown account provenance", async () => {
  vi.mocked(fetchRerollJournal).mockResolvedValue({ entries: [
    { sequence: 1, at: 1, instance: "Air_1", level: "info", kind: "action", message: "Started first", color: "blue" },
    { sequence: 2, at: 2, instance: "Air_2", level: "warning", kind: "action", message: "Waiting second", color: "red" },
  ] });
  render(<HistoryPage />);
  expect(await screen.findByText("Started first")).toBeInTheDocument();
  expect(screen.getAllByText("Account not recorded")).toHaveLength(2);
  fireEvent.change(screen.getByLabelText("Journal emulator"), { target: { value: "Air_2" } });
  expect(screen.queryByText("Started first")).not.toBeInTheDocument();
  expect(screen.getByText("Waiting second")).toBeInTheDocument();
});

test("activity polling neither overlaps pending requests nor continues on another tab", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchRerollJournal).mockReturnValue(new Promise(() => {}));
  const view = render(<HistoryPage />);
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(fetchRerollJournal).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("tab", { name: "Purchases" }));
  expect(screen.getByText("Workshop ledger content")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(fetchRerollJournal).toHaveBeenCalledTimes(1);
  view.unmount();
});

test("shows journal failures and an explicit empty reroll archive", async () => {
  vi.mocked(fetchRerollJournal).mockRejectedValue(new Error("coordinator offline"));
  render(<HistoryPage />);
  expect(await screen.findByRole("alert")).toHaveTextContent("coordinator offline");
  fireEvent.click(screen.getByRole("tab", { name: "Rerolls" }));
  expect(await screen.findByText("No past rerolls recorded yet.")).toBeInTheDocument();
});

test("reroll archive failures remain visible", async () => {
  vi.mocked(listRerolls).mockRejectedValue(new Error("archive offline"));
  render(<HistoryPage />);
  fireEvent.click(screen.getByRole("tab", { name: "Rerolls" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("archive offline");
});

test("past rerolls retain archive links and worker provenance", async () => {
  vi.mocked(listRerolls).mockResolvedValue({ runs: [{ number: 2, name: "Reroll #2", status: "closed", started_at: "2026-09-20T00:00:00Z", member_count: 1, left_count: 1, members: ["Air_1"] }] });
  render(<HistoryPage />);
  fireEvent.click(screen.getByRole("tab", { name: "Rerolls" }));
  expect(await screen.findByRole("link", { name: "Reroll #2" })).toHaveAttribute("href", "/archives/?run=2");
  expect(screen.getByText("Air_1")).toBeInTheDocument();
});
