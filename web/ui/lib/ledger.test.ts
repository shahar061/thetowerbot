import { describe as group, expect, it } from "vitest";
import { currencyOptions, groupLines, sortCurrencies } from "./ledger";
import type { LedgerLine, LedgerPayload } from "./types";

function line(overrides: Partial<LedgerLine>): LedgerLine {
  return {
    id: 1, seq: 1, ts: 0, kind: "MISSION_CLAIM", item: "Kill 1,000 enemies",
    category: "MISSIONS", currency: "coins", delta: 120, price: null,
    balance_after: null, observed: null, dry_run: 0, run_id: null, visit: null,
    reason: null, detail: {},
    ...overrides,
  };
}

group("groupLines", () => {
  it("makes one entry per source event", () => {
    const entries = groupLines([
      line({ id: 20, seq: 501, currency: "gems", delta: 5 }),
      line({ id: 19, seq: 501, currency: "coins", delta: 120 }),
      line({ id: 18, seq: 500, item: "Defense Absolute", delta: -900 }),
    ]);

    expect(entries.map((e) => e.financial.length)).toEqual([2, 1]);
  });

  it("orders a bundle's currencies canonically, not by arrival", () => {
    // Newest-first paging delivers the gem line before the coin line; the
    // row must still read coins, gems - the same slot on every row.
    const [entry] = groupLines([
      line({ id: 3, seq: 9, currency: "stones", delta: 2 }),
      line({ id: 2, seq: 9, currency: "gems", delta: 10 }),
      line({ id: 1, seq: 9, currency: "coins", delta: 250 }),
    ]);

    expect(entry.financial.map((l) => l.currency)).toEqual(["coins", "gems", "stones"]);
  });

  it("keeps the order events arrived in", () => {
    const entries = groupLines([
      line({ id: 3, seq: 30 }), line({ id: 2, seq: 20 }), line({ id: 1, seq: 10 }),
    ]);

    expect(entries.map((e) => e.key)).toEqual(["seq-30", "seq-20", "seq-10"]);
  });

  it("never groups on a null seq", () => {
    const entries = groupLines([
      line({ id: 2, seq: null, kind: "UNEXPLAINED" }),
      line({ id: 1, seq: null, kind: "UNEXPLAINED" }),
    ]);

    expect(entries).toHaveLength(2);
  });

  it("gives a non-financial event no amounts rather than an unknown one", () => {
    const [entry] = groupLines([line({ kind: "VISIT_START", currency: null, delta: null })]);

    expect(entry.financial).toEqual([]);
  });

  it("marks the whole event a rehearsal when its lines are", () => {
    const [entry] = groupLines([
      line({ id: 2, seq: 7, currency: "gems", dry_run: 1 }),
      line({ id: 1, seq: 7, currency: "coins", dry_run: 1 }),
    ]);

    expect(entry.rehearsal).toBe(true);
  });
});

group("sortCurrencies", () => {
  it("puts known currencies in the fixed order and dedupes", () => {
    expect(sortCurrencies(["gems", "coins", "gems", "stones"])).toEqual(["coins", "gems", "stones"]);
  });

  it("keeps a currency it does not know, after the known ones", () => {
    // A versioned resource id is still a reward; dropping it would hide it.
    expect(sortCurrencies(["resource:foo@1", "coins", "bits"])).toEqual([
      "coins", "bits", "resource:foo@1",
    ]);
  });
});

group("currencyOptions", () => {
  const base: LedgerPayload = { lines: [], balances: {}, rehearsals: 0, next: null };

  it("includes what the history holds even when this page does not", () => {
    // Filtered to coins, the page holds only coin lines - but switching back
    // to stones must still be possible.
    const options = currencyOptions(
      { ...base, currencies: ["coins", "stones"], balances: { coins: 10 } },
      [line({ currency: "coins" })],
    );

    expect(options).toEqual(["coins", "gems", "stones"]);
  });

  it("falls back to the balanced pair for a payload that predates the list", () => {
    expect(currencyOptions({ ...base, balances: { coins: null, gems: null } }, [])).toEqual([
      "coins", "gems",
    ]);
  });
});
