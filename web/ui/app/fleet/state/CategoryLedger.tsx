import { cn } from "@/lib/utils";
import { agoText, amount, DASH, priceText, whole } from "./stateFormat";

export interface LedgerRow {
  id: string; name: string; level: number | null;
  invested?: number | null; spent?: number | null;
  next: number | null; maxed?: boolean; locked?: boolean;
}
export interface LedgerRecent {
  key: string; ts: string | null; name: string; detail?: string | null;
  price: number | null; color?: string;
}
export interface LedgerUnlock { name: string; cost: number | null }

/** The shared Workshop, Cards and Labs table: level, invested, next, with the
 * bot's NEXT row highlighted, the next unlock and a scrolling purchase list. */
export function CategoryLedger({
  label, head, rows, nextId, color, recent, nowMs,
  unlock, investedHead = "Invested", nextHead = "Next", nextIsPrice = true,
}: {
  label: string; head: string; rows: LedgerRow[]; nextId: string | null; color: string;
  recent: LedgerRecent[]; nowMs: number;
  /** Undefined hides the line; null means every unlock is owned. */
  unlock?: LedgerUnlock | null;
  /** Null hides the column. */
  investedHead?: string | null; nextHead?: string; nextIsPrice?: boolean;
}): React.JSX.Element {
  const columns = investedHead === null ? 3 : 4;
  return (
    <div className="fs-ledger" style={{ "--c": color } as React.CSSProperties}>
      <div className="fs-tw" role="region" aria-label={label} tabIndex={0}>
        <table>
          <thead>
            <tr>
              <th scope="col">{head}</th>
              <th scope="col" className="n">Lv</th>
              {investedHead !== null && <th scope="col" className="n">{investedHead}</th>}
              <th scope="col" className="n">{nextHead}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={columns} className="fs-dim">Nothing read yet</td></tr>}
            {rows.map(row => {
              const next = row.id === nextId;
              return (
                <tr key={row.id} className={cn(next && "next", row.locked && "locked")}>
                  <td>{row.name}{next && <span className="fs-tn">NEXT</span>}</td>
                  <td className="n">{row.level === null ? DASH : whole(row.level)}</td>
                  {investedHead !== null && (
                    <td className="n fs-dim" title={row.spent ? `Bot spent ${amount(row.spent)}` : undefined}>
                      {amount(row.invested)}
                    </td>)}
                  <td className="n">
                    {row.locked ? <span className="fs-dim">locked</span>
                      : row.maxed ? <span className="fs-dim">MAX</span>
                        : nextIsPrice ? priceText(row.next) : amount(row.next)}
                  </td>
                </tr>);
            })}
          </tbody>
        </table>
      </div>
      {unlock !== undefined && (
        <div className="fs-unlock">
          {unlock === null ? "All unlocked" : <>Next unlock <b>{unlock.name}</b><span className="fs-unlock-cost">{priceText(unlock.cost)}</span></>}
        </div>)}
      <div className="fs-rh">Recent purchases</div>
      {recent.length === 0 ? <p className="fs-none">None recorded</p> : (
        <ol className="fs-recent" tabIndex={0} aria-label={`${label} recent purchases`}>
          {recent.map(item => (
            <li key={item.key}>
              <span className="ago">{agoText(item.ts, nowMs)}</span>
              <i className="cd" style={{ background: item.color ?? color }} aria-hidden="true" />
              <span className="rn">{item.name}{item.detail ? <small> {item.detail}</small> : null}</span>
              <span className="n">{priceText(item.price)}</span>
            </li>))}
        </ol>)}
    </div>);
}
