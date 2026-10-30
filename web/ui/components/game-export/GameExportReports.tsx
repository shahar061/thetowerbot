'use client';
import { useAccountSelection } from '@/lib/AccountSelection';
import { useGameExport, useGameExports } from '@/lib/useGameExport';
import type { ExportContext, GameExportProjection } from '@/lib/gameExport';
import { GameExportPanel, type ExportSection } from './GameExportPanel';
export function AccountExportReport({ context, section = 'all' }: { context: ExportContext; section?: ExportSection }): React.JSX.Element {
  const { data, error } = useGameExport(context);
  return <div className="space-y-3">{error && <p role="alert" className="text-sm text-danger">Official export unavailable: {error}</p>}
    {data ? <GameExportPanel data={error ? { ...data, source_online: false } : data} section={section} />
      : <p role="status" className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">{context.scope && context.accountId ? error ? 'No official game export available. Account observations are unknown.' : 'Loading official game export…' : 'Select a verified account to view official game export observations.'}</p>}</div>;
}
export function SelectedExportReport({ section = 'all' }: { section?: ExportSection }): React.JSX.Element {
  const { selected } = useAccountSelection();
  return <AccountExportReport context={{ scope: selected?.key ?? null, accountId: selected?.account_id ?? null }} section={section} />;
}
export type ExportMember = { name: string; account_id?: string | null; account_key?: string | null };
export function matchExportAccount(data: GameExportProjection, member: ExportMember): boolean {
  return !!member.account_id && data.account_id === member.account_id && (data.worker ?? data.instance) === member.name;
}
export function FleetExportReports({ section = 'all', members, focusWorker }: { section?: ExportSection;
  members?: ExportMember[]; focusWorker?: string | null }): React.JSX.Element {
  const { accounts, error } = useGameExports();
  const filtered: { member: ExportMember; account: GameExportProjection | undefined }[] = members ? members.filter(member => !focusWorker || focusWorker === member.name).map(member => {
    const account = accounts?.find(data => matchExportAccount(data, member));
    return { member, account };
  }) : (accounts ?? []).filter(data => !focusWorker || (data.worker ?? data.instance) === focusWorker)
    .map(account => ({ member: { name: account.worker ?? account.instance ?? account.account_id ?? 'Unverified account' }, account }));
  return <div className="space-y-4">{error && <p role="alert" className="text-sm text-danger">Official fleet exports unavailable: {error}</p>}
    {!accounts && !error && <p role="status">Loading official fleet exports…</p>}
    {accounts && filtered.length === 0 && <p role="status" className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">No verified account exports available.</p>}
    {filtered.map(({ member, account }) => <article aria-label={member.name} key={`${member.name}:${account?.account_id ?? member.account_id ?? ''}`} className="space-y-2"><h2 className="font-medium">{member.name}</h2>
      <GameExportPanel data={account ? error ? { ...account, source_online: false } : account : { available: false, source: 'game_export', account_id: member.account_id ?? null }} section={section} /></article>)}
  </div>;
}
