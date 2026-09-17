"use client";

import { useEffect, useState } from "react";
import { fetchFleetInstances, fetchHostStatus, startFleetInstance, type FleetInstances } from "@/lib/api";

export function EmulatorRecovery() {
  const [inventory, setInventory] = useState<FleetInstances | null>(null);
  const [botError, setBotError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetchFleetInstances().then(value => { if (active) setInventory(value); })
      .catch(failure => { if (active) setError((failure as Error).message); });
    fetchHostStatus().then(value => { if (active) setBotError(value.bot?.error ?? null); }).catch(() => {});
    return () => { active = false; };
  }, []);

  const start = async (name: string) => {
    setBusy(name);
    setError(null);
    try { setInventory(await startFleetInstance(name)); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(null); }
  };

  return <section className="mt-3 rounded-md border bg-muted/20 p-4" aria-label="Available emulators">
    <h2 className="font-medium">Choose an emulator</h2>
    {botError && <p role="status" className="mt-2 text-sm text-warn">The bot could not connect: {botError}</p>}
    <p className="mt-2 text-sm text-muted-foreground">Starting an emulator leaves The Tower closed. The bot connects only after it is launched for that emulator’s ADB endpoint.</p>
    {error && <p role="alert" className="mt-2 text-sm text-danger">Could not load or start an emulator: {error}</p>}
    {!inventory && !error && <p className="mt-3 text-sm">Checking BlueStacks Air…</p>}
    {inventory && <div className="mt-3 flex flex-col gap-2">
      {inventory.instances.length === 0 && <p className="text-sm">No BlueStacks Air instances were found. Set up Fleet to create one.</p>}
      {inventory.instances.map(item => <div key={item.name} className="flex flex-wrap items-center gap-2 rounded-md border bg-background p-3 text-sm">
        <strong>{item.name}</strong>
        <span className="font-mono text-xs text-muted-foreground">{item.endpoint}</span>
        <span className="text-xs text-muted-foreground">{item.template ? "Unopened template · keep Tower closed" : item.state}</span>
        {item.state === "stopped" && <button type="button" disabled={!inventory.can_start || busy !== null}
          onClick={() => void start(item.name)} className="ml-auto rounded-md border px-3 py-1.5 disabled:opacity-50">
          {busy === item.name ? "Starting…" : "Start emulator"}
        </button>}
      </div>)}
      {!inventory.can_start && <p className="text-xs text-muted-foreground">Save Fleet setup to enable starting instances from here.</p>}
    </div>}
  </section>;
}
