'use client';
import { useEffect, useState } from 'react';
import { fetchGameExport, fetchGameExports, type ExportContext, type GameExportProjection } from './gameExport';
function useExportClock(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 5_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
function ageProjection(data: GameExportProjection, now: number): GameExportProjection {
  if (!data.available || data.exported_at == null) return data;
  const age = Math.max(data.age_seconds ?? 0, now - data.exported_at);
  return { ...data, age_seconds: age, stale: !!data.stale || age > 660 };
}
export function useGameExport(context: ExportContext | null): { data: GameExportProjection | null; error: string | null } {
  const now = useExportClock();
  const scope = context?.scope ?? null;
  const accountId = context?.accountId ?? null;
  const key = JSON.stringify([scope, accountId]);
  const [state, setState] = useState<{ key: string; data: GameExportProjection | null; error: string | null }>({ key, data: null, error: null });
  useEffect(() => {
    const abort = new AbortController();
    let pending = false;
    const poll = async (): Promise<void> => {
      if (pending || !scope || !accountId) return;
      pending = true;
      try { const data = await fetchGameExport({ scope, accountId }, abort.signal);
        if (!abort.signal.aborted) setState({ key, data, error: null });
      } catch (error) { if (!abort.signal.aborted) setState(old => ({ key, data: old.key === key ? old.data : null,
        error: error instanceof Error ? error.message : 'Export unavailable' })); }
      finally { pending = false; }
    };
    void poll();
    const timer = setInterval(() => void poll(), 15_000);
    return () => { abort.abort(); clearInterval(timer); };
  }, [scope, accountId, key]);
  return state.key === key ? { ...state, data: state.data ? ageProjection(state.data, now) : null } : { data: null, error: null };
}
export function useGameExports(): { accounts: GameExportProjection[] | null; error: string | null } {
  const now = useExportClock();
  const [accounts, setAccounts] = useState<GameExportProjection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    let pending = false;
    const poll = async (): Promise<void> => {
      if (pending) return;
      pending = true;
      try { const data = await fetchGameExports(abort.signal);
        if (!abort.signal.aborted) { setAccounts(data.accounts); setError(null); }
      } catch (failure) { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : 'Exports unavailable'); }
      finally { pending = false; }
    };
    void poll(); const timer = setInterval(() => void poll(), 15_000);
    return () => { abort.abort(); clearInterval(timer); };
  }, []);
  return { accounts: accounts?.map(data => ageProjection(data, now)) ?? null, error };
}
