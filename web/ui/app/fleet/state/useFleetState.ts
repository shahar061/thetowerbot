"use client";

import { useEffect, useRef, useState } from "react";
import { fetchFleetState } from "@/lib/api";
import { reuseUnchanged, type FleetStateAccount, type FleetStatePayload } from "@/lib/fleetState";

export const POLL_MS = 2000;

export interface FleetStateView {
  payload: FleetStatePayload | null;
  /** Unchanged accounts keep their previous object, so memoized columns skip. */
  accounts: FleetStateAccount[];
  /** Date.now() when each account last changed; drives the "scan age". */
  changedAt: Record<string, number>;
  /** The last poll failed. The previous payload stays on screen. */
  connectionLost: boolean;
}

const EMPTY: FleetStateView = { payload: null, accounts: [], changedAt: {}, connectionLost: false };

/** Polls /api/fleet/state every 2 s, one request at a time. */
export function useFleetState(intervalMs: number = POLL_MS): FleetStateView {
  const [view, setView] = useState<FleetStateView>(EMPTY);
  const previous = useRef<FleetStateAccount[] | null>(null);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const tick = async (): Promise<void> => {
      try {
        const payload = await fetchFleetState();
        if (!active) return;
        const before = previous.current;
        const accounts = reuseUnchanged(before, payload.accounts);
        previous.current = accounts;
        const now = Date.now();
        setView(old => ({
          payload, accounts, connectionLost: false,
          changedAt: Object.fromEntries(accounts.map(account => [account.id,
            before?.includes(account) ? (old.changedAt[account.id] ?? now) : now])),
        }));
      } catch {
        if (active) setView(old => ({ ...old, connectionLost: true }));
      } finally {
        if (active) timer = window.setTimeout(() => { void tick(); }, intervalMs);
      }
    };
    void tick();
    return () => { active = false; window.clearTimeout(timer); };
  }, [intervalMs]);

  return view;
}
