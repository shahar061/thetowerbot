import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ assign: vi.fn(), saveSettings: vi.fn(), importProfile: vi.fn(), selected: null as null | { key: string; kind: string; instance: string; account_id: null }, contexts: [] as Array<{scope: string | null}> }));
vi.mock('@/lib/AccountSelection', () => ({ useAccountSelection: () => ({ selected: mocks.selected, choose: vi.fn() }) }));
vi.mock('@/lib/api', () => ({ fetchUpgrades: async () => [] }));
vi.mock('./StrategyStudio', () => ({ StrategyStudio: () => <div>Shared plan editor</div> }));
vi.mock('./BotSettings', () => ({ BotSettings: () => <div>Local timing controls</div> }));
vi.mock('@/lib/strategyClient', () => ({ createSingleStudioClient: (context: {scope: string | null}) => { mocks.contexts.push(context); return ({
  readLabs: async () => ({ workers: [], automated: [], reference: null }),
  readLibrary: async () => ({ revision: 0, templates: [], strategies: [] }),
  readAssignment: async () => ({ revision: 1, assignments: {} }),
  readStatus: async () => ({ state: 'pending', target_id: 'standalone', account_id: 'ACCOUNT-A',
    strategy_name: 'Opening', strategy_version: 1, revision: 1, generation: 'g1', epoch: 0 }),
  readSettings: async () => ({}), assign: mocks.assign,
  saveSettings: mocks.saveSettings, importProfile: mocks.importProfile,
}); } }));

test('one workspace separates plan, bot settings and applied state', async () => {
  const { StrategyWorkspace } = await import('./StrategyWorkspace');
  render(<StrategyWorkspace />);
  await waitFor(() => expect(screen.getByText('Shared plan editor')).toBeVisible());
  expect(screen.getByText(/^pending$/i)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Bot settings' }));
  expect(screen.getByText('Local timing controls')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Active strategy' }));
  expect(screen.getByText(/Waiting for the bot/)).toBeVisible();
  expect(mocks.assign).not.toHaveBeenCalled();
});


afterEach(() => { cleanup(); mocks.selected = null; mocks.contexts.length = 0; });

test('remote unattributed history never becomes an unscoped local request', async () => {
  mocks.selected = { key: 'unattributed:remote', kind: 'unattributed', instance: 'remote', account_id: null };
  const { StrategyWorkspace } = await import('./StrategyWorkspace');
  render(<StrategyWorkspace />);
  await waitFor(() => expect(screen.getByText('Shared plan editor')).toBeVisible());
  expect(mocks.contexts.length).toBeGreaterThan(0);
  expect(mocks.contexts.every(context => context.scope === 'unattributed:remote')).toBe(true);
});


test('overview cadence link opens the bot settings tab', async () => {
  window.history.replaceState(null, '', '/strategy/?tab=settings');
  try {
    const { StrategyWorkspace } = await import('./StrategyWorkspace');
    render(<StrategyWorkspace />);
    await waitFor(() => expect(screen.getByText('Local timing controls')).toBeVisible());
  } finally { window.history.replaceState(null, '', '/'); }
});
