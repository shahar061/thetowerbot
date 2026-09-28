"use client";

import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void): () => void {
  document.addEventListener("visibilitychange", onChange);
  return () => document.removeEventListener("visibilitychange", onChange);
}

/** False while the tab is hidden, so screen views stop pulling video nobody sees. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribe, () => document.visibilityState !== "hidden", () => true);
}
