import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { DEFAULT_RULES, type AutomatedBlock, type LabBlock, type LabsReference, type RouteRules } from "@/lib/labs";
import { ResourceBlocks } from "./ResourceBlocks";
vi.mock("./routeCanvas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));

type Gems = BuildRouteDocument["baseline"]["gems"];
type Labs = BuildRouteDocument["baseline"]["labs"];
const AUTOMATED: AutomatedBlock[] = [{ lane: "gems", type: "unlock_lab_slot", slot: 2 },
  { lane: "labs", type: "research", lab_id: "labs.game-speed", slot: 1 }];
const catalog = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true },
  { id: "labs.attack-speed", name: "Attack Speed", max_level: null, priced: false }],
  game_speed: Array.from({ length: 7 }, (_, index) => ({ level: index + 1, coins: 1, seconds: 1, max_speed: 2 })),
  lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] } as LabsReference;
const stepsGems: Gems = { lab_slot2_reserve: 100, spend_limit_pct: 100, steps: ["unlock_lab_slot_2", "cards"] };
const stepsLabs: Labs = { slot1_research: "game_speed", steps: ["research_game_speed"] };
const blockLabs: Labs = { ...stepsLabs, mode: "blocks", blocks: [{ id: "slot1", type: "slot_track", slots: [1], children: [
  { id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 },
  { id: "as", type: "research", lab_id: "labs.attack-speed", to_level: 50 },
  { id: "w", type: "wait" }] }] };
const blockGems: Gems = { ...stepsGems, mode: "blocks", blocks: [
  { id: "g2", type: "unlock_lab_slot", slot: 2 },
  { id: "cm", type: "buy_cards", purpose: "card_missions" }] };
const tightRules: RouteRules = { ...DEFAULT_RULES, labs: { ...DEFAULT_RULES.labs,
  pool: { selection: "cheapest", max_price_pct_of_wallet: 10, max_seconds: 1800 } } };
