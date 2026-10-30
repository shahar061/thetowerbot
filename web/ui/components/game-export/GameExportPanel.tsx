'use client';
import { useState } from 'react';
import { gameNumber } from '@/app/fleet/reroll/workshop/workshopFormat';
import type { ExportItem, GameExportProjection } from '@/lib/gameExport';
export type ExportSection = 'all' | 'labs' | 'workshop' | 'cards' | 'runs' | 'tracker' | 'journal' | 'stats';
const value = (input: unknown): string => input === null || input === undefined ? 'Unknown' : typeof input === 'boolean' ? input ? 'Yes' : 'No' : typeof input === 'number' ? input.toLocaleString(undefined, { maximumFractionDigits: 20 }) : typeof input === 'object' ? JSON.stringify(input) : String(input);
const primaryValue = (input: unknown): string => typeof input === 'number'
  ? Math.abs(input) >= 1000 ? gameNumber(input) : input.toLocaleString(undefined, { maximumFractionDigits: 2 }) : value(input);
const researchProgress = (input: number | null | undefined): string => input == null ? 'Unknown'
  : input < 0 || input > 1 ? `Unknown (raw ${value(input)})` : input.toLocaleString(undefined, { style: 'percent', maximumFractionDigits: 2 });
const time = (seconds?: number | null): string => seconds == null ? 'Unknown' : new Date(seconds * 1000).toLocaleString();
const labels: Partial<Record<keyof ExportItem, string>> = { level: 'Level', percent_complete: 'Research progress',
  completes_at: 'Expected finish', category: 'Category', count: 'Spare copies', unlocked: 'Owned', active: 'Equipped', mastery_unlocked: 'Mastery' };
function Items({ items, fields }: { items: ExportItem[]; fields: (keyof ExportItem)[] }): React.JSX.Element {
  if (!items.length) return <p className="text-sm text-muted-foreground">No rows in this export.</p>;
  return <div className="max-h-[36rem] overflow-auto"><table className="w-full text-left text-sm">
    <thead><tr><th className="p-2">Name</th>{fields.map(field => <th className="p-2" key={field}>{labels[field] ?? field}</th>)}</tr></thead>
    <tbody>{items.map((item, index) => <tr key={`${item.category ?? ''}:${item.index}:${index}`} className="border-t">
      <td className="p-2">{item.name ?? 'Unmapped entry'}<span className="block text-xs text-muted-foreground">{!item.name ? `Identity unavailable · Native index ${item.index}` : item.id ?? `Native index ${item.index}`}</span></td>
      {fields.map(field => <td className={`p-2 ${field === 'category' ? 'capitalize' : ''}`} key={field}>{field === 'completes_at'
        ? item.completes_at == null ? 'Unknown' : `${time(item.completes_at)} (projected)`
        : field === 'percent_complete' ? researchProgress(item.percent_complete) : value(item[field])}</td>)}
    </tr>)}</tbody>
  </table></div>;
}

function Metrics({ items }: { items: Record<string, unknown> }): React.JSX.Element {
  return <dl className="grid min-w-0 grid-cols-2 gap-3 @[48rem]/export:grid-cols-4">
    {Object.entries(items).map(([label, val]) => <div className="min-w-0 rounded-lg border bg-muted/20 p-3" key={label}>
      <dt className="break-words text-xs text-muted-foreground">{label}</dt>
      <dd title={typeof val === 'number' ? String(val) : value(val)} className="mt-1 min-w-0 break-words font-mono text-lg [overflow-wrap:anywhere]">{primaryValue(val)}</dd>
    </div>)}
  </dl>;
}

