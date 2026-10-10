"use client";
import { useState } from 'react';
import { CircleCheck, Clock, GitBranch, ShieldAlert } from 'lucide-react';
import type { StrategyStatus } from '@/lib/strategyClient';

export function ActiveStrategy({ status, onRestore }: {
  status: StrategyStatus; onRestore: (version: number) => Promise<void>;
}): React.JSX.Element {
  const [version, setVersion] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const Icon = status.state === 'applied' ? CircleCheck : status.state === 'pending' ? Clock : ShieldAlert;
  const descriptions: Record<StrategyStatus['state'], string> = {
    applied: 'The bot has loaded this exact version for the current account and session.',
    pending: 'Waiting for the bot to finish any pending verification and load the plan at a safe decision.',
    legacy: 'The local profile still controls purchases. Import it or apply a saved Studio plan to switch.',
    blocked: 'Plan spending is blocked until the account and assignment are verified.',
    unavailable: 'The strategy service is unavailable. Refresh after the connection returns.',
  };
  async function restore(): Promise<void> {
    setBusy(true); setError(null);
    try { await onRestore(version); } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4">
    <div className="rounded-2xl border border-border bg-card p-5 space-y-3">
      <div className="flex items-center gap-3"><Icon className="text-primary" size={22} /><h2 className="text-lg font-semibold">{status.strategy_name ?? 'Local profile'}{status.strategy_version ? ` · v${status.strategy_version}` : ''}</h2></div>
      <p>{descriptions[status.state]}</p>
      {status.reason && <p role="status" className="text-sm text-danger">{status.reason}</p>}
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <div><dt className="text-muted-foreground">Assigned version</dt><dd>{status.strategy_version ? `v${status.strategy_version}` : 'Legacy profile'}</dd></div>
        <div><dt className="text-muted-foreground">Bot acknowledgement</dt><dd>{status.state === 'applied' && status.acknowledged?.strategy_version ? `v${status.acknowledged.strategy_version}` : 'Not yet applied in this session'}</dd></div>
        <div><dt className="text-muted-foreground">Account</dt><dd className="break-all">{status.account_id ?? 'Unverified'}</dd></div>
      </dl>
    </div>
    {!!status.lane_sources && <div className="rounded-2xl border border-border bg-card p-5"><h3 className="mb-3 font-semibold">What controls each lane</h3>
      <div className="grid gap-2 sm:grid-cols-2">{Object.entries(status.lane_sources).map(([lane, source]) => <div key={lane} className="flex justify-between rounded-lg bg-background p-3 text-sm"><span className="capitalize">{lane}</span><span>{source.source === 'account_overlay' ? 'Account Cards overlay' : source.source === 'legacy' ? 'Imported policy' : 'Assigned plan'}</span></div>)}</div>
      {status.push_runs?.active && <p className="mt-3 text-sm">Battle temporarily follows the scheduled push run. The pinned base plan resumes when it finishes.</p>}
    </div>}
    {status.strategy_id && (status.strategy_version ?? 0) > 1 && <div className="rounded-2xl border border-border bg-card p-5 space-y-3"><h3 className="flex items-center gap-2 font-semibold"><GitBranch size={16} />Restore a previous version</h3>
      <p className="text-sm text-muted-foreground">Restoring creates a new assignment. It takes effect at the same safe decision boundary.</p>
      <div className="flex items-center gap-3"><label className="text-sm">Version <select value={version} onChange={e => setVersion(Number(e.target.value))} className="ml-2 rounded border p-2">{Array.from({ length: (status.strategy_version ?? 1) - 1 }, (_, i) => <option key={i + 1} value={i + 1}>v{i + 1}</option>)}</select></label>
        <button type="button" disabled={busy || status.state === 'blocked' || status.state === 'unavailable'} onClick={() => void restore()} className="rounded-lg border px-3 py-2 text-sm disabled:opacity-40">Restore version</button></div>
      {error && <p role="alert" className="text-danger">{error}</p>}
    </div>}
  </section>;
}
