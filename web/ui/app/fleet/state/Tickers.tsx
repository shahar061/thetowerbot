"use client";

import { useEffect, useState } from "react";
import { secondsUntil, span } from "./stateFormat";

/** Re-renders only itself once a second, so a ticking clock never
 * re-renders the memoized column around it. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** "12s ago" since a Date.now() timestamp. */
export function Ago({ since }: { since: number }): React.JSX.Element {
  const now = useNow();
  return <span>{span((now - since) / 1000)} ago</span>;
}

/** Time left until an ISO completion time; "done" once it has passed. */
export function Countdown({ until }: { until: string }): React.JSX.Element {
  const now = useNow();
  const left = secondsUntil(until, now);
  return <span className="fs-mono">{left > 0 ? span(left) : "done"}</span>;
}
