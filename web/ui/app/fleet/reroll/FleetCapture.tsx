"use client";

import { useEffect, useRef, useState } from "react";
import { Maximize2, MonitorOff } from "lucide-react";

/** The worker validates scope on each frame; replacing the identity remounts this view. */
export function FleetCapture({ dashboardUrl, scope, instance, accountId }: {
  dashboardUrl: string; scope: string; instance: string; accountId: string;
}): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(true);
  const [foreground, setForeground] = useState(true);
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const update = (): void => setForeground(document.visibilityState !== "hidden");
    update();
    document.addEventListener("visibilitychange", update);
    const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(
      entries => setVisible(entries[0]?.isIntersecting ?? false), { rootMargin: "120px" },
    );
    if (container.current) observer?.observe(container.current);
    return () => { document.removeEventListener("visibilitychange", update); observer?.disconnect(); };
  }, []);
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);
  url.searchParams.set("expected_account_id", accountId);
  const active = visible && foreground;
  return <div ref={container} className="relative flex h-[min(30vh,18rem)] min-h-48 items-center justify-center overflow-hidden rounded-lg border bg-well">
    {active && !failed ? <>
      {/* Native MJPEG works across worker origins without a fetch/CORS proxy. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img key={`${scope}:${attempt}`} src={url.href} alt={`Live screen of ${instance}`}
        onLoad={() => setLoaded(true)} onError={() => { setFailed(true); setLoaded(false); }}
        className="size-full object-contain" />
      {!loaded && <span className="pointer-events-none absolute bottom-2 rounded bg-background/90 px-2 py-1 text-[10px] text-muted-foreground">Connecting screen…</span>}
      <button type="button" aria-label={`Enlarge screen of ${instance}`} title="Open full screen"
        className="absolute right-2 top-2 rounded-md border bg-background/90 p-1.5 text-muted-foreground hover:text-foreground"
        onClick={() => { void container.current?.requestFullscreen?.().catch(() => {}); }}>
        <Maximize2 className="size-3.5" aria-hidden="true" />
      </button>
    </> : <div role="status" className="flex flex-col items-center gap-3 px-5 text-center text-xs text-muted-foreground">
      <MonitorOff className="size-6" aria-hidden="true" />
      <span>{failed ? "Screen unavailable. This worker may be reconnecting." : "Screen paused while out of view."}</span>
      {failed && <button className="rounded-md border px-3 py-1.5 text-foreground" onClick={() => { setFailed(false); setLoaded(false); setAttempt(value => value + 1); }}>Retry screen</button>}
    </div>}
  </div>;
}
