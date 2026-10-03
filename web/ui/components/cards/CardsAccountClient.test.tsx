import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CardsAccountClient } from "./CardsAccountClient";
import { ApiError } from "@/lib/api";
import type { CardsProjection } from "@/lib/cards";
const mocks = vi.hoisted(() => ({
  fetchCards: vi.fn(),
  fetchCardOperation: vi.fn(),
  fetchCardCatalog: vi.fn(),
  cardRequest: vi.fn(),
  previewCardProgram: vi.fn(),
  submitCardCommand: vi.fn(),
  startCardCycle: vi.fn(),
  fetchStrategyRevision: vi.fn(),
  saveStrategyRevision: vi.fn(),
}));
vi.mock("@/lib/cards", async (original) => ({
  ...(await original<typeof import("@/lib/cards")>()),
  ...Object.fromEntries(
    Object.entries(mocks).filter(([name]) => !name.includes("Strategy")),
  ),
}));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchStrategyRevision: mocks.fetchStrategyRevision,
  saveStrategyRevision: mocks.saveStrategyRevision,
}));
const program = {
  version: 1 as const,
  goals: [],
  loadouts: [],
  selected_loadout_id: null,
  gem_cap: 100,
};
const policy = { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" };
const strategy = {
  name: "default",
  cards: program,
  shopping: { enabled: false, armed: false, cards: policy, workshop: [] },
  labs: { keep: "preserved" },
};
const projection = {
  account_id: "a",
  config_owner: "local",
  program,
  program_revision: "rev",
  policy,
  active_budget: null,
  snapshot: null,
  fresh: false,
  preconditions: {
    expected_account_id: "a",
    expected_generation: "g",
    expected_epoch: 1,
    expected_program_revision: "rev",
  },
  read_only_reason: null,
  decision: { kind: "wait", reason: "disabled" },
  preview: null,
  capabilities: {
    inventory: true,
    buy_one: false,
    buy_ten: false,
    buy_slot: false,
    assign: false,
    reasons: {},
  },
  recent_operations: [],
} as unknown as CardsProjection;
afterEach(() => vi.restoreAllMocks());
const context = { scope: "worker:a", accountId: "a", worker: "one" };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchCards.mockResolvedValue(projection);
  mocks.fetchCardCatalog.mockResolvedValue({
    cards: [{ card_id: "cards.damage", name: "Damage", max_level: 7 }],
    max_gem_slots: 21,
  });
  mocks.cardRequest.mockResolvedValue({ data: { active: "default" } });
  mocks.fetchStrategyRevision.mockResolvedValue({
    data: strategy,
    etag: '"one"',
  });
  mocks.previewCardProgram.mockResolvedValue({
    fresh: false,
    goals: [],
    loadouts: [],
  });
});
it("refreshes without a cycle, locks double clicks and retries exact original request", async () => {
  mocks.submitCardCommand
    .mockRejectedValueOnce(new TypeError("Network interrupted"))
    .mockResolvedValueOnce({ operation_id: "op", status: "queued" });
  render(<CardsAccountClient context={context} />);
  await screen.findByRole("button", { name: "Refresh observations" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
  await screen.findByRole("button", { name: "Retry original request" });
  expect(mocks.submitCardCommand).toHaveBeenCalledTimes(1);
  const original = mocks.submitCardCommand.mock.calls[0][1];
  expect(original).toMatchObject({ kind: "refresh", expected_account_id: "a" });
  expect(original.budget_cycle_id).toBeUndefined();
  expect(mocks.startCardCycle).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "Retry original request" }),
  );
  await waitFor(() => expect(mocks.submitCardCommand).toHaveBeenCalledTimes(2));
  expect(mocks.submitCardCommand.mock.calls[1][1]).toEqual(original);
});
it("preserves draft on stale save, full unrelated fields and disabled automation", async () => {
  mocks.saveStrategyRevision.mockRejectedValue(new ApiError(409, "stale"));
  render(<CardsAccountClient context={context} />);
  const cap = await screen.findByLabelText("Plan gem cap");
  fireEvent.blur(cap, { target: { value: "80" } });
  fireEvent.click(screen.getByRole("button", { name: "Save Cards" }));
  await screen.findByText(/Your draft is preserved/);
  expect(screen.getByLabelText("Plan gem cap")).toHaveValue(80);
  const body = mocks.saveStrategyRevision.mock.calls[0][2];
  expect(body.labs).toEqual({ keep: "preserved" });
  expect(body.shopping.cards.enabled).toBe(false);
  expect(body.cards.gem_cap).toBe(80);
  expect(mocks.saveStrategyRevision.mock.calls[0][3]).toBe('"one"');
});
it("discards old account responses and pending retries on account switch", async () => {
  let resolve!: (value: unknown) => void;
  mocks.submitCardCommand.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const view = render(<CardsAccountClient context={context} />);
  await screen.findByRole("button", { name: "Refresh observations" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
  mocks.fetchCards.mockResolvedValue({
    ...projection,
    account_id: "b",
    read_only_reason: "archived",
    config_owner: "unavailable",
    preconditions: null,
  });
  view.rerender(
    <CardsAccountClient
      context={{ scope: "archive:b", accountId: "b", worker: null }}
    />,
  );
  await screen.findByText("Read only: archived");
  await act(async () => resolve({ operation_id: "old", status: "queued" }));
  expect(
    screen.queryByRole("button", { name: "Retry original request" }),
  ).not.toBeInTheDocument();
  expect(mocks.submitCardCommand).toHaveBeenCalledTimes(1);
});
it("edits raw policy and displays stricter effective reserve separately", async () => {
  mocks.fetchCards.mockResolvedValue({
    ...projection,
    policy: { ...policy, gem_floor: 250, max_per_visit: 1 },
  });
  render(<CardsAccountClient context={context} />);
  await screen.findByLabelText("Plan gem cap");
  expect(screen.getByLabelText("Gem reserve")).toHaveValue(40);
  expect(screen.getByText(/Effective reserve: 250/)).toBeInTheDocument();
});
it.each(["selection", "priority", "owner"])(
  "fences remote %s drift before Apply until explicit reload",
  async (change) => {
    let poll!: () => void;
    const timer = vi
      .spyOn(window, "setInterval")
      .mockImplementation((callback, delay) => {
        if (delay === 5000) poll = callback as () => void;
        return 0 as unknown as ReturnType<typeof window.setInterval>;
      });
    const initial = {
      ...program,
      selected_loadout_id: "farm",
      loadouts: [{ id: "farm", name: "Farm", priority: ["cards.damage"] }],
    };
    const next = {
      ...initial,
      selected_loadout_id: change === "selection" ? "boss" : "farm",
      loadouts: [
        {
          id: change === "selection" ? "boss" : "farm",
          name: "Changed",
          priority: ["cards.health"],
        },
      ],
    };
    const live = {
      ...projection,
      program: initial,
      capabilities: { ...projection.capabilities, assign: true },
    };
    mocks.fetchCards.mockResolvedValue(live);
    mocks.fetchStrategyRevision.mockResolvedValue({
      data: { ...strategy, cards: initial },
      etag: '"a"',
    });
    render(<CardsAccountClient context={context} />);
    await screen.findByLabelText("Selected loadout");
    const remote = {
      ...live,
      program: change === "owner" ? initial : next,
      config_owner: change === "owner" ? "fleet" : "local",
      program_revision: change === "owner" ? "rev" : "next",
      preconditions: {
        ...projection.preconditions!,
        expected_program_revision: change === "owner" ? "rev" : "next",
      },
    };
    mocks.fetchCards.mockResolvedValue(remote);
    mocks.fetchStrategyRevision.mockResolvedValue({
      data: { ...strategy, cards: remote.program },
      etag: '"b"',
    });
    await act(async () => poll());
    expect(screen.getByLabelText("Selected loadout")).toHaveValue("farm");
    expect(
      screen.getByRole("button", { name: "Apply saved loadout" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Reload latest plan" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Apply saved loadout" }),
      ).toBeEnabled(),
    );
    mocks.submitCardCommand.mockResolvedValue({
      operation_id: "op",
      status: "queued",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Apply saved loadout" }),
    );
    await waitFor(() =>
      expect(mocks.submitCardCommand).toHaveBeenCalledTimes(1),
    );
    expect(mocks.submitCardCommand.mock.calls[0][1]).toMatchObject({
      loadout_id: remote.program.selected_loadout_id,
      expected_program_revision: remote.program_revision,
    });
    timer.mockRestore();
  },
);
it("preserves dirty drafts on polling and restores raw batch editing without commands or enablement", async () => {
  let poll!: () => void;
  const timer = vi
    .spyOn(window, "setInterval")
    .mockImplementation((callback, delay) => {
      if (delay === 5000) poll = callback as () => void;
      return 0 as unknown as ReturnType<typeof window.setInterval>;
    });
  render(<CardsAccountClient context={context} />);
  await screen.findByLabelText("Plan gem cap");
  fireEvent.change(screen.getByLabelText("Batch preference"), {
    target: { value: "x10" },
  });
  mocks.saveStrategyRevision.mockRejectedValue(new ApiError(409, "changed"));
  fireEvent.click(screen.getByRole("button", { name: "Save Cards" }));
  await screen.findByText(/Your draft is preserved/);
  expect(
    mocks.saveStrategyRevision.mock.calls[0][2].shopping.cards,
  ).toMatchObject({ batch: "x10", enabled: false });
  expect(mocks.submitCardCommand).not.toHaveBeenCalled();
  fireEvent.blur(screen.getByLabelText("Plan gem cap"), {
    target: { value: "80" },
  });
  mocks.fetchCards.mockResolvedValue({
    ...projection,
    program_revision: "next",
    program: { ...program, gem_cap: 200 },
  });
  await act(async () => poll());
  expect(screen.getByLabelText("Plan gem cap")).toHaveValue(80);
  expect(screen.getByLabelText("Batch preference")).toHaveValue("x10");
  expect(screen.getByRole("button", { name: "Save Cards" })).toBeDisabled();
  timer.mockRestore();
});

it("reads evidence through the explicitly selected account operation endpoint", async () => {
  const operation = {
    operation_id: "proof",
    command: { kind: "refresh" },
    created_at: 100,
    status: "confirmed",
    reason: null,
    spent_gems: 0,
    rewards: [],
  };
  mocks.fetchCards.mockResolvedValue({
    ...projection,
    recent_operations: [operation],
  });
  mocks.fetchCardOperation.mockResolvedValue(operation);
  render(<CardsAccountClient context={context} />);
  fireEvent.click(
    await screen.findByRole("button", { name: "View recorded evidence" }),
  );
  await screen.findByRole("region", { name: "Recorded operation evidence" });
  expect(mocks.fetchCardOperation.mock.calls[0][0]).toMatchObject(context);
  expect(mocks.fetchCardOperation.mock.calls[0][1]).toBe("proof");
});
