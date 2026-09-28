import { expect, test } from "vitest";
import { isFleetWorkspacePath, isRerollPath } from "./workspace";

test.each(["/fleet/reroll", "/fleet/reroll/", "/fleet/reroll/strategies/", "/fleet/reroll/progression/", "/fleet/reroll/history/"])("detects reroll route %s", pathname => {
  expect(isRerollPath(pathname)).toBe(true);
});
test.each(["/", "/strategy/", "/fleet/history/", "/fleet/rerolling/", "/fleet/reroll-other/"])("preserves single mode at %s", pathname => {
  expect(isRerollPath(pathname)).toBe(false);
});

test.each(["/fleet/state", "/fleet/state/", "/fleet/reroll/labs/"])("the fleet rail covers %s", pathname => {
  expect(isFleetWorkspacePath(pathname)).toBe(true);
});
test.each(["/", "/fleet/history/", "/fleet/stateful/"])("single mode keeps %s", pathname => {
  expect(isFleetWorkspacePath(pathname)).toBe(false);
});
