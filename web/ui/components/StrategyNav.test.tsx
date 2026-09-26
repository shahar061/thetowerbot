import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StrategyNav } from "./StrategyNav";
import type { Strategy } from "@/lib/types";

const strategy: Strategy = {
  name: "default",
  actions: [],
  affordability: "digits",
  interval: 2,
  menu_interval: 2,
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
  claims: { enabled: false, missions_every_hours: 8, milestones_on_new_best: true },
};

const claimsLink = () => screen.getByRole("link", { name: /claims/i });

describe("StrategyNav", () => {
  it("lists the Claims card, so the section is reachable from the map", () => {
    render(<StrategyNav draft={strategy} saved={strategy} />);
    expect(claimsLink().getAttribute("href")).toBe("#claims");
  });

  it("reads off when the cadence is not running", () => {
    render(<StrategyNav draft={strategy} saved={strategy} />);
    expect(claimsLink().textContent).toContain("off");
  });

  it("reads on when the cadence is running", () => {
    const on: Strategy = { ...strategy, claims: { ...strategy.claims!, enabled: true } };
    render(<StrategyNav draft={on} saved={on} />);
    expect(claimsLink().textContent).toContain("on");
  });

  it("dots Claims when the draft's cadence differs from what was saved", () => {
    // The dot is what tells the reader WHICH card holds the unsaved edit -
    // a claims change that never dots is an edit the map denies exists.
    const draft: Strategy = {
      ...strategy,
      claims: { ...strategy.claims!, missions_every_hours: 12 },
    };
    render(<StrategyNav draft={draft} saved={strategy} />);
    expect(
      claimsLink().querySelector("[aria-label='unsaved changes in this section']"),
    ).not.toBeNull();
  });

  it("leaves Claims undotted when only another section moved", () => {
    const draft: Strategy = { ...strategy, interval: 5 };
    render(<StrategyNav draft={draft} saved={strategy} />);
    expect(
      claimsLink().querySelector("[aria-label='unsaved changes in this section']"),
    ).toBeNull();
  });
});
