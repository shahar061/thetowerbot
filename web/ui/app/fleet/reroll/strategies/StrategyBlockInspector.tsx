"use client";

import { useState } from "react";
import Link from "next/link";
import { LockKeyhole, Trash2 } from "lucide-react";
import type { StrategyBlock, ProgramLane } from "@/lib/strategyStudio";
import type { Upgrade } from "@/lib/types";
import { blockDetail, blockTitle, guideAnchor } from "./strategyBlocks";
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
      <Link className={styles.hint} href={`/fleet/reroll/strategies/guide/#${guideAnchor(block)}`}>Learn more about this block →</Link>
      {locked && <div className={styles.lockNotice}><LockKeyhole size={15} />Protected template. Create a copy to edit.</div>}
      <fieldset disabled={locked} className={styles.fields}>
        <label>Name<input aria-label="Block name" maxLength={60} placeholder={blockTitle({ ...block, label: undefined } as StrategyBlock, names)}
          value={block.label ?? ""} onChange={event => {
            const value = event.target.value;
            if (!value.trim()) { const { label: _drop, ...rest } = block; onChange(rest as StrategyBlock); return; }
            onChange({ ...block, label: value });
          }} /></label>
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
          <label>Account fact<select value={block.field} onChange={event => {
            const field = event.target.value as typeof block.field;
            if (field === "upgrade_value") { onChange({ ...block, field, upgrade_id: available[0]?.id }); return; }
            if (block.field === "upgrade_value") { const { upgrade_id: _drop, ...rest } = block; onChange({ ...rest, field }); return; }
            onChange({ ...block, field });
          }}>
            <option value="best_tier_1_wave">Highest Tier 1 wave</option>{lane === "battle" && <option value="wave">Current wave</option>}<option value="wallet">Available {lane === "battle" ? "cash" : "coins"}</option>
            <option value="upgrade_value">Upgrade value</option>{lane === "battle" && <option value="def_abs_coverage">Def. Abs coverage</option>}</select></label>
          {block.field === "upgrade_value" && <label>Upgrade<select value={block.upgrade_id ?? ""} onChange={event => onChange({ ...block, upgrade_id: event.target.value })}>
            <option value="" disabled>Select an upgrade…</option>
            {available.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>}
          <label>Comparison<select value={block.op} onChange={event => onChange({ ...block, op: event.target.value as "gte" | "lte" | "gt" | "lt" })}>
            <option value="gte">At least (≥)</option><option value="gt">More than (&gt;)</option><option value="lte">At most (≤)</option><option value="lt">Less than (&lt;)</option></select></label>
          <label>Threshold<input type="number" step="any" min={0} value={block.value} onChange={event => onChange({ ...block, value: Number(event.target.value) })} /></label>
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
            {block.targets?.[id] === undefined
              ? <button type="button" aria-label={`Add target for ${names.get(id) ?? id}`} onClick={() => onChange({ ...block, targets: { ...block.targets, [id]: 0 } })}>Target</button>
              : <input aria-label={`Target for ${names.get(id) ?? id}`} type="number" step="any" min={0} value={block.targets[id]} onChange={event => onChange({ ...block, targets: { ...block.targets, [id]: Number(event.target.value) } })} />}
            {block.level_caps?.[id] === undefined
              ? <button type="button" aria-label={`Add level cap for ${names.get(id) ?? id}`} onClick={() => onChange({ ...block, level_caps: { ...block.level_caps, [id]: { base: 1 } } })}>Cap</button>
              : <><input aria-label={`Level cap for ${names.get(id) ?? id}`} type="number" min={0} value={block.level_caps[id].base} onChange={event => onChange({ ...block, level_caps: { ...block.level_caps, [id]: { ...block.level_caps![id], base: Number(event.target.value) } } })} />
                <select aria-label={`Cap grows with for ${names.get(id) ?? id}`} value={block.level_caps[id].per_level_of ?? ""} onChange={event => {
                  const cap = block.level_caps![id];
                  const perLevelOf = event.target.value;
                  onChange({ ...block, level_caps: { ...block.level_caps, [id]: perLevelOf ? { base: cap.base, per_level_of: perLevelOf } : { base: cap.base } } });
                }}>
                  <option value="">Fixed cap</option>
                  {available.filter(item => item.id !== id).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select></>}
          </div>)}</div>
          <div className={styles.fieldHeading}>Price rule<button type="button" onClick={() => onChange({ ...block, discount_pct: block.discount_pct === undefined ? 20 : undefined, reference_upgrade_id: block.discount_pct === undefined ? "priority" : undefined })}>{block.discount_pct === undefined ? "Add discount" : "Remove"}</button></div>
          {block.discount_pct !== undefined && <>
            <label>Compare price with<select value={block.reference_upgrade_id ?? "priority"} onChange={event => onChange({ ...block, reference_upgrade_id: event.target.value })}><option value="priority">Top-priority upgrade</option>{available.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
            <label>Minimum discount (%)<input type="number" min={0} max={100} value={block.discount_pct} onChange={event => onChange({ ...block, discount_pct: Number(event.target.value) })} /></label>
            <div className={styles.priceExample}>Reference price 100 → pay at most <b>{100 - block.discount_pct}</b></div>
          </>}
          <div className={styles.fieldHeading}>Price cap<button type="button" onClick={() => onChange({ ...block, price_cap: block.price_cap === undefined ? 1 : undefined })}>{block.price_cap === undefined ? "Add cap" : "Remove"}</button></div>
          {block.price_cap !== undefined && <label>Maximum price (coins)<input type="number" min={1} step={1} value={block.price_cap} onChange={event => onChange({ ...block, price_cap: Number(event.target.value) })} /></label>}
          <div className={styles.fieldHeading}>Wallet share<button type="button" onClick={() => onChange({ ...block, wallet_share_pct: block.wallet_share_pct === undefined ? 20 : undefined })}>{block.wallet_share_pct === undefined ? "Add limit" : "Remove"}</button></div>
          {block.wallet_share_pct !== undefined && <label>Maximum % of wallet<input type="number" min={1} max={100} value={block.wallet_share_pct} onChange={event => onChange({ ...block, wallet_share_pct: Number(event.target.value) })} /></label>}
          <div className={styles.fieldHeading}>Purchase cap<button type="button" onClick={() => onChange({ ...block, max_purchases: block.max_purchases === undefined ? 8 : undefined })}>{block.max_purchases === undefined ? "Add cap" : "Remove"}</button></div>
          {block.max_purchases !== undefined && <label>Maximum confirmed purchases per upgrade<input type="number" min={1} max={100000} value={block.max_purchases} onChange={event => onChange({ ...block, max_purchases: Number(event.target.value) })} /></label>}
          {(block.max_purchases !== undefined || block.selection === "weighted") && <p className={styles.hint}>Counts confirmed buys {lane === "workshop" ? "across this account’s recorded history" : "in the current run"}. Displayed upgrade levels are separate.</p>}
          {block.selection === "weighted" && <>
            <label>Reduce weight after each buy (%)<input type="number" min={0} max={100} value={block.decay_pct ?? 0} onChange={event => onChange({ ...block, decay_pct: Number(event.target.value) })} /></label>
            <label>Minimum weight<input type="number" min={1} value={block.weight_floor ?? 1} onChange={event => onChange({ ...block, weight_floor: Number(event.target.value) })} /></label>
          </>}
        </>}
        {block.type === "budget" && <>
          <label>Utility target (coins)<input type="number" min={0} value={block.target} onChange={event => onChange({ ...block, target: Number(event.target.value) })} /></label>
          <label>Hard ceiling (coins)<input type="number" min={block.target} value={block.ceiling} onChange={event => onChange({ ...block, ceiling: Number(event.target.value) })} /></label>
          <p className={styles.hint}>Counts verified coins spent on utility upgrades.</p>
        </>}
        {block.type === "while_saving" && <label>Only while saving for<select value={block.upgrade_id ?? ""} onChange={event => {
          if (!event.target.value) { const { upgrade_id: _drop, ...rest } = block; onChange(rest as StrategyBlock); return; }
          onChange({ ...block, upgrade_id: event.target.value });
        }}>
          <option value="">Any goal</option>
          {available.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>}
        {block.type === "save_for" && <p className={styles.hint}>Select the goal pool inside this block to edit its upgrades.</p>}
      </fieldset>
      {block.type === "pool" && block.selection === "weighted" && <WeightPreview key={block.id} block={block} names={names} />}
      {locked ? <button type="button" className={styles.primaryButton} onClick={onCopy}>Create copy to edit</button>
        : <button type="button" className={styles.removeButton} onClick={onRemove}><Trash2 size={14} />Remove block</button>}
    </>}
  </aside>;
}
