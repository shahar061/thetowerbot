import type { BuildRouteDocument } from "./buildRoute";

type Base = { id: string; label?: string };
export type Comparison = "gte" | "lte" | "gt" | "lt" | "eq";
export type LabBlock =
  | (Base & { type: "slot_track"; slots: number[]; children: LabBlock[] })
  | (Base & { type: "research"; lab_id: string; to_level: number })
  | (Base & { type: "lab_pool"; lab_ids: string[]; selection?: "ordered" | "cheapest"; max_seconds?: number;
      max_price_pct_of_wallet?: number; caps?: Record<string, number> })
  | (Base & { type: "condition"; field: "best_tier_1_wave" | "game_speed_maxed" | "lab_level"; cmp: Comparison;
      value: number; lab_id?: string; then: LabBlock[]; else: LabBlock[] })
  | (Base & { type: "wait" });
export type GemBlock =
  | (Base & { type: "unlock_lab_slot"; slot: number })
  | (Base & { type: "card_slots"; up_to: number; when_usable_card: boolean })
  | (Base & { type: "buy_cards"; purpose: "card_missions" | "until_cards"; cards?: string[] })
  | (Base & { type: "save_for"; target: "modules" })
  | (Base & { type: "wait" });
export type ResourceBlock = LabBlock | GemBlock;

export type LabShareMode = "when_affordable" | "save_pct" | "labs_first";
export type RouteRules = {
  coins: { lab_share: { mode: LabShareMode; pct: number }; workshop_spend_limit_pct: number };
  labs: { auto_start: boolean; idle_fill: "leave_idle" | "shortest_under_30m";
    pool: { selection: "cheapest" | "ordered" | "shortest"; max_price_pct_of_wallet: number | null; max_seconds: number | null } };
  gems: { auto_unlock_lab_slots: boolean; spend_limit_pct: number; keep: number };
};
/** Mirrors RouteRules() in fleet/build_route.py: today's behavior. */
export const DEFAULT_RULES: RouteRules = {
  coins: { lab_share: { mode: "when_affordable", pct: 25 }, workshop_spend_limit_pct: 100 },
  labs: { auto_start: true, idle_fill: "leave_idle", pool: { selection: "ordered", max_price_pct_of_wallet: null, max_seconds: null } },
  gems: { auto_unlock_lab_slots: true, spend_limit_pct: 100, keep: 0 },
};
/** Mirrors fleet/resource_blocks.py LEGACY_SLOT2_POOL. */
export const LEGACY_SLOT2_POOL = ["labs.coins-wave", "labs.cash-bonus", "labs.coins-kill-bonus"] as const;

export type AutomatedBlock = { lane: "labs" | "gems"; type: string; lab_id?: string; slot: number };
export type SlotNow = { state: "researching" | "idle" | "locked" | "owned_unread" | "unknown"; level: number | null;
  completes_at: number | null; overdue_seconds: number | null; read_at: number | null; stale: boolean };
export type SlotNext = { lab_id: string; name: string; level: number | null; price: number | null; seconds: number | null };
export type SlotPlan = { slot: number; now: SlotNow; next: SlotNext | null; covered: boolean | null; automated: boolean;
  why: string[]; note: string | null };
export type GemStep = { block_id: string; type: GemBlock["type"]; label: string; state: "done" | "current" | "next";
  price: number | null; automated: boolean };
export type GemPlan = { wallet: number | null; next: GemStep | null; price: number | null; have: number | null;
  need: number | null; automated: boolean; why: string[]; steps: GemStep[] };
export type LabPlan = { wallet_coins: number | null; jar: number; slots: SlotPlan[]; gems: GemPlan };
export type LabsActivity = { at: number; kind: "LAB" | "CARD_BUY"; item: string | null; category: string | null;
  currency: string | null; amount: number | null; reason: string | null };
export type LabsRow = { worker: string; account_id: string | null; strategy_name: string | null; read_at: number | null;
  wallet: { coins: number | null; gems: number | null }; plan: LabPlan | null; state: "ok" | "unknown";
  reason: string | null; recent: LabsActivity[] };
export type LabsReference = { labs: { id: string; name: string; max_level: number | null; priced: boolean }[];
  game_speed: { level: number; coins: number; seconds: number; max_speed: number }[];
  lab_slots: { slot: number; gems: number }[]; card_slots: { slot: number; gems: number }[];
  card_gems: number; labs_unlock_wave: number; sources: { url: string; checked: string }[] };
export type LabsSnapshot = { workers: LabsRow[]; automated: AutomatedBlock[]; reference: LabsReference };

type Baseline = BuildRouteDocument["baseline"];

