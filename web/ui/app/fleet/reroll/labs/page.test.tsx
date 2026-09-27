import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import FleetLabsPage from "./page";
import type { RerollSnapshot } from "@/lib/fleet";
import type { LabsSnapshot } from "@/lib/labs";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
const pool: RerollSnapshot = { candidates: [], members: [{ name: "Air_1", endpoint: "", lease_id: "current", state: "running", account_id: "current" }] };
const row: LabsSnapshot["workers"][number] = { worker: "Air_1", account_id: "old", strategy_name: null, read_at: null,
  wallet: { coins: null, gems: null }, plan: null, state: "unknown", reason: null, recent: [], freshness: "unknown", unknown_slots: 5, blockers: [] };
const snapshot: LabsSnapshot = { workers: [row], automated: [], reference: { labs: [], game_speed: [], lab_slots: [], card_slots: [],
  card_gems: 0, labs_unlock_wave: 40, sources: [] } };
vi.mock("../FleetLabsContext", () => ({ useFleetLabs: () => ({ snapshot, loading: false, error: null, refresh: async () => {} }) }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => ({ pool }) }));

test("labs page excludes an old-account row for the current worker", () => {
  render(<FleetLabsPage />);
  expect(screen.getByText("No account-matched lab observations available.")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Labs and gems" })).not.toBeInTheDocument();
});
