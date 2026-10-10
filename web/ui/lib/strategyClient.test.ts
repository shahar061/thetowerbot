import { afterEach, expect, test, vi } from 'vitest';
import { setAccountScope } from './accountScope';

vi.mock('./api', () => ({ mutationHeaders: () => ({ 'X-Tower-Api-Version': '1' }),
  fetchHostStatus: async () => ({ runtime: { capabilities: ['strategy_studio'] } }) }));
vi.mock('./runtimeCompatibility', () => ({ checkRuntimeCompatibility: () => ({ compatible: true }) }));
afterEach(() => { vi.unstubAllGlobals(); setAccountScope(null); });

test('single assignment keeps the explicitly captured target', async () => {
  const { createSingleStudioClient } = await import('./strategyClient');
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ route: { revision: 1 }, status: 'pending' }) });
  vi.stubGlobal('fetch', fetcher);
  const client = createSingleStudioClient({ scope: 'worker:A', accountId: 'ACCOUNT-A',
    targetId: 'A', generation: 'g1', epoch: 0 });
  setAccountScope('worker:B');
  await client.assign('opening', 1, [{ worker: 'A', account_id: 'ACCOUNT-A' }], 0);
  const [, init] = fetcher.mock.calls[0];
  expect(init.headers['x-account-scope']).toBe('worker:A');
  expect(JSON.parse(init.body)).toMatchObject({ target_id: 'A', account_id: 'ACCOUNT-A' });
  await expect(client.assign('opening', 1, [{ worker: 'B', account_id: 'ACCOUNT-B' }], 0)).rejects.toThrow();
});
