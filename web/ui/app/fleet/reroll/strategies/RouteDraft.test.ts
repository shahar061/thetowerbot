import { expect, test } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { beginDraft, patchWorkshop } from "./RouteDraft";

const route: BuildRouteDocument = {
  schema: 1, revision: 1, authored_at: null, dependencies: {},
  baseline: {
    workshop: { id: "workshop", mode: "legacy_planner", priority_ids: [], banned_upgrade_ids: [], coin_spend_limit_pct: 100, draw_chance_pct: 0, weights: {} },
    battle: { mode: "legacy_policy", branches: [] },
    gems: { lab_slot2_reserve: 0, spend_limit_pct: 100, steps: [] },
    labs: { slot1_research: "health", steps: [] },
  },
  overrides: { Solo: { account_id: "A", patches: {}, lab_share: { mode: "save_pct", pct: 40 } } },
};
test("editing workshop preserves the same account's lab savings override", () => {
  const changed = patchWorkshop(beginDraft(route), "Solo", "A", { coin_spend_limit_pct: 80 });
  expect(changed.route.overrides.Solo.lab_share).toEqual({ mode: "save_pct", pct: 40 });
  expect(changed.route.overrides.Solo.patches.workshop).toEqual({ coin_spend_limit_pct: 80 });
});
test("a replacement account does not inherit the former account's lab savings", () => {
  const changed = patchWorkshop(beginDraft(route), "Solo", "B", { coin_spend_limit_pct: 80 });
  expect(changed.route.overrides.Solo.lab_share).toBeUndefined();
});
