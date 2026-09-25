"use client";

import { useRerollWorkspace } from "../RerollWorkspace";
import { WorkshopMatrix } from "./WorkshopMatrix";

export default function FleetWorkshopPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const members = [...(pool?.members ?? [])].filter(member => !member.hidden).sort((a, b) => a.name.localeCompare(b.name));
  return <main className="space-y-6">
    <header><p className="mb-2 font-mono text-xs uppercase tracking-[.22em] text-primary">Fleet intelligence / workshop</p>
      <h1 className="text-3xl font-semibold tracking-tight">Every upgrade, every emulator.</h1>
      <p className="mt-2 text-sm text-muted-foreground">Current Workshop level out of max and the coin price of the next level, side by side. Click an emulator to focus on it.</p></header>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {loading && !pool ? <p role="status">Loading fleet…</p>
      : !members.length ? <p className="rounded-xl border border-dashed p-10 text-center text-muted-foreground">No visible emulators in this fleet.</p>
      : <WorkshopMatrix members={members} />}
  </main>;
}
