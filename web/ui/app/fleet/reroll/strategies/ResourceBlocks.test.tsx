import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { AutomatedBlock, LabBlock, LabsReference } from "@/lib/labs";
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

function renderLane(kind: "gems" | "labs", props: Partial<Parameters<typeof ResourceBlocks>[0]> = {}) {
  const onGemsChange = vi.fn(), onLabsChange = vi.fn();
  render(<ResourceBlocks kind={kind} gems={stepsGems} labs={blockLabs} onGemsChange={onGemsChange}
    onLabsChange={onLabsChange} automated={AUTOMATED} catalog={catalog} {...props} />);
  return { onGemsChange, onLabsChange };
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
