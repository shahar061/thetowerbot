import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError, activateStrategy, deleteStrategy, fetchControl, fetchErrors,
  fetchRunEvents, fetchRuns, fetchStats, fetchStatus, fetchStrategies,
  fetchStrategy, fetchUnknown, patchControl, saveStrategy, shutdown,
  startBot, stopBot,
  fetchAdvisor, importAdvisor, stageAdvisor, postCommand,
  fetchFleet, requestFleetProvision,
  fetchAccountWorkshopPurchases, fetchAccountRuns, fetchAccountRunPurchases,
} from "./api";
import type { Strategy } from "./types";
import { setAccountScope } from "./accountScope";

/** Every page test mocks `@/lib/api` wholesale and the Python suite tests the
 * routes from the server side, so nothing else in either suite ever asserts
 * that a fetcher points at the URL its server actually serves. A typo in one
 * of these paths would ship green on both. */

const fetchMock = vi.fn();

function respond(status: number, body: unknown): void {
  fetchMock.mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
}

const validStatus = {
  runtime: {
    api_version: 1,
    backend: { revision: "abc", source_hash: "backend-hash", started_at: 10 },
    frontend: { source_hash: "ui-hash", expected_backend_hash: "backend-hash", built_at: 20 },
    capabilities: ["control", "lifecycle", "strategies", "autopilot", "advisor"],
    profile: "default",
    device: { serial: "emulator-5554", game_version: null },
    readiness: { mode: "observing", reasons: [] },
  },
};

function allowWrite(result: unknown = {}): void {
  fetchMock
    .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(validStatus) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(result) });
}

/** The (url, init) the single fetch call was made with. */
function callArgs(): [string, RequestInit | undefined] {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  return fetchMock.mock.calls[0] as [string, RequestInit | undefined];
}

function writeArgs(): [string, RequestInit | undefined] {
  return fetchMock.mock.calls.at(-1) as [string, RequestInit | undefined];
}

const strategy: Strategy = {
  name: "default",
  actions: [],
  affordability: "digits",
  interval: 2,
  click_cooldown: 1,
  auto_navigate: false,
  max_runs: null,
  navigation_cooldown: 3,
  screen_confirmations: 2,
  tap_jitter_px: 8,
  timing_jitter: 0.15,
  tap_delay: 0.12,
  target_speed: null,
  shopping: {
    enabled: false,
    armed: false,
    visit_every_n_runs: 1,
    max_taps_per_visit: 40,
    workshop: [],
    cards: { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" },
  },
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  respond(200, {});
  vi.stubEnv("NEXT_PUBLIC_BACKEND_HASH", "backend-hash");
  vi.stubEnv("NEXT_PUBLIC_UI_HASH", "ui-hash");
});

