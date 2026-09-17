"use client";

import { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchLedger } from "@/lib/api";
import { clock } from "@/lib/format";
import type { LedgerLine, LedgerPayload } from "@/lib/types";
import { useEventStream } from "@/lib/useEventStream";
import { cn } from "@/lib/utils";

/** The event types that change the ledger. The page refetches when one
 *  arrives rather than deriving a line in the browser: the catalog and the
 *  balance arithmetic live in ledger.py, and a second implementation here
 *  would be a second thing to get wrong. */
const LEDGER_EVENTS = new Set([
  "Purchased", "PurchaseSkipped", "ShoppingStarted", "ShoppingEnded",
  "ShoppingUnavailable", "ControlChanged", "RunEnded",
]);

/** The kinds anything actually emits - classify()'s eight, plus the
 *  UNEXPLAINED line the writer derives. The rest of ledger.py's KINDS are
 *  reserved and would be chips that can only ever return nothing. */
const FILTER_KINDS = [
  "RUN_PAYOUT", "WORKSHOP_BUY", "CARD_BUY", "BUY_SKIPPED", "VISIT_START",
  "VISIT_END", "SHOP_UNAVAILABLE", "POLICY_CHANGED", "UNEXPLAINED",
] as const;

const CURRENCIES = ["all", "coins", "gems"] as const;
type CurrencyFilter = (typeof CURRENCIES)[number];

/** Chips read as prose, not as the wire value. The table already prints the
 *  raw kind on every row, and two elements with the identical string is a
 *  worse label for a control than the words it stands for. */
const kindLabel = (kind: string) => kind.toLowerCase().replace(/_/g, " ");

const KIND_TONE: Record<string, string> = {
  UNEXPLAINED: "bg-warn-surface text-warn",
  WORKSHOP_BUY: "bg-chart-2/15 text-chart-2",
  CARD_BUY: "bg-chart-2/15 text-chart-2",
  RUN_PAYOUT: "bg-live-surface text-live",
  BUY_SKIPPED: "bg-muted text-muted-foreground",
};

const num = (value: number | null) =>
  value === null ? "—" : value.toLocaleString("en-US");

