"use client";

import { useState } from "react";
import { LockKeyhole, Trash2 } from "lucide-react";
import type { StrategyBlock, ProgramLane } from "@/lib/strategyStudio";
import type { Upgrade } from "@/lib/types";
import { blockDetail, blockTitle } from "./strategyBlocks";
import styles from "./studio.module.css";

type Pool = Extract<StrategyBlock, { type: "pool" }>;

function WeightPreview({ block, names }: { block: Pool; names: Map<string, string> }): React.JSX.Element {
  const [buys, setBuys] = useState<Record<string, number>>({});
  const weights = block.upgrade_ids.map(id => ({ id, value: Math.max(block.weight_floor ?? 1,
    (block.weights?.[id] ?? 1) * Math.pow(1 - (block.decay_pct ?? 0) / 100, buys[id] ?? 0)) }));
  const total = weights.reduce((sum, item) => sum + item.value, 0);
  return <div className={styles.weightPreview}>
    <p className={styles.eyebrow}>Sample weights → chance</p>
    {weights.map(item => <div key={item.id} className={styles.weightRow}>
      <div><span>{names.get(item.id) ?? item.id}</span><b>{item.value.toFixed(1)} · {total ? Math.round(item.value / total * 100) : 0}%</b></div>
      <div className={styles.weightTrack}><span style={{ width: `${total ? item.value / total * 100 : 0}%` }} /></div>
      <button type="button" onClick={() => setBuys({ ...buys, [item.id]: (buys[item.id] ?? 0) + 1 })}>Simulate a {names.get(item.id) ?? item.id} buy</button>
    </div>)}
    <p className={styles.hint}>Illustration only. Live weights change after confirmed purchases, using the count scope below.</p>
  </div>;
}

