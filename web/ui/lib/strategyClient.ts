import type { LabsSnapshot } from '@/lib/labs';
import { ApiError, fetchHostStatus, mutationHeaders } from './api';
import { checkRuntimeCompatibility } from './runtimeCompatibility';
import type { BuildRouteDocument, BuildRoutePreview } from './buildRoute';
import type { SaveStrategyInput, StrategyLibrary, StrategyLedger } from './strategyStudio';
import type { Strategy, PushRunStatus } from './types';

export type StrategyClientContext = Readonly<{
  scope: string | null; accountId: string | null; targetId: string | null;
  generation: string | null; epoch: number | null;
}>;
export type StrategyStatus = {
  state: 'legacy' | 'pending' | 'applied' | 'blocked' | 'unavailable';
  target_id: string | null; account_id: string | null;
  generation?: string; epoch?: number; revision?: number;
  strategy_id?: string; strategy_name?: string; strategy_version?: number;
  reason?: string | null; kind?: 'native' | 'legacy';
  acknowledged?: { revision?: number; strategy_version?: number; observed_at?: number };
  lane_sources?: Record<string, { source: string; overlay_id?: string; revision?: number }>;
  push_runs?: Partial<PushRunStatus> & { active?: boolean };
};
export interface StrategyStudioClient {
  saveVersion(input: SaveStrategyInput, revision: number): Promise<StrategyLibrary>;
  assign(id: string, version: number, targets: { worker: string; account_id: string }[], revision: number): Promise<BuildRouteDocument>;
  preview(route: BuildRouteDocument, revision: number): Promise<BuildRoutePreview>;
  ledger(): Promise<StrategyLedger>;
}

export function createSingleStudioClient(context: StrategyClientContext) {
  // Capture primitives once; later account-selector changes never retarget writes.
  const selected = { ...context };
  async function request<T>(path: string, body?: unknown): Promise<T> {
    if (body !== undefined) {
      const status = await fetchHostStatus();
      const compatible = checkRuntimeCompatibility(status.runtime);
      if (!compatible.compatible || !status.runtime?.capabilities.includes('strategy_studio')) {
        throw new ApiError(412, 'Strategy Studio runtime is unavailable or incompatible.');
      }
    }
    const response = await fetch(`/api/strategy-studio/${path}`, {
      method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      headers: { ...mutationHeaders(), 'content-type': 'application/json',
        ...(selected.scope ? { 'x-account-scope': selected.scope } : {}),
        ...(selected.scope && selected.accountId ? { 'x-expected-account-id': selected.accountId } : {}) },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const value = await response.json();
    if (!response.ok) throw new ApiError(response.status,
      typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail ?? value));
    return value as T;
  }
  return {
    readLibrary: () => request<StrategyLibrary>('library'),
    readAssignment: () => request<BuildRouteDocument>('route'),
    readLabs: () => request<LabsSnapshot>('labs'),
    readStatus: () => request<StrategyStatus>('status'),
    readSettings: () => request<Strategy>('settings'),
    saveSettings: (settings: Partial<Strategy>) => request<Strategy>('settings', settings),
    importProfile: () => request<StrategyLibrary & { imported_strategy_id?: string }>('import', {}),
    saveVersion: (input: SaveStrategyInput, revision: number) => request<StrategyLibrary>('library', { ...input, expected_revision: revision }),
    async assign(id: string, version: number, targets: { worker: string; account_id: string }[], revision: number): Promise<BuildRouteDocument> {
      if (targets.length !== 1 || targets[0].worker !== selected.targetId || targets[0].account_id !== selected.accountId
          || !selected.generation || selected.epoch === null) throw new ApiError(409, 'Review the current emulator identity before applying.');
      const result = await request<{ route: BuildRouteDocument; status: 'pending' }>('assign', {
        strategy_id: id, strategy_version: version, expected_revision: revision,
        target_id: selected.targetId, account_id: selected.accountId,
        generation: selected.generation, epoch: selected.epoch,
      });
      return result.route;
    },
    preview: (route: BuildRouteDocument, revision: number) => request<BuildRoutePreview>('preview', { route, expected_revision: revision }),
    ledger: () => request<StrategyLedger>('ledger'),
  } satisfies StrategyStudioClient & Record<string, unknown>;
}
