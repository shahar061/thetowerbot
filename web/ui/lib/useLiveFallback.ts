"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { LIVE_RETRY_MS, liveSupported } from "./liveStream";

const neverChanges = (): (() => void) => () => {};

/** Show live video now? Only if this browser can decode it, and it hasn't failed in the last LIVE_RETRY_MS. */
export function useLiveFallback(): { supported: boolean; live: boolean; markUnavailable: () => void } {
  // The static export's server render has no window, so it (and hydration)
  // answers "not supported" and the client corrects it straight after.
  const supported = useSyncExternalStore(neverChanges, liveSupported, () => false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!failed) return;
    const timer = window.setTimeout(() => setFailed(false), LIVE_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [failed]);
  const markUnavailable = useCallback(() => setFailed(true), []);
  return { supported, live: supported && !failed, markUnavailable };
}
