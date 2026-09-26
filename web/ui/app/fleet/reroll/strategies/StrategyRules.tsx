"use client";

import { useState } from "react";
import { LockKeyhole } from "lucide-react";
import { splitPreview, type LabShareMode, type LabsRow, type RouteRules } from "@/lib/labs";

const SAFETY = ["Never rush a lab with gems", "Never cancel a running lab", "Pay only the price read on screen",
  "Keep 100 gems for Lab 2 until it is owned"];
const ORDER = ["Fixed safety rules", "Reserves: gems keep, Lab 2 reserve, lab coin jar", "Coin sharing", "Spend limits",
  "Lane blocks", "Tier 1 Wave 60 stop"];

function Tag({ live }: { live: boolean }): React.JSX.Element {
  return <span className={`ml-2 rounded-full border px-1.5 py-0.5 text-[10px] ${live ? "border-emerald-500/50 text-emerald-600" : "border-border text-muted-foreground"}`}>{live ? "Live" : "Planned"}</span>;
}

const field = "flex flex-col gap-1 text-xs";
const input = "rounded border border-border bg-background px-2 py-1";

export function StrategyRules({ rules, locked, rows, onChange }: {
  rules: RouteRules; locked: boolean; rows: LabsRow[]; onChange: (rules: RouteRules) => void;
}): React.JSX.Element {
  const [worker, setWorker] = useState(rows[0]?.worker ?? "");
  const set = (next: RouteRules): void => { if (!locked) onChange(next); };
  const coins = rules.coins, labs = rules.labs, gems = rules.gems;
  const row = rows.find(item => item.worker === worker) ?? rows[0] ?? null;
  const slot1 = row?.plan?.slots[0] ?? null;
  const price = slot1?.automated && slot1.now.state !== "researching" ? slot1.next?.price ?? null : null;
  const split = row ? splitPreview(rules, row.wallet.coins, row.plan?.jar ?? 0, price) : null;
  const nullable = (value: string): number | null => value === "" ? null : Number(value);

  return <section aria-label="Strategy rules" className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_280px]">
    <div className="grid gap-4 md:grid-cols-2">
      <fieldset aria-label="Coins: Workshop vs Labs" disabled={locked} className="space-y-2 rounded-xl border border-border p-3">
        <legend className="px-1 text-sm font-semibold">Coins: Workshop vs Labs</legend>
        <label className={field}><span>Share coins with Labs<Tag live /></span>
          <select aria-label="Lab share mode" value={coins.lab_share.mode} className={input}
            onChange={event => set({ ...rules, coins: { ...coins, lab_share: { ...coins.lab_share, mode: event.target.value as LabShareMode } } })}>
            <option value="when_affordable">Start labs when affordable (today)</option>
            <option value="save_pct">Save a share for the next lab</option>
            <option value="labs_first">Labs first: pause Workshop while a lab waits</option></select></label>
        {coins.lab_share.mode === "save_pct" && <label className={field}>Share of spare coins saved each visit (%)
          <input aria-label="Lab share percent" type="number" min={5} max={90} value={coins.lab_share.pct} className={input}
            onChange={event => set({ ...rules, coins: { ...coins, lab_share: { ...coins.lab_share, pct: Number(event.target.value) } } })} /></label>}
        <label className={field}><span>Workshop spend limit (% of coins after the jar)<Tag live /></span>
          <input aria-label="Workshop spend limit (%)" type="number" min={10} max={100} value={coins.workshop_spend_limit_pct} className={input}
            onChange={event => set({ ...rules, coins: { ...coins, workshop_spend_limit_pct: Number(event.target.value) } })} /></label>
      </fieldset>
      <fieldset aria-label="Labs" disabled={locked} className="space-y-2 rounded-xl border border-border p-3">
        <legend className="px-1 text-sm font-semibold">Labs</legend>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" aria-label="Start labs automatically" checked={labs.auto_start}
          onChange={event => set({ ...rules, labs: { ...labs, auto_start: event.target.checked } })} />Start labs automatically<Tag live /></label>
        <label className={field}><span>Pool selection<Tag live={false} /></span><select aria-label="Pool selection" value={labs.pool.selection} className={input}
          onChange={event => set({ ...rules, labs: { ...labs, pool: { ...labs.pool, selection: event.target.value as RouteRules["labs"]["pool"]["selection"] } } })}>
          <option value="cheapest">Cheapest</option><option value="ordered">In order</option><option value="shortest">Shortest</option></select></label>
        <label className={field}><span>Pool max price (% of wallet)<Tag live={false} /></span><input aria-label="Pool max price" type="number" min={1} max={100}
          value={labs.pool.max_price_pct_of_wallet ?? ""} className={input}
          onChange={event => set({ ...rules, labs: { ...labs, pool: { ...labs.pool, max_price_pct_of_wallet: nullable(event.target.value) } } })} /></label>
        <label className={field}><span>Pool max duration (seconds)<Tag live={false} /></span><input aria-label="Pool max duration" type="number" min={60}
          value={labs.pool.max_seconds ?? ""} className={input}
          onChange={event => set({ ...rules, labs: { ...labs, pool: { ...labs.pool, max_seconds: nullable(event.target.value) } } })} /></label>
        <label className={field}><span>When a slot has nothing to do<Tag live={false} /></span><select aria-label="Idle fill" value={labs.idle_fill} className={input}
          onChange={event => set({ ...rules, labs: { ...labs, idle_fill: event.target.value as RouteRules["labs"]["idle_fill"] } })}>
          <option value="leave_idle">Leave it idle</option><option value="shortest_under_30m">Shortest lab under 30 minutes</option></select></label>
      </fieldset>
      <fieldset aria-label="Gems" disabled={locked} className="space-y-2 rounded-xl border border-border p-3">
        <legend className="px-1 text-sm font-semibold">Gems</legend>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" aria-label="Unlock lab slots automatically" checked={gems.auto_unlock_lab_slots}
          onChange={event => set({ ...rules, gems: { ...gems, auto_unlock_lab_slots: event.target.checked } })} />Unlock lab slots automatically<Tag live /></label>
        <label className={field}><span>Keep gems (never spend below)<Tag live /></span><input aria-label="Keep gems" type="number" min={0} value={gems.keep} className={input}
          onChange={event => set({ ...rules, gems: { ...gems, keep: Number(event.target.value) } })} /></label>
        <label className={field}><span>Gem spend limit (%)<Tag live={false} /></span><input aria-label="Gem spend limit (%)" type="number" min={10} max={100}
          value={gems.spend_limit_pct} className={input}
          onChange={event => set({ ...rules, gems: { ...gems, spend_limit_pct: Number(event.target.value) } })} /></label>
      </fieldset>
      <fieldset aria-label="Fixed safety rules" disabled className="space-y-1 rounded-xl border border-border p-3 text-xs">
        <legend className="px-1 text-sm font-semibold">Fixed safety rules</legend>
        {SAFETY.map(rule => <p key={rule} className="flex items-center gap-2"><LockKeyhole size={12} />{rule}</p>)}
      </fieldset>
    </div>
    <aside aria-label="Wallet split preview" className="space-y-2 rounded-xl border border-border p-3 text-xs">
      <h4 className="text-sm font-semibold">Wallet split</h4>
      {rows.length ? <label className={field}>Emulator<select aria-label="Preview emulator" value={row?.worker ?? ""} onChange={event => setWorker(event.target.value)} className={input}>
        {rows.map(item => <option key={item.worker} value={item.worker}>{item.worker}</option>)}</select></label>
        : <p className="text-muted-foreground">No emulator evidence to preview.</p>}
      {split && <>
        <p>Lab jar {split.jar} / {split.price ?? "?"} coins</p>
        {split.progress !== null && <div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${Math.round(split.progress * 100)}%` }} /></div>}
        <p>{split.paused ? "Workshop paused until Game Speed starts" : `Workshop may spend ${split.workshop} coins`}</p>
      </>}
      {row && !split && <p className="text-muted-foreground">Wallet not read yet.</p>}
      <h4 className="pt-2 text-sm font-semibold">Rule order</h4>
      <ol className="list-decimal space-y-0.5 pl-4">{ORDER.map(item => <li key={item}>{item}</li>)}</ol>
    </aside>
  </section>;
}