export function rulesOf(baseline: Baseline): RouteRules {
  if (baseline.rules) return baseline.rules;
  return { ...DEFAULT_RULES,
    coins: { ...DEFAULT_RULES.coins, workshop_spend_limit_pct: baseline.workshop.coin_spend_limit_pct },
    gems: { ...DEFAULT_RULES.gems, spend_limit_pct: baseline.gems.spend_limit_pct } };
}

/** Rules own the moved limits; the old fields are written too for one release. */
export function withRules(baseline: Baseline, rules: RouteRules): Baseline {
  return { ...baseline, rules,
    workshop: { ...baseline.workshop, coin_spend_limit_pct: rules.coins.workshop_spend_limit_pct },
    gems: { ...baseline.gems, spend_limit_pct: rules.gems.spend_limit_pct } };
}

/** Amber only when the slot is idle and its next lab is automated and covered. */
export function slotTone(slot: SlotPlan): "ready" | "neutral" {
  return slot.now.state === "idle" && slot.next !== null && slot.automated && slot.covered === true ? "ready" : "neutral";
}

export function isAutomated(lane: "labs" | "gems", block: ResourceBlock, slots: number[], automated: AutomatedBlock[]): boolean {
  return automated.some(entry => entry.lane === lane && entry.type === block.type && (lane === "gems"
    ? "slot" in block && entry.slot === block.slot
    : "lab_id" in block && entry.lab_id === block.lab_id && slots.includes(entry.slot)));
}

export function duration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const days = Math.floor(total / 86400), hours = Math.floor((total % 86400) / 3600), minutes = Math.floor((total % 3600) / 60);
  if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
  if (hours) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  return `${Math.max(1, minutes)}m`;
}

export function nowText(now: SlotNow, at: number): string {
  const stale = now.stale ? " · stale read" : "";
  const level = now.level !== null ? ` L${now.level}` : "";
  if (now.state === "researching") {
    if (now.completes_at === null) return `Researching${level}${stale}`;
    return now.completes_at <= at ? `Should have finished ~${duration(at - now.completes_at)} ago${stale}`
      : `Researching${level} · ${duration(now.completes_at - at)} left${stale}`;
  }
  const words = { idle: "Idle", locked: "Locked", owned_unread: "Owned · research not read", unknown: "Unknown" } as const;
  return `${words[now.state]}${stale}`;
}

export function legacyGemBlocks(steps: string[]): GemBlock[] {
  const slots = steps.filter(step => step.startsWith("unlock_lab_slot_")).map(step => Number(step.split("_").pop())).sort((a, b) => a - b);
  let next = 0;
  return steps.flatMap<GemBlock>(step => {
    const id = `legacy.gems.${step}`;
    if (step.startsWith("unlock_lab_slot_")) return [{ id, type: "unlock_lab_slot", slot: slots[next++] }];
    if (step === "card_slot") return [{ id, type: "card_slots", up_to: 2, when_usable_card: false }];
    if (step === "cards") return [{ id, type: "buy_cards", purpose: "card_missions" }];
    return [];
  });
}

export function legacyLabBlocks(steps: string[], gameSpeedMax: number): LabBlock[] {
  const tracks: LabBlock[] = [{ id: "legacy.labs.slot1", type: "slot_track", slots: [1], children: [
    { id: "legacy.labs.game_speed", type: "research", lab_id: "labs.game-speed", to_level: gameSpeedMax }] }];
  if (steps.includes("slot2_research")) tracks.push({ id: "legacy.labs.slot2", type: "slot_track", slots: [2], children: [
    { id: "legacy.labs.slot2.pool", type: "lab_pool", lab_ids: [...LEGACY_SLOT2_POOL], selection: "cheapest" }] });
  return tracks;
}

export type Branch = "children" | "then" | "else";
export type Container = { id: string; branch: Branch };

function kids(block: ResourceBlock): [Branch, ResourceBlock[]][] {
  if (block.type === "slot_track") return [["children", block.children]];
  if (block.type === "condition") return [["then", block.then], ["else", block.else]];
  return [];
}

function rebuild(block: ResourceBlock, lists: (list: ResourceBlock[], branch: Branch) => ResourceBlock[]): ResourceBlock {
  if (block.type === "slot_track") return { ...block, children: lists(block.children, "children") as LabBlock[] };
  if (block.type === "condition") return { ...block, then: lists(block.then, "then") as LabBlock[], else: lists(block.else, "else") as LabBlock[] };
  return block;
}

export function mapBlocks(blocks: ResourceBlock[], fn: (block: ResourceBlock) => ResourceBlock | null): ResourceBlock[] {
  return blocks.flatMap(block => { const mapped = fn(block); return mapped ? [rebuild(mapped, list => mapBlocks(list, fn))] : []; });
}

