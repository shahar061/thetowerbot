import { fireEvent, render, screen, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import FleetStatePage from './page';
import type { GameExportProjection } from '@/lib/gameExport';
const state = vi.hoisted(() => ({ accounts: null as GameExportProjection[] | null, error: null as string | null }));
vi.mock('@/lib/useGameExport', () => ({ useGameExports: () => state }));
beforeEach(() => {
  state.accounts = null; state.error = null;
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const account = (worker: string, active = true): GameExportProjection => ({ worker, available: true, source: 'game_export', account_id: worker,
  exported_at: 1000, age_seconds: 20, stale: false, current_run: { active, wave: 52, observed_wave: 52, tier: 2 }, wallet: { coins: 12.5 } });
it('filters exported account columns by selection and active battle', () => {
  state.accounts = [account('Air_1'), account('Air_2', false)];
  render(<FleetStatePage />);
  expect(screen.getAllByRole('article')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Air_1' }));
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByRole('article')).toHaveAttribute('aria-label', 'Air_2');
  fireEvent.click(screen.getByRole('button', { name: 'All' }));
  fireEvent.click(screen.getByRole('button', { name: 'Only active battles' }));
  expect(screen.getByRole('article')).toHaveAttribute('aria-label', 'Air_1');
});
it('explains an absent export instead of displaying OCR state', () => {
  state.accounts = [{ worker: 'Air_1', available: false, source: 'game_export', account_id: 'A' }];
  render(<FleetStatePage />);
  expect(screen.getByText(/No official game export available/)).toBeInTheDocument();
  expect(screen.queryByText('Observed wave')).not.toBeInTheDocument();
});
it('reports source failure and suppresses any cached wave estimate', () => {
  state.error = 'collector unavailable';
  state.accounts = [{ ...account('Air_1'), current_run: { active: true, wave: 52, estimated_wave: 99 } }];
  render(<FleetStatePage />);
  expect(screen.getByRole('alert')).toHaveTextContent('collector unavailable');
  expect(screen.getByText(/Source offline/)).toBeInTheDocument();
  expect(screen.queryByText('99')).not.toBeInTheDocument();
});

it('remembers per-viewer tracker account selection after remount', () => {
  state.accounts = [account('Air_1'), account('Air_2')];
  const view = render(<FleetStatePage />);
  fireEvent.click(screen.getByRole('button', { name: 'Air_1' }));
  view.unmount();
  render(<FleetStatePage />);
  expect(screen.getByRole('article')).toHaveAttribute('aria-label', 'Air_2');
});
it('keeps the tracker compact while inventory and account history live on dedicated pages', () => {
  state.accounts = [{ ...account('Air_1'), wallet: { coins: 22967842.88683355, gems: 1190 },
    labs: { slots: [], research: [{ index: 1, id: 'damage', name: 'Large inventory lab', level: 12 }] },
    cards: { slots: 5, items: [{ index: 1, id: 'damage', name: 'Damage card' }] },
    journal: [{ id: 'one', account_id: 'Air_1', kind: 'snapshot_change', summary: 'Large journal sample' }] }];
  render(<FleetStatePage />);
  expect(screen.getByText('Observed wave')).toBeInTheDocument();
  expect(screen.getByText('22.97M')).toBeInTheDocument();
  expect(screen.getByTitle('22967842.88683355')).toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: 'Search exported inventory' })).not.toBeInTheDocument();
  expect(screen.queryByText('Large inventory lab')).not.toBeInTheDocument();
  expect(screen.queryByText('Damage card')).not.toBeInTheDocument();
  expect(screen.queryByText('Large journal sample')).not.toBeInTheDocument();
});