export function GameExportPanel({ data, section = 'all' }: { data: GameExportProjection; section?: ExportSection }): React.JSX.Element {
  const [query, setQuery] = useState('');
  const includes = (name: ExportSection): boolean => section === 'all' || section === name || (section === 'stats' && name === 'runs');
  const filter = (items: ExportItem[]): ExportItem[] => items.filter(item => `${item.name ?? 'Unmapped entry'} ${item.index} ${item.id ?? ''} ${item.category ?? ''}`.toLowerCase().includes(query.toLowerCase()));
  const run = data.current_run;
  const reports = (data.runs ?? []).filter(report => report.account_id === data.account_id);
  const knownWaves = reports.flatMap(report => report.wave == null ? [] : [report.wave]);
  const knownCoins = reports.flatMap(report => report.coins == null ? [] : [report.coins]);
  const age = data.age_seconds;
  const estimate = !data.stale && data.source_online !== false && age != null && age <= 300 && run?.active ? run.estimated_wave : null;
  return <section aria-label="Official game export" className="@container/export min-w-0 space-y-4 rounded-xl border bg-card p-4">
    <header className="space-y-1"><h2 className="font-semibold">Official game export{data.account_id ? ` · ${data.account_id}` : ''}</h2>
      {data.available ? <p className="text-xs text-muted-foreground">tool_export.json · Exported <time>{time(data.exported_at)}</time> · {age == null ? 'Age unknown' : `${Math.round(age)}s old`}{data.stale ? ' · Stale' : ''}{data.source_online === false ? ' · Source offline' : ''}</p> : <p role="status" className="text-sm text-muted-foreground">No official game export available. Account observations are unknown.{data.reason ? ` ${data.reason}` : ''}</p>}
      {data.available && <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Source metadata</summary><p>Read at {time(data.captured_at)} · Game {value(data.versions?.game)} · Data {value(data.versions?.data)} · Export {value(data.versions?.export)}</p></details>}
      {(data.collection_error || data.error) && <p role="alert" className="text-sm text-danger">{data.collection_error ?? data.error}</p>}
      {!!data.warnings?.length && <details className="text-xs text-warn"><summary>Export warnings ({data.warnings.length})</summary><ul>{data.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>}
    </header>
    {data.available && <>
      {section === 'stats' && <section className="space-y-2"><h3 className="font-medium">Imported battle statistics</h3><Metrics items={{ 'Retained completed reports': reports.length, 'Highest reported wave': knownWaves.length ? Math.max(...knownWaves) : null, 'Reported coins total': knownCoins.length ? knownCoins.reduce((sum, coins) => sum + coins, 0) : null }} /><p className="text-xs text-muted-foreground">Totals cover retained imported reports, not lifetime game totals. Coins are known for {knownCoins.length} of {reports.length} reports.</p></section>}
      {(section === 'all' || section === 'tracker') && <Metrics items={data.wallet ?? { Wallet: null }} />}
      {(['all', 'labs', 'workshop', 'cards'] as ExportSection[]).includes(section) && <label className="block text-xs text-muted-foreground">Search exported inventory<input className="mt-1 block w-full rounded border bg-background p-2 text-sm" value={query} onChange={event => setQuery(event.target.value)} /></label>}
      {includes('tracker') && <section aria-label="Export run telemetry" className="space-y-3"><h3 className="font-medium">Current battle</h3><Metrics items={{ Status: run?.active == null ? null : run.active ? 'Active at export' : 'No active battle', Tier: run?.tier, 'Observed wave': run?.observed_wave ?? run?.wave,
        ...(estimate == null ? {} : { 'Estimated wave': estimate }), 'Real elapsed seconds': run?.elapsed_seconds, 'Game elapsed seconds': run?.game_elapsed_seconds,
        'Coins earned this round': run?.coins, 'Cells earned this round': run?.cells, 'Average game / real time': run?.effective_rate_average }} />
        <p className="text-xs text-muted-foreground">Observed values describe the export timestamp. Estimated wave is a display projection for at most 300 seconds. The average time ratio is not the selected game speed.</p></section>}
      {includes('labs') && <section className="space-y-3"><h3 className="font-medium">Active lab queue</h3><p className="text-xs text-muted-foreground">Queue rows describe observed slots; expected finish remains projected until confirmed.</p>
        <div className="grid gap-2 sm:grid-cols-2">{data.labs?.slots?.map(slot => <div key={slot.slot} className="rounded border p-3 text-sm"><b>Slot {slot.slot}</b><p>{slot.active ? slot.name ?? 'Unknown research' : slot.active === false ? 'Idle' : 'Unknown'} · Research level {value(data.labs?.research.find(item => item.index === slot.index)?.level)}</p><p className="text-xs text-muted-foreground">Expected finish: {time(slot.completes_at)} · Speed-up: {value(slot.speed_up)} · Remaining seconds: {value(slot.speed_up_remaining_seconds)}</p></div>) ?? <p>Lab queue unavailable.</p>}</div>
        <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Queue metadata</summary><p>LabsUnlocked: {value(data.labs?.unlocked_raw)}</p>{data.labs?.slots.map(slot => <p key={slot.slot}>Slot {slot.slot} queue level: {value(slot.level)}</p>)}</details>
        <h3 className="font-medium">Research levels</h3><p className="text-xs text-muted-foreground">Research progress uses the exported fraction. Unsupported values remain unknown.</p><Items items={filter(data.labs?.research ?? [])} fields={['level', 'percent_complete', 'completes_at']} /></section>}
      {includes('workshop') && <section className="space-y-2"><h3 className="font-medium">Exact Workshop levels</h3><Items items={filter(data.workshop?.upgrades ?? [])} fields={['category', 'level']} /></section>}
      {includes('cards') && <section className="space-y-2"><h3 className="font-medium">Card inventory</h3><p className="text-sm">Card slots: {value(data.cards?.slots)}</p><Items items={filter(data.cards?.items ?? [])} fields={['level', 'count', 'unlocked', 'active', 'mastery_unlocked']} /></section>}
      {includes('runs') && <section className="space-y-2"><h3 className="font-medium">Completed battle reports</h3><p className="text-xs text-muted-foreground">Imported game reports remain available beyond the rolling game history. These reports do not establish bot actions or purchase receipts.</p>{reports.length ? reports.map(report => <details key={report.id} className="rounded border p-3 text-sm"><summary className="cursor-pointer">{time(report.battle_at)} · Tier {value(report.tier)} · Wave {value(report.wave)} · Coins {value(report.coins)}</summary><dl className="mt-3 grid gap-2 sm:grid-cols-2">{Object.entries(report.report ?? report.raw ?? report).map(([key, val]) => <div key={key} className="min-w-0 break-words"><dt className="text-xs text-muted-foreground">{key}</dt><dd className="font-mono text-xs">{value(val)}</dd></div>)}</dl></details>) : <p className="text-sm text-muted-foreground">No completed reports imported yet.</p>}</section>}
      {includes('journal') && <section className="space-y-2"><h3 className="font-medium">Account changes from JSON</h3><p className="text-xs text-muted-foreground">Changes were observed between exports; only confirmed game completion events have exact event times.</p>{data.journal?.length ? data.journal.filter(entry => entry.account_id === data.account_id).map(entry => <details key={entry.id} className="rounded border p-3 text-sm"><summary className="cursor-pointer">{entry.summary}</summary><p className="mt-2 text-xs text-muted-foreground">{entry.occurred_at != null ? `Game event: ${time(entry.occurred_at)}` : `Observed between ${time(entry.observed_from)} and ${time(entry.observed_to)}`}</p><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(entry.changes, null, 2)}</pre></details>) : <p className="text-sm text-muted-foreground">No changes observed between exports yet.</p>}</section>}
    </>}
  </section>;
}
