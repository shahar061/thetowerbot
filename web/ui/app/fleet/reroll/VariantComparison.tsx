import type { VariantRow } from "@/lib/fleet";
import { hoursMinutes } from "@/lib/rerollState";
import { RerollCard } from "./RerollCard";

/** Which opening variant gets a fresh account to Tier 1 Wave 20 fastest.
 *  Median and fastest cover only accounts that got there, so "Reached"
 *  sits beside them: a variant whose slow accounts are still going would
 *  otherwise look quick. */
export function VariantComparison({ rows }: { rows: VariantRow[] }) {
  return <RerollCard title="Opening variants">
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[13px]">
        <thead className="text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint-foreground">
          <tr><th className="py-1 pr-3">Variant</th><th className="py-1 pr-3">Caps</th>
            <th className="py-1 pr-3">Reached W20</th><th className="py-1 pr-3">Median</th>
            <th className="py-1">Fastest</th></tr>
        </thead>
        <tbody>{rows.map(row => <tr key={row.id} className="border-t border-border">
          <td className="py-1.5 pr-3 font-medium">{row.name}</td>
          <td className="py-1.5 pr-3 text-muted-foreground">{row.caps || "—"}</td>
          <td className="py-1.5 pr-3 font-mono">{row.reached} of {row.accounts}</td>
          <td className="py-1.5 pr-3 font-mono">{row.median_seconds != null ? hoursMinutes(row.median_seconds) : "—"}</td>
          <td className="py-1.5 font-mono">{row.fastest_seconds != null ? hoursMinutes(row.fastest_seconds) : "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </RerollCard>;
}
