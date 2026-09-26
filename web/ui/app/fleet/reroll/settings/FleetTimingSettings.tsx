"use client";

import { useEffect, useState } from "react";
import { NumberField } from "@/components/ui/number-field";
import { SectionCard } from "@/components/ui/section-card";
import { fetchFleetTiming, saveFleetTiming } from "@/lib/api";
import { errorText } from "@/lib/utils";

/** One menu scan pace for every reroll worker. Battle pacing stays per worker. */
export function FleetTimingSettings(): React.JSX.Element {
  const [menuInterval, setMenuInterval] = useState<number | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchFleetTiming()
      .then((timing) => setMenuInterval(timing.menu_interval))
      .catch((reason: unknown) => setError(errorText(reason)));
  }, []);

  async function commit(next: number): Promise<void> {
    if (next === menuInterval) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveFleetTiming({ menu_interval: next });
      setMenuInterval(saved.menu_interval);
      setStatus(`Saved. Applied live to ${saved.workers_updated} running worker(s)`
        + (saved.workers_not_running ? `; ${saved.workers_not_running} will pick it up at next launch.` : "."));
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setSaving(false);
    }
  }

  return <SectionCard title="Timing" contentClassName="flex flex-col gap-3">
    <p className="text-xs text-muted-foreground">
      How long every worker waits between scans outside battle. Menu walks take one tap per scan, so lower is faster. Screen-to-screen taps are still limited by each worker&apos;s navigation cooldown.
    </p>
    {menuInterval !== null && <NumberField
      label="Scan interval, outside battle (s)" value={menuInterval} disabled={saving}
      min={0.1} max={3600} step={0.1} onCommit={(n) => void commit(n)}
    />}
    {status && <p role="status" className="text-sm text-muted-foreground">{status}</p>}
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
  </SectionCard>;
}
