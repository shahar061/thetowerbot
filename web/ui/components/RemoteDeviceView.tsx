"use client";

import { useState } from "react";

export function RemoteDeviceView({ dashboardUrl, scope, instance }: {
  dashboardUrl: string; scope: string; instance: string | null;
}) {
  const [failed, setFailed] = useState(false);
  const url = new URL("api/frame", dashboardUrl);
  url.searchParams.set("scope", scope);

  return <div className="w-full max-w-[400px] overflow-hidden rounded-xl border bg-well">
    {failed ? <div role="status" className="flex aspect-[9/16] flex-col items-start gap-3 p-4 text-sm text-muted-foreground">
      <p>Live screen unavailable. The worker may be starting or reconnecting.</p>
      <button className="rounded border px-3 py-2 text-foreground" onClick={() => setFailed(false)}>Retry screen</button>
    </div> : /* eslint-disable-next-line @next/next/no-img-element */
      <img key={url.href} src={url.href} alt={`Live screen of ${instance ?? "selected emulator"}`}
        onError={() => setFailed(true)} className="block aspect-[9/16] w-full object-contain" />}
  </div>;
}
