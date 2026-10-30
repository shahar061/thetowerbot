export type ExportContext = { scope: string | null; accountId: string | null };
export type ExportItem = { index: number; id: string | null; name: string | null; level?: number | null;
  category?: string; count?: number | null; unlocked?: boolean | null; active?: boolean | null;
  mastery_unlocked?: boolean | null; percent_complete?: number | null; completes_at?: number | null };
export type ExportLabSlot = ExportItem & { slot: number; speed_up?: number | null; speed_up_remaining_seconds?: number | null };
export type ExportRun = { active?: boolean | null; tier?: number | null; wave?: number | null;
  observed_wave?: number | null; estimated_wave?: number | null; elapsed_seconds?: number | null;
  game_elapsed_seconds?: number | null; coins?: number | null; cells?: number | null; effective_rate_average?: number | null };
export type ExportBattle = { id: string; account_id: string; exported_at: number; battle_at?: number | null;
  tier?: number | null; wave?: number | null; elapsed_seconds?: number | null; game_elapsed_seconds?: number | null;
  coins?: number | null; cells?: number | null; killed_by?: string | number | null; is_tournament?: boolean | null;
  raw?: Record<string, unknown>; report?: Record<string, unknown> };
export type ExportJournalEntry = { id: string; account_id: string; kind: string; summary: string;
  observed_from?: number | null; observed_to?: number | null; occurred_at?: number | null; changes?: unknown };
export type GameExportProjection = { available: boolean; account_id: string | null; source: 'game_export';
  exported_at?: number | null; captured_at?: number | null; age_seconds?: number | null; stale?: boolean;
  source_online?: boolean; warnings?: string[]; reason?: string; error?: string; collection_error?: string; worker?: string; instance?: string;
  versions?: { export?: string | number | null; game?: string | number | null; data?: string | number | null };
  wallet?: Record<string, number | null>; labs?: { research: ExportItem[]; slots: ExportLabSlot[]; unlocked_raw?: unknown };
  workshop?: { upgrades: ExportItem[] }; cards?: { items: ExportItem[]; slots?: number | null };
  current_run?: ExportRun; runs?: ExportBattle[]; journal?: ExportJournalEntry[] };
export type GameExports = { accounts: GameExportProjection[]; generated_at?: number };
async function request<T>(path: string, headers: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', headers: { accept: 'application/json', ...headers }, signal });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Export unavailable (${response.status})`);
  return data as T;
}
function checkProjection(data: GameExportProjection, accountId?: string): void {
  if (data.source !== 'game_export') throw new Error('Official export source unavailable');
  if ((data.available && !data.account_id) || (accountId && data.account_id !== accountId))
    throw new Error('Export account changed; refresh account selection');
}
export async function fetchGameExport(context: ExportContext, signal?: AbortSignal): Promise<GameExportProjection> {
  if (!context.scope || !context.accountId) throw new Error('Select a verified account to view its export');
  const data = await request<GameExportProjection>('/api/game-export', { 'x-account-scope': context.scope,
    'x-expected-account-id': context.accountId }, signal);
  checkProjection(data, context.accountId);
  return data;
}
export async function fetchGameExports(signal?: AbortSignal): Promise<GameExports> {
  const data = await request<GameExports>('/api/game-exports', {}, signal);
  if (!Array.isArray(data.accounts)) throw new Error('Fleet export response unavailable');
  data.accounts.forEach(value => checkProjection(value));
  return data;
}