export function findResourceBlock(blocks: ResourceBlock[], id: string): ResourceBlock | null {
  for (const block of blocks) {
    if (block.id === id) return block;
    for (const [, list] of kids(block)) { const found = findResourceBlock(list, id); if (found) return found; }
  }
  return null;
}

export function collectIds(blocks: ResourceBlock[], ids = new Set<string>()): Set<string> {
  for (const block of blocks) { ids.add(block.id); for (const [, list] of kids(block)) collectIds(list, ids); }
  return ids;
}

export function moveWithin(blocks: ResourceBlock[], id: string, direction: -1 | 1): ResourceBlock[] {
  const index = blocks.findIndex(block => block.id === id);
  if (index >= 0) {
    const to = index + direction;
    if (to < 0 || to >= blocks.length) return blocks;
    const next = [...blocks];
    [next[index], next[to]] = [next[to], next[index]];
    return next;
  }
  return blocks.map(block => rebuild(block, list => moveWithin(list, id, direction)));
}

export function appendChild(blocks: ResourceBlock[], parentId: string, branch: Branch, child: ResourceBlock): ResourceBlock[] {
  return blocks.map(block => block.id === parentId
    ? rebuild(block, (list, own) => own === branch ? [...list, child] : list)
    : rebuild(block, list => appendChild(list, parentId, branch, child)));
}

function parentOf(blocks: ResourceBlock[], id: string, owner: Container | null): Container | null | undefined {
  for (const block of blocks) {
    if (block.id === id) return owner;
    for (const [branch, list] of kids(block)) {
      const found = parentOf(list, id, { id: block.id, branch });
      if (found !== undefined) return found;
    }
  }
  return undefined;
}

/** Where "Add" puts a child: into the selected track/condition, else beside the selected block. */
export function containerFor(blocks: ResourceBlock[], selected: string | null): Container | null {
  if (!selected) return null;
  const block = findResourceBlock(blocks, selected);
  if (block?.type === "slot_track") return { id: block.id, branch: "children" };
  if (block?.type === "condition") return { id: block.id, branch: "then" };
  return parentOf(blocks, selected, null) ?? null;
}

export function newResourceBlock(type: ResourceBlock["type"], taken: Set<string>,
  options: { labId?: string; slot?: number } = {}): ResourceBlock {
  let number = 1;
  while (taken.has(`${type}.${number}`)) number += 1;
  const id = `${type}.${number}`;
  const labId = options.labId ?? "labs.game-speed";
  switch (type) {
    case "slot_track": return { id, type, slots: [options.slot ?? 5], children: [] };
    case "research": return { id, type, lab_id: labId, to_level: 1 };
    case "lab_pool": return { id, type, lab_ids: [labId] };
    case "condition": return { id, type, field: "best_tier_1_wave", cmp: "gte", value: 30, then: [], else: [] };
    case "unlock_lab_slot": return { id, type, slot: options.slot ?? 3 };
    case "card_slots": return { id, type, up_to: 10, when_usable_card: true };
    case "buy_cards": return { id, type, purpose: "card_missions" };
    case "save_for": return { id, type, target: "modules" };
    default: return { id, type: "wait" };
  }
}

/** Mirrors fleet/coin_share.py for one visit: jar vs what Workshop may spend.
 *
 * Pauses exactly when the worker's own wait_coins path would: labs_first,
 * auto_start on, a known price, and the wallet short of it. With auto_start
 * off nothing is held back for labs - the jar stays at 0 and Workshop sees
 * the full ceiling, matching the worker (which never runs coin_share at all
 * once auto_start is off). */
export function splitPreview(rules: RouteRules, wallet: number | null, jar: number, price: number | null):
  { jar: number; workshop: number; price: number | null; progress: number | null; paused: boolean } | null {
  if (wallet === null) return null;
  const ceiling = (spendable: number) => Math.floor(Math.max(0, spendable) * rules.coins.workshop_spend_limit_pct / 100);
  if (!rules.labs.auto_start) {
    return { jar: 0, workshop: ceiling(wallet), price, progress: price ? 0 : null, paused: false };
  }
  const mode = rules.coins.lab_share.mode;
  const held = mode === "save_pct" && price !== null ? Math.min(price, Math.max(0, jar)) : 0;
  const paused = mode === "labs_first" && price !== null && wallet < price;
  const workshop = paused ? 0 : ceiling(wallet - held);
  return { jar: held, workshop, price, progress: price ? Math.min(1, held / price) : null, paused };
}