describe("fleet routes", () => {
  it("reads each reroll worker's history independently of the top account selector", async () => {
    setAccountScope("worker:other");
    await fetchAccountWorkshopPurchases("worker:Air_1", 42);
    expect(callArgs()[0]).toBe("/api/ledger?kind=WORKSHOP_BUY&limit=100&before=42");
    expect((callArgs()[1]?.headers as Record<string, string>)["x-account-scope"])
      .toBe("worker:Air_1");

    fetchMock.mockClear();
    await fetchAccountRuns("worker:Air_1");
    expect(callArgs()[0]).toBe("/api/runs?limit=1");
    expect((callArgs()[1]?.headers as Record<string, string>)["x-account-scope"])
      .toBe("worker:Air_1");

    fetchMock.mockClear();
    await fetchAccountRunPurchases("worker:Air_1", 7);
    expect(callArgs()[0]).toBe("/api/runs/7/purchases");
    expect((callArgs()[1]?.headers as Record<string, string>)["x-account-scope"])
      .toBe("worker:Air_1");
    setAccountScope(null);
  });
  it("reads the fleet snapshot", async () => {
    await fetchFleet();
    expect(callArgs()[0]).toBe("/api/fleet");
  });
  it("requires the fleet capability before sending a clone request", async () => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(validStatus) });
    await expect(requestFleetProvision({ mode: "clone", source: "seed", count: 2,
      targets: ["seed_1", "seed_2"], state: "eligible" })).rejects.toMatchObject({ status: 412 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("advisor routes", () => {
  it("encodes the selected profile in a read-only snapshot request", async () => {
    await fetchAdvisor("profile a&b");
    expect(callArgs()[0]).toBe("/api/advisor?profile=profile%20a%26b");
  });
  it("posts normalized imports without altering their content", async () => {
    const body = { profile: "default", filename: "advisor.json", content: "{\n\"schema_version\":1\n}" };
    allowWrite(validStatus);
    await importAdvisor(body);
    const [url, init] = writeArgs();
    expect(url).toBe("/api/advisor/import");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });
  it("requests a draft transformation with the import revision", async () => {
    const body = { profile: "default", import_id: "revision", recommendation_id: "health", draft: strategy };
    allowWrite(validStatus);
    await stageAdvisor(body);
    const [url, init] = writeArgs();
    expect(url).toBe("/api/advisor/draft");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(body);
  });
});

afterEach(() => {
  setAccountScope(null);
  vi.unstubAllGlobals();
});

it("sends the chosen account with history reads", async () => {
  setAccountScope("worker:Tiramisu64_18");
  await fetchRuns();
  const [, init] = callArgs();
  expect((init?.headers as Record<string, string>)["x-account-scope"]).toBe("worker:Tiramisu64_18");
});

describe("read routes", () => {
  const reads: [string, () => Promise<unknown>, string][] = [
    ["fetchStatus", fetchStatus, "/api/status"],
    ["fetchRuns", () => fetchRuns(), "/api/runs?limit=30"],
    ["fetchRuns(5)", () => fetchRuns(5), "/api/runs?limit=5"],
    ["fetchRunEvents", () => fetchRunEvents(7), "/api/runs/7/events"],
    ["fetchUnknown", fetchUnknown, "/api/unknown"],
    ["fetchStats", fetchStats, "/api/stats"],
    ["fetchErrors", () => fetchErrors(), "/api/errors?limit=100"],
    ["fetchControl", fetchControl, "/api/control"],
    ["fetchStrategies", fetchStrategies, "/api/strategies"],
    ["fetchStrategy", () => fetchStrategy("crit"), "/api/strategies/crit"],
  ];

  it.each(reads)("%s GETs %s", async (_name, call, url) => {
    await call();
    const [got, init] = callArgs();
    expect(got).toBe(url);
    // No `method` at all is a GET; anything else here would be a bug.
    expect(init?.method).toBeUndefined();
  });

  it("percent-encodes a profile name into the path", async () => {
    // The server refuses such a name, but it must arrive as a path segment
    // rather than as an extra one.
    await fetchStrategy("a/b").catch(() => {});
    expect(callArgs()[0]).toBe("/api/strategies/a%2Fb");
  });
});

describe("write routes", () => {
  it("saveStrategy PUTs the whole profile under its name", async () => {
    fetchMock.mockReset();
    allowWrite(strategy);
    await saveStrategy("crit", strategy);
    const [url, init] = writeArgs();
    expect(url).toBe("/api/strategies/crit");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual(strategy);
  });

  it("activateStrategy POSTs to the profile's activate sub-route", async () => {
    fetchMock.mockReset();
    allowWrite({ active: "crit", names: ["crit"] });
    await activateStrategy("crit");
    const [url, init] = writeArgs();
    expect(url).toBe("/api/strategies/crit/activate");
    expect(init?.method).toBe("POST");
  });

  it("deleteStrategy DELETEs the profile", async () => {
    fetchMock.mockReset();
    allowWrite({ active: "default", names: ["default"] });
    await deleteStrategy("crit");
    const [url, init] = writeArgs();
    expect(url).toBe("/api/strategies/crit");
    expect(init?.method).toBe("DELETE");
  });

  it("patchControl PATCHes /api/control with the patch", async () => {
    fetchMock.mockReset();
    allowWrite({ paused: false, strategy, affordability_available: [] });
    await patchControl({ interval: 3 });
    const [url, init] = writeArgs();
    expect(url).toBe("/api/control");
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({ interval: 3 });
    expect(init?.headers).toMatchObject({
      "X-Tower-Api-Version": "1",
      "X-Tower-Backend-Hash": "backend-hash",
      "X-Tower-Ui-Hash": "ui-hash",
    });
  });

  const lifecycle: [string, () => Promise<unknown>, string][] = [
    ["startBot", startBot, "/api/bot/start"],
    ["stopBot", stopBot, "/api/bot/stop"],
    ["shutdown", shutdown, "/api/shutdown"],
  ];

  it.each(lifecycle)("%s POSTs %s", async (_name, call, url) => {
    fetchMock.mockReset();
    if (_name === "stopBot") respond(200, {}); else allowWrite({});
    await call();
    const [got, init] = writeArgs();
    expect(got).toBe(url);
    expect(init?.method).toBe("POST");
  });

  it("rechecks status before every ordinary mutation", async () => {
    fetchMock.mockReset();
    allowWrite({ queued: "speed_up" });
    await postCommand("speed_up");
    allowWrite({ queued: "speed_down" });
    await postCommand("speed_down");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/status", "/api/control/command", "/api/status", "/api/control/command",
    ]);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ cache: "no-store" });
  });

  it("blocks a mutation when runtime metadata is absent", async () => {
    fetchMock.mockReset();
    respond(200, {});
    const err = await startBot().catch((error: unknown) => error);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(412);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("blocks only mutations whose required capability is absent", async () => {
    fetchMock.mockReset();
    const withoutStrategies = { ...validStatus, runtime: { ...validStatus.runtime, capabilities: ["control"] } };
    respond(200, withoutStrategies);
    const err = await saveStrategy("crit", strategy).catch((error: unknown) => error);
    expect((err as ApiError).message).toMatch(/strategies capability/i);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps emergency pause and stop available without preflight", async () => {
    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({}) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({}) });
    await patchControl({ paused: true });
    await stopBot();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/control", "/api/bot/stop"]);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ paused: true });
  });

  it("omits the served UI identity header in explicit development mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEV_UI", "true");
    fetchMock.mockReset();
    const developmentStatus = { ...validStatus, runtime: { ...validStatus.runtime, frontend: { source_hash: null, expected_backend_hash: null, built_at: null } } };
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(developmentStatus) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({ queued: "speed_up" }) });
    await postCommand("speed_up");
    expect(writeArgs()[1]?.headers).not.toHaveProperty("X-Tower-Ui-Hash");
  });

  it("fails closed when the status preflight cannot be fetched", async () => {
    fetchMock.mockReset();
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    const err = await postCommand("speed_up").catch((error: unknown) => error);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(412);
    expect((err as ApiError).message).toMatch(/verify runtime compatibility/i);
  });
});

describe("failures", () => {
  it("a failed read carries the server's reason and its status", async () => {
    respond(422, { detail: "crit.json is not valid JSON" });
    const err = await fetchStrategy("crit").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).message).toBe("crit.json is not valid JSON");
  });

  it("a failed write flattens pydantic's structured detail into one line", async () => {
    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(validStatus) })
      .mockResolvedValueOnce({ ok: false, status: 422, json: () => Promise.resolve({ detail: [{ msg: "interval: too large" }, { msg: "name: bad" }] }) });
    const err = await saveStrategy("crit", strategy).catch((e: unknown) => e);
    expect((err as ApiError).message).toBe("interval: too large; name: bad");
  });

  it("falls back to method, path and status when there is no detail", async () => {
    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(validStatus) })
      .mockResolvedValueOnce({ ok: false, status: 500, json: () => Promise.resolve({}) });
    const err = await startBot().catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(500);
    expect((err as ApiError).message).toBe("POST /api/bot/start -> 500");
  });

  it("keeps 409 distinguishable from any other failure", async () => {
    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(validStatus) })
      .mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ detail: "the bot is already running" }) });
    const err = await startBot().catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(409);
  });
});
