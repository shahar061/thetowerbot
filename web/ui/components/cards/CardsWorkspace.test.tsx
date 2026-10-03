import { render, screen, fireEvent } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CardCollection } from "./CardCollection";
import { CardsWorkspace } from "./CardsWorkspace";
import type { CardsProjection } from "@/lib/cards";
const data = {
  account_id: "a",
  config_owner: "local",
  fresh: false,
  snapshot: null,
  program: null,
  policy: null,
  active_budget: null,
  preconditions: null,
  read_only_reason: "selected_account_not_running",
  capabilities: {
    inventory: false,
    buy_one: false,
    buy_ten: false,
    buy_slot: false,
    assign: false,
    reasons: { buy_ten: "unsupported_x10" },
  },
  decision: { kind: "wait", reason: "unknown" },
  recent_operations: [],
} as unknown as CardsProjection;
it("does not present an unobserved collection as empty", () => {
  render(<CardCollection items={[]} collectionComplete={false} />);
  expect(screen.getByText("Collection not fully observed")).toBeInTheDocument();
});
it("shows archive and unsupported reasons and keeps actions disabled", () => {
  render(
    <CardsWorkspace
      context={{ scope: "archive:a", accountId: "a", worker: null }}
      data={data}
      catalog={{ cards: [], max_gem_slots: 21 }}
      draft={null}
      saved={null}
      policy={null}
      pending={false}
      onChange={vi.fn()}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={vi.fn()}
      onStartCycle={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "Buy x10" })).toBeDisabled();
  expect(screen.getByText("unsupported_x10")).toBeInTheDocument();
  expect(screen.getByRole("status")).toBeInTheDocument();
});
it("requires explicit clear confirmation and never clears from an empty loadout", () => {
  const command = vi.fn();
  const live = {
    ...data,
    read_only_reason: null,
    preconditions: {},
    capabilities: { ...data.capabilities, assign: true },
  } as CardsProjection;
  render(
    <CardsWorkspace
      context={{ scope: null, accountId: "a", worker: null }}
      data={live}
      catalog={{ cards: [], max_gem_slots: 21 }}
      draft={null}
      saved={null}
      policy={null}
      pending={false}
      onChange={vi.fn()}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={command}
      onStartCycle={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Clear equipment" }));
  expect(command).not.toHaveBeenCalled();
  fireEvent.click(
    screen.getByRole("button", { name: "Confirm clear equipment" }),
  );
  expect(command).toHaveBeenCalledWith({ kind: "clear" });
});
it("offers guarded automation separately from program saves", () => {
  const automate = vi.fn();
  render(
    <CardsWorkspace
      context={{ scope: null, accountId: "a", worker: null }}
      data={
        {
          ...data,
          preconditions: {},
          read_only_reason: null,
        } as CardsProjection
      }
      catalog={{ cards: [], max_gem_slots: 21 }}
      draft={null}
      saved={null}
      policy={null}
      pending={false}
      onChange={vi.fn()}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={vi.fn()}
      onStartCycle={vi.fn()}
      automationEnabled={false}
      onAutomation={automate}
    />,
  );
  fireEvent.click(
    screen.getByRole("switch", { name: "Automatic Cards purchases" }),
  );
  expect(automate).toHaveBeenCalledWith(true);
});
it("edits ordered goals, levels, priority fallbacks and finite caps from server previews", () => {
  const change = vi.fn();
  const draft = {
    version: 1 as const,
    gem_cap: 100,
    selected_loadout_id: "farm",
    goals: [
      {
        id: "g",
        kind: "acquire" as const,
        targets: [{ card_id: "cards.damage", min_level: null }],
      },
      { id: "s", kind: "slots" as const, capacity: 2, when_usable_card: true },
    ],
    loadouts: [
      { id: "farm", name: "Farm", priority: ["cards.damage", "cards.health"] },
    ],
  };
  render(
    <CardsWorkspace
      context={{ scope: null, accountId: "a", worker: null }}
      data={
        {
          ...data,
          read_only_reason: null,
          preconditions: {},
        } as CardsProjection
      }
      catalog={{
        cards: [
          { card_id: "cards.damage", name: "Damage", max_level: 7 },
          { card_id: "cards.health", name: "Health", max_level: 7 },
        ],
        max_gem_slots: 21,
      }}
      draft={draft}
      saved={draft}
      policy={{ enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" }}
      preview={{
        fresh: true,
        goals: [{ id: "g", met: false }],
        loadouts: [
          {
            id: "farm",
            resolution: {
              desired: ["cards.health"],
              missing: ["cards.damage"],
              reason: null,
            },
          },
        ],
      }}
      pending={false}
      onChange={change}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={vi.fn()}
      onStartCycle={vi.fn()}
    />,
  );
  expect(screen.getByText("Server preview: Health")).toBeInTheDocument();
  expect(screen.getByText("Skipped: Damage")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Move goal 2 up" }));
  expect(
    change.mock.calls.at(-1)![0].goals.map((goal: { id: string }) => goal.id),
  ).toEqual(["s", "g"]);
  fireEvent.change(screen.getByLabelText("Target level for cards.damage"), {
    target: { value: "3" },
  });
  expect(change.mock.calls.at(-1)![0].goals[0].targets[0].min_level).toBe(3);
  fireEvent.click(screen.getByRole("button", { name: "Move Health up" }));
  expect(change.mock.calls.at(-1)![0].loadouts[0].priority).toEqual([
    "cards.health",
    "cards.damage",
  ]);
  const count = change.mock.calls.length;
  fireEvent.blur(screen.getByLabelText("Plan gem cap"), {
    target: { value: "9007199254740992" },
  });
  expect(change).toHaveBeenCalledTimes(count);
});
it("does not apply a saved selection while showing an unsaved loadout preview", () => {
  const onCommand = vi.fn();
  const saved = {
    version: 1 as const,
    gem_cap: 100,
    goals: [],
    loadouts: [
      { id: "saved", name: "Saved", priority: ["cards.damage"] },
      { id: "draft", name: "Draft", priority: ["cards.health"] },
    ],
    selected_loadout_id: "saved",
  };
  const draft = { ...saved, selected_loadout_id: "draft" };
  render(
    <CardsWorkspace
      context={{ scope: null, accountId: "a", worker: null }}
      data={
        {
          ...data,
          program: saved,
          read_only_reason: null,
          preconditions: {
            expected_program_revision: "saved-revision",
            expected_account_id: "a",
            expected_generation: "g",
            expected_epoch: 1,
          },
          capabilities: { ...data.capabilities, assign: true },
        } as CardsProjection
      }
      catalog={{ cards: [], max_gem_slots: 21 }}
      draft={draft}
      saved={saved}
      policy={null}
      pending={false}
      onChange={vi.fn()}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={onCommand}
      onStartCycle={vi.fn()}
    />,
  );
  const apply = screen.getByRole("button", { name: "Apply saved loadout" });
  expect(apply).toBeDisabled();
  expect(
    screen.getByText("Save or revert the draft before applying"),
  ).toBeInTheDocument();
  fireEvent.click(apply);
  expect(onCommand).not.toHaveBeenCalled();
});
it("filters targeted cards and shows known collection and tile observation times", () => {
  const item = (card_id: string) => ({
    card_id,
    ownership: "owned" as const,
    level: 1,
    copies: 0,
    copies_needed: 3,
    maxed: false,
    equipped: false,
    observed_at: 100,
  });
  const view = render(
    <CardCollection
      items={[item("cards.damage"), item("cards.health")]}
      collectionComplete={false}
      catalog={{
        cards: [
          { card_id: "cards.damage", name: "Damage", max_level: 7 },
          { card_id: "cards.health", name: "Health", max_level: 7 },
        ],
        max_gem_slots: 21,
      }}
      targetedIds={["cards.damage"]}
      observedAt={120}
    />,
  );
  fireEvent.change(screen.getByLabelText("Collection filter"), {
    target: { value: "targeted" },
  });
  expect(screen.getByRole("heading", { name: "Damage" })).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Health" }),
  ).not.toBeInTheDocument();
  expect(
    view.container.querySelector('time[datetime="1970-01-01T00:02:00.000Z"]'),
  ).toBeInTheDocument();
  expect(
    view.container.querySelector('time[datetime="1970-01-01T00:01:40.000Z"]'),
  ).toBeInTheDocument();
});
it("renders server-owned capacity exclusion and equipment changes", () => {
  const program = {
    version: 1 as const,
    gem_cap: 100,
    goals: [],
    loadouts: [
      { id: "farm", name: "Farm", priority: ["cards.damage", "cards.health"] },
    ],
    selected_loadout_id: "farm",
  };
  render(
    <CardsWorkspace
      context={{ scope: null, accountId: "a", worker: null }}
      data={data}
      catalog={{
        cards: [
          { card_id: "cards.damage", name: "Damage", max_level: 7 },
          { card_id: "cards.health", name: "Health", max_level: 7 },
        ],
        max_gem_slots: 21,
      }}
      draft={program}
      saved={program}
      policy={null}
      pending={false}
      onChange={vi.fn()}
      onPolicyChange={vi.fn()}
      onSave={vi.fn()}
      onRevert={vi.fn()}
      onCommand={vi.fn()}
      onStartCycle={vi.fn()}
      preview={{
        fresh: true,
        goals: [],
        loadouts: [
          {
            id: "farm",
            resolution: {
              desired: ["cards.damage"],
              missing: [],
              reason: null,
            },
            capacity: 1,
            capacity_limited: true,
            capacity_excluded: ["cards.health"],
            observed_equipped: ["cards.health"],
            additions: ["cards.damage"],
            removals: ["cards.health"],
          },
        ],
      }}
    />,
  );
  expect(screen.getByText(/Insufficient slots.*Health/)).toBeInTheDocument();
  expect(
    screen.getByText("Equipment changes — Add: Damage · Remove: Health"),
  ).toBeInTheDocument();
});
