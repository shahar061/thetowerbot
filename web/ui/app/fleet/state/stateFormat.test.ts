import { expect, test } from "vitest";
import { agoText, amount, priceText, secondsUntil, span, whole } from "./stateFormat";

test("null renders as a dash or 'price unknown', never as a number", () => {
  expect(amount(null)).toBe("—");
  expect(amount(undefined)).toBe("—");
  expect(amount(0)).toBe("0");
  expect(amount(8.42e9)).toBe("8.42B");
  expect(priceText(null)).toBe("price unknown");
  expect(priceText(1.2e8)).toBe("120.00M");
  expect(whole(null)).toBe("—");
  expect(whole(18442)).toBe("18,442");
});

test("durations and times", () => {
  expect(span(5780)).toBe("1h 36m");
  expect(span(245)).toBe("4m 05s");
  expect(span(42)).toBe("42s");
  expect(span(null)).toBe("—");
  const now = Date.parse("2026-09-28T10:00:00Z");
  expect(secondsUntil("2026-09-28T10:01:00Z", now)).toBe(60);
  expect(secondsUntil("2026-09-28T09:59:00Z", now)).toBe(0);
  expect(agoText("2026-09-28T09:55:00Z", now)).toBe("5m ago");
  expect(agoText(null, now)).toBe("—");
});
