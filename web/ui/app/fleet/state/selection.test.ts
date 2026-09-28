import { afterEach, expect, test, vi } from "vitest";
import { makeAccount } from "./fixtures";
import { DEFAULT_SELECTION, loadOpen, loadSelection, saveSelection, sectionOpen, toggleAccount, visibleAccounts } from "./selection";

const live = makeAccount({ id: "Air_1", online: true, bot: { screen: "IN_RUN", now: null, live: true } });
const menu = makeAccount({ id: "Air_2", online: true, bot: { screen: "MAIN_MENU", now: null, live: false } });
const gone = makeAccount({ id: "Air_3" });
const fleet = [live, menu, gone];

afterEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

test("all, only live, and a custom pick", () => {
  expect(visibleAccounts(fleet, DEFAULT_SELECTION)).toEqual(fleet);
  expect(visibleAccounts(fleet, { mode: "live", ids: [] })).toEqual([live]);
  const picked = toggleAccount(DEFAULT_SELECTION, "Air_2", fleet);
  expect(picked).toEqual({ mode: "custom", ids: ["Air_1", "Air_3"] });
  expect(toggleAccount(picked, "Air_2", fleet)).toEqual(DEFAULT_SELECTION);
});

test("the selection survives a reload and blocked storage falls back to all", () => {
  saveSelection({ mode: "custom", ids: ["Air_2"] });
  expect(loadSelection()).toEqual({ mode: "custom", ids: ["Air_2"] });
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  expect(loadSelection()).toEqual(DEFAULT_SELECTION);
  expect(loadOpen()).toEqual({});
  expect(() => saveSelection(DEFAULT_SELECTION)).not.toThrow();
});

test("cards and labs open by default only while one or two columns show", () => {
  expect(sectionOpen({}, "Air_1", "cards", 2)).toBe(true);
  expect(sectionOpen({}, "Air_1", "cards", 3)).toBe(false);
  expect(sectionOpen({}, "Air_1", "workshop", 4)).toBe(true);
  expect(sectionOpen({ "Air_1:cards": true }, "Air_1", "cards", 4)).toBe(true);
});
