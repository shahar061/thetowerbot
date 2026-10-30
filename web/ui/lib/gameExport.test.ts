import { describe, expect, it, vi, afterEach } from 'vitest';
import { fetchGameExport, fetchGameExports } from './gameExport';
afterEach(() => vi.unstubAllGlobals());
describe('official export reporting client', () => {
  it('pins the scope and expected account and preserves unavailable state', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: false, account_id: 'A', source: 'game_export' }) });
    vi.stubGlobal('fetch', fetcher);
    expect((await fetchGameExport({ scope: 'worker:one', accountId: 'A' })).available).toBe(false);
    const headers = fetcher.mock.calls[0][1].headers;
    expect(headers['x-account-scope']).toBe('worker:one');
    expect(headers['x-expected-account-id']).toBe('A');
  });
  it('rejects a replacement account response instead of displaying its observations', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ available: true, account_id: 'B', source: 'game_export' }) }));
    await expect(fetchGameExport({ scope: 'worker:one', accountId: 'A' })).rejects.toThrow(/account/i);
  });
  it('rejects fleet observations without an account binding or export source', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ accounts: [{ available: true, account_id: null, source: 'ocr' }] }) }));
    await expect(fetchGameExports()).rejects.toThrow(/account|source/i);
  });
});
