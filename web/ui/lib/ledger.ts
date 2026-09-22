import type { LedgerLine, LedgerPayload } from "@/lib/types";

/** The event types that change the ledger. The page refetches when one
 *  arrives rather than deriving a line in the browser: the catalog and the
 *  balance arithmetic live in ledger.py, and a second implementation here
 *  would be a second thing to get wrong.
 *
 *  Mirrors `ledger._REPLAYABLE` exactly, and tests/test_ledger.py fails if
 *  the two drift. They already did once: this list stopped at the shopping
 *  events, so a mission or milestone claim only appeared after something
 *  unrelated happened to trigger a reload. */
export const LEDGER_EVENTS = new Set([
  "RunEnded",
  "Purchased",
  "PurchaseSkipped",
  "ShoppingStarted",
  "ShoppingEnded",
  "ShoppingUnavailable",
  "ControlChanged",
  "ClaimStarted",
  "MissionClaimed",
  "ClaimSkipped",
  "ClaimEnded",
  "MilestoneClaimed",
  "ClaimUncertain",
  "FloatingGemClaimed",
]);

/** Every kind `classify()` can emit - `ledger.KINDS` minus its six reserved
 *  entries, which nothing produces and which would be chips that can only
 *  ever return nothing. tests/test_ledger.py pins this against the Python
 *  list: the claim kinds were missing for exactly as long as nothing did,
 *  so the multi-currency rows were the ones you could not filter to.
 *
 *  Ordered by what a reader is looking for - money in, money out, things
 *  that did not happen, then bookkeeping - not by the Python declaration. */
export const FILTER_KINDS = [
  "RUN_PAYOUT",
  "MISSION_CLAIM",
  "MILESTONE_CLAIM",
  "GEM_CLAIM",
  "WORKSHOP_BUY",
  "CARD_BUY",
  "BUY_SKIPPED",
  "CLAIM_SKIPPED",
  "CLAIM_UNCERTAIN",
  "VISIT_START",
  "VISIT_END",
  "SHOP_UNAVAILABLE",
  "POLICY_CHANGED",
  "UNEXPLAINED",
] as const;

/** The order currencies always appear in - in a bundle's amount cell, the
 *  filter and the header. Fixed rather than by size or recency so the same
 *  currency sits in the same slot on every row and the eye can run down a
 *  column. `cash` is deliberately absent: it is in-run money, and the
 *  ledger excludes it by design. */
export const CURRENCY_ORDER = [
  "coins", "gems", "stones", "medals", "cells", "keys", "shards", "tickets", "bits",
] as const;

/** What the writer balances when the route predates saying so. */
const DEFAULT_BALANCED = ["coins", "gems"] as const;

const rank = (currency: string) => {
  const at = (CURRENCY_ORDER as readonly string[]).indexOf(currency);
  return at === -1 ? CURRENCY_ORDER.length : at;
};

/** Dedupe, then canonical order. A currency this list does not know - a
 *  versioned resource id, or one added to currencies.py later - sorts after
 *  the known ones, alphabetically, rather than being dropped: a reward the
 *  page cannot name is still a reward. */
export function sortCurrencies(currencies: Iterable<string>): string[] {
  return [...new Set(currencies)].sort(
    (a, b) => rank(a) - rank(b) || a.localeCompare(b),
  );
}

/** One source event and every line it produced. */
export interface LedgerEntry {
  key: string;
  /** The first line - kind, item, time and note are shared by every line of
   *  one event, so any of them would do; the first is simply stable. */
  head: LedgerLine;
  /** The lines that moved a currency, in CURRENCY_ORDER. Empty for a visit
   *  boundary or a policy change, which have no financial character. */
  financial: LedgerLine[];
  /** One dry-run line makes the whole event a rehearsal: its lines all came
   *  from the same event, so they cannot disagree. */
  rehearsal: boolean;
}

/** Collapse ledger lines into one entry per source event.
 *
 *  The ledger stores one line PER CURRENCY - a mission paying coins and gems
 *  is two lines sharing a `seq` - which is right for the running balances
 *  and reconciliation, and wrong to show: two unrelated-looking rows with
 *  nothing tying them together but a timestamp. So storage stays per
 *  currency and this is where it becomes per event.
 *
 *  Run over the ACCUMULATED lines, never one page at a time: "Load more" can
 *  land an event's gem line on the page after its coin line, and grouping
 *  per page would show two half-rewards.
 *
 *  A null `seq` is never a key. Derived UNEXPLAINED lines have none, and
 *  grouping on null would fold every one of them into a single row. */
export function groupLines(lines: readonly LedgerLine[]): LedgerEntry[] {
  const entries: LedgerEntry[] = [];
  const bySeq = new Map<number, { entry: LedgerEntry; lines: LedgerLine[] }>();

  for (const line of lines) {
    const existing = line.seq === null ? undefined : bySeq.get(line.seq);
    if (existing) {
      existing.lines.push(line);
      continue;
    }
    const entry: LedgerEntry = {
      key: line.seq === null ? `line-${line.id}` : `seq-${line.seq}`,
      head: line,
      financial: [],
      rehearsal: false,
    };
    entries.push(entry);
    if (line.seq !== null) bySeq.set(line.seq, { entry, lines: [line] });
    else finish(entry, [line]);
  }
  for (const { entry, lines: grouped } of bySeq.values()) finish(entry, grouped);
  return entries;
}

function finish(entry: LedgerEntry, lines: LedgerLine[]) {
  entry.financial = lines
    .filter((line) => line.currency !== null)
    .sort((a, b) => rank(a.currency!) - rank(b.currency!) || a.currency!.localeCompare(b.currency!));
  entry.rehearsal = lines.some((line) => Boolean(line.dry_run));
}

/** The currencies to offer as filters and show in the header.
 *
 *  The union of everything that can name one: the route's list of what the
 *  history holds, the balances it reported, the balanced set, and whatever
 *  is on screen. A union rather than any single source, so a currency never
 *  vanishes from the filter because the current page happens not to contain
 *  it - which would make the active filter impossible to switch back off. */
export function currencyOptions(
  payload: LedgerPayload | null,
  lines: readonly LedgerLine[],
): string[] {
  return sortCurrencies([
    ...(payload?.currencies ?? []),
    ...Object.keys(payload?.balances ?? {}),
    ...balancedCurrencies(payload),
    ...lines.flatMap((line) => (line.currency === null ? [] : [line.currency])),
  ]);
}

/** Currencies with a running balance at all. A null balance for one of these
 *  means "not read yet"; for any other currency it means "never tracked". */
export function balancedCurrencies(payload: LedgerPayload | null): readonly string[] {
  return payload?.balanced ?? DEFAULT_BALANCED;
}
