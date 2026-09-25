import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { Upgrade } from "@/lib/types";
import { FlowBuilder } from "./FlowBuilder";
vi.mock("./routeCanvas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

const api = vi.hoisted(() => ({ preview: vi.fn(), publish: vi.fn(), revisions: vi.fn(), rollback: vi.fn(), rebind: vi.fn() }));
vi.mock("@/lib/api", () => ({
  previewBuildRoute: api.preview, publishBuildRoute: api.publish,
  fetchBuildRouteRevisions: api.revisions, rollbackBuildRoute: api.rollback,
  previewBuildRouteRebind: api.rebind,
}));

const route: BuildRouteDocument = {
  schema: 1, revision: 2, authored_at: null,
  baseline: {
    workshop: { id: "workshop.default", mode: "legacy_planner", priority_ids: ["damage", "cash_per_wave"],
      banned_upgrade_ids: [], coin_spend_limit_pct: 100, draw_chance_pct: 0, weights: {} },
    battle: { mode: "legacy_policy", branches: [] },
    gems: { lab_slot2_reserve: 100, spend_limit_pct: 100, steps: ["unlock_lab_slot_2"] },
    labs: { slot1_research: "game_speed", steps: ["research_game_speed"] },
  }, overrides: {}, dependencies: {},
};
const catalog: Upgrade[] = [
  { id: "damage", name: "Damage", category: "ATTACK", aliases: [], unlock: false },
  { id: "cash_per_wave", name: "Cash/Wave", category: "UTILITY", aliases: [], unlock: false },
  { id: "defense_absolute", name: "Defense Absolute", category: "DEFENSE", aliases: [], unlock: false },
];
const members = [{ name: "Air_38", account_id: "account-a" }, { name: "Air_39", account_id: "account-b" }];

beforeEach(() => {
  vi.clearAllMocks();
  api.preview.mockResolvedValue({ saved_revision: 2, proposed_revision: 3, members: [{ worker: "Air_38", account_id: "account-a",
    current: { status: "observed", decision: { item: "Damage" } }, proposed: { status: "projected", decision: { item: "Cash/Wave" } } }] });
  api.publish.mockImplementation(async (draft: BuildRouteDocument) => ({ ...draft, revision: 3 }));
  api.revisions.mockResolvedValue({ revisions: [route] });
  api.rebind.mockResolvedValue({ worker: "Air_38", old_account_id: "old-account",
    new_account_id: "account-a", patched_rules: ["workshop.default"],
    old_effective: { workshop: { coin_spend_limit_pct: 30 } },
    new_effective: { workshop: { coin_spend_limit_pct: 100 } } });
});

test("reorders with buttons and pointer drag, keeping keyboard focus", () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  const move = screen.getByRole("button", { name: "Move Cash/Wave up" });
  move.focus();
  fireEvent.click(move);
  expect(within(screen.getAllByTestId("priority-row")[0]).getByText("Cash/Wave")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Move Cash/Wave down" })).toHaveFocus();
  fireEvent.dragStart(screen.getAllByTestId("priority-row")[0]);
  fireEvent.dragOver(screen.getAllByTestId("priority-row")[1]);
  fireEvent.drop(screen.getAllByTestId("priority-row")[1]);
  expect(within(screen.getAllByTestId("priority-row")[0]).getByText("Damage")).toBeInTheDocument();
});

test("moves a palette block to Never Buy without checkboxes and publishes budget and luck separately", async () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.change(screen.getByRole("searchbox", { name: "Search upgrades" }), { target: { value: "defense" } });
  fireEvent.dragStart(screen.getByTestId("palette-defense_absolute"));
  fireEvent.dragOver(screen.getByTestId("never-buy-drop"));
  fireEvent.drop(screen.getByTestId("never-buy-drop"));
  expect(screen.getByText("Never Buy · 1")).toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: /Never buy/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Spend limit"), { target: { value: "30" } });
  fireEvent.change(screen.getByLabelText("Weighted luck"), { target: { value: "25" } });
  expect(screen.getByLabelText("Spend limit")).toHaveValue(30);
  expect(screen.getByLabelText("Weighted luck")).toHaveValue(25);
  expect(screen.getByLabelText("Weight for Cash/Wave")).toHaveValue(1);
  fireEvent.click(screen.getByRole("button", { name: "Publish route" }));
  await waitFor(() => expect(api.publish).toHaveBeenCalledWith(expect.objectContaining({
    baseline: expect.objectContaining({ workshop: expect.objectContaining({
      banned_upgrade_ids: ["defense_absolute"], coin_spend_limit_pct: 30, draw_chance_pct: 25,
    }) }),
  }), 2));
});

test("previews current and proposed per account before publishing", async () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(await screen.findByText("Current · Damage")).toBeInTheDocument();
  expect(screen.getByText("Proposed · Cash/Wave")).toBeInTheDocument();
  expect(api.preview).toHaveBeenCalledWith(expect.objectContaining({ revision: 2 }), 2);
});

