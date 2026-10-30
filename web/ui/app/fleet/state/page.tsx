'use client';
import { useEffect, useState } from 'react';
import { DEFAULT_SELECTION, loadSelection, saveSelection, type Selection } from './selection';
import { useGameExports } from '@/lib/useGameExport';
import { GameExportPanel } from '@/components/game-export/GameExportPanel';
export default function FleetStatePage(): React.JSX.Element {
  const { accounts, error } = useGameExports();
  const [selection, setSelection] = useState<Selection>(DEFAULT_SELECTION);
  useEffect(() => setSelection(loadSelection()), []);
  const choose = (next: Selection): void => { setSelection(next); saveSelection(next); };
  const id = (account: NonNullable<typeof accounts>[number]): string => account.worker ?? account.instance ?? account.account_id ?? 'Unverified';
  const shown = (accounts ?? []).filter(account => selection.mode === 'all'
    || (selection.mode === 'live' ? account.source_online !== false && account.current_run?.active : selection.ids.includes(id(account))));
  return <main className="space-y-5 p-4"><header><h1 className="text-2xl font-semibold">Account tracker</h1><p className="mt-2 text-sm text-muted-foreground">Official JSON observations and labeled estimates across verified accounts.</p></header>
    <nav aria-label="Tracker accounts" className="flex flex-wrap gap-2"><button className="rounded border px-3 py-2 text-sm" onClick={() => choose(DEFAULT_SELECTION)}>All</button><button className="rounded border px-3 py-2 text-sm" aria-pressed={selection.mode === 'live'} onClick={() => choose({ mode: 'live', ids: [] })}>Only active battles</button>
      {(accounts ?? []).map(account => { const key = `${account.worker}:${account.account_id}`; return <button key={key} className="rounded border px-3 py-2 text-sm" aria-pressed={shown.includes(account)} onClick={() => { const ids = shown.map(id); choose({ mode: 'custom', ids: ids.includes(id(account)) ? ids.filter(key => key !== id(account)) : [...ids, id(account)] }); }}>{account.worker ?? account.instance ?? account.account_id ?? 'Unverified'}</button>; })}</nav>
    {error && <p role="alert" className="text-danger">Official fleet exports unavailable: {error}</p>}
    {!accounts && !error && <p role="status">Loading official fleet exports…</p>}
    {accounts && shown.length === 0 && <p role="status">No account exports selected or available.</p>}
    <div className={`grid min-w-0 items-start gap-4 ${shown.length > 1 ? "xl:grid-cols-2" : "grid-cols-1"}`}>{shown.map(account => <article className="min-w-0" key={`${account.worker}:${account.account_id}`} aria-label={account.worker ?? account.instance ?? account.account_id ?? 'Unverified'}><GameExportPanel section="tracker" data={error ? { ...account, source_online: false } : account} /></article>)}</div>
  </main>;
}
