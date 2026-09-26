"use client";

import { useState } from "react";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { appendChild, collectIds, containerFor, findResourceBlock, isAutomated, laneProblems, legacyGemBlocks,
  legacyLabBlocks, mapBlocks, moveWithin, newResourceBlock, type AutomatedBlock, type LabBlock, type LabsReference,
  type ResourceBlock, type RouteRules } from "@/lib/labs";
import styles from "./routeCanvas.module.css";

type Gems = BuildRouteDocument["baseline"]["gems"];
type Labs = BuildRouteDocument["baseline"]["labs"];
type Kind = "gems" | "labs";

const GEM_OPTIONS = ["unlock_lab_slot_3", "unlock_lab_slot_4", "unlock_lab_slot_5", "card_slot", "cards"];
const LAB_OPTIONS = ["slot2_research"];
const LABELS: Record<string, string> = {
  unlock_lab_slot_2: "Unlock lab slot 2", unlock_lab_slot_3: "Unlock lab slot 3",
  unlock_lab_slot_4: "Unlock lab slot 4", unlock_lab_slot_5: "Unlock lab slot 5",
  card_slot: "Card slot", cards: "Cards", research_game_speed: "Game Speed research",
  slot2_research: "Slot 2 research",
};
const TYPE_LABELS: Record<ResourceBlock["type"], string> = { slot_track: "Slot track", research: "Research",
  lab_pool: "Lab pool", condition: "Condition", wait: "Wait", unlock_lab_slot: "Unlock lab slot",
  card_slots: "Card slots", buy_cards: "Buy cards", save_for: "Save for modules" };
const LAB_TYPES = ["slot_track", "research", "lab_pool", "condition", "wait"] as const;
const GEM_TYPES = ["unlock_lab_slot", "card_slots", "buy_cards", "save_for", "wait"] as const;

type Props = { kind: Kind; gems: Gems; labs: Labs; onGemsChange: (gems: Gems) => void; onLabsChange: (labs: Labs) => void;
  locked?: boolean; automated?: AutomatedBlock[]; catalog?: LabsReference | null;
  // Studio drives gems.spend_limit_pct through the Strategy rules panel (withRules); this steps-mode
  // control is only for standalone callers (FlowBuilder) that never touch RouteRules at all.
  hideGemSpendLimit?: boolean;
  // Only meaningful in blocks mode: the lab-pool inheritance hints and the laneProblems check
  // (fleet/resource_blocks.py's shape rules) need the live strategy rules, not just the catalog.
  rules?: RouteRules | null };

export function ResourceBlocks(props: Props): React.JSX.Element {
  const lane = props.kind === "gems" ? props.gems : props.labs;
  return lane.mode === "blocks" ? <BlocksEditor {...props} /> : <StepsEditor {...props} />;
}