test("a 409 keeps the draft and requires refreshing the saved revision", async () => {
  api.publish.mockRejectedValueOnce({ status: 409, message: "revision conflict" });
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Spend limit"), { target: { value: "30" } });
  fireEvent.click(screen.getByRole("button", { name: "Publish route" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("revision conflict");
  expect(screen.getByLabelText("Spend limit")).toHaveValue(30);
  expect(screen.getByRole("button", { name: "Publish route" })).toBeDisabled();
});

test("shows inactive account binding and blocks publication", () => {
  const stale = { ...route, overrides: { Air_38: { account_id: "old-account", patches: {} } } };
  render(<FlowBuilder saved={stale} catalog={catalog} members={members} onPublished={vi.fn()} />);
  expect(screen.getByText(/Inactive: account changed/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Publish route" })).toBeDisabled();
});

test("rollback requires confirmation and reports the new revision", async () => {
  api.rollback.mockResolvedValue({ ...route, revision: 3 });
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Route history" }));
  fireEvent.click(await screen.findByRole("button", { name: "Restore revision 2" }));
  expect(screen.getByText("Restore revision 2 as a new revision?")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Confirm restore" }));
  await waitFor(() => expect(api.rollback).toHaveBeenCalledWith(2, 2));
  expect(await screen.findByText("Revision 3 published")).toBeInTheDocument();
});

test("account scope edits a patch and reset restores fleet inheritance", () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Strategy scope"), { target: { value: "Air_38" } });
  fireEvent.change(screen.getByLabelText("Spend limit"), { target: { value: "30" } });
  expect(screen.getByRole("button", { name: "Reset override for Air_38" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Strategy scope"), { target: { value: "fleet" } });
  expect(screen.getByLabelText("Spend limit")).toHaveValue(100);
  fireEvent.click(screen.getByRole("button", { name: "Reset override for Air_38" }));
  fireEvent.change(screen.getByLabelText("Strategy scope"), { target: { value: "Air_38" } });
  expect(screen.getByLabelText("Spend limit")).toHaveValue(100);
});

test("rebind needs a server preview and an explicit reapply action", async () => {
  const stale = { ...route, overrides: { Air_38: { account_id: "old-account", patches: {
    "workshop.default": { coin_spend_limit_pct: 30 },
  } } } };
  render(<FlowBuilder saved={stale} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Preview rebind for Air_38" }));
  expect(await screen.findByRole("dialog", { name: "Reapply account override" })).toHaveTextContent("100% → reapplying 30%");
  expect(screen.getByRole("button", { name: "Publish route" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reapply override" }));
  expect(screen.getByRole("button", { name: "Publish route" })).toBeEnabled();
});

test("builds a highest-wave economy branch with ten-wave budget and separate luck", () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "In-game lane" }));
  fireEvent.click(screen.getByRole("button", { name: "Build battle phases" }));
  fireEvent.click(screen.getByRole("button", { name: "Add 50-wave economy branch" }));
  expect(screen.getByLabelText("Highest-wave gate for battle.economy.1")).toHaveValue(50);
  expect(screen.getByLabelText("End wave for battle.economy.1.first10")).toHaveValue(10);
  fireEvent.change(screen.getByLabelText("Cash spend limit for battle.economy.1.first10"), { target: { value: "30" } });
  fireEvent.change(screen.getByLabelText("Weighted luck for battle.economy.1.first10"), { target: { value: "25" } });
  expect(screen.getByLabelText("Cash spend limit for battle.economy.1.first10")).toHaveValue(30);
  expect(screen.getByLabelText("Weighted luck for battle.economy.1.first10")).toHaveValue(25);
  fireEvent.dragStart(screen.getByTestId("battle-palette-battle.economy.1.first10-damage"));
  fireEvent.dragOver(screen.getByTestId("battle-path-battle.economy.1.first10"));
  fireEvent.drop(screen.getByTestId("battle-path-battle.economy.1.first10"));
  expect(screen.getByTestId("battle-priority-battle.economy.1.first10-damage")).toBeInTheDocument();
});

test("resource paths publish planned blocks without enabling unsupported automation", async () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /Gems lane/ }));
  fireEvent.dragStart(screen.getByTestId("resource-palette-cards"));
  fireEvent.dragOver(screen.getByTestId("gem-path-drop"));
  fireEvent.drop(screen.getByTestId("gem-path-drop"));
  expect(screen.getAllByText("Planned · not automated").length).toBeGreaterThan(0);
  expect(screen.getByTestId("resource-step-cards")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Labs lane/ }));
  fireEvent.click(screen.getByRole("button", { name: /Add Slot 2 research/ }));
  expect(screen.getByTestId("resource-step-slot2_research")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Publish route" }));
  await waitFor(() => expect(api.publish).toHaveBeenCalledWith(expect.objectContaining({
    baseline: expect.objectContaining({ gems: expect.objectContaining({ steps: ["unlock_lab_slot_2", "cards"] }),
      labs: expect.objectContaining({ steps: ["research_game_speed", "slot2_research"] }) }),
  }), 2));
});

test("gem spending limit validates before publication", () => {
  render(<FlowBuilder saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /Gems lane/ }));
  fireEvent.change(screen.getByLabelText("Gem spend limit"), { target: { value: "120" } });
  expect(screen.getByRole("button", { name: "Publish route" })).toBeDisabled();
  expect(screen.getByText("Gem spend limit must be between 0 and 100%.")).toBeInTheDocument();
});
