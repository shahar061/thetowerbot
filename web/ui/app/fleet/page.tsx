"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { fetchFleet, fetchFleetPreview, requestFleetProvision, resolveFleetTarget } from "@/lib/api";
import type { FleetPreview, FleetSnapshot } from "@/lib/fleet";

export default function FleetPage() {
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
  const [preview, setPreview] = useState<FleetPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"fresh" | "clone">("fresh");
  const [source, setSource] = useState("");
  const [count, setCount] = useState(1);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const refresh = () => fetchFleet().then(value => {
      if (!active) return;
      setFleet(value);
      setSource(current => current || value.sources.find(item => item.state === "parallel_session_qualified")?.instance || "");
      setError(null);
    }).catch((failure: Error) => { if (active) setError(failure.message); });
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    let active = true;
    const selectedSource = mode === "clone" ? source : null;
    setPreview(null);
    if (!fleet || (mode === "clone" && !source)) return;
    fetchFleetPreview(mode, selectedSource, count).then(value => {
      if (active && value.mode === mode && value.source === selectedSource && value.count === count)
        setPreview(value);
    }).catch((failure: Error) => {
      if (active) setPreview({ mode, source: selectedSource, count, targets: [],
                               state: "blocked", reason: failure.message });
    });
    return () => { active = false; };
  }, [mode, source, count, fleet]);

  const qualified = fleet?.sources.filter(item => item.state === "parallel_session_qualified") ?? [];
  const canRequest = preview?.state === "eligible" && preview.targets.length === count
    && confirmed && !busy && !error;
  const submit = async () => {
    if (!canRequest || !preview) return;
    setBusy(true);
    setError(null);
    try {
      await requestFleetProvision(preview);
      setConfirmed(false);
      setFleet(await fetchFleet());
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const resolve = async (jobId: string, index: number, action: "retry" | "quarantine") => {
    setBusy(true);
    setError(null);
    try {
      await resolveFleetTarget(jobId, index, action);
      setFleet(await fetchFleet());
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return <div className="mx-auto flex max-w-6xl flex-col gap-4">
    <PageHeader title="Fleet" meta="BlueStacks Air provisioning" />
    <p className="max-w-3xl text-sm text-muted-foreground">Fresh instances require a proven host create flow. Clones require a current, parallel session qualified template. A provisioned instance stays unavailable until exact host ownership and account evidence pass. No account is linked automatically.</p>
    {error && <p role="alert" className="rounded-md border border-danger p-3 text-sm text-danger">Fleet state or request failed: {error}. Refresh before trying again.</p>}
    <SectionCard title="Provision instances">
      {fleet?.unavailable && <p className="text-sm text-muted-foreground">Fleet unavailable: {fleet.unavailable}</p>}
      {!fleet && !error && <p className="text-sm">Loading fleet state…</p>}
      {fleet?.capacity && <p className="text-sm">Capacity: {fleet.capacity.used} of {fleet.capacity.limit} used; {fleet.capacity.available} available.</p>}
      <div className="mt-3 flex gap-4 text-sm">
        <label><input type="radio" name="provision-mode" checked={mode === "fresh"} onChange={() => { setMode("fresh"); setConfirmed(false); }} /> Fresh instance</label>
        <label><input type="radio" name="provision-mode" checked={mode === "clone"} onChange={() => { setMode("clone"); setConfirmed(false); }} /> Clone template</label>
      </div>
      {mode === "clone" && <>
        {fleet?.sources.map(item => <p key={item.instance} className="mt-2 text-sm"><strong>{item.instance}</strong> · {item.state} · {item.reason.replaceAll("_", " ")}{item.evidence_at ? ` · evidence ${new Date(item.evidence_at * 1000).toLocaleString()}` : ""}{item.evidence_url && <> · <a href={item.evidence_url}>View qualification evidence</a></>}</p>)}
        <label className="mt-3 flex flex-col gap-1 text-sm">Qualified template
          <select value={source} onChange={event => { setSource(event.target.value); setConfirmed(false); }} className="w-fit rounded-md border bg-background px-3 py-2">
            <option value="">Select a source</option>
            {qualified.map(item => <option key={item.instance} value={item.instance}>{item.instance}</option>)}
          </select>
        </label>
      </>}
      <label className="mt-3 flex flex-col gap-1 text-sm">Instances (1–5)
        <input type="number" min={1} max={5} value={count} onChange={event => { setCount(Math.max(1, Math.min(5, Number(event.target.value) || 1))); setConfirmed(false); }} className="w-28 rounded-md border bg-background px-3 py-2" />
      </label>
      {preview?.state === "blocked" && <p role="status" className="mt-3 text-sm text-danger">Blocked: {preview.reason?.replaceAll("_", " ")}</p>}
      {preview?.state === "eligible" && <p className="mt-3 text-sm">Target names: <span className="font-mono">{preview.targets.map((name, index) => <span key={name}>{index > 0 ? ", " : ""}{name}</span>)}</span></p>}
      <label className="mt-4 flex max-w-2xl items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> Confirm {mode === "clone" ? `cloning from ${source || "the selected source"}` : "fresh instance creation"} for the exact target names shown. Manager operations may require operator review.</label>
      <button onClick={submit} disabled={!canRequest} className="mt-3 rounded-md border px-3 py-2 text-sm disabled:opacity-50">{busy ? "Requesting…" : mode === "clone" ? "Request clones" : "Request fresh instances"}</button>
    </SectionCard>
    <SectionCard title="Provisioning">
      {!fleet?.jobs.length && <p className="text-sm text-muted-foreground">No provisioning requests recorded.</p>}
      {fleet?.jobs.map(job => <div key={job.id} className="mt-3 rounded-md border p-3 text-sm">
        <p className="font-medium">{job.mode} · {job.source || "new instance"} · {new Date(job.requested_at * 1000).toLocaleString()}</p>
        {job.manager_result_url && <a className="text-sm underline" href={job.manager_result_url}>View request and Manager steps</a>}
        <ol className="mt-2 space-y-2">{job.clones.map((clone, index) => <li key={index} className="rounded-md bg-muted p-2">
          <strong>{clone.instance || `Instance ${index + 1}`}</strong> · {clone.state} · {clone.reason.replaceAll("_", " ")}
          {clone.account_id && clone.state === "ready" && <span> · Tower account {clone.account_id}</span>}
          {clone.evidence_ref && <span> · Evidence: {clone.evidence_ref}</span>}
          {clone.registration_evidence_ref && <span> · Registration: {clone.registration_evidence_ref}</span>}
          {clone.steps && <details className="mt-1"><summary>Host steps ({clone.steps.length})</summary><ol>{clone.steps.map((step, stepIndex) => <li key={stepIndex}>{new Date(step.at * 1000).toLocaleString()} · {step.state} · {step.reason.replaceAll("_", " ")}{step.endpoint ? ` · ${step.endpoint}` : ""}</li>)}</ol></details>}
          {(clone.state === "blocked" || clone.state === "quarantined") && <div className="mt-2 flex gap-2">
            <button disabled={busy} onClick={() => resolve(job.id, index, "retry")} className="rounded border px-2 py-1 disabled:opacity-50">Retry exact target</button>
            <button disabled={busy} onClick={() => resolve(job.id, index, "quarantine")} className="rounded border px-2 py-1 disabled:opacity-50">Quarantine</button>
          </div>}
        </li>)}</ol>
      </div>)}
    </SectionCard>
  </div>;
}
