"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { fetchFleetLabs } from "@/lib/api";
import type { LabsSnapshot } from "@/lib/labs";
import { fleetScope, validatedLabsSnapshot } from "./fleetOverview";
import { useRerollWorkspace } from "./RerollWorkspace";

export type FleetLabsState = {
  snapshot: LabsSnapshot | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
};

const Context = createContext<FleetLabsState | null>(null);

export function FleetLabsProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const { pool } = useRerollWorkspace();
  const scope = pool ? fleetScope(pool.members) : null;
  const [result, setResult] = useState<{ scope: string; snapshot: LabsSnapshot | null; error: string | null; loading: boolean } | null>(null);
  const request = useRef<() => Promise<void>>(async () => {});
  const refresh = useCallback((): Promise<void> => request.current(), []);

  useEffect(() => {
    if (scope === null) return;
    let active = true;
    let inFlight: Promise<void> | null = null;
    let queuedAfter: Promise<void> | null = null;
    let queuedPromise: Promise<void> | null = null;
    const load = (explicit = false): Promise<void> => {
      if (!active) return Promise.resolve();
      // An assignment may finish while an earlier read is pending. Schedule
      // its requested refresh after that read, without overlapping polls.
      if (inFlight) {
        if (!explicit) return inFlight;
        const current = inFlight;
        if (queuedAfter !== current) {
          queuedAfter = current;
          queuedPromise = current.then(() => {
            if (queuedAfter === current) queuedAfter = null;
            return load();
          });
        }
        return queuedPromise!;
      }
      setResult(previous => ({ scope, snapshot: previous?.scope === scope ? previous.snapshot : null,
        error: previous?.scope === scope ? previous.error : null, loading: true }));
      inFlight = (async () => {
        try {
          const next = validatedLabsSnapshot(await fetchFleetLabs());
          if (!next) throw new Error("Invalid labs response");
          if (active) setResult({ scope, snapshot: next, error: null, loading: false });
        } catch (failure) {
          if (active) setResult(previous => ({ scope, snapshot: previous?.scope === scope ? previous.snapshot : null,
            error: failure instanceof Error ? failure.message : "Labs unavailable", loading: false }));
        } finally {
          inFlight = null;
        }
      })();
      return inFlight;
    };
    request.current = () => load(true);
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 30_000);
    const onVisible = (): void => { if (document.visibilityState === "visible") void load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      request.current = async () => {};
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [scope]);

  const value = useMemo<FleetLabsState>(() => ({
    snapshot: result?.scope === scope ? result.snapshot : null,
    loading: scope === null || result?.scope !== scope || result.loading,
    error: result?.scope === scope ? result.error : null,
    refresh,
  }), [result, scope, refresh]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useFleetLabs(): FleetLabsState {
  const value = useContext(Context);
  if (!value) throw new Error("Fleet pages require FleetLabsProvider");
  return value;
}
