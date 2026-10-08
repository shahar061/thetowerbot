"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { amount } from "@/app/fleet/state/stateFormat";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/ui/section-card";
import { fetchLabSavings, saveLabSavings } from "@/lib/api";
import type { FleetStateCoinSplit } from "@/lib/fleetState";
import type { LabSavingsSettings, LabShareMode } from "@/lib/singleAccount";
import { errorText } from "@/lib/utils";

interface LabSavingsProps {
  accountKey: string;
  expectedAccountId?: string | null;
  split: FleetStateCoinSplit | null;
  onSaved: () => void;
}

type Draft = { mode: LabShareMode; pct: string; revision: number };

function draftFor(settings: LabSavingsSettings): Draft | null {
  if (settings.share_mode === null || settings.share_pct === null || settings.revision === null) return null;
  return { mode: settings.share_mode, pct: String(settings.share_pct), revision: settings.revision };
}

/** Remounting discards drafts and invalidates pending requests on account changes. */
export function LabSavings(props: LabSavingsProps): React.JSX.Element {
  return <LabSavingsForm key={`${props.accountKey}:${props.expectedAccountId ?? ""}`} {...props} />;
}

function LabSavingsForm({ expectedAccountId, split, onSaved }: LabSavingsProps): React.JSX.Element {
  const id = useId();
  const [settings, setSettings] = useState<LabSavingsSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const alive = useRef(false);
  const request = useRef(0);
  const dirty = useRef(false);
  const busy = useRef(false);

  const refresh = useCallback(async (discardDraft = false): Promise<void> => {
    if (busy.current) return;
    const ticket = ++request.current;
    if (discardDraft) setLoading(true);
    try {
      const value = await fetchLabSavings();
      if (!alive.current || ticket !== request.current) return;
      setSettings(value);
      if (discardDraft || !dirty.current) {
        setDraft(draftFor(value));
        dirty.current = false;
        setConflict(false);
        setError(null);
        setNotice(null);
      }
    } catch (reason) {
      if (alive.current && ticket === request.current) setError(errorText(reason));
    } finally {
      if (alive.current && ticket === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 15_000);
    return () => { alive.current = false; request.current += 1; window.clearInterval(timer); };
  }, [refresh]);

  const wrongAccount = settings !== null && expectedAccountId !== undefined && settings.account_id !== expectedAccountId;
  const changed = settings !== null && draft !== null && (
    draft.mode !== settings.share_mode || draft.pct !== String(settings.share_pct)
  );
  const staleDraft = draft !== null && settings !== null && draft.revision !== settings.revision;
  const pct = Number(draft?.pct);
  const valid = draft !== null && draft.pct.trim() !== "" && Number.isInteger(pct) && pct >= 5 && pct <= 90;
  const available = settings !== null && settings.share_mode !== null && settings.share_pct !== null && settings.revision !== null;
  const editable = available && !!settings?.editable && !!settings.account_id && !wrongAccount;
  const disabled = !editable || saving || loading;
  const canSave = changed && valid && !disabled && !conflict && !staleDraft;

  function edit(next: Draft): void {
    dirty.current = settings !== null && (next.mode !== settings.share_mode || next.pct !== String(settings.share_pct));
    setDraft(next);
    setNotice(null);
  }

  function cancel(): void {
    if (!settings) return;
    setDraft(draftFor(settings));
    dirty.current = false;
    setError(null);
    setNotice(null);
    setConflict(false);
  }

  async function save(): Promise<void> {
    if (!canSave || !draft || !settings?.account_id || busy.current) return;
    busy.current = true;
    const ticket = ++request.current;
    const expectedAccount = settings.account_id;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const value = await saveLabSavings({ expected_account_id: expectedAccount,
        expected_revision: draft.revision, share_mode: draft.mode, share_pct: pct });
      if (!alive.current || ticket !== request.current) return;
      if (value.account_id !== expectedAccount) {
        setConflict(true);
        throw new Error("Settings came from a different account. Reload before saving.");
      }
      setSettings(value);
      setDraft(draftFor(value));
      dirty.current = false;
      setConflict(false);
      setNotice("Lab settings saved. They apply at the next planning cycle.");
      onSaved();
    } catch (reason) {
      if (!alive.current || ticket !== request.current) return;
      setError(errorText(reason));
      if (reason && typeof reason === "object" && "status" in reason && reason.status === 409) setConflict(true);
    } finally {
      if (alive.current && ticket === request.current) { busy.current = false; setSaving(false); }
    }
  }

  const target = split?.jar_target;
  const targetPct = target?.price != null && target.price > 0 && split !== null
    ? Math.max(0, Math.min(100, split.jar / target.price * 100)) : null;

  return <SectionCard title="Lab savings" className="min-w-0" id="lab-savings"
    action={settings && <span className="text-xs text-muted-foreground">{!available ? "Unavailable" : settings.auto_start ? "Automatic labs" : "Labs paused"}</span>}>
    <dl className="grid grid-cols-3 gap-2 rounded-lg bg-muted/40 p-3">
      {[["Coin wallet", split?.wallet], ["Lab jar", split?.jar], ["Workshop budget", split?.workshop_budget]].map(([label, value]) =>
        <div key={label} className="min-w-0"><dt className="text-[11px] text-muted-foreground">{label}</dt>
          <dd className="mt-1 font-mono text-lg font-semibold tabular-nums sm:text-xl">{amount(value as number | undefined)}</dd></div>)}
    </dl>
    {target && <div className="space-y-2 text-sm">
      <div className="flex flex-wrap justify-between gap-x-3 gap-y-1">
        <span className="min-w-0 break-words">Saving for <b>{target.name}</b>{target.level !== null && ` · level ${target.level}`}</span>
        <span className="font-mono text-xs text-muted-foreground">{amount(split?.jar)} / {target.price === null ? "price unknown" : amount(target.price)}</span>
      </div>
      {targetPct !== null && <div role="progressbar" aria-label="Lab savings target" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(targetPct)} className="h-2 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${targetPct}%` }} />
      </div>}
    </div>}
    {loading && <p role="status" className="text-sm text-muted-foreground">Loading lab settings…</p>}
    {wrongAccount && <p role="alert" className="text-sm text-danger">Settings came from a different account. Reload before saving.</p>}
    {error && <p role="alert" className="break-words text-sm text-danger">{error}</p>}
    {settings?.reason && <p className="text-sm text-muted-foreground">{settings.reason}</p>}
    {settings && !available && !settings.reason && <p className="text-sm text-muted-foreground">Lab savings settings are unavailable for this account.</p>}
    {settings && available && !settings.auto_start && <p className="rounded-md bg-warn/10 p-3 text-sm text-warn">
      Automatic lab starts are paused. Saving settings will not activate the jar until they resume.
    </p>}
    {draft && <>
      <fieldset disabled={disabled} className="min-w-0 space-y-3 disabled:opacity-60">
        <div className="space-y-1.5">
          <label htmlFor={`${id}-mode`} className="text-sm font-medium">Lab savings mode</label>
          <select id={`${id}-mode`} value={draft.mode} onChange={event => edit({ ...draft, mode: event.target.value as LabShareMode })}
            className="min-h-11 w-full min-w-0 rounded-md border bg-background px-3 text-sm">
            <option value="save_pct">Save a share of income</option>
            <option value="when_affordable">Start labs when affordable</option>
            <option value="labs_first">Labs first · pause Workshop while waiting</option>
            <option value="just_in_time" disabled={settings?.share_mode !== "just_in_time"}>Just in time · needs a ranked lab list</option>
          </select>
        </div>
        {draft.mode === "save_pct" ? <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label htmlFor={`${id}-pct`} className="text-sm font-medium">Income saved for labs (%)</label>
            <input id={`${id}-pct`} type="number" min={5} max={90} step={1} inputMode="numeric" value={draft.pct}
              onChange={event => edit({ ...draft, pct: event.target.value })} aria-invalid={!valid}
              aria-describedby={`${id}-explanation`} className="min-h-11 w-24 rounded-md border bg-background px-3 font-mono text-sm" />
          </div>
          <input type="range" min={5} max={90} step={1} value={valid ? pct : 5} aria-label="Lab savings percentage"
            onChange={event => edit({ ...draft, pct: event.target.value })} className="h-11 w-full accent-primary" />
          <div className="grid grid-cols-4 gap-2">
            {[10, 25, 50, 75].map(value => <Button key={value} type="button" variant={pct === value ? "secondary" : "outline"}
              aria-pressed={pct === value} className="min-h-11" onClick={() => edit({ ...draft, pct: String(value) })}>{value}%</Button>)}
          </div>
          {!valid && <p className="text-xs text-danger">Enter a whole percentage from 5 to 90.</p>}
          <p id={`${id}-explanation`} className="text-xs leading-relaxed text-muted-foreground">
            The jar sets aside {valid ? `${pct}%` : "a share"} of new coin income, up to the next lab&apos;s cost. A fresh jar may start from spare coins already in your wallet. Workshop uses its remaining budget.
          </p>
        </> : <p className="text-xs leading-relaxed text-muted-foreground">
          {draft.mode === "when_affordable" ? "Labs start when the wallet can afford them; no percentage jar is reserved."
            : draft.mode === "labs_first" ? "Workshop waits while coins are needed for the next lab."
            : "The ranked lab list determines how much to reserve for upcoming research."}
        </p>}
      </fieldset>
      {staleDraft && <p className="text-sm text-warn">Settings changed elsewhere. Reload current settings or cancel your changes before saving.</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="button" className="min-h-11" disabled={!canSave} onClick={() => { void save(); }}>{saving ? "Saving…" : "Save lab settings"}</Button>
        <Button type="button" variant="outline" className="min-h-11" disabled={(!changed && !conflict && !staleDraft) || saving || loading} onClick={cancel}>Cancel changes</Button>
      </div>
    </>}
    {(conflict || staleDraft || wrongAccount || (!settings && error)) && <Button type="button" variant="outline" className="min-h-11 self-start"
      disabled={saving || loading} onClick={() => { void refresh(true); }}>Reload current settings</Button>}
    {notice && <p role="status" className="text-sm text-live">{notice}</p>}
  </SectionCard>;
}
