"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchFleet, requestFleetClones } from "@/lib/api";
import type { FleetSnapshot } from "@/lib/fleet";

export default function FleetPage() {
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState("");
  const [count, setCount] = useState(1);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const refresh = () => fetchFleet().then(value => {
      if (!active) return;
      setFleet(value);
      setSource(current => current || value.sources.find(item => item.state === "qualified")?.instance || "");
      setError(null);
    }).catch((failure: Error) => { if (active) setError(failure.message); });
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  const qualified = fleet?.sources.filter(item => item.state === "qualified") ?? [];
  const canRequest = qualified.some(item => item.instance === source) && confirmed && !busy && !error;
  const submit = async () => {
    if (!canRequest) return;
    setBusy(true);
    setError(null);
    try {
      await requestFleetClones(source, count);
      setConfirmed(false);
      setFleet(await fetchFleet());
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return <div className="mx-auto flex max-w-6xl flex-col gap-4">
    <PageHeader title="Fleet" meta="BlueStacks Air clone staging" />
    <p className="max-w-3xl text-sm text-muted-foreground">Only a source with current live qualification can be selected. New clones stay unavailable until their account identity is reset and fresh evidence passes. No account is linked automatically.</p>
    {error && <p role="alert" className="rounded-md border border-danger p-3 text-sm text-danger">Fleet state or request failed: {error}. Refresh before trying again.</p>}
    <SectionCard title="Clone source">
      {fleet?.unavailable && <p className="text-sm text-muted-foreground">Fleet unavailable: {fleet.unavailable}</p>}
      {!fleet && !error && <p className="text-sm">Loading fleet state…</p>}
      {fleet?.sources.map(item => <p key={item.instance} className="mt-2 text-sm"><strong>{item.instance}</strong> · {item.state} · {item.reason.replaceAll("_", " ")}</p>)}
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">Qualified template
          <select value={source} onChange={event => { setSource(event.target.value); setConfirmed(false); }} className="rounded-md border bg-background px-3 py-2">
            <option value="">Select a source</option>
            {qualified.map(item => <option key={item.instance} value={item.instance}>{item.instance}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">Clones (1–5)
          <input type="number" min={1} max={5} value={count} onChange={event => { setCount(Math.max(1, Math.min(5, Number(event.target.value) || 1))); setConfirmed(false); }} className="w-28 rounded-md border bg-background px-3 py-2" />
        </label>
      </div>
      <label className="mt-4 flex max-w-2xl items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> Confirm staging {count} clone{count === 1 ? "" : "s"} from {source || "the selected source"}. This creates BlueStacks instances; identity reset may pause for operator review.</label>
      <button onClick={submit} disabled={!canRequest} className="mt-3 rounded-md border px-3 py-2 text-sm disabled:opacity-50">{busy ? "Requesting…" : "Request clones"}</button>
    </SectionCard>
    <SectionCard title="Provisioning">
      {!fleet?.jobs.length && <p className="text-sm text-muted-foreground">No clone requests recorded.</p>}
      {fleet?.jobs.map(job => <div key={job.id} className="mt-3 rounded-md border p-3 text-sm">
        <p className="font-medium">{job.source} · {new Date(job.requested_at * 1000).toLocaleString()}</p>
        <ol className="mt-2 space-y-2">{job.clones.map((clone, index) => <li key={index} className="rounded-md bg-muted p-2">
          <strong>{clone.instance || `Clone ${index + 1}`}</strong> · {clone.state} · {clone.reason.replaceAll("_", " ")}
          {clone.account_id && clone.state === "ready" && <span> · Tower account {clone.account_id}</span>}
        </li>)}</ol>
      </div>)}
    </SectionCard>
  </div>;
}
