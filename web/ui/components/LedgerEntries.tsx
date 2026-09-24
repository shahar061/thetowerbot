"use client";

import { CurrencyAmount } from "@/components/CurrencyAmount";
import { CurrencyGlyph } from "@/components/CurrencyGlyph";
import { clock } from "@/lib/format";
import type { LedgerEntry } from "@/lib/ledger";
import type { LedgerLine } from "@/lib/types";
import { cn } from "@/lib/utils";

const KIND_TONE: Record<string, string> = {
  UNEXPLAINED: "bg-warn-surface text-warn",
  // "Did it land?" is unanswered, which is the same kind of attention as a
  // balance that moved on its own.
  CLAIM_UNCERTAIN: "bg-warn-surface text-warn",
  WORKSHOP_BUY: "bg-chart-2/15 text-chart-2",
  CARD_BUY: "bg-chart-2/15 text-chart-2",
  RUN_PAYOUT: "bg-live-surface text-live",
  MISSION_CLAIM: "bg-live-surface text-live",
  MAIL_CLAIM: "bg-live-surface text-live",
  MILESTONE_CLAIM: "bg-live-surface text-live",
  GEM_CLAIM: "bg-live-surface text-live",
  BUY_SKIPPED: "bg-muted text-muted-foreground",
  CLAIM_SKIPPED: "bg-muted text-muted-foreground",
};

/** A balance of this currency, or why there is none. `null` alone cannot say
 *  which: for a balanced currency it is "not read yet", for anything else the
 *  writer never tracks it at all - and showing either as blank would read
 *  as an empty wallet. */
export function LedgerBalance({
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

export type LedgerTableEntry = LedgerEntry & {
  source?: { name: string; accountId: string; color: string };
  balanced?: readonly string[];
};

export function LedgerEntries({ entries, balanced = ["coins", "gems"], kind, onKindChange, withSources = false }: {
  entries: LedgerTableEntry[];
  balanced?: readonly string[];
  kind: string | null;
  onKindChange: (kind: string | null) => void;
  withSources?: boolean;
}): React.JSX.Element {
  return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-[0.12em] text-faint-foreground">
                  {withSources && <th className="py-1 pr-3 font-medium">Emulator</th>}
                  <th className="py-1 pr-3 font-medium">Time</th>
                  <th className="py-1 pr-3 font-medium">Kind</th>
                  <th className="py-1 pr-3 font-medium">Item</th>
                  <th className="py-1 pr-3 text-right font-medium">Amount</th>
                  <th className="py-1 pr-3 text-right font-medium">Balance</th>
                  <th className="py-1 font-medium">Note</th>
                </tr>
              </thead>
              <tbody>
                {entries.map(({ key, head, financial, rehearsal, source, balanced: ownBalanced }) => (
                  <tr key={key} className="border-t">
                    {withSources && <td className="py-2 pr-4"><span className="font-medium" style={{ color: source?.color }}>{source?.name}</span><span className="block font-mono text-[10px] text-muted-foreground">{source?.accountId}</span></td>}
                    <td className="py-1.5 pr-3 font-mono text-[11px] text-faint-foreground">
                      {withSources && <span className="block">{new Date(head.ts * 1000).toLocaleDateString()}</span>}{clock(head.ts)}
                    </td>
                    <td className="py-1.5 pr-3">
                      <button
                        type="button"
                        aria-label={`Filter ${head.kind}`}
                        aria-pressed={kind === head.kind}
                        onClick={() => onKindChange(kind === head.kind ? null : head.kind)}
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
                            <LedgerBalance
                              key={line.id}
                              currency={line.currency!}
                              value={line.balance_after}
                              balanced={ownBalanced ?? balanced}
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
  );
}
