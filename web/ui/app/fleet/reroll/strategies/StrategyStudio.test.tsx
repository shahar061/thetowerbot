import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { StrategyLibrary } from "@/lib/strategyStudio";
import { StrategyStudio } from "./StrategyStudio";
import { StrategyCanvas } from "./StrategyCanvas";
import { StrategyBlockInspector } from "./StrategyBlockInspector";

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
const template = { id: "turtle", name: "Turtle", version: 1, source_template: "turtle" as const, builtin: true, baseline };
const library: StrategyLibrary = { revision: 0,
  templates: [template, { ...template, id: "opening", name: "Opening", source_template: "opening" }],
  strategies: [] };
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
    id: "custom-1", builtin: false, name: input.name, source_template: input.source_template, baseline: input.baseline }] }));
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

test("shows explicit handoff conditions between native phases", () => {
  render(<StrategyCanvas blocks={[
    { id: "opening.economy", type: "native", policy: "opening", phase: "economy" },
    { id: "opening.objectives", type: "native", policy: "opening", phase: "objectives" },
  ]} names={new Map()} selected={null} locked onSelect={vi.fn()} onTarget={vi.fn()} onDrop={vi.fn()}
  onMove={vi.fn()} onDrag={vi.fn()} target={{ parent: null, branch: "root", index: 0 }} />);
  expect(screen.getByText("When the utility allocation is reached → Upgrade objectives")).toBeInTheDocument();
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

test("creates and saves a non-spending scratch strategy", async () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "Create from scratch" }));
  expect(screen.getByRole("dialog", { name: "Create strategy from scratch" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Strategy name"), { target: { value: "My blank route" } });
  fireEvent.click(screen.getByRole("button", { name: "Create blank strategy" }));

  expect(screen.getByText("No purchases configured")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save strategy" }));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({
    name: "My blank route", source_template: "scratch",
    baseline: expect.objectContaining({
      workshop: expect.objectContaining({ mode: "blocks", blocks: [] }),
      battle: expect.objectContaining({ mode: "blocks", blocks: [], branches: [] }),
    }),
  }), 0));
  expect(api.assign).not.toHaveBeenCalled();
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

test("canvas renders nested budget and save-for containers with chips", () => {
  const blocks = [{ id: "econ", type: "budget" as const, metric: "utility_spent" as const, target: 350, ceiling: 400, blocks: [
    { id: "goal", type: "save_for" as const, goal: [{ id: "pool", type: "pool" as const, upgrade_ids: ["thorns"], selection: "priority" as const,
      targets: { thorns: 51 }, level_caps: { thorns: { base: 5 } } }] }] }];
  render(<StrategyCanvas blocks={blocks} names={new Map([["thorns", "Thorn Damage"]])} selected={null} locked
    target={{ parent: null, branch: "root", index: 0 }} onSelect={() => {}} onTarget={() => {}} onDrop={() => {}} onMove={() => {}} onDrag={() => {}} />);
  expect(screen.getByText("Budget · 350/400 utility coins")).toBeInTheDocument();
  expect(screen.getByText("Within budget")).toBeInTheDocument();
  expect(screen.getByText("Goal")).toBeInTheDocument();
  expect(screen.getByText("Thorn Damage → 51")).toBeInTheDocument();
  expect(screen.getByText("Thorn Damage ≤ 5")).toBeInTheDocument();
});

test("legacy native blocks are badged", () => {
  render(<StrategyCanvas blocks={[{ id: "n", type: "native", policy: "turtle", phase: "battle" }]} names={new Map()} selected={null} locked
    target={{ parent: null, branch: "root", index: 0 }} onSelect={() => {}} onTarget={() => {}} onDrop={() => {}} onMove={() => {}} onDrag={() => {}} />);
  expect(screen.getByText(/LEGACY BUILT-IN/)).toBeInTheDocument();
});

test("inspector edits pool target and links to the guide", () => {
  const onChange = vi.fn();
  render(<StrategyBlockInspector block={{ id: "p", type: "pool", upgrade_ids: ["thorns"], selection: "priority" }} lane="workshop"
    catalog={catalog} locked={false} onChange={onChange} onRemove={() => {}} onCopy={() => {}} />);
  expect(screen.getByRole("link", { name: /learn more/i })).toHaveAttribute("href", "/fleet/reroll/strategies/guide/#block-pool");
  fireEvent.click(screen.getByRole("button", { name: "Add target for Thorn Damage" }));
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ targets: { thorns: 0 } }));
});

test("studio header links to the guide", () => {
  render(<StrategyStudio library={library} saved={route} catalog={catalog} members={members} onPublished={() => {}} />);
  expect(screen.getByRole("link", { name: /how it works/i })).toHaveAttribute("href", "/fleet/reroll/strategies/guide/");
});

test("inspector Block name field edits and clears the block label", () => {
  const onChange = vi.fn();
  render(<StrategyBlockInspector block={{ id: "p", type: "pool", upgrade_ids: ["thorns"], selection: "priority" }} lane="workshop"
    catalog={catalog} locked={false} onChange={onChange} onRemove={() => {}} onCopy={() => {}} />);
  fireEvent.change(screen.getByLabelText("Block name"), { target: { value: "Cheap stuff" } });
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ label: "Cheap stuff" }));
  onChange.mockClear();
  fireEvent.change(screen.getByLabelText("Block name"), { target: { value: "   " } });
  const [[cleared]] = onChange.mock.calls;
  expect(cleared).not.toHaveProperty("label");
});
