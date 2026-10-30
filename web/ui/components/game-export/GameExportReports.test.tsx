import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FleetExportReports, AccountExportReport } from './GameExportReports';
import type { GameExportProjection } from '@/lib/gameExport';
const state = vi.hoisted(() => ({ accounts: [] as GameExportProjection[], error: null as string | null,
  selected: null as GameExportProjection | null }));
vi.mock('@/lib/useGameExport', () => ({ useGameExports: () => state,
  useGameExport: () => ({ data: state.selected, error: state.error }) }));
afterEach(() => { cleanup(); state.accounts = []; state.selected = null; state.error = null; });
it('withholds a previous account export when a worker receives a new account', () => {
  state.accounts = [{ available: true, source: 'game_export', worker: 'One', account_id: 'OLD', workshop: {
    upgrades: [{ index: 1, id: 'damage', name: 'Old account Damage', level: 999 }] } }];
  render(<FleetExportReports section="workshop" members={[{ name: 'One', account_id: 'NEW' }]} />);
  expect(screen.getByText(/No official game export/)).toBeInTheDocument();
  expect(screen.queryByText('Old account Damage')).not.toBeInTheDocument();
});
it('renders offline persisted exports for the matching account with source metadata', () => {
  state.accounts = [{ available: true, source: 'game_export', worker: 'One', account_id: 'A', source_online: false,
    exported_at: 1000, stale: true, age_seconds: 999, workshop: { upgrades: [{ index: 1, id: 'damage', name: 'Damage', level: 42 }] } }];
  render(<FleetExportReports section="workshop" members={[{ name: 'One', account_id: 'A' }]} />);
  expect(screen.getByText('Damage')).toBeInTheDocument();
  expect(screen.getByText('42')).toBeInTheDocument();
  expect(screen.getByText(/999s old.*Stale.*Source offline/)).toBeInTheDocument();
});
it('renders absent official evidence independently from operational APIs', () => {
  state.selected = { available: false, source: 'game_export', account_id: 'A' };
  render(<AccountExportReport context={{ scope: 'worker:one', accountId: 'A' }} section="cards" />);
  expect(screen.getByText(/No official game export/)).toBeInTheDocument();
});
