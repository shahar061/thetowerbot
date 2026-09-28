"use client";

import { useCallback, useEffect, useState } from "react";
import { AccountColumn } from "./AccountColumn";
import { FleetStateBar } from "./FleetStateBar";
import { DEFAULT_SELECTION, loadOpen, loadSelection, saveOpen, saveSelection, visibleAccounts, type Selection } from "./selection";
import { accentAt } from "./stateFormat";
import { useFleetState } from "./useFleetState";

export default function FleetStatePage(): React.JSX.Element {
  const { payload, accounts, changedAt, connectionLost } = useFleetState();
  const [selection, setSelection] = useState<Selection>(DEFAULT_SELECTION);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // Read after mount: storage is per viewer and the export has no server render.
  useEffect(() => { setSelection(loadSelection()); setOpen(loadOpen()); }, []);
  const choose = useCallback((next: Selection) => { setSelection(next); saveSelection(next); }, []);
  const toggleSection = useCallback((key: string, value: boolean) => setOpen(old => {
    const next = { ...old, [key]: value };
    saveOpen(next);
    return next;
  }), []);
  const shown = visibleAccounts(accounts, selection);
  const order = new Map(accounts.map((account, index) => [account.id, index]));

  let body: React.ReactNode;
  if (payload === null) body = <div className="fs-empty">{connectionLost ? "Fleet state unavailable. Retrying…" : "Loading fleet state…"}</div>;
  else if (accounts.length === 0) body = <div className="fs-empty">No emulators in the fleet yet.</div>;
  else if (shown.length === 0) body = <div className="fs-empty">No emulators selected. Pick one above, or press <b>All</b>.</div>;
  else body = shown.map(account => (
    <AccountColumn key={account.id} account={account} accent={accentAt(order.get(account.id) ?? 0)}
      changedAt={changedAt[account.id] ?? 0} shownCount={shown.length} open={open} onToggleSection={toggleSection} />));

  return (
    <div className="fs-root">
      <FleetStateBar accounts={accounts} selection={selection} onSelect={choose} connectionLost={connectionLost} />
      <main className="fs-grid" aria-live="off">{body}</main>
    </div>);
}
