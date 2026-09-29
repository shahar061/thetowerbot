"use client";

import { useEffect, useRef, useState } from "react";
import { Maximize2, MonitorOff } from "lucide-react";
import { FeedBadge } from "@/components/FeedBadge";
import { LiveVideo } from "@/components/LiveVideo";
import { useLiveFallback } from "@/lib/useLiveFallback";
import { usePageVisible } from "@/lib/usePageVisible";
import { cn } from "@/lib/utils";

/** The worker validates scope on each frame; replacing the identity remounts this view. */
export function FleetCapture({ dashboardUrl, scope, instance, accountId, fill = false, paused = false }: {
  dashboardUrl: string; scope: string; instance: string; accountId: string;
  /** Fill the parent (the live wall) instead of the card's fixed-height box. */
  fill?: boolean;
  /** The live wall is showing this screen, so the card's copy lets go of its stream. */
  paused?: boolean;
}): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(true);
  const foreground = usePageVisible();
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { live, markUnavailable } = useLiveFallback();
  useEffect(() => {
    const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(
      entries => setVisible(entries[0]?.isIntersecting ?? false), { rootMargin: "120px" },
    );
    if (container.current) observer?.observe(container.current);
    return () => observer?.disconnect();
  }, []);
  useEffect(() => setLoaded(false), [live]);
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);
  url.searchParams.set("expected_account_id", accountId);
  // Off-screen or hidden cards unmount their feed, which closes the live
  // stream. The worker keeps it for 10 s, so scrolling back is instant.
  const active = visible && foreground && !paused;
  return <div ref={container} className={cn("relative flex items-center justify-center overflow-hidden bg-well",
    fill ? "size-full" : "h-[min(30vh,18rem)] min-h-48 rounded-lg border")}>
    {active && !failed ? <>
      {live
        ? <LiveVideo key={`${scope}:${attempt}`} dashboardUrl={dashboardUrl} scope={scope} expectedAccountId={accountId}
            label={`Live screen of ${instance}`} className="size-full object-contain"
            onFrame={() => setLoaded(true)} onUnavailable={markUnavailable} />
        : <>
          {/* Native MJPEG works across worker origins without a fetch/CORS proxy. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img key={`${scope}:${attempt}`} src={url.href} alt={`Live screen of ${instance}`}
            onLoad={() => setLoaded(true)} onError={() => { setFailed(true); setLoaded(false); }}
            className="size-full object-contain" />
        </>}
      <FeedBadge live={live} />
      {!loaded && <span className="pointer-events-none absolute bottom-2 rounded bg-background/90 px-2 py-1 text-[10px] text-muted-foreground">Connecting screen…</span>}
      {!fill && <button type="button" aria-label={`Enlarge screen of ${instance}`} title="Open full screen"
        className="absolute right-2 top-2 rounded-md border bg-background/90 p-1.5 text-muted-foreground hover:text-foreground"
        onClick={() => { void container.current?.requestFullscreen?.().catch(() => {}); }}>
        <Maximize2 className="size-3.5" aria-hidden="true" />
      </button>}
    </> : <div role="status" className="flex flex-col items-center gap-3 px-5 text-center text-xs text-muted-foreground">
      <MonitorOff className="size-6" aria-hidden="true" />
      <span>{failed ? "Screen unavailable. This worker may be reconnecting."
        : paused ? "Screen paused while the live wall is open." : "Screen paused while out of view."}</span>
      {failed && <button className="rounded-md border px-3 py-1.5 text-foreground" onClick={() => { setFailed(false); setLoaded(false); setAttempt(value => value + 1); }}>Retry screen</button>}
    </div>}
  </div>;
}
