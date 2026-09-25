"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/ui/section-card";
import { Switch } from "@/components/ui/switch";
import { fetchTelegramSettings, previewTelegramMessage, saveTelegramSettings } from "@/lib/api";
import { TELEGRAM_FIELDS } from "@/lib/telegram";
import type { TelegramMode, TelegramProfile, TelegramSettingsResponse } from "@/lib/telegram";
import { errorText } from "@/lib/utils";

function validMinutes(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const minutes = Number(value);
  return Number.isInteger(minutes) && minutes >= 1 && minutes <= 1440 ? minutes : null;
}

export function TelegramSettings({ mode }: { mode: TelegramMode }): React.JSX.Element {
  const [saved, setSaved] = useState<TelegramSettingsResponse | null>(null);
  const [draft, setDraft] = useState<TelegramProfile | null>(null);
  const [minutesInput, setMinutesInput] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    setSaved(null);
    setDraft(null);
    setPreview(null);
    void fetchTelegramSettings(mode).then(value => {
      if (!current) return;
      setSaved(value);
      setDraft(value.profile);
      setMinutesInput(String(value.profile.interval_minutes));
    }).catch(reason => {
      if (current) setError(errorText(reason));
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [mode]);

  useEffect(() => {
    if (!draft) return;
    let current = true;
    setPreview(null);
    setPreviewError(null);
    void previewTelegramMessage(mode, draft).then(result => {
      if (current) setPreview(result.message);
    }).catch(reason => {
      if (current) setPreviewError(errorText(reason));
    });
    return () => { current = false; };
  }, [mode, draft]);

  const minutes = validMinutes(minutesInput);
  const changed = saved !== null && draft !== null && (
    minutesInput !== String(saved.profile.interval_minutes)
    || draft.enabled !== saved.profile.enabled
    || JSON.stringify(draft.fields) !== JSON.stringify(saved.profile.fields)
  );

  function toggleField(field: string, checked: boolean): void {
    setDraft(previous => {
      if (!previous) return previous;
      const fields = TELEGRAM_FIELDS[mode].map(item => item.id).filter(id =>
        id === field ? checked : previous.fields.includes(id));
      return { ...previous, fields };
    });
    setNotice(null);
  }

  function reset(): void {
    if (!saved) return;
    setDraft(saved.profile);
    setMinutesInput(String(saved.profile.interval_minutes));
    setError(null);
    setNotice(null);
  }

  async function save(): Promise<void> {
    if (!draft || minutes === null || saving) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await saveTelegramSettings(mode, { ...draft, interval_minutes: minutes });
      setSaved(result);
      setDraft(result.profile);
      setMinutesInput(String(result.profile.interval_minutes));
      setNotice(result.profile.enabled ? "Saved. Future updates use these settings." : "Saved. Telegram updates are off.");
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setSaving(false);
    }
  }

  const title = mode === "fleet" ? "Fleet Telegram settings" : "Telegram settings";

  return <div className="flex max-w-3xl flex-col gap-5">
    <PageHeader title={title} meta={mode === "fleet" ? "one combined fleet update" : "single emulator updates"} />
    <p className="max-w-[68ch] text-sm text-muted-foreground">
      Choose how often Telegram receives an update and which details appear in it. The preview uses sample data and does not send a message.
    </p>

    {loading && <p role="status" className="text-sm text-muted-foreground">Loading Telegram settings…</p>}
    {error && <p role="alert" className="rounded-md border border-danger p-3 text-sm text-danger">{error}</p>}

    {saved && draft && <>
      <SectionCard title="Connection">
        {saved.configured
          ? <p className="text-sm">Telegram is configured{saved.chat_id_masked ? ` for chat ${saved.chat_id_masked}` : ""}.</p>
          : <p className="text-sm text-warn">Sending is unavailable until you set {[
              !saved.token_present && <code key="token">TELEGRAM_BOT_TOKEN</code>,
              !saved.chat_id_present && <code key="chat">TELEGRAM_CHAT_ID</code>,
            ].filter(Boolean).map((part, index) => <span key={index}>{index > 0 ? " and " : null}{part}</span>)} in the bot&apos;s environment and restart it.</p>}
        {saved.suppressed && <p className="text-sm text-warn">This process has Telegram suppressed by its startup options. Saved choices remain available for a later run.</p>}
        {saved.interval_overridden && <p className="text-sm text-warn">A command-line interval override is active{saved.effective_interval_seconds !== null ? ` (${saved.effective_interval_seconds} seconds)` : ""}. Saving a frequency here will not change this process&apos;s send interval.</p>}
        {mode === "fleet" && <p className="text-xs text-muted-foreground">The coordinator sends one update for the fleet. Individual workers do not send their own digests.</p>}
      </SectionCard>

      <SectionCard title="Schedule">
        <div className="flex items-center justify-between gap-4">
          <div><p className="text-sm font-medium">Telegram updates</p><p className="text-xs text-muted-foreground">Turn periodic messages on or off for this workspace.</p></div>
          <Switch label="Telegram updates" checked={draft.enabled}
            onCheckedChange={enabled => { setDraft({ ...draft, enabled }); setNotice(null); }} />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label htmlFor={`telegram-minutes-${mode}`} className="text-sm font-medium">Update every</label>
          <input id={`telegram-minutes-${mode}`} type="number" min={1} max={1440} step={1}
            value={minutesInput} onChange={event => { setMinutesInput(event.target.value); setNotice(null); }}
            aria-invalid={minutes === null}
            className="w-28 rounded-md border bg-background px-3 py-1.5 font-mono text-sm" />
          <span className="text-sm text-muted-foreground">minutes</span>
        </div>
        {minutes === null && <p className="text-xs text-danger">Enter a whole number from 1 to 1,440.</p>}
      </SectionCard>

      <SectionCard title="Message contents">
        <p className="text-xs text-muted-foreground">
          {mode === "fleet" ? "Fleet status, emulator count, and each emulator’s name and state are always included." : "Running state and uptime are always included."}
        </p>
        <div className="divide-y divide-border">
          {TELEGRAM_FIELDS[mode].map(field => <div key={field.id} className="flex items-center justify-between gap-4 py-3">
            <div><p className="text-sm font-medium">{field.label}</p><p className="text-xs text-muted-foreground">{field.description}</p></div>
            <Switch label={field.label} checked={draft.fields.includes(field.id)}
              onCheckedChange={checked => toggleField(field.id, checked)} />
          </div>)}
        </div>
      </SectionCard>

      <SectionCard title="Preview">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Sample message — no message sent</p>
        {previewError ? <p role="alert" className="text-sm text-danger">Preview unavailable: {previewError}</p>
          : <pre aria-label="Sample Telegram message" className="overflow-x-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-4 font-mono text-xs leading-relaxed">{preview ?? "Loading sample…"}</pre>}
      </SectionCard>

      <div className="flex flex-wrap items-center gap-2 pb-6">
        <Button type="button" onClick={() => void save()} disabled={!changed || minutes === null || saving}>Save settings</Button>
        <Button type="button" variant="outline" onClick={reset} disabled={saving}>Reset changes</Button>
        {notice && <p role="status" className="text-sm text-live">{notice}</p>}
      </div>
    </>}
  </div>;
}
