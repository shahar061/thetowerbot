import { describe, expect, it } from "vitest";
import type { RunRow } from "@/lib/types";
import { killedBy, killedLabel, mergeRuns, recordTip, totalCoins } from "./runsView";

const row = (over: Partial<RunRow>): RunRow => ({
  id: 1, started_at: 0, ended_at: 100, wave: 10, coins: 50, tier: 1, abandoned: 0, scan_count: 0, tap_count: 0, ...over,
});
const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `acc-${name}` });

describe("mergeRuns", () => {
  it("orders all emulators' runs newest first, live runs by their start", () => {
    const runs = mergeRuns([
      { member: member("A"), runs: [row({ id: 1, ended_at: 100 }), row({ id: 2, ended_at: null, started_at: 500 })] },
      { member: member("B"), runs: [row({ id: 7, ended_at: 300 })] },
    ]);
    expect(runs.map(r => `${r.emulator}#${r.id}`)).toEqual(["A#2", "B#7", "A#1"]);
    expect(runs[1].accountKey).toBe("worker:B");
  });
});

describe("killedLabel", () => {
  it.each([
    [row({ ended_at: null }), "In progress"],
    [row({ abandoned: 1 }), "Abandoned"],
    [row({ killed_by: null }), "Unreadable"],
    [row({ killed_by: "Tank" }), "Tank"],
  ])("%#", (run, label) => expect(killedLabel(run)).toBe(label));
});

describe("killedBy", () => {
  it("counts only finished, read, non-abandoned runs and reports the rest as excluded", () => {
    const summary = killedBy([
      row({ killed_by: "Tank" }), row({ killed_by: "Tank" }), row({ killed_by: "Boss" }),
      row({ abandoned: 1 }), row({ killed_by: null }), row({ ended_at: null, killed_by: "Fast" }),
    ]);
    expect(summary.counts).toEqual([
      { name: "Tank", count: 2, share: 67 }, { name: "Boss", count: 1, share: 33 },
    ]);
    expect(summary.excluded).toBe(2);
  });
});

describe("recordTip and totalCoins", () => {
  it("names the beaten run and the breaker", () => {
    expect(recordTip("wave", row({ tier: 1, wave_record: "broken", wave_prev: { run_id: 3, value: 9 }, wave_broken_by: 8 })))
      .toBe("Wave record at T1 · beat #3 (9) · broken by #8");
    expect(recordTip("coin", row({ coin_record: "standing", coin_prev: null }))).toBe("Coin record · first run · still standing");
    expect(recordTip("wave", row({}))).toBeNull();
  });
  it("says just 'broken' when a broken record has no breaker id", () => {
    expect(recordTip("wave", row({ tier: 1, wave_record: "broken", wave_prev: { run_id: 3, value: 9 }, wave_broken_by: null })))
      .toBe("Wave record at T1 · beat #3 (9) · broken");
  });
  it("formats a large previous value with thousands separators", () => {
    expect(recordTip("coin", row({ coin_record: "standing", coin_prev: { run_id: 3, value: 12345 } })))
      .toBe("Coin record · beat #3 (12,345) · still standing");
  });
  it("prefers the server total and falls back to earned + ad", () => {
    expect(totalCoins(row({ total_coins: 90 }))).toBe(90);
    expect(totalCoins(row({ coins: 50, ad_coins: 10 }))).toBe(60);
    expect(totalCoins(row({ coins: null }))).toBeNull();
  });
});
