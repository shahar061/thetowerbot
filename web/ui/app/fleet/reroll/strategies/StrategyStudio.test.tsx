import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { StrategyStudio } from "./StrategyStudio";

vi.mock("./studio.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));
const api = vi.hoisted(() => ({ save: vi.fn(), assign: vi.fn(), preview: vi.fn() }));
vi.mock("@/lib/api", () => ({ saveFleetStrategy: api.save, assignFleetStrategy: api.assign, previewBuildRoute: api.preview }));

const baseline: BuildRouteDocument["baseline"] = {
  workshop: { id: "workshop.default", mode: "blocks", blocks: [
    { id: "turtle.objectives", type: "native", policy: "turtle", phase: "objectives" },
  ], priority_ids: [], banned_upgrade_ids: [], coin_spend_limit_pct: 100, draw_chance_pct: 0, weights: {} },
  battle: { mode: "blocks", branches: [], blocks: [{ id: "battle", type: "native", policy: "turtle", phase: "battle" }] },
  gems: { lab_slot2_reserve: 100, spend_limit_pct: 100, steps: ["unlock_lab_slot_2"] },
  labs: { slot1_research: "game_speed", steps: ["research_game_speed"] },
};
const route: BuildRouteDocument = { schema: 1, revision: 4, authored_at: null, baseline, overrides: {}, dependencies: {} };
const template = { id: "turtle", name: "Turtle", version: 1, source_template: "turtle", builtin: true, baseline };
const library = { revision: 0, templates: [template, { ...template, id: "opening", name: "Opening", source_template: "opening" }], strategies: [] };
const catalog = [
  { id: "damage", name: "Damage", category: "ATTACK" as const, aliases: [], unlock: false },
  { id: "thorns", name: "Thorn Damage", category: "DEFENSE" as const, aliases: [], unlock: false },
  { id: "defense_absolute", name: "Defense Absolute", category: "DEFENSE" as const, aliases: [], unlock: false },
  { id: "cash_per_wave", name: "Cash / Wave", category: "UTILITY" as const, aliases: [], unlock: false },
];
const members = [{ name: "Air_38", account_id: "account-a" }, { name: "Air_39", account_id: "account-b" },
  { name: "Air_18", account_id: "old-account", hidden: true }];

beforeEach(() => {
  vi.clearAllMocks();
  api.save.mockImplementation(async (input) => ({ ...library, revision: 1, strategies: [{ ...template,
    id: "custom-1", builtin: false, name: input.name, baseline: input.baseline }] }));
  api.assign.mockResolvedValue({ ...route, revision: 5 });
  api.preview.mockResolvedValue({ saved_revision: 4, proposed_revision: 5, members: [] });
});

function setup(): void {
  render(<StrategyStudio library={library} saved={route} catalog={catalog} members={members} onPublished={vi.fn()} />);
}
function copy(): void {
  fireEvent.click(screen.getByRole("button", { name: "Create copy" }));
  fireEvent.change(screen.getByLabelText("Strategy name"), { target: { value: "Balanced turtle" } });
  fireEvent.click(screen.getByRole("button", { name: "Create editable copy" }));
}

test("shows protected native blocks, requiring a copy before editing", () => {
  setup();
  expect(screen.getByText("Protected template")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Turtle objectives/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Add Cheap pool" }));
  expect(screen.getByRole("dialog", { name: "Create your strategy" })).toBeInTheDocument();
  expect(api.save).not.toHaveBeenCalled();
});

test("copies template, edits a cheap pool, and saves without activating it", async () => {
  setup(); copy();
  fireEvent.click(screen.getByRole("button", { name: "Add Cheap pool" }));
  fireEvent.change(screen.getByLabelText("Minimum discount (%)"), { target: { value: "30" } });
  fireEvent.click(screen.getByRole("button", { name: "Save strategy" }));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ name: "Balanced turtle", source_template: "turtle",
    baseline: expect.objectContaining({ workshop: expect.objectContaining({ blocks: expect.arrayContaining([
      expect.objectContaining({ type: "pool", discount_pct: 30 }),
    ]) }) }) }), 0));
  expect(api.assign).not.toHaveBeenCalled();
  expect(template.baseline.workshop.blocks).toHaveLength(1);
  expect(await screen.findByText("Saved · v1")).toBeInTheDocument();
});

test("assigns the saved version only to selected visible current accounts", async () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "Assign" }));
  const dialog = screen.getByRole("dialog", { name: "Assign saved strategy" });
  expect(within(dialog).queryByText("Air_18")).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: /Air_38/ }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Assign to 1 emulator" }));
  await waitFor(() => expect(api.assign).toHaveBeenCalledWith("turtle", 1, [{ worker: "Air_39", account_id: "account-b" }], 4));
});

test("keeps a side palette in fullscreen and exits with Escape", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
  expect(screen.getByRole("region", { name: "Strategy Studio editor" })).toHaveAttribute("data-fullscreen", "true");
  expect(screen.getByRole("complementary", { name: "Block palette" })).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByRole("button", { name: "Full screen" })).toBeInTheDocument();
});

test("adds nested branches with keyboard controls and preserves them when saving", async () => {
  setup(); copy();
  fireEvent.click(screen.getByRole("button", { name: "Add If / else" }));
  fireEvent.click(screen.getByRole("button", { name: "Add to Then" }));
  fireEvent.click(screen.getByRole("button", { name: "Add Purchase cap" }));
  fireEvent.change(screen.getByLabelText("Maximum confirmed purchases per upgrade"), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "Save strategy" }));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ baseline: expect.objectContaining({
    workshop: expect.objectContaining({ blocks: expect.arrayContaining([expect.objectContaining({ type: "condition",
      then: [expect.objectContaining({ type: "pool", max_purchases: 8 })] })]) }),
  }) }), 0));
});

test("keeps draft after save failure and prevents assigning unsaved changes", async () => {
  api.save.mockRejectedValueOnce(new Error("Library changed. Reload before saving."));
  setup(); copy();
  fireEvent.click(screen.getByRole("button", { name: "Save strategy" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Library changed");
  expect(screen.getByRole("button", { name: "Assign" })).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Strategy" })).toHaveDisplayValue("Balanced turtle · draft");
});
