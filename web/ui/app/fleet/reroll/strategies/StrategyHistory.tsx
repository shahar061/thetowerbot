import { History } from "lucide-react";
import type { StrategyLedgerEntry, StrategyLedgerPin } from "@/lib/strategyStudio";
import styles from "./studio.module.css";

const KIND_LABEL: Record<StrategyLedgerEntry["kind"], string> = {
  assigned: "Assigned", reassigned: "Reassigned", unassigned: "Unassigned", saved: "Saved" };
const pin = (value: StrategyLedgerPin | null): string =>
  value ? `${value.strategy_name} v${value.strategy_version}` : "none";

/** Read-only: every row comes from history the server already keeps, so
 *  there is nothing here to edit or undo — rollback lives on the route page. */
export function StrategyHistory({ entries, error }: { entries: StrategyLedgerEntry[] | null; error: string }): React.JSX.Element {
  return <section role="region" aria-label="History" className={styles.history}>
    <h3><History size={15} />History</h3>
    {error ? <p role="alert" className={styles.hint}>History unavailable: {error}</p>
      : entries === null ? <p className={styles.hint}>Loading history…</p>
      : !entries.length ? <p className={styles.hint}>No strategy changes yet</p>
      : <ol>{entries.map((entry, index) => <li key={`${entry.kind}-${entry.at}-${index}`}>
          <time dateTime={new Date(entry.at * 1000).toISOString()}>{new Date(entry.at * 1000).toLocaleString()}</time>
          <span className={styles.historyKind} data-kind={entry.kind}>{KIND_LABEL[entry.kind]}</span>
          {entry.kind === "saved" ? <>
            <strong>{`${entry.strategy_name} v${entry.strategy_version}`}</strong><span>{`from ${entry.source_template}`}</span>
          </> : <>
            <strong>{entry.worker}</strong><span>{`${pin(entry.before)} → ${pin(entry.after)}`}</span>
            <span>{`by ${entry.actor} · route rev ${entry.route_revision}`}</span>
          </>}
        </li>)}</ol>}
  </section>;
}