const poolLabs: Labs = { ...stepsLabs, mode: "blocks", blocks: [
  { id: "slot1", type: "slot_track", slots: [1], children: [
    { id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
  { id: "slot2", type: "slot_track", slots: [2], children: [
    { id: "pool", type: "lab_pool", lab_ids: ["labs.attack-speed"], max_seconds: 3600 }] },
  { id: "slot3", type: "slot_track", slots: [3], children: [
    { id: "cond", type: "condition", field: "best_tier_1_wave", cmp: "gte", value: 30, then: [], else: [] }] }] };

function renderLane(kind: "gems" | "labs", props: Partial<Parameters<typeof ResourceBlocks>[0]> = {}) {
  const onGemsChange = vi.fn(), onLabsChange = vi.fn();
  const { unmount } = render(<ResourceBlocks kind={kind} gems={stepsGems} labs={blockLabs} onGemsChange={onGemsChange}
    onLabsChange={onLabsChange} automated={AUTOMATED} catalog={catalog} {...props} />);
  return { onGemsChange, onLabsChange, unmount };
}

test("a steps-mode lane offers Convert to blocks with the server's legacy ids", () => {
  const { onGemsChange } = renderLane("gems");
  fireEvent.click(screen.getByRole("button", { name: "Convert to blocks" }));
  expect(onGemsChange).toHaveBeenCalledWith({ ...stepsGems, mode: "blocks", blocks: [
    { id: "legacy.gems.unlock_lab_slot_2", type: "unlock_lab_slot", slot: 2 },
    { id: "legacy.gems.cards", type: "buy_cards", purpose: "card_missions" }] });
});

test("badges come from the server's automated set only", () => {
  renderLane("labs");
  expect(within(screen.getByTestId("resource-block-gs")).getByText("Automated")).toBeInTheDocument();
  expect(within(screen.getByTestId("resource-block-as")).getByText("Planned · not automated")).toBeInTheDocument();
});

test("with no automated set nothing claims to be automated", () => {
  renderLane("labs", { automated: [] });
  expect(screen.queryByText("Automated")).not.toBeInTheDocument();
});

test("adds a block to the selected track, edits it, moves and removes it", () => {
  const { onLabsChange } = renderLane("labs");
  fireEvent.click(screen.getByRole("button", { name: "Select Slots 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Add Wait" }));
  const added = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((added.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children.map(child => child.id)).toEqual(["gs", "as", "w", "wait.1"]);
  fireEvent.click(screen.getByRole("button", { name: "Select Attack Speed to 50" }));
  fireEvent.change(screen.getByLabelText("To level"), { target: { value: "30" } });
  const edited = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((edited.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children[1]).toMatchObject({ id: "as", to_level: 30 });
  // "as" sits directly after the pinned slot-1 Game Speed research: it must stay first, so its Up button is disabled
  // (spec §2 invariants - moving it above the pinned block would fail the save server-side).
  expect(screen.getByRole("button", { name: "Move Attack Speed to 50 up" })).toBeDisabled();
  // "w" is not adjacent to the pinned block (its predecessor "as" is not pinned), so it may move freely.
  fireEvent.click(screen.getByRole("button", { name: "Move Wait up" }));
  const moved = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((moved.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children.map(child => child.id)).toEqual(["gs", "w", "as"]);
  fireEvent.click(screen.getByRole("button", { name: "Remove Attack Speed to 50" }));
  const removed = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((removed.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children.map(child => child.id)).toEqual(["gs", "w"]);
  expect(screen.queryByRole("button", { name: "Remove Game Speed to 7" })).not.toBeInTheDocument();
});

test("a locked template shows blocks without edit controls", () => {
  renderLane("labs", { locked: true });
  expect(screen.getByTestId("resource-block-as")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Remove Attack Speed to 50" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Add Wait" })).toBeDisabled();
});

test("the pinned slot-1 research and its track lock their must-stay fields in the inspector", () => {
  renderLane("labs");
  fireEvent.click(screen.getByRole("button", { name: "Select Game Speed to 7" }));
  expect(screen.getByLabelText("Lab")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Select Slots 1" }));
  const checkboxes = screen.getAllByRole("checkbox");
  expect(checkboxes[0]).toBeDisabled();
  expect(checkboxes[0]).toBeChecked();
  expect(checkboxes[1]).not.toBeDisabled();
});

test("the pinned first gem block locks its slot in the inspector", () => {
  renderLane("gems", { gems: blockGems });
  fireEvent.click(screen.getByRole("button", { name: "Select Unlock lab slot 2" }));
  expect(screen.getByLabelText("Lab slot")).toBeDisabled();
});

test("research to_level clamps to the lab's max level and ignores an empty input", () => {
  const { onLabsChange } = renderLane("labs");
  fireEvent.click(screen.getByRole("button", { name: "Select Game Speed to 7" }));
  fireEvent.change(screen.getByLabelText("To level"), { target: { value: "999" } });
  const clamped = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((clamped.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children[0]).toMatchObject({ id: "gs", to_level: 7 });
  const callsBefore = onLabsChange.mock.calls.length;
  fireEvent.change(screen.getByLabelText("To level"), { target: { value: "" } });
  expect(onLabsChange.mock.calls.length).toBe(callsBefore);
});

test("changing a research block's lab resets its level to 1", () => {
  const { onLabsChange } = renderLane("labs");
  fireEvent.click(screen.getByRole("button", { name: "Select Attack Speed to 50" }));
  fireEvent.change(screen.getByLabelText("Lab"), { target: { value: "labs.game-speed" } });
  const changed = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((changed.blocks![0] as Extract<LabBlock, { type: "slot_track" }>).children[1]).toMatchObject({ id: "as", lab_id: "labs.game-speed", to_level: 1 });
});

test("a lab pool hints the strategy rule's cap and flags a block that is looser", () => {
  renderLane("labs", { labs: poolLabs, rules: tightRules });
  expect(screen.getByRole("alert")).toHaveTextContent("This pool's max duration is looser than the strategy rule.");
  fireEvent.click(screen.getByRole("button", { name: "Select Inherited pick of Attack Speed" }));
  expect(screen.getByLabelText(/Max duration \(seconds\)/)).toHaveAttribute("placeholder", "1800");
  expect(screen.getByLabelText(/Max duration \(seconds\)/)).toHaveAttribute("max", "1800");
});

test("changing a condition's fact resets its value to a valid default", () => {
  const { onLabsChange } = renderLane("labs", { labs: poolLabs, rules: tightRules });
  fireEvent.click(screen.getByRole("button", { name: "Select If best tier 1 wave gte 30" }));
  fireEvent.change(screen.getByLabelText("Fact"), { target: { value: "game_speed_maxed" } });
  const toMaxed = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((toMaxed.blocks![2] as Extract<LabBlock, { type: "slot_track" }>).children[0]).toMatchObject({ field: "game_speed_maxed", value: 1 });
  fireEvent.change(screen.getByLabelText("Fact"), { target: { value: "lab_level" } });
  const toLabLevel = onLabsChange.mock.calls.at(-1)![0] as Labs;
  expect((toLabLevel.blocks![2] as Extract<LabBlock, { type: "slot_track" }>).children[0]).toMatchObject({ field: "lab_level", value: 1, lab_id: "labs.game-speed" });
});

test("an until_cards block with no cards is flagged inline and in the inspector", () => {
  const emptyUntil: Gems = { ...stepsGems, mode: "blocks", blocks: [
    { id: "g2", type: "unlock_lab_slot", slot: 2 },
    { id: "empty", type: "buy_cards", purpose: "until_cards", cards: [] }] };
  renderLane("gems", { gems: emptyUntil, rules: DEFAULT_RULES });
  expect(screen.getAllByText("Pick at least one card.")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /Select Cards until/ }));
  expect(screen.getAllByText("Pick at least one card.")).toHaveLength(2);
});

test("an out-of-order gem slot is flagged inline only when rules are supplied", () => {
  const outOfOrder: Gems = { ...stepsGems, mode: "blocks", blocks: [
    { id: "g2", type: "unlock_lab_slot", slot: 2 },
    { id: "g5", type: "unlock_lab_slot", slot: 5 },
    { id: "g3", type: "unlock_lab_slot", slot: 3 }] };
  const { unmount } = renderLane("gems", { gems: outOfOrder, rules: DEFAULT_RULES });
  expect(screen.getByText("Lab slots must unlock in increasing order.")).toBeInTheDocument();
  unmount();
  renderLane("gems", { gems: outOfOrder });
  expect(screen.queryByText("Lab slots must unlock in increasing order.")).not.toBeInTheDocument();
});
