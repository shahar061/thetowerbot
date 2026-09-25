"use client";

import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { Upgrade } from "@/lib/types";

type Battle = BuildRouteDocument["baseline"]["battle"];
type Branch = Battle["branches"][number];
type Phase = Branch["phases"][number];

export function BattleFlow({ battle, catalog, onChange }: {
  battle: Battle; catalog: Upgrade[]; onChange: (battle: Battle) => void;
}): React.JSX.Element {
  const names = new Map(catalog.map(upgrade => [upgrade.id, upgrade.name]));
  const upgrades = catalog.filter(upgrade => !upgrade.unlock);
  function editBranch(id: string, patch: Partial<Branch>): void {
    onChange({ ...battle, branches: battle.branches.map(branch => branch.id === id ? { ...branch, ...patch } : branch) });
  }
  function editPhase(branch: Branch, id: string, patch: Partial<Phase>): void {
    editBranch(branch.id, { phases: branch.phases.map(phase => phase.id === id ? { ...phase, ...patch } : phase) });
  }
  function phase(id: string, start: number, end: number | null, ids: string[]): Phase {
    return { id, start_wave: start, end_wave: end, priority_ids: ids,
      cash_spend_limit_pct: 100, draw_chance_pct: 0, weights: {}, emergency_survival: true };
  }
  return <section className="space-y-3 rounded-2xl border border-border bg-card p-4" aria-label="Battle flow builder">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-heading font-semibold">Battle flow</h3>
      <p className="mt-1 text-xs text-muted-foreground">Best wave selects a branch. Current wave selects one phase. Every tap still checks its live row and cash.</p></div>
      <button type="button" onClick={() => onChange({ ...battle, mode: battle.mode === "phases" ? "legacy_policy" : "phases",
        branches: battle.mode === "phases" ? battle.branches : [{ id: "battle.opening", min_best_tier_1_wave: null,
          phases: [phase("battle.opening.all", 1, null, ["defense_absolute", "thorns", "damage"])] }] })}
        className="rounded-md border border-border px-3 py-2 text-xs">{battle.mode === "phases" ? "Use current battle policy" : "Build battle phases"}</button></div>
    {battle.mode === "phases" && <>
      <div className="flex flex-wrap gap-2"><button type="button" onClick={() => {
        const id = `battle.economy.${battle.branches.length}`;
        onChange({ ...battle, branches: [...battle.branches, { id, min_best_tier_1_wave: 50,
          phases: [phase(`${id}.first10`, 1, 10, ["cash_per_wave", "coins_per_kill_bonus", "defense_absolute"]),
                   phase(`${id}.after10`, 11, null, ["defense_absolute", "thorns", "health"])] }] });
      }} className="rounded-md border border-primary/50 px-3 py-2 text-xs text-primary">Add 50-wave economy branch</button></div>
      {battle.branches.map(branch => <article key={branch.id} className="space-y-3 rounded-xl border border-border bg-background/40 p-3" aria-label={`Battle branch ${branch.id}`}>
        <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-mono text-xs font-semibold">{branch.id}</p>{branch.min_best_tier_1_wave === null ? <span className="rounded-full border border-border px-2 py-0.5 text-xs">Fallback</span> :
          <div className="flex items-center gap-2"><label className="text-xs">Highest Tier 1 wave ≥ <input type="number" min={0} aria-label={`Highest-wave gate for ${branch.id}`} value={branch.min_best_tier_1_wave} onChange={event => editBranch(branch.id, { min_best_tier_1_wave: Number(event.target.value) })} className="w-20 rounded border border-border bg-background px-2 py-1" /></label>
            <button type="button" onClick={() => onChange({ ...battle, branches: battle.branches.filter(value => value.id !== branch.id) })} aria-label={`Remove ${branch.id}`} className="rounded border border-border px-2 py-1 text-xs">Remove</button></div>}</div>
        <div className="grid gap-3 md:grid-cols-2">{branch.phases.map(entry => <section key={entry.id} className="space-y-2 rounded-lg border border-border bg-card p-3 text-xs" aria-label={`Wave phase ${entry.id}`}>
          <p className="font-mono font-semibold">{entry.id}</p>
          <div className="flex gap-2"><label>From wave <input type="number" min={1} aria-label={`Start wave for ${entry.id}`} value={entry.start_wave} onChange={event => editPhase(branch, entry.id, { start_wave: Number(event.target.value) })} className="w-16 rounded border border-border bg-background px-1" /></label>
            <label>Through <input type="number" min={1} aria-label={`End wave for ${entry.id}`} value={entry.end_wave ?? ""} placeholder="∞" onChange={event => editPhase(branch, entry.id, { end_wave: event.target.value === "" ? null : Number(event.target.value) })} className="w-16 rounded border border-border bg-background px-1" /></label></div>
          <label className="block">Cash spend limit % <input type="number" min={0} max={100} aria-label={`Cash spend limit for ${entry.id}`} value={entry.cash_spend_limit_pct} onChange={event => editPhase(branch, entry.id, { cash_spend_limit_pct: Number(event.target.value) })} className="ml-2 w-16 rounded border border-border bg-background px-1" /></label>
          <label className="block">Weighted luck % <input type="number" min={0} max={100} aria-label={`Weighted luck for ${entry.id}`} value={entry.draw_chance_pct} onChange={event => editPhase(branch, entry.id, { draw_chance_pct: Number(event.target.value) })} className="ml-2 w-16 rounded border border-border bg-background px-1" /></label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={entry.emergency_survival} onChange={event => editPhase(branch, entry.id, { emergency_survival: event.target.checked })} />Survival emergency can interrupt</label>
          <ol className="space-y-1">{entry.priority_ids.map((id, index) => <li key={id} className="flex items-center gap-1"><span className="flex-1">{names.get(id) ?? id}</span><button type="button" disabled={index === 0} aria-label={`Move ${names.get(id) ?? id} up in ${entry.id}`} onClick={() => { const ids = [...entry.priority_ids]; [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]]; editPhase(branch, entry.id, { priority_ids: ids }); }} className="rounded border border-border px-1 disabled:opacity-40">↑</button><button type="button" onClick={() => editPhase(branch, entry.id, { priority_ids: entry.priority_ids.filter(value => value !== id), weights: Object.fromEntries(Object.entries(entry.weights).filter(([key]) => key !== id)) })} aria-label={`Remove ${names.get(id) ?? id} from ${entry.id}`} className="rounded border border-border px-1">×</button>
            {entry.draw_chance_pct > 0 && <input type="number" min={1} aria-label={`Weight for ${names.get(id) ?? id} in ${entry.id}`} value={entry.weights[id] ?? 1} onChange={event => editPhase(branch, entry.id, { weights: { ...entry.weights, [id]: Number(event.target.value) } })} className="w-12 rounded border border-border bg-background px-1" />}</li>)}</ol>
          <select aria-label={`Add battle upgrade to ${entry.id}`} value="" onChange={event => { if (event.target.value) editPhase(branch, entry.id, { priority_ids: [...entry.priority_ids, event.target.value] }); }} className="w-full rounded border border-border bg-background p-1"><option value="">Add upgrade…</option>{upgrades.filter(item => !entry.priority_ids.includes(item.id)).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
          {branch.phases.length > 1 && <button type="button" onClick={() => editBranch(branch.id, { phases: branch.phases.filter(value => value.id !== entry.id) })} className="rounded border border-border px-2 py-1">Remove phase</button>}
        </section>)}</div>
        <button type="button" onClick={() => { const last = branch.phases.at(-1); const start = last?.end_wave !== null && last?.end_wave !== undefined ? last.end_wave + 1 : 1; editBranch(branch.id, { phases: [...branch.phases, phase(`${branch.id}.phase${branch.phases.length + 1}`, start, null, ["defense_absolute"])] }); }} className="rounded border border-border px-2 py-1 text-xs">Add wave phase</button>
      </article>)}
    </>}
  </section>;
}
