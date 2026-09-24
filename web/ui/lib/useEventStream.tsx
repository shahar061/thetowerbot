"use client";

import { usePathname } from "next/navigation";
import { isRerollPath } from "./workspace";
import { createContext, useContext, useEffect, useReducer, useState } from "react";
import { feedReducer } from "./eventReducer";
import { useAccountSelection } from "./AccountSelection";
import type { BotEvent } from "./types";

/** Two contexts, not one, and that split is the point.
 *
 * `connected` changes twice a session; `events` changes once or twice a
 * second. A single context would re-render every consumer - the nav rail on
 * every page included - on every scan the bot completes, just to keep a
 * two-pixel dot honest. */
const EventsContext = createContext<BotEvent[]>([]);
const ConnectedContext = createContext(false);

/**
 * The single SSE subscription for the tab.
 *
 * EventSource reconnects on its own and replays Last-Event-ID, so a dropped
 * connection costs nothing as long as the gap fits inside the server's ring.
 *
 * This lives in the root layout, which is what lets `useEventStream` be called
 * from more than one place - the Live feed, useControlSync, and the rail's
 * connection badge - without each caller opening a socket of its own.
 */
export function EventStreamProvider({ children }: { children: React.ReactNode }) {
  const { selected } = useAccountSelection();
  const reroll = isRerollPath(usePathname());
  const [events, dispatch] = useReducer(feedReducer, []);
  const [connected, setConnected] = useState(false);
  const key = selected?.key;
  const running = selected?.running;
  const dashboardUrl = selected?.dashboard_url;

  useEffect(() => {
    dispatch({ kind: "clear" });
    setConnected(false);
    if (reroll || !key || !running || (dashboardUrl && new URL(dashboardUrl).origin !== window.location.origin)) return;
    let active = true;
    const source = new EventSource(`/api/events/stream?scope=${encodeURIComponent(key)}`);
    source.onopen = () => { if (active) setConnected(true); };
    source.onerror = () => { if (active) setConnected(false); };
    source.onmessage = (message) => {
      if (active) dispatch({ kind: "event", event: JSON.parse(message.data) as BotEvent });
    };
    return () => { active = false; source.close(); };
  }, [key, running, dashboardUrl, reroll]);

  return (
    <ConnectedContext.Provider value={connected}>
      <EventsContext.Provider value={events}>{children}</EventsContext.Provider>
    </ConnectedContext.Provider>
  );
}

/** The stream, for callers that read the events themselves.
 *
 * With no provider above it this reads as an empty, disconnected stream -
 * which is exactly how the UI renders an unreachable bot, so a missing
 * provider shows up as a dot stuck on "reconnecting" rather than as a crash. */
export function useEventStream(): { events: BotEvent[]; connected: boolean } {
  return { events: useContext(EventsContext), connected: useContext(ConnectedContext) };
}

/** Just the connection, for callers that only report liveness. Subscribing to
 *  this instead of the whole stream is what keeps the rail from re-rendering
 *  on every event. */
export function useConnected(): boolean {
  return useContext(ConnectedContext);
}
