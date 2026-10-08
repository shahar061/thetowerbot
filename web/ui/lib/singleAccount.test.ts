import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchSingleAccountState, saveLabSavings } from "./api";
import { setAccountScope } from "./accountScope";

const fetchMock = vi.fn();
const reply = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const runtime = {
  api_version: 1,
  backend: { revision: "abc", source_hash: "backend", started_at: 10 },
  frontend: { source_hash: "ui", expected_backend_hash: "backend", built_at: 20 },
  capabilities: ["account_savings"], profile: "default",
  device: { serial: null, game_version: null },
  readiness: { mode: "stopped", reasons: [] },
};
beforeEach(() => {
  fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_BACKEND_HASH", "backend");
  vi.stubEnv("NEXT_PUBLIC_UI_HASH", "ui");
  vi.stubEnv("NEXT_PUBLIC_DEV_UI", "false");
  setAccountScope("worker:single");
});
afterEach(() => { setAccountScope(null); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

test("single overview reads only the selected account scope", async () => {
  fetchMock.mockResolvedValue(reply({ account: null, account_id: "A", generated_at: "now" }));
  await fetchSingleAccountState();
  expect(fetchMock).toHaveBeenCalledWith("/api/account/state", expect.objectContaining({
    headers: expect.objectContaining({ "x-account-scope": "worker:single" }),
  }));
});

test("a savings write stays bound to its account when selection changes during preflight", async () => {
  fetchMock.mockImplementationOnce(async () => {
    setAccountScope("worker:other");
    return reply({ runtime });
  }).mockResolvedValueOnce(reply({ share_pct: 40 }));
  const input = { expected_account_id: "A", expected_revision: 3, share_mode: "save_pct" as const, share_pct: 40 };
  await saveLabSavings(input);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/status");
  expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("x-account-scope");
  expect(fetchMock.mock.calls[1]).toEqual(["/api/account/lab-savings", expect.objectContaining({
    method: "PUT", body: JSON.stringify(input),
    headers: expect.objectContaining({ "x-account-scope": "worker:single", "X-Tower-Backend-Hash": "backend" }),
  })]);
});

test("missing selection cannot send a savings mutation", async () => {
  setAccountScope(null);
  await expect(saveLabSavings({ expected_account_id: "A", expected_revision: 0, share_mode: "save_pct", share_pct: 30 }))
    .rejects.toThrow(/Select an account/);
  expect(fetchMock).not.toHaveBeenCalled();
});