export function StrategyBlockInspector({ block, lane, catalog, locked, onChange, onRemove, onCopy }: {
  block?: StrategyBlock; lane: ProgramLane; catalog: Upgrade[]; locked: boolean;
  onChange: (block: StrategyBlock) => void; onRemove: () => void; onCopy: () => void;
}): React.JSX.Element {
  const names = new Map(catalog.map(item => [item.id, item.name]));
  const available = catalog.filter(item => lane === "workshop" || !item.unlock);
  return <aside className={styles.inspector} aria-label="Block settings">
    <p className={styles.eyebrow}>Block settings</p>
    {!block ? <p className={styles.hint}>Select a block to inspect its behavior.</p> : <>
      <h3>{blockTitle(block, names)}</h3><p className={styles.hint}>{blockDetail(block)}</p>
      {locked && <div className={styles.lockNotice}><LockKeyhole size={15} />Protected template. Create a copy to edit.</div>}
      <fieldset disabled={locked} className={styles.fields}>
        {block.type === "native" && <>
          <div className={styles.lockNotice}><strong>Phase handoff</strong><p>{block.phase === "starter" && block.policy === "turtle"
            ? "Turtle skips Survival Starter and advances to the next configured phase."
            : block.phase === "starter"
            ? "Starts for eligible early Opening accounts. Completes when starter rows are satisfied or Tier 1 reaches Wave 20."
            : block.phase === "economy"
              ? "Runs while the utility allocation is below target. It waits for affordable upgrades; it does not hand off just because the wallet is low."
              : block.phase === "objectives"
                ? "Runs after earlier phases complete and waits for eligible objective upgrades."
                : block.phase === "fallback"
                  ? "Looks for a cheap filler while the main goal is waiting for funds."
                  : "Uses the current in-game policy until its eligible upgrades are complete."}</p></div>
          <label>Policy<select value={block.policy} onChange={event => onChange({ ...block, policy: event.target.value as "opening" | "turtle" })}><option value="opening">Opening</option><option value="turtle">Turtle</option></select></label>
          <p className={styles.hint}>This grouped block runs the existing bot policy. Reorder or replace it with individual blocks to customize the route.</p>
        </>}
        {block.type === "buy" && <label>Upgrade<select value={block.upgrade_id} onChange={event => onChange({ ...block, upgrade_id: event.target.value })}>{available.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
        {block.type === "condition" && <>
          <label>Account fact<select value={block.field} onChange={event => onChange({ ...block, field: event.target.value as typeof block.field })}>
            <option value="best_tier_1_wave">Highest Tier 1 wave</option>{lane === "battle" && <option value="wave">Current wave</option>}<option value="wallet">Available {lane === "battle" ? "cash" : "coins"}</option></select></label>
          <label>Comparison<select value={block.op} onChange={event => onChange({ ...block, op: event.target.value as "gte" | "lte" })}><option value="gte">At least (≥)</option><option value="lte">At most (≤)</option></select></label>
          <label>Threshold<input type="number" min={0} value={block.value} onChange={event => onChange({ ...block, value: Number(event.target.value) })} /></label>
          <p className={styles.hint}>Nest another If / else inside a branch to combine conditions, such as best wave ≥50 and current wave ≤10.</p>
        </>}
        {block.type === "pool" && <>
          <div className={styles.fieldHeading}>Eligible upgrades <span>{block.upgrade_ids.length} in pool</span></div>
          <div className={styles.poolChoices}>{available.map(item => <button type="button" key={item.id} aria-pressed={block.upgrade_ids.includes(item.id)} onClick={() => {
            const ids = block.upgrade_ids.includes(item.id) ? block.upgrade_ids.filter(id => id !== item.id) : [...block.upgrade_ids, item.id];
            onChange({ ...block, upgrade_ids: ids, weights: Object.fromEntries(ids.map(id => [id, block.weights?.[id] ?? 1])) });
          }}>{item.name}</button>)}</div>
          {!block.upgrade_ids.length && <p role="alert">Choose at least one upgrade.</p>}
          <label>Selection<select value={block.selection} onChange={event => onChange({ ...block, selection: event.target.value as Pool["selection"] })}><option value="priority">First eligible in pool order</option><option value="weighted">Weighted draw</option></select></label>
          <div className={styles.orderedPool}>{block.upgrade_ids.map((id, index) => <div key={id}>
            <span>{index + 1}. {names.get(id) ?? id}</span><button type="button" disabled={index === 0} aria-label={`Prioritize ${names.get(id) ?? id}`} onClick={() => { const ids = [...block.upgrade_ids]; [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]]; onChange({ ...block, upgrade_ids: ids }); }}>↑</button>
            {block.selection === "weighted" && <input aria-label={`Weight for ${names.get(id) ?? id}`} type="number" min={1} max={10000} value={block.weights?.[id] ?? 1} onChange={event => onChange({ ...block, weights: { ...block.weights, [id]: Number(event.target.value) } })} />}
          </div>)}</div>
          <div className={styles.fieldHeading}>Price rule<button type="button" onClick={() => onChange({ ...block, discount_pct: block.discount_pct === undefined ? 20 : undefined, reference_upgrade_id: block.discount_pct === undefined ? "priority" : undefined })}>{block.discount_pct === undefined ? "Add discount" : "Remove"}</button></div>
          {block.discount_pct !== undefined && <>
            <label>Compare price with<select value={block.reference_upgrade_id ?? "priority"} onChange={event => onChange({ ...block, reference_upgrade_id: event.target.value })}><option value="priority">Top-priority upgrade</option>{available.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
            <label>Minimum discount (%)<input type="number" min={0} max={100} value={block.discount_pct} onChange={event => onChange({ ...block, discount_pct: Number(event.target.value) })} /></label>
            <div className={styles.priceExample}>Reference price 100 → pay at most <b>{100 - block.discount_pct}</b></div>
          </>}
          <div className={styles.fieldHeading}>Purchase cap<button type="button" onClick={() => onChange({ ...block, max_purchases: block.max_purchases === undefined ? 8 : undefined })}>{block.max_purchases === undefined ? "Add cap" : "Remove"}</button></div>
          {block.max_purchases !== undefined && <label>Maximum confirmed purchases per upgrade<input type="number" min={1} max={100000} value={block.max_purchases} onChange={event => onChange({ ...block, max_purchases: Number(event.target.value) })} /></label>}
          {(block.max_purchases !== undefined || block.selection === "weighted") && <p className={styles.hint}>Counts confirmed buys {lane === "workshop" ? "across this account’s recorded history" : "in the current run"}. Displayed upgrade levels are separate.</p>}
          {block.selection === "weighted" && <>
            <label>Reduce weight after each buy (%)<input type="number" min={0} max={100} value={block.decay_pct ?? 0} onChange={event => onChange({ ...block, decay_pct: Number(event.target.value) })} /></label>
            <label>Minimum weight<input type="number" min={1} value={block.weight_floor ?? 1} onChange={event => onChange({ ...block, weight_floor: Number(event.target.value) })} /></label>
          </>}
        </>}
      </fieldset>
      {block.type === "pool" && block.selection === "weighted" && <WeightPreview key={block.id} block={block} names={names} />}
      {locked ? <button type="button" className={styles.primaryButton} onClick={onCopy}>Create copy to edit</button>
        : <button type="button" className={styles.removeButton} onClick={onRemove}><Trash2 size={14} />Remove block</button>}
    </>}
  </aside>;
}
