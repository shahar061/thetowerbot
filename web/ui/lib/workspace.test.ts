import { expect, test } from "vitest";
import { isRerollPath } from "./workspace";

test.each(["/fleet/reroll", "/fleet/reroll/", "/fleet/reroll/strategies/", "/fleet/reroll/progression/", "/fleet/reroll/history/"])("detects reroll route %s", pathname => {
  expect(isRerollPath(pathname)).toBe(true);
});
test.each(["/", "/strategy/", "/fleet/history/", "/fleet/rerolling/", "/fleet/reroll-other/"])("preserves single mode at %s", pathname => {
  expect(isRerollPath(pathname)).toBe(false);
});