function StepsEditor({ kind, gems, labs, onGemsChange, onLabsChange, locked = false, catalog = null, hideGemSpendLimit = false }: Props): React.JSX.Element {
  const [dragged, setDragged] = useState<string | null>(null);
  const isGems = kind === "gems";
  const steps = isGems ? gems.steps : labs.steps;
  const options = isGems ? GEM_OPTIONS : LAB_OPTIONS;
  const fixed = isGems ? "unlock_lab_slot_2" : "research_game_speed";
  const color = isGems ? styles.gem : styles.lab;

  function update(next: string[]): void {
    if (isGems) onGemsChange({ ...gems, steps: next });
    else onLabsChange({ ...labs, steps: next });
  }
  function add(step: string, before?: string): void {
    if (!options.includes(step) && step !== fixed) return;
    const targetIndex = before ? steps.indexOf(before) : -1;
    const next = steps.filter(value => value !== step);
    next.splice(targetIndex < 1 ? next.length : Math.min(targetIndex, next.length), 0, step);
    update(next);
  }
  function move(step: string, direction: -1 | 1): void {
    const next = [...steps];
    const from = next.indexOf(step);
    const to = from + direction;
    if (from < 1 || to < 1 || to >= next.length) return;
    [next[from], next[to]] = [next[to], next[from]];
    update(next);
  }
  function convert(): void {
    if (isGems) onGemsChange({ ...gems, mode: "blocks", blocks: legacyGemBlocks(gems.steps) });
    else if (catalog) onLabsChange({ ...labs, mode: "blocks", blocks: legacyLabBlocks(labs.steps, catalog.game_speed.length) });
  }

  return <section className={`${styles.canvas} space-y-4`} aria-label={`${isGems ? "Gem" : "Lab"} block canvas`}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-widest text-primary">{isGems ? "GEM ECONOMY" : "RESEARCH QUEUE"}</p>
        <h3 className="font-heading text-xl font-bold">{isGems ? "Spend the gems" : "Run the labs"}</h3>
        <p className="text-xs text-muted-foreground">Fleet-wide path. Drag blocks to arrange it; the first block is locked to protect current automation.</p></div>
      <span className="rounded-full border border-primary/40 bg-primary/10 px-3 py-1 text-xs text-primary">{isGems ? "100 gems reserved" : "Slot 1 dedicated"}</span>
    </div>
    {isGems && !hideGemSpendLimit && <label className="block max-w-xs text-xs font-semibold">Planned gem spend limit · % of observed balance
      <input type="number" aria-label="Gem spend limit" min={0} max={100} value={gems.spend_limit_pct}
        onChange={event => onGemsChange({ ...gems, spend_limit_pct: Number(event.target.value) })}
        className="mt-2 w-full rounded-lg border border-border bg-background px-3 py-2 [user-select:text]" />
      <span className="mt-1 block font-normal text-muted-foreground">Planning only. The bot still reserves and spends the first 100 gems on lab slot 2; this cap does not alter that unlock.</span>
    </label>}
    <div data-testid={`${isGems ? "gem" : "lab"}-path-drop`} aria-label={`${isGems ? "Gem" : "Lab"} path drop zone`}
      onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragged) add(dragged); setDragged(null); }}
      className={`${styles.drop} min-h-24`}>
      {steps.map((step, index) => <div key={step}>
        {index > 0 && <div className={styles.connector} aria-hidden="true" />}
        <div data-testid={`resource-step-${step}`} draggable={index > 0}
          onDragStart={event => { event.stopPropagation(); setDragged(step); }} onDragEnd={() => setDragged(null)}
          onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); if (dragged && dragged !== step) add(dragged, step); setDragged(null); }}
          className={`${styles.block} ${color} flex flex-wrap items-center justify-between gap-2 px-4 py-3`}>
          <div><span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{index === 0 ? "START · LOCKED" : `STEP ${index + 1}`}</span>
            <strong className="block text-sm">{LABELS[step] ?? step}</strong></div>
          <div className="flex items-center gap-1 text-xs"><span className="mr-2 text-muted-foreground">{index === 0 ? "Automated" : "Planned · not automated"}</span>
            {index > 0 && <><button type="button" aria-label={`Move ${LABELS[step]} up`} disabled={index === 1} onClick={() => move(step, -1)} className="rounded border border-border px-2 py-1 disabled:opacity-40">↑</button>
              <button type="button" aria-label={`Move ${LABELS[step]} down`} disabled={index === steps.length - 1} onClick={() => move(step, 1)} className="rounded border border-border px-2 py-1 disabled:opacity-40">↓</button>
              <button type="button" aria-label={`Remove ${LABELS[step]}`} onClick={() => update(steps.filter(value => value !== step))} className="rounded border border-border px-2 py-1">×</button></>}
          </div>
        </div>
      </div>)}
    </div>
    <div className="rounded-xl border border-border bg-card/90 p-4"><h4 className="text-sm font-semibold">Block palette</h4>
      <p className="mb-3 text-xs text-muted-foreground">Drag a block into the path or use Add. Planned steps are visual strategy only until automation supports them.</p>
      <div className="grid gap-2 sm:grid-cols-2">{options.filter(step => !steps.includes(step)).map(step => <div key={step} data-testid={`resource-palette-${step}`} draggable
        onDragStart={() => setDragged(step)} onDragEnd={() => setDragged(null)}
        className={`${styles.block} ${color} flex items-center justify-between gap-2 p-3 text-xs`}>
        <div><strong>{LABELS[step]}</strong><span className="block text-muted-foreground">Planned · not automated</span></div>
        <button type="button" aria-label={`Add ${LABELS[step]}`} onClick={() => add(step)} className="rounded border border-border px-2 py-1">+ Add</button>
      </div>)}</div>
    </div>
    <button type="button" onClick={convert} disabled={locked || (!isGems && !catalog)}
      className="rounded-md border border-primary/50 px-3 py-1 text-xs text-primary disabled:opacity-40">Convert to blocks</button>
  </section>;
}

