"use client";

import type { FleetStateAccount } from "@/lib/fleetState";
import { isLive, toggleAccount, visibleAccounts, type Selection } from "./selection";
import { accentAt, amount, DASH, whole } from "./stateFormat";

/** Sticky header: emulator chips, All / Only live, and the fleet heartbeat. */
export function FleetStateBar({ accounts, selection, onSelect, connectionLost }: {
  accounts: FleetStateAccount[]; selection: Selection;
  onSelect: (next: Selection) => void; connectionLost: boolean;
}): React.JSX.Element {
  const shown = new Set(visibleAccounts(accounts, selection).map(account => account.id));
  const scans = accounts.flatMap(account => account.scan === null ? [] : [account.scan]);
  const scan = scans.length ? Math.max(...scans) : null;
  const coins = accounts.reduce<number | null>(
    (sum, account) => account.balances?.coins == null ? sum : (sum ?? 0) + account.balances.coins, null);
  return (
    <header className="fs-top">
      <div className="fs-top-in">
        <div className="fs-brand-row">
          <h1 className="fs-brand">Fleet<span>{"//"}</span>State</h1>
          {connectionLost && <span className="fs-lost" role="status">connection lost</span>}
          <div className="fs-gstats">
            <span><i key={scan ?? 0} className="fs-beat" aria-hidden="true" />Scan <b>{scan === null ? DASH : `#${whole(scan)}`}</b></span>
            <span><b>{accounts.filter(isLive).length}</b> live</span>
            <span>Fleet coins <b>{amount(coins)}</b></span>
          </div>
        </div>
        <div className="fs-bar" role="toolbar" aria-label="Emulator filter">
          <div className="fs-chips">
            {accounts.map((account, index) => (
              <button key={account.id} type="button" className="fs-chip" aria-pressed={shown.has(account.id)}
                data-state={isLive(account) ? "live" : account.online ? "online" : "offline"}
                style={{ "--acc": accentAt(index) } as React.CSSProperties}
                onClick={() => onSelect(toggleAccount(selection, account.id, accounts))}>
                <i className="sd" aria-hidden="true" />
                <span className="c-id">{account.id}</span>
                <span className="c-s">{isLive(account) && account.battle?.wave != null ? `W${whole(account.battle.wave)}`
                  : account.online ? (account.bot.screen ?? "") : "offline"}</span>
              </button>))}
          </div>
          <div className="fs-seg">
            <button type="button" aria-pressed={selection.mode === "all"} onClick={() => onSelect({ mode: "all", ids: [] })}>All</button>
            <button type="button" aria-pressed={selection.mode === "live"} onClick={() => onSelect({ mode: "live", ids: [] })}>Only live</button>
          </div>
        </div>
      </div>
    </header>);
}
