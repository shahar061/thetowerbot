import { describe as group, expect, it } from "vitest";
import { clock, describe, money } from "./format";

group("money", () => {
  it("renders a dash for absent values", () => {
    expect(money(null)).toBe("-");
    expect(money(undefined)).toBe("-");
  });
  it("prefixes a dollar sign", () => {
    expect(money(1200)).toBe("$1200");
  });
});

group("clock", () => {
  it("renders unix seconds as a wall clock", () => {
    // 1970-01-01T00:00:05Z, formatted in whatever zone the test runs in, so
    // assert the shape rather than the digits.
    expect(clock(5)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});

group("describe", () => {
  it("distinguishes a verified battle purchase from a tap with the confirmed item and cost", () => {
    const line = describe({
      type: "BattlePurchased", seq: 1, ts: 0, item: "Defense Absolute",
      upgrade_id: "defense_absolute", price: 125, value: 42,
    });
    expect(line).toContain("Confirmed Defense Absolute");
    expect(line).toContain("$125");
    expect(line).toContain("value=42");
  });

  it("renders an autopilot decision with its phase, target and reason", () => {
    const line = describe({
      type: "AutopilotDecided", seq: 1, ts: 0, phase: "saving",
      upgrade_id: "damage", reason: "Saving cash for Damage; reserve protected",
    });
    expect(line).toBe("PLAN   saving damage Saving cash for Damage; reserve protected");
  });

  it("shows run purpose while keeping historical events without purpose readable", () => {
    expect(describe({ type: "RunStarted", seq: 1, ts: 0, run_id: 7, purpose: "milestone" })).toContain("milestone");
    expect(describe({ type: "RunStarted", seq: 2, ts: 0, run_id: 8 })).toContain("#8 started");
  });

  it("renders a tap with its score and price", () => {
    const line = describe({
      type: "Tapped", seq: 1, ts: 0, action: "Damage",
      x: 10, y: 20, score: 0.98123, price: 500, wallet: 900,
    });
    expect(line).toContain("TAP");
    expect(line).toContain("Damage");
    expect(line).toContain("0.981");
    expect(line).toContain("$500");
  });

  it("falls back to the bare type for an event it has never seen", () => {
    expect(describe({ type: "SomethingNew", seq: 2, ts: 0 } as never)).toBe("SomethingNew");
  });

  it("renders a screen change using `curr`", () => {
    const line = describe({
      type: "ScreenChanged", seq: 3, ts: 0,
      prev: "MENU", curr: "IN_RUN", confidence: 0.999, scores: {},
    });
    expect(line).toContain("MENU");
    expect(line).toContain("IN_RUN");
  });

  it("renders a control change with what moved and who moved it", () => {
    const line = describe({
      type: "ControlChanged", seq: 4, ts: 0,
      changed: { paused: true }, source: "web",
    });
    expect(line).toContain("paused=true");
    expect(line).toContain("web");
  });
});

group("splitEvent shopping events", () => {
  it("describes a purchase with what it cost and what it spent", () => {
    const line = describe({
      seq: 1, ts: 0, type: "Purchased", item: "Health", category: "DEFENSE",
      price: 75, coins_before: 1770, gems_before: null, dry_run: false,
    });

    expect(line).toContain("BUY");
    expect(line).toContain("Health");
    expect(line).toContain("75");
  });

  it("marks a rehearsal so it cannot be read as a real purchase", () => {
    const line = describe({
      seq: 1, ts: 0, type: "Purchased", item: "Health", category: "DEFENSE",
      price: 75, coins_before: 1770, gems_before: null, dry_run: true,
    });

    expect(line).toContain("rehearsal");
  });

  it("describes a skipped purchase with its reason", () => {
    const line = describe({
      seq: 1, ts: 0, type: "PurchaseSkipped", item: "Damage",
      reason: "unaffordable", detail: "", coins_before: 1770, gems_before: null,
    });

    expect(line).toContain("NOBUY");
    expect(line).toContain("unaffordable");
  });

  it("describes the end of a shopping visit", () => {
    const line = describe({
      seq: 1, ts: 0, type: "ShoppingEnded", visit: 3, bought: 2, spent: 95,
      aborted: false, reason: "",
    });

    expect(line).toContain("SHOP");
    expect(line).toContain("2");
    expect(line).toContain("95");
  });

  it("says a visit's spend is unknown rather than printing null", () => {
    const line = describe({
      seq: 1, ts: 0, type: "ShoppingEnded", visit: 3, bought: 1, spent: null,
      aborted: true, reason: "purchase acknowledgement was inconclusive",
    });

    expect(line).toContain("spent=unknown");
    expect(line).not.toContain("null");
  });
});

group("splitEvent speed events", () => {
  it("describes a policy nudge with where it was and where it is going", () => {
    const line = describe({
      seq: 1, ts: 0, type: "SpeedAdjusted", direction: "up",
      source: "policy", reading: 1, target: 2,
    });

    expect(line).toContain("SPEED");
    expect(line).toContain("up");
    expect(line).toContain("x1.0");
    expect(line).toContain("x2.0");
  });

  it("describes a manual nudge, which has no reading and no target", () => {
    // A dashboard press means "one step from wherever it is now", so this
    // path deliberately never read the widget. The line must not render
    // "x null" or claim a target nobody set.
    const line = describe({
      seq: 1, ts: 0, type: "SpeedAdjusted", direction: "down",
      source: "web", reading: null, target: null,
    });

    expect(line).toContain("SPEED");
    expect(line).toContain("down");
    expect(line).not.toContain("null");
  });
});
