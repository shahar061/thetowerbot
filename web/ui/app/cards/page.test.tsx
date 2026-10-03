import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import CardsPage from "./page";
import type { AccountChoice } from "@/lib/api";

const selection = vi.hoisted(() => ({ selected: null as AccountChoice | null, loading: false }));
vi.mock("@/lib/AccountSelection", () => ({ useAccountSelection: () => selection }));
const account = (id: string, archive = false): AccountChoice => ({
  key: `${archive ? "archive" : "worker"}:${id}`, account_id: id,
  instance: archive ? null : `worker-${id}`, kind: archive ? "archive" : "worker",
} as AccountChoice);
const projection = (id: string, archive = false) => ({
  account_id: id, config_owner: archive ? "unavailable" : "fleet", snapshot: null,
  fresh: false, program: { version: 1, goals: [], loadouts: [], selected_loadout_id: null, gem_cap: 100 },
  program_revision: "rev", policy: { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" },
  active_budget: null, preview: null, decision: { kind: "wait", reason: "disabled" },
  preconditions: archive ? null : { expected_account_id: id, expected_generation: "g", expected_epoch: 1, expected_program_revision: "rev" },
  capabilities: { inventory: true, buy_one: false, buy_ten: false, buy_slot: false, assign: false, reasons: {} },
  recent_operations: [], read_only_reason: archive ? "archived" : null,
});
const json = (value: unknown): Response => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
beforeEach(() => {
  selection.loading = false;
  selection.selected = null;
  vi.stubEnv("NEXT_PUBLIC_BACKEND_HASH", "backend");
  vi.stubEnv("NEXT_PUBLIC_UI_HASH", "ui");
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it("shows loading and empty selection without making account requests", () => {
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  selection.loading = true;
  const view = render(<CardsPage />);
  expect(screen.getByText("Loading account…")).toBeInTheDocument();
  selection.loading = false; view.rerender(<CardsPage />);
  expect(screen.getByText("Select an account to view Cards.")).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});

it("aborts a pending old-account request and renders the new archived account through real clients", async () => {
  let resolve!: (response: Response) => void;
  let commandSignal: AbortSignal | undefined;
  const commands: { body: Record<string, unknown>; headers: Headers }[] = [];
  const fetcher = vi.fn(async (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    if (path === "/api/cards") {
      const archived = headers.get("x-account-scope") === "archive:b";
      return json(projection(archived ? "b" : "a", archived));
    }
    if (path === "/api/cards/catalog") return json({ cards: [], max_gem_slots: 21 });
    if (path === "/api/status") return json({ runtime: {
      api_version: 1, backend: { revision: "r", source_hash: "backend", started_at: 1 },
      frontend: { source_hash: "ui", expected_backend_hash: "backend", built_at: 2 },
      capabilities: ["control", "strategies"], profile: "default",
      device: { serial: null, game_version: null }, readiness: { mode: "observing", reasons: [] },
    } });
    if (path === "/api/cards/commands") {
      commandSignal = init.signal as AbortSignal;
      commands.push({ body: JSON.parse(init.body as string), headers });
      return new Promise<Response>(done => { resolve = done; });
    }
    throw new Error(`Unexpected fixture request: ${path}`);
  });
  vi.stubGlobal("fetch", fetcher);
  selection.selected = account("a");
  const view = render(<CardsPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Refresh observations" }));
  await waitFor(() => expect(commands).toHaveLength(1));
  expect(commands[0].body).toMatchObject({ kind: "refresh", expected_account_id: "a" });
  expect(commands[0].headers.get("x-account-scope")).toBe("worker:a");
  selection.selected = account("b", true); view.rerender(<CardsPage />);
  await screen.findByText("Read only: archived");
  expect(commandSignal?.aborted).toBe(true);
  await act(async () => resolve(json({ operation_id: "old-operation", status: "queued" })));
  expect(screen.queryByText(/old-operation/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry original request" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh observations" })).toBeDisabled();
  expect(commands).toHaveLength(1);
});
