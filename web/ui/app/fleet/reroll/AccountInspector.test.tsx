import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { fetchAccountSnapshot } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { AccountInspector } from "./AccountInspector";

vi.mock("@/lib/api", () => ({ fetchAccountSnapshot: vi.fn() }));
vi.mock("./Purchases", () => ({ WorkerBattlePurchases: () => <p>Battle evidence</p>, SharedWorkshopLedger: () => <p>Scoped ledger</p> }));
const member: RerollMember = { name: "Air_1", endpoint: "e", lease_id: "lease", state: "running", account_key: "worker:Air_1", account_id: "100" };
beforeEach(() => vi.clearAllMocks());

test("workshop reads explicit identity and distinguishes missing levels from purchases", async () => {
  vi.mocked(fetchAccountSnapshot).mockResolvedValue({ persistence_available: true, error: null,
    errors: { account: null, run: null }, revision: null, unknown_state: null });
  render(<AccountInspector member={member} onClose={() => {}}><p>Overview details</p></AccountInspector>);
  expect(fetchAccountSnapshot).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("tab", { name: "Workshop" }));
  expect(await screen.findByText("Exact levels have not been observed.")).toBeInTheDocument();
  expect(fetchAccountSnapshot).toHaveBeenCalledWith("worker:Air_1");
});

test("a mismatched saved account is rejected", async () => {
  vi.mocked(fetchAccountSnapshot).mockResolvedValue({ persistence_available: true, error: null,
    errors: { account: null, run: null }, unknown_state: null,
    revision: { account_id: "another", revision_id: 1, parent_revision_id: null, created_at: null, game_version: null,
      registry_version: "1", workshop_stats: [], workshop_levels: [], lab_levels: null,
      effective_account_stats: null, inventory: null, unlocks: null, settings: null } });
  render(<AccountInspector member={member} onClose={() => {}}><p>Overview details</p></AccountInspector>);
  fireEvent.click(screen.getByRole("tab", { name: "Workshop" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Account identity changed");
});
