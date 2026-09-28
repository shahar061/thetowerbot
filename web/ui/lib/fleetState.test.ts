import { expect, test } from "vitest";
import { makeAccount } from "@/app/fleet/state/fixtures";
import { accountKey, reuseUnchanged } from "./fleetState";

test("an account keeps its previous object until its scan or reachability changes", () => {
  const before = [makeAccount({ id: "Air_1", scan: 5, online: true }), makeAccount({ id: "Air_2", scan: 9, online: true })];
  const after = reuseUnchanged(before, [makeAccount({ id: "Air_1", scan: 5, online: true }), makeAccount({ id: "Air_2", scan: 10, online: true })]);
  expect(after[0]).toBe(before[0]);
  expect(after[1]).not.toBe(before[1]);
  expect(after[1].scan).toBe(10);
  expect(reuseUnchanged(null, before)).toBe(before);
  expect(accountKey(makeAccount({ online: false, stale_seconds: 42 }))).not.toBe(accountKey(makeAccount({ online: false, stale_seconds: 44 })));
});
