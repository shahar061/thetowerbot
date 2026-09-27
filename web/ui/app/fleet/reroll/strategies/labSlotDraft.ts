import type { BuildRouteDocument } from "@/lib/buildRoute";
import { collectIds, legacyLabBlocks, mapBlocks, newResourceBlock, type LabBlock, type LabsReference } from "@/lib/labs";

type Labs = BuildRouteDocument["baseline"]["labs"];
export type SlotTrack = Extract<LabBlock, { type: "slot_track" }>;

export function labSlotBlocks(labs: Labs, reference: LabsReference | null): LabBlock[] | null {
  if (labs.mode === "blocks") return labs.blocks ?? [];
  const maximum = reference?.labs.find(lab => lab.id === "labs.game-speed")?.max_level;
  if (!maximum) return null;
  return legacyLabBlocks(labs.steps, maximum);
}

export function trackForSlot(blocks: LabBlock[], slot: number): SlotTrack | null {
  const owners = blocks.filter((block): block is SlotTrack => block.type === "slot_track" && block.slots.includes(slot));
  return owners.length === 1 ? owners[0] : null;
}

/** Explicitly detach, preserving every branch and giving every cloned node a fresh ID. */
export function detachLabSlot(labs: Labs, slot: number, reference: LabsReference | null): Labs {
  const blocks = labSlotBlocks(labs, reference);
  if (!blocks) return labs;
  const track = trackForSlot(blocks, slot);
  if (!track || track.slots.length < 2) return labs;
  const ids = collectIds(blocks);
  const clone = mapBlocks([structuredClone(track)], block => {
    const id = newResourceBlock(block.type, ids).id;
    ids.add(id);
    return { ...block, id };
  })[0] as SlotTrack;
  const policy = track.slot_policies?.[String(slot)];
  const detached: SlotTrack = { ...clone, slots: [slot], slot_policies: policy ? { [slot]: { ...policy } } : {} };
  const remaining: SlotTrack = { ...track, slots: track.slots.filter(value => value !== slot),
    slot_policies: Object.fromEntries(Object.entries(track.slot_policies ?? {}).filter(([key]) => key !== String(slot))) };
  return { ...labs, mode: "blocks", blocks: [...blocks.map(block => block.id === track.id ? remaining : block), detached] };
}

/** Shared owners require detachment; edits never implicitly replace an advanced program. */
export function editLabSlot(labs: Labs, slot: number, reference: LabsReference | null,
  update: (track: SlotTrack, ids: Set<string>) => SlotTrack): Labs {
  const blocks = labSlotBlocks(labs, reference);
  if (!blocks || slot < 1 || slot > 5) return labs;
  const owners = blocks.filter(block => block.type === "slot_track" && block.slots.includes(slot));
  if (owners.length > 1) return labs;
  const old = trackForSlot(blocks, slot);
  if (old && old.slots.length > 1) return labs;
  const ids = collectIds(blocks);
  const track = old ?? newResourceBlock("slot_track", ids, { slot }) as SlotTrack;
  ids.add(track.id);
  const next = update(track, ids);
  if (slot === 1 && (next.children[0]?.type !== "research" || next.children[0].lab_id !== "labs.game-speed"
      || (old && JSON.stringify(next.children[0]) !== JSON.stringify(old.children[0])))) return labs;
  return { ...labs, mode: "blocks", blocks: old
    ? blocks.map(block => block.id === old.id ? next : block) : [...blocks, next] };
}
