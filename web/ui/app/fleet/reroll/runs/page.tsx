"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAccountRunHistory } from "@/lib/api";
import type { RunRow } from "@/lib/types";
import { useRerollWorkspace } from "../RerollWorkspace";
import { KilledByStrip } from "./KilledByStrip";
import { RunDetailPanel } from "./RunDetailPanel";
import { RunsTable, runKey } from "./RunsTable";
import { mergeRuns } from "./runsView";

type Source = { runs: RunRow[]; error: string | null };

export default function FleetRunsPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const members = useMemo(() => [...(pool?.members ?? [])].filter(m => !m.hidden)
    .sort((a, b) => a.name.localeCompare(b.name)), [pool]);
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => [name, account_key, account_id, lease_id]));
  const [sources, setSources] = useState<Record<string, Source>>({});
  const [off, setOff] = useState<Set<string>>(new Set());
  const [recordsOnly, setRecordsOnly] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const epoch = useRef(0);

  useEffect(() => {
    const generation = ++epoch.current;
    setSources({});
    for (const member of members) {
      (async () => {
        let source: Source;
        try {
          if (!member.account_key || !member.account_id) throw new Error("Waiting for a verified account.");
          source = { runs: await fetchAccountRunHistory(member.account_key, member.account_id), error: null };
        } catch (failure) {
          source = { runs: [], error: failure instanceof Error ? failure.message : "Runs unavailable" };
        }
        if (epoch.current === generation) setSources(current => ({ ...current, [member.name]: source }));
      })();
    }
  }, [identity]); // eslint-disable-line react-hooks/exhaustive-deps -- identity captures members

  const all = mergeRuns(members.filter(m => sources[m.name] && m.account_key && m.account_id).map(m => ({
    member: { name: m.name, account_key: m.account_key!, account_id: m.account_id! }, runs: sources[m.name].runs,
  })));
  const shown = all.filter(run => !off.has(run.emulator))
    .filter(run => !recordsOnly || run.wave_record || run.coin_record);
  const active = all.find(run => runKey(run) === selected) ?? null;
  const now = Date.now() / 1000;

  return <main className="flex h-full flex-col gap-4 p-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div><h1 className="text-xl font-bold">Runs</h1>
        <p className="text-sm text-muted-foreground">Every battle across the fleet, newest first. Select a run to see what it bought.</p></div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {members.map(member => <button key={member.name} type="button" aria-pressed={!off.has(member.name)}
          onClick={() => setOff(current => { const next = new Set(current); if (next.has(member.name)) next.delete(member.name); else next.add(member.name); return next; })}
          className={`h-8 rounded-full px-3 ${off.has(member.name) ? "border border-dashed text-muted-foreground" : "border bg-muted"}`}>{member.name}</button>)}
        <button type="button" aria-pressed={recordsOnly} onClick={() => setRecordsOnly(value => !value)}
          className={`h-8 rounded-full border px-3 ${recordsOnly ? "border-[oklch(0.82_0.14_85)] text-[oklch(0.82_0.14_85)]" : ""}`}>Records only</button>
      </div>
    </header>
    {error && <p role="alert" className="text-sm text-danger">Fleet unavailable: {error}</p>}
    {members.map(member => sources[member.name]?.error
      ? <p key={member.name} role="alert" className="text-sm text-danger">{member.name}: {sources[member.name].error}</p> : null)}
    {loading && !pool ? <p role="status" className="text-sm text-muted-foreground">Loading fleet…</p>
      : !members.length ? <p className="text-sm text-muted-foreground">No visible emulators in this reroll.</p>
      : <>
        <KilledByStrip runs={shown} />
        <div className="flex min-h-0 flex-1 gap-4">
          <RunsTable runs={shown} selected={selected} onSelect={run => setSelected(runKey(run))} now={now} />
          {active && <RunDetailPanel run={active} onClose={() => setSelected(null)} />}
        </div>
      </>}
  </main>;
}