export default function LedgerPage() {
  const [data, setData] = useState<LedgerPayload | null>(null);
  // A failed fetch must never fall into the same empty state as a healthy
  // account - reporting an unreachable server as "nothing recorded" would
  // be the same defect the Errors page calls out.
  const [failed, setFailed] = useState<string | null>(null);
  const [rehearsals, setRehearsals] = useState(false);
  // A single kind, not a set: the route takes one `kind`, so clicking the
  // active chip clears it rather than building a filter the server cannot
  // answer.
  const [kind, setKind] = useState<string | null>(null);
  const [currency, setCurrency] = useState<CurrencyFilter>("all");
  // The pages fetched so far. Kept beside `data` rather than read out of it
  // because "Load more" appends: `data` is only ever the LAST page, and it
  // is what carries the balances, the rehearsal count and the next cursor.
  const [lines, setLines] = useState<LedgerLine[]>([]);
  const { events } = useEventStream();

  const query = useCallback(
    (before?: number) => {
      const opts: Parameters<typeof fetchLedger>[0] = {};
      if (rehearsals) opts.includeRehearsals = true;
      if (kind) opts.kind = kind;
      if (currency !== "all") opts.currency = currency;
      if (before !== undefined) opts.before = before;
      return opts;
    },
    [rehearsals, kind, currency],
  );

  const load = useCallback(() => {
    fetchLedger(query())
      .then((payload) => {
        setData(payload);
        // Replace, never append: this is page one again, and every filter
        // change lands here.
        setLines(payload.lines);
        setFailed(null);
      })
      .catch((e: Error) => setFailed(e.message));
  }, [query]);

  useEffect(load, [load]);

  // Refetch on the last event only, not the whole array: the feed grows on
  // every scan and re-running this for each one would hammer the route.
  const latest = events[events.length - 1];
  useEffect(() => {
    if (latest && LEDGER_EVENTS.has(latest.type)) load();
  }, [latest, load]);

  const next = data?.next ?? null;
  const loadMore = useCallback(() => {
    if (next === null) return;
    fetchLedger(query(next))
      .then((payload) => {
        setData(payload);
        setLines((prev) => [...prev, ...payload.lines]);
        setFailed(null);
      })
      .catch((e: Error) => setFailed(e.message));
  }, [next, query]);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Ledger"
        meta="everything outside a run"
        action={
          data ? (
            <span className="font-mono text-xs text-muted-foreground">
              {num(data.balances.coins)} coins · {num(data.balances.gems)} gems
            </span>
          ) : null
        }
      />

      <SectionCard
        title="History"
        tone={failed ? "warn" : undefined}
        action={
          data?.rehearsals ? (
            <button
              type="button"
              onClick={() => setRehearsals((on) => !on)}
              className="text-xs text-muted-foreground underline-offset-2 hover:underline"
            >
              {rehearsals
                ? `hide rehearsals (${data.rehearsals})`
                : `show rehearsals (${data.rehearsals})`}
            </button>
          ) : null
        }
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="flex items-center gap-1" role="group" aria-label="Currency">
            {CURRENCIES.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={currency === option}
                onClick={() => setCurrency(option)}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs transition-colors",
                  currency === option
                    ? "border-transparent bg-primary text-primary-foreground"
                    : "border-border text-muted-foreground hover:bg-muted",
                )}
              >
                {option}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Kind">
            {FILTER_KINDS.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={kind === option}
                // Clicking the active chip clears it - the route takes one
                // kind, so "none selected" is the only other state.
                onClick={() => setKind((on) => (on === option ? null : option))}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs transition-colors",
                  kind === option
                    ? "border-transparent bg-primary text-primary-foreground"
                    : "border-border text-muted-foreground hover:bg-muted",
                )}
              >
                {kindLabel(option)}
              </button>
            ))}
          </div>
        </div>

        {failed ? (
          <div className="rounded-md border border-warn bg-warn-surface p-3">
            <p className="text-sm text-warn">Could not load the ledger.</p>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{failed}</p>
            <p className="mt-2 text-xs text-muted-foreground">
              This is not the same as an empty account — the dashboard could not
              reach the bot.
            </p>
          </div>
        ) : lines.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-[0.12em] text-faint-foreground">
                  <th className="py-1 pr-3 font-medium">Time</th>
                  <th className="py-1 pr-3 font-medium">Kind</th>
                  <th className="py-1 pr-3 font-medium">Item</th>
                  <th className="py-1 pr-3 text-right font-medium">Δ</th>
                  <th className="py-1 pr-3 text-right font-medium">Balance</th>
                  <th className="py-1 font-medium">Note</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((line) => (
                  <tr key={line.id} className="border-t">
                    <td className="py-1.5 pr-3 font-mono text-[11px] text-faint-foreground">
                      {clock(line.ts)}
                    </td>
                    <td className="py-1.5 pr-3">
                      <button
                        type="button"
                        aria-label={`Filter ${line.kind}`}
                        aria-pressed={kind === line.kind}
                        onClick={() => setKind((on) => (on === line.kind ? null : line.kind))}
                        className={cn(
                          "rounded px-1.5 py-0.5 font-mono text-[10px] hover:underline focus-visible:outline-2 focus-visible:outline-primary",
                          KIND_TONE[line.kind] ?? "bg-muted text-muted-foreground",
                        )}
                      >
                        {line.kind}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3">
                      {line.item ?? "—"}
                      {line.dry_run ? (
                        <span className="ml-1.5 text-xs text-muted-foreground">
                          rehearsal
                        </span>
                      ) : null}
                    </td>
                    <td
                      className={cn(
                        "py-1.5 pr-3 text-right font-mono",
                        line.delta !== null && line.delta > 0 && "text-live",
                        line.delta !== null && line.delta < 0 && "text-muted-foreground",
                      )}
                    >
                      {/* "?" means "moved by an unknown amount", which only a
                          line with a currency can have. A visit boundary or a
                          policy change has no financial character at all, and
                          marking the page's most common rows as unknown
                          teaches the reader to ignore the symbol. */}
                      {line.currency === null
                        ? "—"
                        : line.delta === null
                          ? "?"
                          : line.delta > 0
                            ? `+${line.delta}`
                            : line.delta}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted-foreground">
                      {num(line.balance_after)}
                      {line.currency ? (
                        <span className="ml-1 text-[10px] text-faint-foreground">
                          {line.currency}
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5 text-xs text-muted-foreground">
                      {line.kind === "UNEXPLAINED"
                        ? "balance moved outside the bot"
                        : line.reason ?? ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Nothing recorded yet.</p>
        )}

        {/* Only when the route says there is an older page. A permanent
            history capped at its most recent 50 lines is not a history. */}
        {!failed && next !== null ? (
          <div>
            <button
              type="button"
              onClick={loadMore}
              className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted"
            >
              Load more
            </button>
          </div>
        ) : null}
      </SectionCard>
    </div>
  );
}
