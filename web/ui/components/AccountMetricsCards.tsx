"use client";

import { useEffect, useState } from "react";
import { fetchAccountMetrics } from "@/lib/api";
import { accountAge, coinsPerSecond, type AccountMetrics } from "@/lib/accountMetrics";

export function AccountMetricsCards({ accountKey }: { accountKey: string | undefined }) {
  const [metrics, setMetrics] = useState<AccountMetrics | null>(null);
  useEffect(() => {
    if (!accountKey) { setMetrics(null); return; }
    let alive = true;
    setMetrics(null);
    const load = () => fetchAccountMetrics().then(value => { if (alive) setMetrics(value); })
      .catch(() => { if (alive) setMetrics(null); });
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [accountKey]);
  if (!accountKey) return null;
  return <div className="grid gap-3 sm:grid-cols-2">
    <div className="rounded-xl border bg-card p-4">
      <p className="text-xs text-muted-foreground">Account age</p>
      <p className="mt-1 text-2xl font-bold">{accountAge(metrics?.account_age_days ?? null)}</p>
      <p className="mt-1 text-xs text-muted-foreground">{metrics?.game_started ? `Game Started: ${metrics.game_started} · date precision` : "Awaiting a readable Game Started stat."}</p>
    </div>
    <div className="rounded-xl border bg-card p-4">
      <p className="text-xs text-muted-foreground">Recent CPS</p>
      <p className="mt-1 text-2xl font-bold">{coinsPerSecond(metrics?.recent_cps ?? null)}</p>
      <p className="mt-1 text-xs text-muted-foreground">Game&apos;s Recent Coins Per Hour ÷ 3,600; last collected Stats reading.</p>
    </div>
  </div>;
}
