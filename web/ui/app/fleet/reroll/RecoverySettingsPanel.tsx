"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { fetchRecoverySettings, saveRecoverySettings } from "@/lib/api";
import { formatUsd, parseUsdMicros, type RecoverySettings, type RecoverySettingsResponse } from "@/lib/recovery";

const modelPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]*\/[A-Za-z0-9][A-Za-z0-9._:-]*$/;

export function RecoverySettingsPanel({ compact = false }: { compact?: boolean }): React.JSX.Element {
  const [saved, setSaved] = useState<RecoverySettingsResponse | null>(null);
  const [draft, setDraft] = useState<RecoverySettings | null>(null);
  const [shadowWorker, setShadowWorker] = useState("");
  const [incidentUsd, setIncidentUsd] = useState("");
  const [dailyUsd, setDailyUsd] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [mustReload, setMustReload] = useState(false);
  const requestEpoch = useRef(0);
  const savePending = useRef(false);

  async function reload(): Promise<void> {
    if (savePending.current) return;
    const epoch = ++requestEpoch.current;
    setLoading(true); setError(null);
    try {
      const result = await fetchRecoverySettings();
      if (epoch !== requestEpoch.current) return;
      setSaved(result); setDraft(result.settings); setShadowWorker(result.shadow_worker ?? "");
      setIncidentUsd(formatUsd(result.settings.incident_limit_microusd).slice(1));
      setDailyUsd(formatUsd(result.settings.daily_limit_microusd).slice(1));
      setMustReload(false); setNotice(null);
    } catch (reason) {
      if (epoch === requestEpoch.current) setError((reason as Error).message);
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  }

  useEffect(() => { void reload(); }, []);

  function set<K extends keyof RecoverySettings>(field: K, value: RecoverySettings[K]): void {
    if (saving || loading) return;
    setDraft(previous => previous ? { ...previous, [field]: value } : previous);
    setNotice(null);
  }

  const incidentMicros = parseUsdMicros(incidentUsd);
  const dailyMicros = parseUsdMicros(dailyUsd);
  const valid = draft !== null && draft.mode !== "assist"
    && (draft.mode !== "shadow" || (shadowWorker.trim() === shadowWorker && shadowWorker.length > 0 && shadowWorker.length <= 160))
    && modelPattern.test(draft.model)
    && draft.deadline_seconds > 0 && draft.deadline_seconds <= 15
    && Number.isInteger(draft.max_calls) && draft.max_calls >= 1 && draft.max_calls <= 2
    && Number.isInteger(draft.max_actions) && draft.max_actions >= 1 && draft.max_actions <= 3
    && draft.cooldown_seconds >= 600 && draft.cooldown_seconds <= 86_400
    && incidentMicros !== null && incidentMicros <= 50_000
    && dailyMicros !== null && dailyMicros <= 1_000_000;
  const changed = saved !== null && draft !== null && (JSON.stringify(saved.settings) !== JSON.stringify(draft)
    || (draft.mode === "shadow" && shadowWorker !== (saved.shadow_worker ?? ""))
    || incidentMicros !== saved.settings.incident_limit_microusd
    || dailyMicros !== saved.settings.daily_limit_microusd);

  async function save(): Promise<void> {
    if (!saved || !draft || !valid || !changed || saving || savePending.current || mustReload) return;
    ++requestEpoch.current; // A pending GET cannot replace this reviewed mutation.
    savePending.current = true;
    setSaving(true); setLoading(false); setError(null); setNotice(null);
    try {
      const result = await saveRecoverySettings({ ...draft,
        incident_limit_microusd: incidentMicros!,
        daily_limit_microusd: dailyMicros!,
      }, draft.mode === "shadow" ? shadowWorker : null,
        saved.settings_revision, saved.policy.active.revision);
      setSaved(result); setDraft(result.settings); setShadowWorker(result.shadow_worker ?? "");
      setIncidentUsd(formatUsd(result.settings.incident_limit_microusd).slice(1));
      setDailyUsd(formatUsd(result.settings.daily_limit_microusd).slice(1));
      setMustReload(false);
      setNotice("Saved. Worker application remains unknown until current status is reported.");
    } catch (reason) {
      setMustReload(true);
      setError(`${(reason as Error).message}. Reload the active policy before retrying.`);
    } finally {
      savePending.current = false;
      setSaving(false);
    }
  }

  if (compact) return <section aria-label="Model recovery overview" className="rounded-xl border bg-card p-3 shadow-sm sm:p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-heading text-base font-semibold">Model recovery</h2>
      <Link href="/fleet/reroll/settings/" className="inline-flex min-h-11 items-center rounded-md px-2 text-sm font-medium text-primary hover:underline">Recovery settings →</Link>
    </div>
    {loading && <p role="status" className="text-sm">Loading recovery status…</p>}
    {/* A status, not an alert: this read-only summary must not pre-empt the Live page's own alerts. */}
    {error && <p role="status" className="text-sm text-danger">Recovery status unavailable: {error}</p>}
    {saved && <div className="mt-2 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
      <p>Saved mode <strong>{saved.settings.mode}</strong>{saved.shadow_worker ? ` · ${saved.shadow_worker}` : ""}</p>
      <p>Worker application <strong>{saved.status.producer === "unknown" ? "unknown" : `${saved.status.workers.length} reported`}</strong> · per-worker status on each Live card</p>
      <p>Active day cap <strong>{formatUsd(saved.policy.active.daily_limit_microusd)}</strong></p>
      <p>Day remaining <strong>{saved.daily_budget ? formatUsd(saved.daily_budget.remaining_microusd) : "unknown"}</strong></p>
      {saved.policy.requested_policy_conflict && <p role="alert" className="text-warn sm:col-span-2 lg:col-span-4">Requested and active budget caps differ; review settings.</p>}
      {saved.policy.active.disabled_request_id && <p role="alert" className="text-danger sm:col-span-2 lg:col-span-4">Paid calls disabled after overcharge.</p>}
    </div>}
  </section>;

  return <section aria-label="Model recovery settings" className="rounded-xl border bg-card p-3 shadow-sm sm:p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-heading text-base font-semibold">Model recovery</h2>
      <Button variant="outline" className="min-h-11" onClick={() => void reload()} disabled={loading || saving}>Reload status</Button>
    </div>
    {loading && <p role="status" className="mt-2 text-sm">Loading recovery settings…</p>}
    {error && <p role="alert" className="mt-2 rounded-lg border border-danger p-3 text-sm text-danger">{error}</p>}
    {notice && <p role="status" className="mt-2 text-sm">{notice}</p>}
    {saved && draft && <div className="mt-3 space-y-4 text-sm">
      <p>Deterministic recovery remains available when OpenRouter is unconfigured, offline, rate limited or over budget.</p>
      <p>Set <code>CLAUDE_OPENROUTER_API_KEY</code> in the backend environment and restart the worker. The web app never stores or accepts the key.</p>
      <p role="status">{saved.status.producer === "unknown" ? "Worker recovery status unknown; no current producer evidence." : `${saved.status.workers.length} worker statuses reported.`}</p>
      <div className="grid grid-cols-1 gap-2 rounded-lg bg-well p-3 sm:grid-cols-2">
        <p>Active policy revision <strong>{saved.policy.active.revision}</strong></p>
        <p>Settings revision <strong>{saved.settings_revision}</strong></p>
        <p>Active incident cap <strong>{formatUsd(saved.policy.active.incident_limit_microusd)}</strong></p>
        <p>Active fleet day cap <strong>{formatUsd(saved.policy.active.daily_limit_microusd)}</strong></p>
        <p>Active calls per incident <strong>{saved.policy.active.max_calls}</strong></p>
        <p>Paid calls <strong>{saved.policy.active.disabled_request_id ? "Disabled after overcharge" : "Not disabled by overcharge"}</strong></p>
        <p>Fleet day committed <strong>{saved.daily_budget ? formatUsd(saved.daily_budget.total_microusd) : "Unknown"}</strong></p>
        <p>Fleet day remaining <strong>{saved.daily_budget ? formatUsd(saved.daily_budget.remaining_microusd) : "Unknown"}</strong></p>
      </div>
      {saved.budget_observed_at_utc && <p className="text-xs text-muted-foreground">Budget snapshot observed {saved.budget_observed_at_utc}. It can change after this read.</p>}
      {saved.policy.requested_policy_conflict && <p role="alert" className="rounded-lg border border-warn/40 p-3 text-warn">Requested budget differs from the active fleet policy. Review the active caps and revisions before saving.</p>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex min-w-0 flex-col gap-1">Recovery mode
          <select aria-label="Recovery mode" className="min-h-11 rounded-md border bg-background px-3" value={draft.mode}
            disabled={loading || saving}
            onChange={event => set("mode", event.target.value as RecoverySettings["mode"])}>
            <option value="off">Off</option><option value="shadow">Shadow</option>
            <option value="assist" disabled={saved.assist_allowed_actions.length === 0}>Assist — requires replay and recorded canary</option>
          </select>
        </label>
        <label className="flex min-w-0 flex-col gap-1">Model
          <input aria-label="Model" className="min-h-11 min-w-0 rounded-md border bg-background px-3" value={draft.model}
            disabled={loading || saving}
            onChange={event => set("model", event.target.value)} />
        </label>
        {draft.mode === "shadow" && <label className="flex min-w-0 flex-col gap-1">Shadow worker ID
          <input aria-label="Shadow worker ID" className="min-h-11 min-w-0 rounded-md border bg-background px-3" value={shadowWorker}
            disabled={loading || saving}
            onChange={event => { if (loading || saving) return; setShadowWorker(event.target.value); setNotice(null); }} placeholder="Stable worker name" />
        </label>}
        {([
          ["deadline_seconds", "Request deadline (seconds)", 0.1, 15, 0.1],
          ["max_calls", "Calls per incident", 1, 2, 1],
          ["max_actions", "Recovery actions per incident", 1, 3, 1],
          ["cooldown_seconds", "Fingerprint cooldown (seconds)", 600, 86400, 1],
        ] as const).map(([field, label, min, max, step]) => <label key={field} className="flex min-w-0 flex-col gap-1">{label}
          <input aria-label={label} type="number" min={min} max={max} step={step}
            className="min-h-11 min-w-0 rounded-md border bg-background px-3" value={draft[field]} disabled={loading || saving}
            onChange={event => set(field, Number(event.target.value))} />
        </label>)}
        <label className="flex min-w-0 flex-col gap-1">Incident cap (USD)
          <input aria-label="Incident cap (USD)" type="text" inputMode="decimal" value={incidentUsd}
            className="min-h-11 min-w-0 rounded-md border bg-background px-3" disabled={loading || saving}
            onChange={event => { if (loading || saving) return; setIncidentUsd(event.target.value); setNotice(null); }} />
        </label>
        <label className="flex min-w-0 flex-col gap-1">Fleet day cap (USD)
          <input aria-label="Fleet day cap (USD)" type="text" inputMode="decimal" value={dailyUsd}
            className="min-h-11 min-w-0 rounded-md border bg-background px-3" disabled={loading || saving}
            onChange={event => { if (loading || saving) return; setDailyUsd(event.target.value); setNotice(null); }} />
        </label>
      </div>
      <p className="text-muted-foreground">Start with shadow on one worker. Assist is unavailable until each action class passes local replay and recorded canary postcondition checks.</p>
      <div className="flex flex-wrap gap-2">
        <Button className="min-h-11" disabled={!valid || !changed || saving || mustReload} onClick={() => void save()}>Save recovery settings</Button>
        <Button variant="outline" className="min-h-11" disabled={!changed || saving || loading} onClick={() => { setDraft(saved.settings); setShadowWorker(saved.shadow_worker ?? ""); setIncidentUsd(formatUsd(saved.settings.incident_limit_microusd).slice(1)); setDailyUsd(formatUsd(saved.settings.daily_limit_microusd).slice(1)); setError(null); }}>Reset changes</Button>
      </div>
    </div>}
  </section>;
}