function summary(block: ResourceBlock, labName: (id: string) => string): string {
  switch (block.type) {
    case "slot_track": return `Slots ${block.slots.join(", ")}`;
    case "research": return `${labName(block.lab_id)} to ${block.to_level}`;
    case "lab_pool": return `${block.selection ?? "Inherited"} pick of ${block.lab_ids.map(labName).join(", ")}`;
    case "condition": return `If ${block.field.replaceAll("_", " ")} ${block.cmp} ${block.value}`;
    case "unlock_lab_slot": return `Unlock lab slot ${block.slot}`;
    case "card_slots": return `Card slots up to ${block.up_to}${block.when_usable_card ? " · when a usable card waits" : ""}`;
    case "buy_cards": return block.purpose === "card_missions" ? "Cards for card-buy missions" : `Cards until ${(block.cards ?? []).join(", ")}`;
    case "save_for": return "Save gems for modules";
    default: return "Wait";
  }
}

function BlocksEditor({ kind, gems, labs, onGemsChange, onLabsChange, locked = false, automated = [], catalog = null,
  rules = null }: Props): React.JSX.Element {
  const blocks = ((kind === "gems" ? gems.blocks : labs.blocks) ?? []) as ResourceBlock[];
  const [selected, setSelected] = useState<string | null>(null);
  const block = selected ? findResourceBlock(blocks, selected) : null;
  const labName = (id: string): string => catalog?.labs.find(lab => lab.id === id)?.name ?? id;
  const name = (item: ResourceBlock): string => item.label ?? summary(item, labName);
  const color = kind === "gems" ? styles.gem : styles.lab;
  const usedSlots = new Set(blocks.flatMap(item => item.type === "slot_track" ? item.slots : []));
  const freeSlot = [1, 2, 3, 4, 5].find(slot => !usedSlots.has(slot));
  const lastLabSlot = Math.max(1, ...blocks.flatMap(item => item.type === "unlock_lab_slot" ? [item.slot] : []));
  const slot1Track = blocks.find(item => item.type === "slot_track" && item.slots.includes(1)) as Extract<LabBlock, { type: "slot_track" }> | undefined;
  // The first gem block and the slot-1 Game Speed research keep today's automation; they cannot move or go.
  const pinned = (item: ResourceBlock): boolean => kind === "gems" ? item.id === blocks[0]?.id
    : item.id === slot1Track?.id || item.id === slot1Track?.children[0]?.id;
  // Mirrors fleet/resource_blocks.py's shape checks so a doomed save is flagged here, not at save time.
  const problems = rules ? laneProblems(kind, blocks, rules) : {};
  const poolRule = rules?.labs.pool ?? null;

  function change(next: ResourceBlock[]): void {
    if (locked) return;
    if (kind === "gems") onGemsChange({ ...gems, mode: "blocks", blocks: next as Gems["blocks"] });
    else onLabsChange({ ...labs, mode: "blocks", blocks: next as LabBlock[] });
  }
  function add(type: ResourceBlock["type"]): void {
    const created = newResourceBlock(type, collectIds(blocks), { labId: catalog?.labs[0]?.id,
      slot: type === "slot_track" ? freeSlot : lastLabSlot + 1 });
    if (kind === "gems" || type === "slot_track") change([...blocks, created]);
    else {
      const container = containerFor(blocks, selected);
      if (!container) return;
      change(appendChild(blocks, container.id, container.branch, created));
    }
    setSelected(created.id);
  }
  const canAdd = (type: ResourceBlock["type"]): boolean => !locked && (kind === "gems"
    ? type !== "unlock_lab_slot" || lastLabSlot < 5
    : type === "slot_track" ? freeSlot !== undefined : containerFor(blocks, selected) !== null);

  function list(items: ResourceBlock[], slots: number[], depth: number): React.JSX.Element {
    return <ol className="space-y-2" style={{ marginLeft: depth ? 16 : 0 }}>{items.map((item, index) => {
      const itemSlots = item.type === "slot_track" ? item.slots : slots;
      const container = item.type === "slot_track" || item.type === "condition";
      const auto = isAutomated(kind, item, itemSlots, automated);
      // The block directly after a pinned block must stay first, else saves fail server-side (spec §2 invariants).
      const afterPinned = index > 0 && pinned(items[index - 1]);
      return <li key={item.id}>
        <div data-testid={`resource-block-${item.id}`} aria-current={selected === item.id}
          className={`${styles.block} ${color} flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs`}>
          <button type="button" aria-label={`Select ${name(item)}`} onClick={() => setSelected(item.id)} className="text-left">
            <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{TYPE_LABELS[item.type]}</span>
            <strong className="block text-sm">{name(item)}</strong></button>
          {!container && <span className="text-muted-foreground">{auto ? "Automated" : "Planned · not automated"}</span>}
          {!locked && !pinned(item) && <span className="flex gap-1">
            <button type="button" aria-label={`Move ${name(item)} up`} disabled={afterPinned} onClick={() => change(moveWithin(blocks, item.id, -1))} className="rounded border border-border px-2 py-1 disabled:opacity-40">↑</button>
            <button type="button" aria-label={`Move ${name(item)} down`} onClick={() => change(moveWithin(blocks, item.id, 1))} className="rounded border border-border px-2 py-1">↓</button>
            <button type="button" aria-label={`Remove ${name(item)}`} onClick={() => { change(mapBlocks(blocks, other => other.id === item.id ? null : other)); setSelected(null); }} className="rounded border border-border px-2 py-1">×</button>
          </span>}
        </div>
        {problems[item.id] && <p role="alert" className="ml-1 mt-1 text-[10px] text-danger">{problems[item.id]}</p>}
        {item.type === "slot_track" && list(item.children, item.slots, depth + 1)}
        {item.type === "condition" && <><p className="ml-4 mt-1 text-[10px] uppercase text-muted-foreground">Then</p>{list(item.then, slots, depth + 1)}
          <p className="ml-4 mt-1 text-[10px] uppercase text-muted-foreground">Else</p>{list(item.else, slots, depth + 1)}</>}
      </li>;
    })}</ol>;
  }

  return <section className={`${styles.canvas} space-y-4`} aria-label={`${kind === "gems" ? "Gem" : "Lab"} blocks`}>
    <p className="text-xs text-muted-foreground">{kind === "gems" ? "Evaluated top to bottom; the first unmet step is next." : "Each slot follows its track; the first unmet block wins."} Only blocks marked Automated run today.</p>
    {list(blocks, [], 0)}
    <div className="flex flex-wrap gap-2" aria-label="Add a block">{(kind === "gems" ? GEM_TYPES : LAB_TYPES).map(type =>
      <button key={type} type="button" aria-label={`Add ${TYPE_LABELS[type]}`} disabled={!canAdd(type)} onClick={() => add(type)}
        className="rounded border border-border px-2 py-1 text-xs disabled:opacity-40">+ {TYPE_LABELS[type]}</button>)}</div>
    {block && <BlockInspector block={block} locked={locked} catalog={catalog} pinned={pinned(block)} poolRule={poolRule}
      onChange={next => change(mapBlocks(blocks, other => other.id === next.id ? next : other))} />}
  </section>;
}

