import { expect, test } from "vitest";
import { formatUsd, parseUsdMicros } from "./recovery";

test("formats exact micro-dollar accounting without rounded inconsistencies", () => {
  expect(formatUsd(15_000)).toBe("$0.015");
  expect(formatUsd(985_000)).toBe("$0.985");
  expect(formatUsd(50_000)).toBe("$0.05");
  expect(formatUsd(1_000_000)).toBe("$1.00");
  expect(formatUsd(1)).toBe("$0.000001");
});

test("parses dollar inputs precisely to integer micro USD", () => {
  expect(parseUsdMicros("0.05")).toBe(50_000);
  expect(parseUsdMicros("1.00")).toBe(1_000_000);
  expect(parseUsdMicros("0.000001")).toBe(1);
  expect(parseUsdMicros("0.0000001")).toBeNull();
  expect(parseUsdMicros("1e-2")).toBeNull();
  expect(parseUsdMicros("$0.05")).toBeNull();
  expect(parseUsdMicros("0.051x")).toBeNull();
});
