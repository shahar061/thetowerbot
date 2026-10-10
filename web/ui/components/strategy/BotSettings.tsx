"use client";
import { useEffect, useRef, useState } from 'react';
import { StrategyEditor } from '@/components/StrategyEditor';
import type { Strategy, PushRunStatus } from '@/lib/types';
import type { createSingleStudioClient } from '@/lib/strategyClient';

const BOT_FIELDS = ['interval', 'menu_interval', 'click_cooldown', 'auto_navigate',
  'max_runs', 'push_every_farm_runs', 'navigation_cooldown', 'screen_confirmations', 'tap_jitter_px',
  'timing_jitter', 'tap_delay', 'target_speed', 'auto_fastest', 'claims'] as const;

export function BotSettings({ client, onDirty, push }: {
  client: ReturnType<typeof createSingleStudioClient>; onDirty: (dirty: boolean) => void;
  push?: Partial<PushRunStatus>;
}): React.JSX.Element {
  const [saved, setSaved] = useState<Strategy | null>(null);
  const [draft, setDraft] = useState<Strategy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dirtyRef = useRef(false);
  const dirty = !!draft && JSON.stringify(draft) !== JSON.stringify(saved);
  dirtyRef.current = dirty;
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    let alive = true;
    void client.readSettings().then(value => { if (alive && !dirtyRef.current) { setSaved(value); setDraft(value); setError(null); } })
      .catch(failure => { if (alive) setError((failure as Error).message); });
    return () => { alive = false; };
  }, [client]);
  async function save(): Promise<void> {
    if (!draft) return;
    setBusy(true); setError(null);
    try {
      const patch = Object.fromEntries(BOT_FIELDS.map(key => [key, draft[key]])) as Partial<Strategy>;
      const value = await client.saveSettings(patch);
      setSaved(value); setDraft(value);
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-card p-4">
      <div><h2 className="font-semibold">How this bot runs</h2><p className="text-sm text-muted-foreground">Timing, navigation and claims belong to this emulator. Purchase and progression rules belong to its plan.</p></div>
      <div className="flex gap-2"><button type="button" disabled={!dirty || busy} onClick={() => setDraft(saved)} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">Revert</button>
        <button type="button" disabled={!dirty || busy} onClick={() => void save()} className="rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40">{busy ? 'Saving…' : 'Save bot settings'}</button></div>
    </div>
    {push && <p className="text-sm text-muted-foreground" role="status">
      {push.blocker ? push.blocker : push.phase === 'returning'
        ? `Returning to farming tier ${push.farm_tier ?? '—'}.`
        : push.phase && push.phase !== 'farming'
          ? `Push scheduled${push.target_tier ? ` at tier ${push.target_tier}` : ''}.`
          : push.every && push.farms_remaining !== undefined
            ? `${Math.max(0, push.every - push.farms_remaining)} of ${push.every} farming runs completed · next push in ${push.farms_remaining} runs.`
            : 'Scheduled push runs are off.'}
    </p>}
    {error && <p role="alert" className="text-danger">{error}</p>}
    {draft ? <StrategyEditor value={draft} onChange={setDraft} disabled={busy} hidePurchases botSettingsOnly /> : !error && <p role="status">Loading bot settings…</p>}
  </section>;
}
