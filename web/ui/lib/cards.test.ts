import { expect, it, vi, afterEach } from "vitest";
import {
  CardActionSession,
  safeInteger,
  fetchCards,
  type CardCommandRequest,
} from "./cards";
import { setAccountScope } from "./accountScope";
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  setAccountScope(null);
});
it("rejects unsafe integer budgets", () => {
  expect(safeInteger(Number.MAX_SAFE_INTEGER + 1)).toBe(false);
  expect(safeInteger(10)).toBe(true);
  expect(safeInteger(1.5)).toBe(false);
});
it("uses explicit account context despite global selection", async () => {
  setAccountScope("worker:other");
  const fetcher = vi.fn().mockResolvedValue(new Response("{}"));
  vi.stubGlobal("fetch", fetcher);
  await fetchCards({ scope: "worker:a", accountId: "a", worker: "one" });
  expect(
    new Headers(fetcher.mock.calls[0][1].headers).get("x-account-scope"),
  ).toBe("worker:a");
});
it("locks duplicates, retries exact original intent, invalidates on switch", () => {
  const session = new CardActionSession();
  const body = {
    kind: "refresh",
    idempotency_key: "original",
    expected_epoch: 1,
  } as CardCommandRequest;
  const pending = session.begin("a", body);
  expect(
    session.begin("a", { ...body, idempotency_key: "duplicate" }),
  ).toBeNull();
  session.failed(pending!);
  expect(session.retry("a")?.body).toEqual(body);
  session.switchAccount("b");
  expect(session.retry("b")).toBeNull();
  expect(session.isCurrent(pending!)).toBe(false);
});

it("strategy CAS transport preserves explicit scope and narrow Cards identity headers", async () => {
  const { fetchStrategyRevision, saveStrategyRevision } = await import("./api");
  vi.stubEnv("NEXT_PUBLIC_BACKEND_HASH", "backend");
  vi.stubEnv("NEXT_PUBLIC_UI_HASH", "ui");
  const status = {
    runtime: {
      api_version: 1,
      backend: { revision: "r", source_hash: "backend", started_at: 1 },
      frontend: {
        source_hash: "ui",
        expected_backend_hash: "backend",
        built_at: 2,
      },
      capabilities: ["control", "strategies"],
      profile: "default",
      device: { serial: null, game_version: null },
      readiness: { mode: "observing", reasons: [] },
    },
  };
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(
      new Response('{"name":"default"}', { headers: { etag: '"one"' } }),
    )
    .mockResolvedValueOnce(new Response(JSON.stringify(status)))
    .mockResolvedValueOnce(
      new Response('{"name":"default"}', { headers: { etag: '"two"' } }),
    );
  vi.stubGlobal("fetch", fetcher);
  const context = { scope: "worker:a", accountId: "a", worker: "one" };
  const loaded = await fetchStrategyRevision(context, "default");
  expect(loaded.etag).toBe('"one"');
  const pre = {
    expected_account_id: "a",
    expected_generation: "g",
    expected_epoch: 1,
    expected_program_revision: "r",
  };
  await saveStrategyRevision(
    context,
    "default",
    loaded.data,
    loaded.etag!,
    undefined,
    pre,
  );
  const headers = new Headers(fetcher.mock.calls[2][1].headers);
  expect(headers.get("if-match")).toBe('"one"');
  expect(JSON.parse(headers.get("x-cards-preconditions")!)).toEqual(pre);
  expect(headers.get("x-account-scope")).toBe("worker:a");
});
it("rejects a server budget beyond JavaScript integer precision before saving another edit", async () => {
  const { cardDraftError, emptyCardProgram } = await import("./cards");
  expect(
    cardDraftError(
      { ...emptyCardProgram(), gem_cap: Number.MAX_SAFE_INTEGER + 1 },
      null,
    ),
  ).toMatch(/safe integer/);
});

it("fleet reads ignore global selection and retain explicit account scope including lease", async () => {
  const { fetchFleetCards, fleetCardAuthority } = await import("./cards");
  setAccountScope("worker:unrelated");
  const fetcher = vi.fn().mockResolvedValue(new Response('{"revision":1,"accounts":[]}'));
  vi.stubGlobal("fetch", fetcher);
  await fetchFleetCards();
  expect(new Headers(fetcher.mock.calls[0][1].headers).get("x-account-scope")).toBeNull();
  const row = { account_id: "a", worker: "one", assignment: null, cards: { scope: { lease_id: "first" }, program: null } } as unknown as import("./cards").FleetCardsRow;
  expect(fleetCardAuthority(row)).not.toBe(fleetCardAuthority({ ...row, cards: { ...row.cards, scope: { account_id: "a", lease_id: "second", generation: "g", epoch: 1 } } }));
});
it("generic route drafts retain Cards overlays and program content when another lane changes", async () => {
  const { beginDraft, changeWorkshop } = await import("../app/fleet/reroll/strategies/RouteDraft");
  const program = { version: 1 as const, goals: [], loadouts: [], selected_loadout_id: null, gem_cap: 40 };
  const overlays = { a: { account_id: "a", worker: "one", strategy_id: "saved", strategy_version: 2, overlay_id: "immutable", published_revision: 3, program } };
  const document = { schema: 1, revision: 3, baseline: { workshop: { coin_spend_limit_pct: 100 }, cards: program }, card_assignments: overlays } as unknown as import("./buildRoute").BuildRouteDocument;
  const next = changeWorkshop(beginDraft(document), { coin_spend_limit_pct: 20 }).route;
  expect(JSON.parse(JSON.stringify(next)).card_assignments).toEqual(overlays);
  expect(next.baseline.cards).toEqual(program);
  expect(document.baseline.workshop.coin_spend_limit_pct).toBe(100);
});
