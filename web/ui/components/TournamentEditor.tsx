"use client";

import { useState } from "react";
import type { TournamentConfig } from "@/lib/types";

export const tournamentDefaults: TournamentConfig = {
  enabled: true, public_name: null,
  opening_cash: { cash_bonus_target: null, cash_per_wave_target: null, cash_budget: null, until_wave: null },
  rules: ["health", "attack_speed", "damage"].map(upgrade_id => ({ upgrade_id, enabled: true, target: null })),
  cash_reserve: 0, cash_spend_limit_pct: 100,
};

export function TournamentEditor({ value = tournamentDefaults, onChange, disabled = false }: {
  value?: TournamentConfig; onChange: (value: TournamentConfig) => void; disabled?: boolean;
}): React.JSX.Element {
  const [selected, setSelected] = useState('defense_percent');
  const upgrades = ['health','attack_speed','damage','defense_percent','defense_absolute','thorns','lifesteal','knockback_chance','knockback_force','orb_speed','orbs','recovery_amount','max_recovery','package_chance','enemy_attack_level_skip','enemy_health_level_skip'];
  return <fieldset disabled={disabled} className="space-y-3 rounded-xl border bg-card p-4">
    <legend className="px-1 font-semibold">Tournaments</legend>
    <label className="flex items-center gap-2"><input disabled={disabled} type="checkbox" checked={value.enabled}
      onChange={e => onChange({ ...value, enabled: e.target.checked })} />Enter with a free ticket</label>
    <p className="text-sm text-muted-foreground">Wednesday and Saturday (UTC), after the current run. One entry per tournament. Never spend gems or buy coin upgrades. Set your public player name in the game first.</p>
    <p className="text-sm">Uncapped combat priorities rotate after each purchase. Optional cash opening ends at its targets, budget or wave limit. Fill all four limits to enable it.</p>
    <div className="space-y-2" aria-label="Tournament combat priorities">
      {value.rules.map((rule, index) => <div key={rule.upgrade_id} className="flex items-center gap-2 text-sm">
        <input disabled={disabled} type="checkbox" aria-label={`Enable ${rule.upgrade_id}`} checked={rule.enabled !== false} onChange={e => onChange({ ...value, rules: value.rules.map((r, i) => i === index ? { ...r, enabled: e.target.checked } : r) })} />
        <span className="min-w-24">{rule.upgrade_id.replaceAll('_', ' ')}</span>
        <label>Target <input disabled={disabled} aria-label={`${rule.upgrade_id} target`} type="number" min="0" step="any" className="w-24 rounded border px-2 py-1"
          placeholder="uncapped" value={rule.target ?? ''} onChange={e => onChange({ ...value, rules: value.rules.map((r, i) => i === index ? { ...r, target: e.target.value === '' ? null : Number(e.target.value) } : r) })} /></label>
        <button type="button" disabled={disabled || index === 0} aria-label={`Move ${rule.upgrade_id} up`} onClick={() => {
          const rules = [...value.rules]; [rules[index - 1], rules[index]] = [rules[index], rules[index - 1]];
          onChange({ ...value, rules });
        }}>↑</button>
        <button disabled={disabled} type="button" aria-label={`Remove ${rule.upgrade_id}`} onClick={() => onChange({ ...value, rules: value.rules.filter((_, i) => i !== index) })}>Remove</button>
      </div>)}
    </div>
    <div className="flex gap-2 text-sm">
      <select disabled={disabled} aria-label="Tournament upgrade" value={selected} onChange={e => setSelected(e.target.value)} className="rounded border px-2 py-1">
        {upgrades.map(id => <option key={id} value={id}>{id.replaceAll('_',' ')}</option>)}
      </select>
      <button type="button" disabled={disabled || value.rules.some(r => r.upgrade_id === selected)} onClick={() => onChange({ ...value, rules: [...value.rules, { upgrade_id: selected, enabled: true, target: ['health','attack_speed','damage'].includes(selected) ? null : 0 }] })}>Add upgrade</button>
    </div>
    <div className="grid grid-cols-2 gap-3">
      {([['cash_bonus_target', 'Cash bonus target'], ['cash_per_wave_target', 'Cash per wave target'], ['cash_budget', 'Opening cash budget'], ['until_wave', 'Last opening wave']] as const).map(([key, label]) =>
        <label key={key} className="space-y-1 text-sm"><span>{label}</span><input disabled={disabled} aria-label={label} type="number" min="0"
          step={key.endsWith('target') ? 'any' : 1} className="block w-full rounded border px-2 py-1"
          value={value.opening_cash[key] ?? ''} onChange={e => onChange({ ...value,
            opening_cash: { ...value.opening_cash, [key]: e.target.value === '' ? null : Number(e.target.value) } })} /></label>)}
    </div>
    <p className="text-xs text-muted-foreground">Final prize claiming is currently manual. Tournament results are shown separately from farming records.</p>
  </fieldset>;
}
