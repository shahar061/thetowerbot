import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import MilestonesPage from "./page";
import { fetchAccountMetrics, fetchMilestoneRoadmap } from "@/lib/api";

let selection: { selected: { key: string; account_id: string } | null; loading: boolean };
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => selection }));
vi.mock("@/lib/api", () => ({ fetchMilestoneRoadmap: vi.fn(), fetchAccountMetrics: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  selection = { selected: { key: "worker:Air_20", account_id: "ACCOUNT20" }, loading: false };
  vi.mocked(fetchAccountMetrics).mockResolvedValue({ account_id: "ACCOUNT20", game_started: "2026-08-29",
    account_age_days: 20, recent_cps: .2, lifetime_coins: 1000, lifetime_coins_incomplete: false });
  vi.mocked(fetchMilestoneRoadmap).mockResolvedValue({ schema_version: 1, account_id: "ACCOUNT20",
    best_waves: { "1": 27 }, nodes: [
      { id: "cards.available", title: "Cards", group: "Cards", description: "Use cards.", kind: "available", tier: null, wave: null, requires: [], source_url: "https://example.com/cards", status: "available", progress: null },
      { id: "labs.unlocked", title: "Unlock Labs", group: "Labs", description: "Claim Labs.", kind: "unlock", tier: 1, wave: 30, requires: [], source_url: "https://example.com/labs", status: "in_progress", progress: { current: 27, target: 30 } },
    ] });
});

test("shows account path, progress, and selectable milestone evidence", async () => {
  render(<MilestonesPage />);
  expect(await screen.findByRole("heading", { name: "Milestone path" })).toBeInTheDocument();
  expect(screen.getByText("ACCOUNT20")).toBeInTheDocument();
  expect(screen.getByText("27 / 30")).toBeInTheDocument();
  expect(await screen.findByText("20 days")).toBeInTheDocument();
  expect(screen.getByText("0.2")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cards, Available" }));
  expect(screen.getByRole("heading", { name: "Cards" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Read game guide" })).toHaveAttribute("href", "https://example.com/cards");
});

test("asks for account selection when none is selected", () => {
  selection = { selected: null, loading: false };
  render(<MilestonesPage />);
  expect(screen.getByRole("heading", { name: "Choose a game account" })).toBeInTheDocument();
  expect(fetchMilestoneRoadmap).not.toHaveBeenCalled();
});
