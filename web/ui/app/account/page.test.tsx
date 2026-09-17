import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccountPage from "./page";
const { fetchAccount, fetchConcepts, collectStats, claimMissions, claimMilestones } = vi.hoisted(() => ({
  fetchAccount: vi.fn(), fetchConcepts: vi.fn(), collectStats: vi.fn(), claimMissions: vi.fn(), claimMilestones: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ fetchAccount, fetchConcepts, collectStats, claimMissions, claimMilestones }));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => ({ selected: { running: true } }) }));
const concept = { concept_id: "stats.damage", name: "Damage", domain: "stats", kind: "stat", unit: null, execution_scopes: ["workshop"], prerequisites: null, unlocks: [], rule_verified: false };
const unknown = { revision_id: null, registry_version: "1", account_id: null, game_version: null, workshop_stats: [], workshop_levels: null, lab_levels: null, inventory: null, effective_account_stats: null, unlocks: null, settings: null };
const snapshot = { persistence_available: true, error: null, errors: { account: null, run: null }, revision: null, unknown_state: unknown };
const fact = { concept_id: "stats.damage", value: 0, status: "verified", evidence: { observed_at: 1, confidence: .96, raw_name: "Damage", raw_value: "0.00", rect: [1, 2, 3, 4], frame_width: 400, frame_height: 800, frame_digest: "abc123", frame_ref: null } };
beforeEach(() => {
  fetchAccount.mockReset().mockResolvedValue(snapshot);
  collectStats.mockReset().mockResolvedValue({ status: "running", step: "open_settings", requested_at: 1, trail: ["open_settings"], result: null });
  claimMissions.mockReset().mockResolvedValue({ status: "running", step: "open_missions", requested_at: 1, claimed: 0, trail: ["open_missions"], result: null });
  claimMilestones.mockReset().mockResolvedValue({ status: "running", step: "open_milestones", requested_at: 1, claimed: 0, trail: ["open_milestones"], result: null });
  fetchConcepts.mockReset().mockResolvedValue({ registry_version: "1", concepts: [concept, { ...concept, concept_id: "cards.damage", name: "Damage card", domain: "cards", kind: "card", execution_scopes: [] }] });
});
describe("Account inspector", () => {
  it("distinguishes unscanned values and unsupported readers without inventing levels or locks", async () => {
    render(<AccountPage />);
    await screen.findByText("Unknown account");
    expect(screen.getByText("stats · Not yet scanned / no verified value")).toBeDefined();
    expect(screen.getByText("cards · Unknown · reader unavailable")).toBeDefined();
    expect(screen.getAllByText("Unknown · reader unavailable in this API")).toHaveLength(6);
    expect(screen.getAllByText("Prerequisites: Unknown in catalog.")).toHaveLength(2);
  });
  it("traces zero values to stale evidence without inventing a source image", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, revision: { ...unknown, revision_id: 2, workshop_stats: [fact] } });
    render(<AccountPage />);
    await screen.findByText("0");
    fireEvent.click(screen.getAllByText("Damage")[0]);
    expect(screen.getByText("Damage → 0.00")).toBeDefined();
    expect(screen.getByText("abc123")).toBeDefined();
    expect(screen.getByText("verified · Stale saved evidence")).toBeDefined();
    expect(screen.getByText("Image not retained; digest and OCR evidence only.")).toBeDefined();
    expect(screen.queryByRole("img")).toBeNull();
  });
  it("retains observations through metadata outages and failed refreshes", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, revision: { ...unknown, revision_id: 2, workshop_stats: [fact] } });
    fetchConcepts.mockRejectedValue(new Error("metadata offline"));
    render(<AccountPage />);
    await screen.findByText("abc123");
    expect(screen.getByRole("alert").textContent).toContain("metadata");
    fetchAccount.mockRejectedValue(new Error("503"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText(/Could not load account: 503/);
    expect(screen.getByText("abc123")).toBeDefined();
  });
  it("does not infer unscanned data when the initial account request fails", async () => {
    fetchAccount.mockRejectedValue(new Error("503"));
    render(<AccountPage />);
    await screen.findByText(/Could not load account: 503/);
    expect(screen.getByText("account state unavailable")).toBeDefined();
    expect(screen.getByText("stats · Unknown · account state unavailable")).toBeDefined();
    expect(screen.queryByText("no saved revision")).toBeNull();
    expect(screen.queryByText("stats · Not yet scanned / no verified value")).toBeNull();
  });
  it("does not report zero observations when account storage could not restore a revision", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, error: "corrupt storage", errors: { account: "corrupt storage", run: null } });
    render(<AccountPage />);
    await screen.findByText("Unavailable");
    expect(screen.getByText("account state unavailable")).toBeDefined();
    expect(screen.getByText("stats · Unknown · account state unavailable")).toBeDefined();
    expect(screen.queryByText("0")).toBeNull();
    expect(screen.queryByText("no saved revision")).toBeNull();
    expect(screen.queryByText("Not yet scanned / no verified values")).toBeNull();
  });
  it("filters catalog inventory domains", async () => {
    render(<AccountPage />);
    await screen.findByText("Damage card");
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "cards" } });
    expect(screen.queryByText("Damage")).toBeNull();
    expect(screen.getByText("Damage card")).toBeDefined();
  });
  it("arms the read-only collection and reports that the bot is walking the game", async () => {
    const idle = { status: "idle", step: "idle", requested_at: null, trail: [], result: null };
    fetchAccount.mockResolvedValue({ ...snapshot, collection: idle });
    render(<AccountPage />);
    await screen.findByText("No collection has run this session.");
    fetchAccount.mockResolvedValue({ ...snapshot, collection: { ...idle, status: "running", step: "open_stats" } });
    fireEvent.click(screen.getByRole("button", { name: "Collect stats" }));
    await screen.findByText("Walking the game now: open stats. All other automation is held.");
    expect(collectStats).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Collecting…" })).toHaveProperty("disabled", true);
  });
  it("reports where a stopped collection stopped without implying anything was read", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, collection: {
      status: "failed", step: "idle", requested_at: 1, trail: ["open_settings", "open_stats"],
      result: { status: "failed", reason: "settings_not_reached", detail: "The Settings panel was not observed.", screen_id: null, finished_at: 9 },
    } });
    render(<AccountPage />);
    await screen.findByText(/Last run stopped \(settings not reached\)/);
    expect(screen.getByText(/The Settings panel was not observed\./)).toBeDefined();
    // A stopped run must never read as a completed one, nor as a Stats panel.
    expect(screen.queryByText(/returned to the main menu/)).toBeNull();
    expect(screen.getByRole("button", { name: "Collect stats" })).toHaveProperty("disabled", false);
  });
  it("reports a completed collection by the panel it actually read", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, collection: {
      status: "completed", step: "idle", requested_at: 1, trail: ["open_settings", "open_stats", "collect", "confirm_home"],
      result: { status: "completed", reason: "collected", detail: "ok", screen_id: "account.stats.summary", finished_at: 9 },
    } });
    render(<AccountPage />);
    await screen.findByText("Last run read account.stats.summary and returned to the main menu.");
    expect(screen.getByRole("button", { name: "Collect stats" })).toHaveProperty("disabled", false);
  });
  it("surfaces a refusal without claiming the game was touched", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, collection: { status: "idle", step: "idle", requested_at: null, trail: [], result: null } });
    collectStats.mockRejectedValue(new Error("Collecting stats requires a confirmed main menu"));
    render(<AccountPage />);
    await screen.findByText("No collection has run this session.");
    fireEvent.click(screen.getByRole("button", { name: "Collect stats" }));
    await screen.findByText(/Could not start a collection: Collecting stats requires a confirmed main menu\. Nothing was tapped\./);
  });
  it("arms a missions claim walk and disables the button while it is in flight", async () => {
    render(<AccountPage />);
    await screen.findByText("Unknown account");
    fireEvent.click(screen.getByRole("button", { name: "Claim missions" }));
    expect(claimMissions).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Claiming…" })).toHaveProperty("disabled", true);
    await screen.findByRole("button", { name: "Claim missions" });
    expect(screen.getByRole("button", { name: "Claim missions" })).toHaveProperty("disabled", false);
  });
  it("surfaces a milestones claim refusal without claiming the game was touched", async () => {
    claimMilestones.mockRejectedValue(new Error("Milestones claim requires a confirmed main menu"));
    render(<AccountPage />);
    await screen.findByText("Unknown account");
    fireEvent.click(screen.getByRole("button", { name: "Claim milestones" }));
    await screen.findByText(/Could not start a claim: Milestones claim requires a confirmed main menu\. Nothing was tapped\./);
    expect(screen.getByRole("button", { name: "Claim milestones" })).toHaveProperty("disabled", false);
  });
  it("distinguishes a backend that cannot walk the game from an idle transaction", async () => {
    render(<AccountPage />);
    await screen.findByText("This backend cannot walk the game. Open Settings → Stats yourself, then refresh.");
    expect(screen.queryByRole("button", { name: "Collect stats" })).toBeNull();
    expect(collectStats).not.toHaveBeenCalled();
  });
  it("renders screen observations separately and retains them after failed refresh", async () => {
    fetchAccount.mockResolvedValue({ ...snapshot, screen_readings: {
      current_screen_id: "account.settings", error: null, readings: [{
        screen_id: "account.settings", observed_at: 100, frame_width: 1080, frame_height: 2400,
        frame_digest: "settings-frame", tiers: [], fields: [{
          key: "game_version", label: "Game version", raw_value: "v29.0.1", status: "observed", confidence: .99, rect: [824, 1908, 124, 38],
        }],
      }],
    } });
    render(<AccountPage />);
    await screen.findByText("v29.0.1");
    expect(screen.getByText("no saved revision")).toBeDefined();
    expect(screen.getByText("Unknown account")).toBeDefined();
    expect(screen.getByText("Visible at last refresh")).toBeDefined();
    fetchAccount.mockRejectedValue(new Error("503"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText(/Could not load account: 503/);
    expect(screen.getByText("v29.0.1")).toBeDefined();
    expect(screen.getByText("settings-frame")).toBeDefined();
  });
});
