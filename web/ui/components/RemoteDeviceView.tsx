"use client";

import { useState } from "react";
import { FullscreenFrame } from "@/components/FullscreenFrame";
import { FeedBadge } from "@/components/FeedBadge";
import { LiveVideo } from "@/components/LiveVideo";
import { useLiveFallback } from "@/lib/useLiveFallback";
import { usePageVisible } from "@/lib/usePageVisible";

export function RemoteDeviceView({ dashboardUrl, scope, instance }: {
  dashboardUrl: string; scope: string; instance: string | null;
}) {
  const [failed, setFailed] = useState(false);
  const foreground = usePageVisible();
  const { live, markUnavailable } = useLiveFallback();
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);
  const label = `Live screen of ${instance ?? "selected emulator"}`;

  return <FullscreenFrame className="relative w-full max-w-[400px] overflow-hidden rounded-xl border bg-well">
    {failed ? <div role="status" className="flex aspect-[9/16] flex-col items-start gap-3 p-4 text-sm text-muted-foreground">
      <p>Live screen unavailable. The worker may be starting or reconnecting.</p>
      <button className="rounded border px-3 py-2 text-foreground" onClick={() => setFailed(false)}>Retry screen</button>
    </div> : !foreground ? <p role="status" className="flex aspect-[9/16] items-center justify-center p-4 text-sm text-muted-foreground">
      Screen paused while this tab is hidden.
    </p> : live ? <LiveVideo dashboardUrl={dashboardUrl} scope={scope} label={label}
      onUnavailable={markUnavailable} className="block aspect-[9/16] w-full object-contain" />
      : /* eslint-disable-next-line @next/next/no-img-element */
      <img key={url.href} src={url.href} alt={label}
        onError={() => setFailed(true)} className="block aspect-[9/16] w-full object-contain" />}
    <div className="flex h-7 items-center border-t px-3">{!failed && foreground && <FeedBadge live={live} />}</div>
  </FullscreenFrame>;
}
