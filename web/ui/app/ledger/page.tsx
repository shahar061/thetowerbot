"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CurrencyAmount } from "@/components/CurrencyAmount";
import { CurrencyGlyph } from "@/components/CurrencyGlyph";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchLedger } from "@/lib/api";
import { clock } from "@/lib/format";
import {
  FILTER_KINDS,
  LEDGER_EVENTS,
  balancedCurrencies,
  currencyOptions,
  groupLines,
} from "@/lib/ledger";
import type { LedgerLine, LedgerPayload } from "@/lib/types";
import { useEventStream } from "@/lib/useEventStream";
import { cn } from "@/lib/utils";

/** "all", or one currency id. A string rather than a fixed union because the
 *  currencies come from the account's history, not from this file. */
type CurrencyFilter = string;

/** Chips read as prose, not as the wire value. The table already prints the
 *  raw kind on every row, and two elements with the identical string is a
 *  worse label for a control than the words it stands for. */
const kindLabel = (kind: string) => kind.toLowerCase().replace(/_/g, " ");

const KIND_TONE: Record<string, string> = {
  UNEXPLAINED: "bg-warn-surface text-warn",
  // "Did it land?" is unanswered, which is the same kind of attention as a
  // balance that moved on its own.
  CLAIM_UNCERTAIN: "bg-warn-surface text-warn",
  WORKSHOP_BUY: "bg-chart-2/15 text-chart-2",
  CARD_BUY: "bg-chart-2/15 text-chart-2",
  RUN_PAYOUT: "bg-live-surface text-live",
  MISSION_CLAIM: "bg-live-surface text-live",
  MILESTONE_CLAIM: "bg-live-surface text-live",
  GEM_CLAIM: "bg-live-surface text-live",
  BUY_SKIPPED: "bg-muted text-muted-foreground",
  CLAIM_SKIPPED: "bg-muted text-muted-foreground",
};

/** A balance of this currency, or why there is none. `null` alone cannot say
 *  which: for a balanced currency it is "not read yet", for anything else the
 *  writer never tracks it at all - and showing either as blank would read
 *  as an empty wallet. */
function Balance({
  currency,
  value,
  balanced,
}: {
  currency: string;
  value: number | null;
  balanced: readonly string[];
}) {
  if (value !== null) {
    return (
      <span className="inline-flex items-center gap-1">
        <span>{num(value)}</span>
        <CurrencyGlyph currency={currency} />
        <span className="sr-only">{currency}</span>
      </span>
    );
  }
  if (balanced.includes(currency)) {
    return <span title={`${currency} balance not read yet`}>—</span>;
  }
  return (
    <span className="inline-flex items-center gap-1 text-faint-foreground" title={`the ledger keeps no running ${currency} balance`}>
      <CurrencyGlyph currency={currency} />
      <span className="text-[11px] italic">not tracked</span>
      <span className="sr-only">{currency}</span>
    </span>
  );
}

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

  // Grouped over EVERY page fetched so far, not per page: "Load more" can
  // land an event's gem line on the page after its coin line.
  const entries = useMemo(() => groupLines(lines), [lines]);
  const options = useMemo(() => currencyOptions(data, lines), [data, lines]);
  const balanced = balancedCurrencies(data);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Ledger"
        meta="everything outside a run"
        action={
          data ? (
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-muted-foreground">
              {options.map((option) => (
                <Balance
                  key={option}
                  currency={option}
                  value={data.balances[option] ?? null}
                  balanced={balanced}
                />
              ))}
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
            {["all", ...options].map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={currency === option}
                onClick={() => setCurrency(option)}
                className={cn(
                  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition-colors",
                  currency === option
                    ? "border-transparent bg-primary text-primary-foreground"
                    : "border-border text-muted-foreground hover:bg-muted",
                )}
              >
                {option === "all" ? null : <CurrencyGlyph currency={option} />}
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
        ) : entries.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-[0.12em] text-faint-foreground">
                  <th className="py-1 pr-3 font-medium">Time</th>
                  <th className="py-1 pr-3 font-medium">Kind</th>
                  <th className="py-1 pr-3 font-medium">Item</th>
                  <th className="py-1 pr-3 text-right font-medium">Amount</th>
                  <th className="py-1 pr-3 text-right font-medium">Balance</th>
                  <th className="py-1 font-medium">Note</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(({ key, head, financial, rehearsal }) => (
                  <tr key={key} className="border-t">
                    <td className="py-1.5 pr-3 font-mono text-[11px] text-faint-foreground">
                      {clock(head.ts)}
                    </td>
                    <td className="py-1.5 pr-3">
                      <button
                        type="button"
                        aria-label={`Filter ${head.kind}`}
                        aria-pressed={kind === head.kind}
                        onClick={() => setKind((on) => (on === head.kind ? null : head.kind))}
                        className={cn(
                          "rounded px-1.5 py-0.5 font-mono text-[10px] hover:underline focus-visible:outline-2 focus-visible:outline-primary",
                          KIND_TONE[head.kind] ?? "bg-muted text-muted-foreground",
                        )}
                      >
                        {head.kind}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3">
                      {head.item ?? "—"}
                      {rehearsal ? (
                        <span className="ml-1.5 text-xs text-muted-foreground">
                          rehearsal
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5 pr-3 text-right">
                      {/* One chip per currency the event moved, so a mission
                          paying coins and gems reads as one reward rather than
                          two rows. A visit boundary or a policy change has no
                          financial character at all: a dash, not a chip, and
                          never "?" - marking the page's most common rows as
                          unknown teaches the reader to ignore the symbol. */}
                      {financial.length ? (
                        <span className="inline-flex flex-wrap justify-end gap-1">
                          {financial.map((line: LedgerLine) => (
                            <CurrencyAmount
                              key={line.id}
                              currency={line.currency!}
                              delta={line.delta}
                              className="text-xs"
                            />
                          ))}
                        </span>
                      ) : (
                        <span className="font-mono text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-muted-foreground">
                      {/* Per currency, each labelled by its glyph - a bundle
                          has one balance per currency it moved, and a single
                          unlabelled number would say which one by accident. */}
                      {financial.length ? (
                        <span className="inline-flex flex-col items-end gap-0.5">
                          {financial.map((line: LedgerLine) => (
                            <Balance
                              key={line.id}
                              currency={line.currency!}
                              value={line.balance_after}
                              balanced={balanced}
                            />
                          ))}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-1.5 text-xs text-muted-foreground">
                      {head.kind === "UNEXPLAINED"
                        ? "balance moved outside the bot"
                        : head.reason ?? ""}
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
