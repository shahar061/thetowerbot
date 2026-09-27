import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabsReference, LabsRow, SlotNow } from "@/lib/labs";
import { LabSlotPlanner } from "./LabSlotPlanner";

type Labs = BuildRouteDocument["baseline"]["labs"];
afterEach(() => { vi.restoreAllMocks(); });
const reference: LabsReference = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true },
  { id: "labs.attack-speed", name: "Attack Speed", max_level: 99, priced: false }],
  game_speed: [{ level: 1, coins: 100, seconds: 60, max_speed: 2 }], lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] };
const labs: Labs = { slot1_research: "game_speed", steps: [], mode: "blocks", blocks: [
  { id: "slot1", type: "slot_track", slots: [1], children: [{ id: "gs", type: "research", lab_id: "labs.game-speed", to_level: 7 }] },
  { id: "slot2", type: "slot_track", slots: [2], children: [
    { id: "a", type: "research", lab_id: "labs.attack-speed", to_level: 10 },
    { id: "b", type: "research", lab_id: "labs.game-speed", to_level: 5 }] },
] };
function Editor({ initial = labs, catalog = reference }: { initial?: Labs; catalog?: LabsReference | null }): React.JSX.Element {
  const [value, setValue] = useState(initial);
  return <LabSlotPlanner labs={value} reference={catalog} automated={[]} observed={null} locked={false} onChange={setValue} />;
}
test("renders five slots, unknown observations and honest estimates", () => {
  render(<Editor />);
  for (let slot = 1; slot <= 5; slot++) expect(screen.getByRole("region", { name: `Lab ${slot}` })).toBeInTheDocument();
  expect(screen.getAllByText(/Current job unknown/)).toHaveLength(5);
  expect(screen.getByText(/Repeat each research/)).toBeInTheDocument();
  expect(screen.getAllByText(/Cost unknown/).length).toBeGreaterThan(0);
});
test("edits ordered targets and retains keyboard focus after moving", () => {
  render(<Editor />);
  const up = screen.getByRole("button", { name: "Move Lab 2 research 2 up" });
  up.focus(); fireEvent.click(up);
  expect(screen.getByLabelText("Lab 2 research 1")).toHaveValue("labs.game-speed");
  expect(screen.getByRole("button", { name: "Move Lab 2 research 1 up" })).toHaveFocus();
  fireEvent.change(screen.getByLabelText("Lab 2 target level 1"), { target: { value: "999" } });
  expect(screen.getByLabelText("Lab 2 target level 1")).toHaveValue(7);
});
test("protects Game Speed first and allows later slot-one queue targets", () => {
  render(<Editor />);
  expect(screen.getByLabelText("Lab 1 research 1")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove Lab 1 research 1" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Add research to Lab 1" }));
  expect(screen.getByLabelText("Lab 1 research 2")).toBeInTheDocument();
});
test("missing catalog retains unavailable selections and disables additions", () => {
  render(<Editor catalog={null} />);
  expect(screen.getByLabelText("Lab 2 research 1")).toHaveDisplayValue("labs.attack-speed · unavailable");
  expect(screen.getByRole("button", { name: "Add research to Lab 2" })).toBeDisabled();
});
test("advanced conditions are not flattened by simple edits", () => {
  const change = vi.fn();
  render(<LabSlotPlanner labs={{ ...labs, blocks: [...labs.blocks!, { id: "complex", type: "slot_track", slots: [3, 4, 5], children: [
    { id: "if", type: "condition", field: "game_speed_maxed", cmp: "eq", value: 1, then: [], else: [] }] }] }}
    reference={reference} automated={[]} observed={null} locked={false} onChange={change} />);
  const three = within(screen.getByRole("region", { name: "Lab 3" }));
  expect(three.getByText(/Advanced program/)).toBeInTheDocument();
  expect(three.queryByRole("button", { name: "Add research to Lab 3" })).not.toBeInTheDocument();
  fireEvent.click(three.getByRole("button", { name: "Edit Lab 3 separately" }));
  expect(change.mock.calls[0][0].blocks.at(-1).children[0].type).toBe("condition");
});
test("edits pause and blocked policy without deleting the queue", () => {
  render(<Editor />);
  fireEvent.click(screen.getByLabelText("Pause Lab 2"));
  expect(screen.getByLabelText("Pause Lab 2")).toBeChecked();
  fireEvent.change(screen.getByLabelText("When Lab 2 is blocked"), { target: { value: "skip" } });
  expect(screen.getByLabelText("When Lab 2 is blocked")).toHaveValue("skip");
  expect(screen.getByLabelText("Lab 2 research 1")).toHaveValue("labs.attack-speed");
});
test("protected template controls never mutate a queue", () => {
  const change = vi.fn();
  render(<LabSlotPlanner labs={labs} reference={reference} automated={[]} observed={null} locked onChange={change} />);
  expect(screen.getByLabelText("Lab 2 research 1")).toBeDisabled();
  expect(screen.getByLabelText("Pause Lab 2")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Move Lab 2 research 2 up" }));
  expect(change).not.toHaveBeenCalled();
});
test("editing a detached shared slot leaves the other two queues unchanged", () => {
  const initial: Labs = { ...labs, blocks: [...labs.blocks!, { id: "shared", type: "slot_track", slots: [3, 4, 5], children: [
    { id: "shared-research", type: "research", lab_id: "labs.attack-speed", to_level: 10 }] }] };
  render(<Editor initial={initial} />);
  expect(screen.getByLabelText("Lab 3 target level 1")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Edit Lab 3 separately" }));
  fireEvent.change(screen.getByLabelText("Lab 3 target level 1"), { target: { value: "15" } });
  expect(screen.getByLabelText("Lab 3 target level 1")).toHaveValue(15);
  expect(screen.getByLabelText("Lab 4 target level 1")).toHaveValue(10);
  expect(screen.getByLabelText("Lab 5 target level 1")).toHaveValue(10);
});

function observation(now: Partial<SlotNow> = {}, slot = 1): LabsRow {
  return { worker: "Air_1", account_id: "a", strategy_name: null, read_at: 1_000, state: "ok", reason: null,
    recent: [], freshness: "observed", wallet: { coins: null, gems: null }, plan: { wallet_coins: null, jar: 0,
      gems: { wallet: null, next: null, price: null, have: null, need: null, automated: false, why: [], steps: [] },
      slots: [{ slot, next: null, covered: null, automated: false, why: [], note: null, now: {
        state: "researching", research_name: "Game Speed", level: 2, completes_at: 1_600, overdue_seconds: null,
        read_at: 1_000, stale: false, evidence_status: "current", ...now } }] } };
}
function observe(row: LabsRow, catalog: LabsReference | null = reference): void {
  render(<LabSlotPlanner labs={labs} reference={catalog} automated={[]} observed={row} locked={false} onChange={vi.fn()} />);
}
test("current research shows expected completion and remaining time without claiming completion", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation());
  expect(screen.getByText(/Expected completion:.*10m remaining/)).toBeInTheDocument();
});
test("an elapsed deadline requests verification and never announces a finished job", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ completes_at: 900, overdue_seconds: 100 }));
  expect(screen.getByText(/Due for verification.*Completion not confirmed/)).toBeInTheDocument();
  expect(screen.queryByText(/\d+[mh] remaining/)).not.toBeInTheDocument();
});
test("historical research reports the old deadline without a live countdown", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ stale: true, evidence_status: "historical" }));
  expect(screen.getByText(/Last expected completion:.*Remaining time unknown/)).toBeInTheDocument();
  expect(screen.queryByText(/10m remaining/)).not.toBeInTheDocument();
});
test("unknown research deadlines stay unknown", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ completes_at: null }));
  expect(screen.getByText(/Expected completion unknown.*remaining time unknown/)).toBeInTheDocument();
});
test("locked slots show catalog unlock cost and prior-slot requirement", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ state: "locked", completes_at: null }, 3), { ...reference, lab_slots: [{ slot: 3, gems: 400 }] });
  expect(within(screen.getByRole("region", { name: "Lab 3" })).getByText(/Unlock requirement: 400 gems.*Lab 2 owned/)).toBeInTheDocument();
});
test("locked first lab shows unlock wave while unavailable requirements remain unknown", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ state: "locked", completes_at: null }), reference);
  expect(screen.getByText(/Unlock requirement: Tier 1 Wave 30/)).toBeInTheDocument();
});
test("historical locked slot cannot assert current ownership and missing catalog stays unknown", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  observe(observation({ state: "locked", completes_at: null, stale: true, evidence_status: "historical" }, 3), null);
  expect(screen.getByText(/If still locked: unlock requirement unknown/)).toBeInTheDocument();
});
