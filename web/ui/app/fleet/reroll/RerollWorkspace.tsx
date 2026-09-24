"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { fetchReroll } from "@/lib/api";
import type { RerollSnapshot } from "@/lib/fleet";
import { rerollCoordinatorUrl } from "@/lib/fleetRedirect";

type RerollWorkspaceValue = {
  pool: RerollSnapshot | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  setPool: React.Dispatch<React.SetStateAction<RerollSnapshot | null>>;
};

const Workspace = createContext<RerollWorkspaceValue | null>(null);

export function RerollWorkspaceProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [pool, updatePool] = useState<RerollSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const revision = useRef(0);
  const request = useRef<() => Promise<void>>(async () => {});
  const refresh = useCallback((): Promise<void> => request.current(), []);
  // A lifecycle action may finish while a previously started read is pending.
  const setPool = useCallback<React.Dispatch<React.SetStateAction<RerollSnapshot | null>>>((value) => {
    revision.current += 1;
    updatePool(value);
  }, []);

  useEffect(() => {
    let active = true;
    let inFlight: Promise<void> | null = null;
    const load = (): Promise<void> => {
      if (!active) return Promise.resolve();
      if (inFlight) return inFlight;
      const startedRevision = revision.current;
      inFlight = (async () => {
        try {
          const next = await fetchReroll();
          if (active && startedRevision === revision.current) {
            updatePool(next);
            setError(null);
          }
        } catch (failure) {
          if (!active || startedRevision !== revision.current) return;
          const destination = rerollCoordinatorUrl(failure, window.location.href);
          if (destination) window.location.replace(destination);
          else setError(failure instanceof Error ? failure.message : String(failure));
        } finally {
          inFlight = null;
          if (active) setLoading(false);
        }
      })();
      return inFlight;
    };
    request.current = load;
    void load();
    const timer = window.setInterval(() => { void load(); }, 5_000);
    return () => {
      active = false;
      window.clearInterval(timer);
      request.current = async () => {};
    };
  }, []);

  const value = useMemo(() => ({ pool, loading, error, refresh, setPool }), [pool, loading, error, refresh, setPool]);
  return <Workspace.Provider value={value}>{children}</Workspace.Provider>;
}

export function useRerollWorkspace(): RerollWorkspaceValue {
  const workspace = useContext(Workspace);
  if (!workspace) throw new Error("Reroll pages require RerollWorkspaceProvider");
  return workspace;
}