function BlockInspector({ block, locked, catalog, pinned, poolRule, onChange }: { block: ResourceBlock; locked: boolean;
  catalog: LabsReference | null; pinned: boolean; poolRule: RouteRules["labs"]["pool"] | null;
  onChange: (block: ResourceBlock) => void }): React.JSX.Element {
  const labs = catalog?.labs ?? [];
  const set = (patch: Record<string, unknown>): void => onChange({ ...block, ...patch } as ResourceBlock);
  const optional = (value: string): number | undefined => value === "" ? undefined : Number(value);
  return <div aria-label="Resource block settings" className="grid gap-2 rounded-xl border border-border bg-card p-3 text-xs sm:grid-cols-2">
    <label className="flex flex-col gap-1">Block name<input disabled={locked} maxLength={60} value={block.label ?? ""}
      onChange={event => { const label = event.target.value; if (label.trim()) set({ label }); else { const { label: _unused, ...rest } = block; onChange(rest as ResourceBlock); } }}
      className="rounded border border-border bg-background px-2 py-1" /></label>
    {block.type === "slot_track" && <fieldset className="flex gap-2"><legend>Slots</legend>{[1, 2, 3, 4, 5].map(slot =>
      <label key={slot} className="flex items-center gap-1"><input type="checkbox" disabled={locked || (pinned && slot === 1)} checked={block.slots.includes(slot)}
        onChange={event => set({ slots: event.target.checked ? [...block.slots, slot].sort() : block.slots.filter(value => value !== slot) })} />{slot}</label>)}</fieldset>}
    {(block.type === "research" || (block.type === "condition" && block.field === "lab_level")) &&
      <label className="flex flex-col gap-1">Lab<select disabled={locked || (block.type === "research" && pinned)} value={block.lab_id}
        onChange={event => set(block.type === "research" ? { lab_id: event.target.value, to_level: 1 } : { lab_id: event.target.value })}
        className="rounded border border-border bg-background px-2 py-1">{labs.map(lab => <option key={lab.id} value={lab.id}>{lab.name}</option>)}</select></label>}
    {block.type === "research" && (() => {
      const labMax = labs.find(lab => lab.id === block.lab_id)?.max_level ?? null;
      return <label className="flex flex-col gap-1">To level<input type="number" min={1} max={labMax ?? undefined} step={1} disabled={locked}
        value={block.to_level} onChange={event => {
          const raw = event.target.value;
          if (raw === "") return;
          const parsed = Number(raw);
          if (!Number.isFinite(parsed)) return;
          set({ to_level: Math.max(1, labMax !== null ? Math.min(Math.round(parsed), labMax) : Math.round(parsed)) });
        }} className="rounded border border-border bg-background px-2 py-1" /></label>;
    })()}
    {block.type === "lab_pool" && <>
      <fieldset className="flex flex-col gap-1"><legend>Labs in the pool</legend>{labs.map(lab => <label key={lab.id} className="flex items-center gap-1">
        <input type="checkbox" disabled={locked} checked={block.lab_ids.includes(lab.id)}
          onChange={event => set({ lab_ids: event.target.checked ? [...block.lab_ids, lab.id] : block.lab_ids.filter(id => id !== lab.id) })} />{lab.name}</label>)}</fieldset>
      <label className="flex flex-col gap-1">Selection<select disabled={locked} value={block.selection ?? ""} onChange={event => set({ selection: event.target.value || undefined })}
        className="rounded border border-border bg-background px-2 py-1"><option value="">Inherit strategy rule</option><option value="ordered">In order</option><option value="cheapest">Cheapest</option></select></label>
      <label className="flex flex-col gap-1">Max duration (seconds){poolRule?.max_seconds != null && <span className="text-muted-foreground"> · rule caps at {poolRule.max_seconds}</span>}
        <input type="number" min={60} max={poolRule?.max_seconds ?? undefined} placeholder={poolRule?.max_seconds != null ? String(poolRule.max_seconds) : undefined}
          disabled={locked} value={block.max_seconds ?? ""}
          onChange={event => set({ max_seconds: optional(event.target.value) })} className="rounded border border-border bg-background px-2 py-1" /></label>
      <label className="flex flex-col gap-1">Max price (% of wallet){poolRule?.max_price_pct_of_wallet != null && <span className="text-muted-foreground"> · rule caps at {poolRule.max_price_pct_of_wallet}</span>}
        <input type="number" min={1} max={poolRule?.max_price_pct_of_wallet ?? 100} placeholder={poolRule?.max_price_pct_of_wallet != null ? String(poolRule.max_price_pct_of_wallet) : undefined}
          disabled={locked} value={block.max_price_pct_of_wallet ?? ""}
          onChange={event => set({ max_price_pct_of_wallet: optional(event.target.value) })} className="rounded border border-border bg-background px-2 py-1" /></label>
      <p className="text-muted-foreground sm:col-span-2">Unset limits inherit the strategy's lab pool rule; a block may only be stricter.</p>
    </>}
    {block.type === "condition" && <>
      <label className="flex flex-col gap-1">Fact<select disabled={locked} value={block.field} onChange={event => {
          const field = event.target.value as typeof block.field;
          const value = field === "best_tier_1_wave" ? 30 : 1;
          set({ field, value, ...(field === "lab_level" ? { lab_id: labs[0]?.id } : { lab_id: undefined }) });
        }} className="rounded border border-border bg-background px-2 py-1"><option value="best_tier_1_wave">Highest Tier 1 wave</option><option value="game_speed_maxed">Game Speed maxed (1 = yes)</option><option value="lab_level">Lab level</option></select></label>
      <label className="flex flex-col gap-1">Comparison<select disabled={locked} value={block.cmp} onChange={event => set({ cmp: event.target.value })}
        className="rounded border border-border bg-background px-2 py-1"><option value="gte">at least</option><option value="lte">at most</option><option value="gt">more than</option><option value="lt">less than</option><option value="eq">equal to</option></select></label>
      {block.field === "game_speed_maxed"
        ? <label className="flex flex-col gap-1">Value<select disabled={locked} value={block.value} onChange={event => set({ value: Number(event.target.value) })}
            className="rounded border border-border bg-background px-2 py-1"><option value={0}>0 · not maxed</option><option value={1}>1 · maxed</option></select></label>
        : <label className="flex flex-col gap-1">Value<input type="number" min={0} disabled={locked} value={block.value} onChange={event => set({ value: Number(event.target.value) })}
            className="rounded border border-border bg-background px-2 py-1" /></label>}
    </>}
    {block.type === "unlock_lab_slot" && <label className="flex flex-col gap-1">Lab slot<select disabled={locked || pinned} value={block.slot} onChange={event => set({ slot: Number(event.target.value) })}
      className="rounded border border-border bg-background px-2 py-1">{[2, 3, 4, 5].map(slot => <option key={slot} value={slot}>{slot}</option>)}</select></label>}
    {block.type === "card_slots" && <>
      <label className="flex flex-col gap-1">Up to slot<input type="number" min={2} max={10} disabled={locked} value={block.up_to} onChange={event => set({ up_to: Number(event.target.value) })}
        className="rounded border border-border bg-background px-2 py-1" /></label>
      <label className="flex items-center gap-1"><input type="checkbox" disabled={locked} checked={block.when_usable_card} onChange={event => set({ when_usable_card: event.target.checked })} />Only when a usable card waits</label>
    </>}
    {block.type === "buy_cards" && <>
      <label className="flex flex-col gap-1">Purpose<select disabled={locked} value={block.purpose} onChange={event => set(event.target.value === "card_missions" ? { purpose: "card_missions", cards: undefined } : { purpose: "until_cards", cards: block.cards ?? [] })}
        className="rounded border border-border bg-background px-2 py-1"><option value="card_missions">Card-buy missions</option><option value="until_cards">Until I have these cards</option></select></label>
      {block.purpose === "until_cards" && <label className="flex flex-col gap-1">Cards (comma separated)<input disabled={locked} value={(block.cards ?? []).join(", ")}
        onChange={event => set({ cards: event.target.value.split(",").map(card => card.trim()).filter(Boolean) })} className="rounded border border-border bg-background px-2 py-1" /></label>}
      {block.purpose === "until_cards" && !(block.cards ?? []).length && <p role="alert" className="text-danger sm:col-span-2">Pick at least one card.</p>}
    </>}
  </div>;
}
