import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useGameExport } from './useGameExport';
const requests = vi.hoisted(() => ({ selected: vi.fn(), fleet: vi.fn() }));
vi.mock('./gameExport', () => ({ fetchGameExport: requests.selected, fetchGameExports: requests.fleet }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('immediately hides the former account and ignores its late response after selection changes', async () => {
  const resolve: Record<string, (data: unknown) => void> = {};
  requests.selected.mockImplementation((context: { accountId: string }) => new Promise(done => { resolve[context.accountId] = done; }));
  const view = renderHook(({ accountId }) => useGameExport({ scope: 'worker:one', accountId }), { initialProps: { accountId: 'A' } });
  await act(async () => resolve.A({ account_id: 'A', available: true, source: 'game_export' }));
  expect(view.result.current.data?.account_id).toBe('A');
  view.rerender({ accountId: 'B' });
  expect(view.result.current.data).toBeNull();
  await act(async () => resolve.B({ account_id: 'B', available: true, source: 'game_export' }));
  expect(view.result.current.data?.account_id).toBe('B');
});
it('never lets a pending previous account populate the current account', async () => {
  let oldResolve!: (data: unknown) => void;
  requests.selected.mockImplementation((context: { accountId: string }) => context.accountId === 'A'
    ? new Promise(done => { oldResolve = done; }) : Promise.resolve({ account_id: 'B', available: false, source: 'game_export' }));
  const view = renderHook(({ accountId }) => useGameExport({ scope: 'worker:one', accountId }), { initialProps: { accountId: 'A' } });
  view.rerender({ accountId: 'B' });
  await waitFor(() => expect(view.result.current.data?.account_id).toBe('B'));
  await act(async () => oldResolve({ account_id: 'A', available: true, source: 'game_export' }));
  expect(view.result.current.data?.account_id).toBe('B');
});
