import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import type { RerollMember } from '@/lib/fleet';
import { AccountInspector } from './AccountInspector';
vi.mock('./Purchases', () => ({ WorkerBattlePurchases: () => <p>Battle action evidence</p>, SharedWorkshopLedger: () => <p>Scoped ledger</p> }));
const member: RerollMember = { name: 'Air_1', endpoint: 'e', lease_id: 'lease', state: 'running', account_key: 'worker:Air_1', account_id: '100' };
afterEach(() => vi.unstubAllGlobals());
const response = (accountId: string, available = false): Response => new Response(JSON.stringify({ available,
  source: 'game_export', account_id: accountId, workshop: { upgrades: [{ index: 1, name: 'Damage', id: 'damage', level: 42 }] } }), { headers: { 'content-type': 'application/json' } });
test('workshop requests official export with explicit account and scope instead of OCR snapshots', async () => {
  const fetcher = vi.fn().mockResolvedValue(response('100', true));
  vi.stubGlobal('fetch', fetcher);
  render(<AccountInspector member={member} onClose={() => {}}><p>Overview details</p></AccountInspector>);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('tab', { name: 'Workshop' }));
  expect(await screen.findByText('Damage')).toBeInTheDocument();
  expect(screen.getByText('42')).toBeInTheDocument();
  expect(fetcher.mock.calls[0][0]).toBe('/api/game-export');
  expect(fetcher.mock.calls[0][1].headers).toMatchObject({ 'x-account-scope': 'worker:Air_1', 'x-expected-account-id': '100' });
});
test('a mismatched export account is rejected and its values stay hidden', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response('another', true)));
  render(<AccountInspector member={member} onClose={() => {}}><p>Overview details</p></AccountInspector>);
  fireEvent.click(screen.getByRole('tab', { name: 'Workshop' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Export account changed');
  expect(screen.queryByText('42')).not.toBeInTheDocument();
});
