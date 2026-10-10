"use client";
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Layers, SlidersHorizontal, Activity, Download } from 'lucide-react';
import { PageHeader } from '@/components/PageHeader';
import { useAccountSelection } from '@/lib/AccountSelection';
import { fetchUpgrades } from '@/lib/api';
import { createSingleStudioClient, type StrategyClientContext, type StrategyStatus } from '@/lib/strategyClient';
import type { StrategyDefinition, StrategyLibrary } from '@/lib/strategyStudio';
import type { BuildRouteDocument } from '@/lib/buildRoute';
import type { LabsSnapshot } from '@/lib/labs';
import type { Upgrade } from '@/lib/types';
import { StrategyStudio } from './StrategyStudio';
import { BotSettings } from './BotSettings';
import { ActiveStrategy } from './ActiveStrategy';

type TargetSelection = { scope: string | null; accountId: string | null; name: string };
export function StrategyWorkspace(): React.JSX.Element {
  const { selected, choose } = useAccountSelection();
  const selection = { scope: selected && (selected.kind === 'worker' || selected.instance) ? selected.key : null,
    accountId: selected?.account_id ?? null,
    name: selected?.instance ?? 'This emulator' };
  const selectedKey = JSON.stringify(selection);
  const [editing, setEditing] = useState<TargetSelection>(selection);
  const editingKey = JSON.stringify(editing);
  const [planDirty, setPlanDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const dirty = planDirty || settingsDirty;
  const [tab, setTab] = useState<'plan' | 'settings' | 'active'>('plan');
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('tab') === 'settings') setTab('settings');
  }, []);
  const [library, setLibrary] = useState<StrategyLibrary | null>(null);
  const [route, setRoute] = useState<BuildRouteDocument | null>(null);
  const [labs, setLabs] = useState<LabsSnapshot | null>(null);
  const [catalog, setCatalog] = useState<Upgrade[]>([]);
  const [status, setStatus] = useState<StrategyStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preferredPlan, setPreferredPlan] = useState<string | undefined>(undefined);
  const context = useMemo<StrategyClientContext>(() => ({ scope: editing.scope,
    accountId: editing.accountId ?? status?.account_id ?? null, targetId: status?.target_id ?? null,
    generation: status?.generation ?? null, epoch: status?.epoch ?? null }),
    [editing.scope, editing.accountId, status?.account_id, status?.target_id, status?.generation, status?.epoch]);
  const client = useMemo(() => createSingleStudioClient(context), [context]);
  useEffect(() => {
    if (selectedKey !== editingKey && !dirty) setEditing(JSON.parse(selectedKey) as TargetSelection);
  }, [selectedKey, editingKey, dirty]);
  useEffect(() => {
    let alive = true;
    setLabs(null); setLibrary(null); setRoute(null); setStatus(null); setError(null); setPreferredPlan(undefined);
    const reader = createSingleStudioClient({ scope: editing.scope, accountId: editing.accountId,
      targetId: null, generation: null, epoch: null });
    void Promise.all([reader.readLibrary(), reader.readAssignment(), reader.readStatus(), fetchUpgrades()])
      .then(([nextLibrary, nextRoute, nextStatus, upgrades]) => {
        if (alive) { setLibrary(nextLibrary); setRoute(nextRoute); setStatus(nextStatus); setCatalog(upgrades); }
      }).catch(failure => { if (alive) setError((failure as Error).message); });
    void reader.readLabs().then(value => { if (alive) setLabs(value); }).catch(() => {});
    const timer = window.setInterval(() => {
      void Promise.all([reader.readStatus(), reader.readAssignment()]).then(([nextStatus, nextRoute]) => {
        if (alive) { setStatus(nextStatus); setRoute(nextRoute); }
      }).catch(failure => { if (alive) { setStatus(previous => previous ? { ...previous, state: 'unavailable', reason: (failure as Error).message } : null); } });
    }, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [editingKey, editing.scope, editing.accountId]);
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent): void => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    const navigation = (event: MouseEvent): void => {
      const link = (event.target as Element | null)?.closest('a[href]') as HTMLAnchorElement | null;
      if (dirty && link && link.origin === window.location.origin && link.pathname !== window.location.pathname
          && !window.confirm('Discard unsaved Strategy Studio changes and leave this page?')) {
        event.preventDefault(); event.stopPropagation();
      }
    };
    window.addEventListener('beforeunload', protect);
    document.addEventListener('click', navigation, true);
    return () => { window.removeEventListener('beforeunload', protect); document.removeEventListener('click', navigation, true); };
  }, [dirty]);
  const refreshed = useCallback(async (nextRoute: BuildRouteDocument): Promise<void> => {
    setRoute(nextRoute);
    setStatus(previous => previous ? { ...previous, state: 'pending' } : null);
    try { setStatus(await client.readStatus()); } catch (failure) { setError((failure as Error).message); }
  }, [client]);
  const members = status?.target_id && status.account_id && status.state !== 'blocked' && status.state !== 'unavailable'
    ? [{ name: status.target_id, account_id: status.account_id }] : [];
  const assigned = status?.target_id ? route?.assignments?.[status.target_id] : null;
  const exact = assigned && library ? [...library.templates, ...library.strategies].find(item => item.id === assigned.strategy_id && item.version === assigned.strategy_version) : undefined;
  const snapshot: StrategyDefinition | undefined = assigned && !exact ? {
    id: `assigned:${assigned.strategy_id}:${assigned.strategy_version}`, name: assigned.strategy_name,
    version: assigned.strategy_version, source_template: 'scratch', builtin: true,
    baseline: assigned.baseline, kind: assigned.kind, legacy_snapshot: assigned.legacy_snapshot,
  } : undefined;
  async function importProfile(): Promise<void> {
    setBusy(true); setError(null);
    try { const imported = await client.importProfile(); setLibrary(imported); setPreferredPlan(imported.imported_strategy_id); } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="space-y-5">
    <PageHeader title="Strategy Studio" meta={`${editing.name} · ${status?.account_id ?? 'account verification required'}`} />
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-card p-4">
      <div><p className="text-sm font-medium">{status?.strategy_name ?? 'Local profile'}{status?.strategy_version ? ` · v${status.strategy_version}` : ''}</p>
        <p className="text-xs text-muted-foreground">Save a version, then apply it. Bot settings remain specific to this emulator.</p></div>
      <span className="rounded-full border border-border px-3 py-1 text-xs font-semibold capitalize">{status?.state ?? 'Loading'}</span>
    </div>
    {selectedKey !== editingKey && dirty && <div role="alert" className="rounded-xl border border-warning p-4 space-y-2"><p>You have unsaved changes for {editing.name}. Requests still target that emulator.</p>
      <div className="flex gap-3"><button type="button" onClick={() => { if (editing.scope) choose(editing.scope); }} className="rounded border px-3 py-2 text-sm">Keep editing</button>
        <button type="button" onClick={() => { setPlanDirty(false); setSettingsDirty(false); setEditing(selection); }} className="rounded border px-3 py-2 text-sm">Discard and switch</button></div></div>}
    {error && <p role="alert" className="text-danger">{error}</p>}
    <div role="tablist" aria-label="Strategy views" className="grid grid-cols-3 gap-2 rounded-2xl border border-border bg-card p-2">
      {[{ id: 'plan', label: 'Plan', Icon: Layers }, { id: 'settings', label: 'Bot settings', Icon: SlidersHorizontal }, { id: 'active', label: 'Active strategy', Icon: Activity }].map(({ id, label, Icon }) =>
        <button type="button" role="tab" aria-selected={tab === id} key={id} onClick={() => setTab(id as typeof tab)} className={`flex items-center justify-center gap-2 rounded-xl px-2 py-3 text-sm font-medium ${tab === id ? 'bg-primary text-primary-foreground' : 'hover:bg-background'}`}><Icon size={16} /><span>{label}</span></button>)}
    </div>
    <div hidden={tab !== 'plan'} role="tabpanel" aria-label="Plan">
      {status?.state === 'legacy' && <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border p-4"><p className="text-sm">Your current profile remains active until you apply a saved plan. Import it to preserve its rules as a version.</p>
        <button type="button" disabled={busy || dirty} onClick={() => void importProfile()} className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm disabled:opacity-40"><Download size={15} />Import current profile</button></div>}
      {library && route ? <StrategyStudio key={`${editingKey}:${preferredPlan ?? ''}`} library={library} saved={route} catalog={catalog} members={members}
        labsSnapshot={labs} labObservations={labs?.workers} client={client} mode="single" onPublished={next => void refreshed(next)} onLibrarySaved={setLibrary}
        initialStrategyId={preferredPlan ?? exact?.id} assignedSnapshot={preferredPlan ? undefined : snapshot} onDraftChange={setPlanDirty}
        linkIdentity={editingKey} /> : !error && <p role="status">Loading strategy library…</p>}
    </div>
    <div hidden={tab !== 'settings'} role="tabpanel" aria-label="Bot settings"><BotSettings key={editingKey} client={client} onDirty={setSettingsDirty} push={status?.push_runs} /></div>
    <div hidden={tab !== 'active'} role="tabpanel" aria-label="Active strategy">{status && <ActiveStrategy status={status} onRestore={async version => {
      if (!status.strategy_id || !members.length) throw new Error('Verify this emulator before restoring.');
      await refreshed(await client.assign(status.strategy_id, version, [{ worker: members[0].name, account_id: members[0].account_id }], route?.revision ?? 0));
    }} />}</div>
  </div>;
}
