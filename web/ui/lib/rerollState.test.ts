import { expect, test } from "vitest";
import { LADDER, attentionRank, decisionFor, ladderProgress, standingFor, STONES_WAVE } from "./rerollState";

test("a state this build has never heard of is surfaced, not greyed out", () => {
  const unknown = standingFor("warp_core_breach");
  // The failure mode this guards: a state added on the Python side lands in
  // the UI as an unstyled grey word and nobody notices the worker is stuck.
  expect(unknown.needsYou).toBe(true);
  expect(unknown.label).toBe("warp core breach");
  expect(attentionRank("warp_core_breach")).toBeLessThan(attentionRank("running"));
});

test("attention outranks life, and life outranks everything idle", () => {
  const order = ["identity_changed", "needs_choice", "running", "starting", "paused"];
  const ranks = order.map(attentionRank);
  expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
});

test("a transition is not something to act on", () => {
  expect(standingFor("capacity_wait").needsYou).toBe(false);
  expect(standingFor("capacity_wait").transient).toBe(true);
  expect(standingFor("paused").transient).toBe(false);
});

test("no verified run is not wave zero", () => {
  const unread = ladderProgress(null);
  expect(unread.wave).toBeNull();
  expect(ladderProgress(0).wave).toBe(0);
  // Both sit at the start of the ladder; only the first is allowed to render
  // as "unknown", which is why `wave` survives rather than being clamped.
  expect(unread.percent).toBe(0);
  expect(ladderProgress(0).percent).toBe(0);
});

test("the ladder's rungs are the planner's own build thresholds", () => {
  expect(ladderProgress(19).rung).toBe(0);
  expect(ladderProgress(20).rung).toBe(1);
  expect(ladderProgress(STONES_WAVE - 1).rung).toBe(1);
  expect(ladderProgress(STONES_WAVE).rung).toBe(2);
  expect(ladderProgress(STONES_WAVE).percent).toBe(100);
  expect(ladderProgress(STONES_WAVE).remaining).toBeNull();
  expect(ladderProgress(50).remaining).toBe(10);
  expect(LADDER.map((step) => step.id)).toEqual(["opening", "turtle", "stones"]);
});

test("progress never runs backwards or past the end", () => {
  const points = [null, 0, 5, 20, 40, 59, 60, 400].map((wave) => ladderProgress(wave).percent);
  expect(points).toEqual([...points].sort((a, b) => a - b));
  expect(Math.max(...points)).toBe(100);
});

test("the five decision states keep their separate remedies", () => {
  // save_coins and observe_price are both "not buying yet" and want
  // completely different things from the operator; if these ever collapse
  // into one sentence the plan strip stops being actionable.
  expect(decisionFor("save_coins").hint).not.toEqual(decisionFor("observe_price").hint);
  expect(decisionFor("buy").tone).toBe("live");
  expect(decisionFor("needs_operator").label).toBe("Needs you");
});

test("retirement states are labelled and ask for a person only when retiring failed", () => {
  expect(standingFor("retired")).toMatchObject({ label: "Retired", needsYou: false });
  expect(standingFor("retire_failed")).toMatchObject({ label: "Retire failed", needsYou: true, tone: "error" });
});
