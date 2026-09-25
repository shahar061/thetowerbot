"use client";

import { useLayoutEffect, useMemo, useState } from "react";
import type { BuildRouteDocument, BuildRoutePreview, BuildRouteRebindPreview } from "@/lib/buildRoute";
import type { Upgrade } from "@/lib/types";
import { fetchBuildRouteRevisions, previewBuildRoute, previewBuildRouteRebind, publishBuildRoute, rollbackBuildRoute } from "@/lib/api";
import { beginDraft, draftErrors, patchWorkshop, resetOverride, scopedWorkshop } from "./RouteDraft";
import { RouteInspector } from "./RouteInspector";
import { BattleFlow } from "./BattleFlow";

type MemberIdentity = { name: string; account_id?: string | null; hidden?: boolean };

function errorText(error: unknown, fallback: string): string {
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return fallback;
}

export function FlowBuilder({ saved, catalog, members, onPublished }: {
  saved: BuildRouteDocument; catalog: Upgrade[]; members: MemberIdentity[];
  onPublished: (route: BuildRouteDocument) => void;
}): React.JSX.Element {
  const [draft, setDraft] = useState(() => beginDraft(saved));
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState<BuildRoutePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);
  const [history, setHistory] = useState<BuildRouteDocument[] | null>(null);
  const [restoreRevision, setRestoreRevision] = useState<number | null>(null);
  const [focusAfterMove, setFocusAfterMove] = useState<{ id: string; direction: "up" | "down" } | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [scope, setScope] = useState("fleet");
  const [rebind, setRebind] = useState<BuildRouteRebindPreview | null>(null);
  const scopedMember = members.find(member => member.name === scope);
  const scopedWorker = scopedMember?.name ?? null;
  const scopedAccount = scopedMember?.account_id ?? null;
  const workshop = scopedWorkshop(draft, scopedWorker, scopedAccount);
  const byId = useMemo(() => new Map(catalog.map(item => [item.id, item])), [catalog]);
  const errors = draftErrors(draft.route, new Set(byId.keys()));
  const stale = Object.entries(draft.route.overrides).filter(([worker, override]) =>
    members.find(member => member.name === worker)?.account_id !== override.account_id);
  const canPublish = !busy && !conflict && stale.length === 0 && errors.length === 0;
  const filtered = catalog.filter(item => `${item.name} ${item.category}`.toLowerCase().includes(search.toLowerCase()));

  useLayoutEffect(() => {
    if (!focusAfterMove) return;
    const button = [...document.querySelectorAll<HTMLButtonElement>("[data-move-id]")]
      .find(element => element.dataset.moveId === focusAfterMove.id && element.dataset.direction === focusAfterMove.direction);
    button?.focus();
    setFocusAfterMove(null);
  }, [draft, focusAfterMove]);

  function edit(next: typeof draft): void {
    setDraft(next);
    setPreview(null);
    setMessage("");
  }

  function editWorkshop(change: Partial<typeof workshop>): void {
    const update = { ...change };
    if (change.priority_ids) {
      update.weights = Object.fromEntries(Object.entries(workshop.weights)
        .filter(([id]) => change.priority_ids?.includes(id)));
    }
    if (change.draw_chance_pct && change.draw_chance_pct > 0) update.mode = "priorities";
    edit(patchWorkshop(draft, scopedWorker, scopedAccount, update));
  }

  function move(id: string, direction: -1 | 1): void {
    const ids = [...workshop.priority_ids];
    const from = ids.indexOf(id);
    const to = from + direction;
    if (from < 0 || to < 0 || to >= ids.length) return;
    [ids[from], ids[to]] = [ids[to], ids[from]];
    editWorkshop({ priority_ids: ids, mode: "priorities" });
    setFocusAfterMove({ id, direction: direction < 0 ? "down" : "up" });
  }

  async function previewRebind(worker: string, oldAccount: string, newAccount: string): Promise<void> {
    setBusy(true);
    try { setRebind(await previewBuildRouteRebind(draft.route, draft.expectedRevision,
                                                  worker, oldAccount, newAccount)); }
    catch (error) { setMessage(errorText(error, "Rebind preview unavailable")); }
    finally { setBusy(false); }
  }

  function confirmRebind(): void {
    if (!rebind) return;
    const override = draft.route.overrides[rebind.worker];
    if (!override || override.account_id !== rebind.old_account_id) return;
    edit({ ...draft, route: { ...draft.route, overrides: {
      ...draft.route.overrides,
      [rebind.worker]: { ...override, account_id: rebind.new_account_id },
    } } });
    setRebind(null);
  }

  async function previewDraft(): Promise<void> {
    setBusy(true);
    setMessage("");
    try {
      setPreview(await previewBuildRoute(draft.route, draft.expectedRevision));
    } catch (error) {
      if ((error as { status?: number }).status === 409) setConflict(true);
      setMessage(errorText(error, "Preview unavailable"));
    } finally { setBusy(false); }
  }

  async function publish(): Promise<void> {
    setBusy(true);
    setMessage("");
    try {
      const next = await publishBuildRoute(draft.route, draft.expectedRevision);
      setDraft(beginDraft(next));
      setPreview(null);
      setMessage(`Revision ${next.revision} published`);
      onPublished(next);
    } catch (error) {
      if ((error as { status?: number }).status === 409) setConflict(true);
      setMessage(errorText(error, "Publish failed"));
    } finally { setBusy(false); }
  }

  async function showHistory(): Promise<void> {
    try { setHistory((await fetchBuildRouteRevisions()).revisions); }
    catch (error) { setMessage(errorText(error, "History unavailable")); }
  }

  async function restore(): Promise<void> {
    if (restoreRevision === null) return;
    setBusy(true);
    try {
      const next = await rollbackBuildRoute(restoreRevision, draft.expectedRevision);
      setDraft(beginDraft(next));
      setRestoreRevision(null);
      setHistory(null);
      setPreview(null);
      setMessage(`Revision ${next.revision} published`);
      onPublished(next);
    } catch (error) {
      if ((error as { status?: number }).status === 409) setConflict(true);
      setMessage(errorText(error, "Restore failed"));
    } finally { setBusy(false); }
  }

  return <section aria-label="Flow Builder" className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-primary/30 bg-card p-4">
      <div><p className="text-xs font-semibold uppercase tracking-widest text-primary">Build Route · draft</p>
        <h2 className="font-heading text-xl font-bold">Design the next decision</h2>
        <p className="text-xs text-muted-foreground">Revision {draft.expectedRevision} · edits stay local until published</p></div>
      <div className="flex flex-wrap gap-2">
        <button type="button" onClick={() => void previewDraft()} disabled={busy || errors.length > 0} className="rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-50">Preview changes</button>
        <button type="button" onClick={() => void publish()} disabled={!canPublish} className="rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground disabled:opacity-50">Publish route</button>
        <button type="button" onClick={() => void showHistory()} className="rounded-lg border border-border px-3 py-2 text-sm">Route history</button>
      </div>
    </div>
    {message && <p role={conflict ? "alert" : "status"} className="rounded-lg border border-border bg-card px-3 py-2 text-sm">{message}</p>}
    {conflict && <p className="text-sm text-danger">Saved route changed. Refresh the page to compare the new revision; your draft remains here.</p>}
    {errors.map(error => <p key={error} role="alert" className="text-sm text-danger">{error}</p>)}
    {stale.map(([worker, override]) => {
      const account = members.find(member => member.name === worker)?.account_id;
      return <div key={worker} className="flex flex-wrap items-center gap-2 rounded-lg border border-danger/40 p-3 text-sm text-danger">
        <span>{worker}: Inactive: account changed. Reset or preview a rebind before publishing.</span>
        {account && <button type="button" onClick={() => void previewRebind(worker, override.account_id, account)} disabled={busy} className="rounded border border-border px-2 py-1 text-xs">Preview rebind for {worker}</button>}
      </div>;
    })}
    {rebind && <div role="dialog" aria-label="Reapply account override" className="rounded-xl border border-primary bg-card p-4 text-sm">
      <p>Reapply {rebind.worker}&apos;s override to account {rebind.new_account_id}?</p>
      <p className="mt-2 text-xs text-muted-foreground">Current spend cap {rebind.new_effective.workshop.coin_spend_limit_pct}% → reapplying {rebind.old_effective.workshop.coin_spend_limit_pct}%. Only after you publish will this affect the new account.</p>
      <div className="mt-3 flex gap-2"><button type="button" onClick={confirmRebind} className="rounded bg-primary px-3 py-2 text-primary-foreground">Reapply override</button><button type="button" onClick={() => setRebind(null)} className="rounded border border-border px-3 py-2">Cancel</button></div>
    </div>}
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(300px,1fr)]">
      <div className="space-y-4">
        <div className="grid gap-3 md:grid-cols-3" aria-label="Route lanes">
          <section className="rounded-xl border border-primary/40 bg-card p-3"><h3 className="font-heading font-semibold">Workshop</h3><p className="text-xs text-muted-foreground">Priorities → budget → weighted pick</p></section>
          <section className="rounded-xl border border-border bg-card p-3"><h3 className="font-heading font-semibold">Gems</h3><p className="text-xs text-muted-foreground">100 gems → second lab · cards planned</p></section>
          <section className="rounded-xl border border-border bg-card p-3"><h3 className="font-heading font-semibold">Labs</h3><p className="text-xs text-muted-foreground">Slot 1 → Game Speed until max</p></section>
        </div>
        <section className="space-y-3 rounded-2xl border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-2"><h3 className="font-heading font-semibold">Workshop priority</h3>
            <select aria-label="Workshop selection mode" value={workshop.mode} onChange={event => editWorkshop({ mode: event.target.value as typeof workshop.mode })} className="rounded-md border border-border bg-background p-1 text-xs"><option value="legacy_planner">Current turtle policy</option><option value="priorities">Route priorities</option></select></div>
          <label className="block text-xs">Strategy scope <select aria-label="Strategy scope" value={scope} onChange={event => setScope(event.target.value)} className="ml-2 rounded-md border border-border bg-background px-2 py-1"><option value="fleet">Entire fleet</option>{members.filter(member => member.account_id).map(member => <option key={member.name} value={member.name}>{member.name} · {member.account_id}</option>)}</select></label>
          {scopedWorker && <p className="text-xs text-muted-foreground">Edits here apply only to {scopedWorker}. Unchanged fields inherit from the fleet route.</p>}
          <p className="text-xs text-muted-foreground">Drag within this list or use the arrow buttons. The bot checks affordability, unlocks and account evidence again before buying.</p>
          <ol className="space-y-2">{workshop.priority_ids.map((id, index) => <li key={id} data-testid="priority-row" draggable onDragStart={() => setDragId(id)} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragId && dragId !== id) {
            const ids = [...workshop.priority_ids]; const to = ids.indexOf(id); ids.splice(ids.indexOf(dragId), 1); ids.splice(to, 0, dragId);
            editWorkshop({ priority_ids: ids, mode: "priorities" });
          } setDragId(null); }} onDragEnd={() => setDragId(null)}
            className="flex select-none items-center gap-2 rounded-lg border border-border bg-background/60 px-3 py-2 text-sm cursor-grab active:cursor-grabbing">
            <span aria-hidden="true" className="text-muted-foreground">⠿</span><span className="min-w-0 flex-1">{byId.get(id)?.name ?? id}</span>
            <button type="button" data-move-id={id} data-direction="up" onClick={() => move(id, -1)} disabled={index === 0} aria-label={`Move ${byId.get(id)?.name ?? id} up`} className="rounded border border-border px-2 disabled:opacity-40">↑</button>
            <button type="button" data-move-id={id} data-direction="down" onClick={() => move(id, 1)} disabled={index === workshop.priority_ids.length - 1} aria-label={`Move ${byId.get(id)?.name ?? id} down`} className="rounded border border-border px-2 disabled:opacity-40">↓</button>
            <button type="button" onClick={() => editWorkshop({ priority_ids: workshop.priority_ids.filter(value => value !== id) })} aria-label={`Remove ${byId.get(id)?.name ?? id} priority`} className="rounded border border-border px-2">×</button>
          </li>)}</ol>
          <label className="block text-xs">Add priority <select aria-label="Add priority" value="" onChange={event => { if (event.target.value) editWorkshop({ priority_ids: [...workshop.priority_ids, event.target.value], mode: "priorities" }); }} className="mt-1 w-full rounded-md border border-border bg-background p-2"><option value="">Choose an upgrade…</option>{catalog.filter(item => !workshop.priority_ids.includes(item.id) && !workshop.banned_upgrade_ids.includes(item.id)).map(item => <option key={item.id} value={item.id}>{item.name} · {item.category.toLowerCase()}</option>)}</select></label>
        </section>
        <section className="grid gap-4 rounded-2xl border border-border bg-card p-4 md:grid-cols-2">
          <label className="text-sm font-medium">Spend limit <span className="text-xs text-muted-foreground">· % of observed coins</span><input type="number" aria-label="Spend limit" min={0} max={100} value={workshop.coin_spend_limit_pct} onChange={event => editWorkshop({ coin_spend_limit_pct: Number(event.target.value) })} className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2" /></label>
          <label className="text-sm font-medium">Weighted luck <span className="text-xs text-muted-foreground">· % chance to draw</span><input type="number" aria-label="Weighted luck" min={0} max={100} value={workshop.draw_chance_pct} onChange={event => editWorkshop({ draw_chance_pct: Number(event.target.value) })} className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2" /></label>
          <p className="md:col-span-2 text-xs text-muted-foreground">Spend limit caps one purchase. Weighted luck decides how often to choose among eligible upgrades; individual weights set their relative odds.</p>
          {workshop.priority_ids.map(id => <label key={id} className="flex items-center justify-between gap-3 text-xs">Weight for {byId.get(id)?.name ?? id}<input type="number" aria-label={`Weight for ${byId.get(id)?.name ?? id}`} min={1} value={workshop.weights[id] ?? 1} onChange={event => editWorkshop({ weights: { ...workshop.weights, [id]: Number(event.target.value) } })} className="w-20 rounded-md border border-border bg-background px-2 py-1" /></label>)}
        </section>
        <section className="rounded-2xl border border-border bg-card p-4"><h3 className="font-heading font-semibold">Never Buy · {workshop.banned_upgrade_ids.length}</h3>
          <p className="mt-1 text-xs text-muted-foreground">Banned upgrades and their dependent unlocks leave the candidate pool.</p>
          <input type="search" aria-label="Search upgrades" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search upgrades" className="mt-3 w-full rounded-md border border-border bg-background px-3 py-2 text-sm" />
          <div className="mt-3 grid max-h-56 gap-1 overflow-y-auto sm:grid-cols-2">{filtered.map(item => <label key={item.id} className="flex items-center gap-2 rounded-md px-2 py-1 text-xs hover:bg-background"><input type="checkbox" aria-label={`Never buy ${item.name}`} checked={workshop.banned_upgrade_ids.includes(item.id)} onChange={event => { const ids = new Set(workshop.banned_upgrade_ids); if (event.target.checked) ids.add(item.id); else ids.delete(item.id); editWorkshop({ banned_upgrade_ids: [...ids].sort() }); }} />{item.name}<span className="ml-auto text-[10px] text-muted-foreground">{item.category.toLowerCase()}</span></label>)}</div>
        </section>
        <BattleFlow battle={draft.route.baseline.battle} catalog={catalog} onChange={battle => edit({ ...draft,
          route: { ...draft.route, baseline: { ...draft.route.baseline, battle } } })} />
        <section className="grid gap-4 md:grid-cols-2" aria-label="Resource paths">
          <div className="rounded-2xl border border-border bg-card p-4"><h3 className="font-heading font-semibold">Gem path</h3>
            <p className="text-xs text-muted-foreground">The first 100 gems stay reserved for lab slot 2.</p>
            <ol className="mt-3 space-y-2">{draft.route.baseline.gems.steps.map((step, index) => <li key={step} className="flex items-center gap-2 rounded-lg border border-border bg-background/40 p-2 text-xs"><span className="flex-1">{index + 1}. {step.replaceAll("_", " ")}</span><span className="text-muted-foreground">{step === "unlock_lab_slot_2" ? "Automated" : "Planned · not automated"}</span>{index > 1 && <button type="button" aria-label={`Move ${step} up`} onClick={() => { const steps = [...draft.route.baseline.gems.steps]; [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]]; edit({ ...draft, route: { ...draft.route, baseline: { ...draft.route.baseline, gems: { ...draft.route.baseline.gems, steps } } } }); }} className="rounded border border-border px-1">↑</button>}{index > 0 && <button type="button" aria-label={`Remove ${step}`} onClick={() => edit({ ...draft, route: { ...draft.route, baseline: { ...draft.route.baseline, gems: { ...draft.route.baseline.gems, steps: draft.route.baseline.gems.steps.filter(value => value !== step) } } } })} className="rounded border border-border px-1">×</button>}</li>)}</ol>
            <select aria-label="Add gem path step" value="" onChange={event => { if (!event.target.value) return; edit({ ...draft, route: { ...draft.route, baseline: { ...draft.route.baseline, gems: { ...draft.route.baseline.gems, steps: [...draft.route.baseline.gems.steps, event.target.value] } } } }); }} className="mt-3 w-full rounded border border-border bg-background p-2 text-xs"><option value="">Add planned step…</option>{["unlock_lab_slot_3", "unlock_lab_slot_4", "unlock_lab_slot_5", "card_slot", "cards"].filter(step => !draft.route.baseline.gems.steps.includes(step)).map(step => <option key={step} value={step}>{step.replaceAll("_", " ")} · Planned</option>)}</select>
          </div>
          <div className="rounded-2xl border border-border bg-card p-4"><h3 className="font-heading font-semibold">Lab path</h3><p className="text-xs text-muted-foreground">Slot 1 stays on Game Speed until maxed.</p>
            <ol className="mt-3 space-y-2">{draft.route.baseline.labs.steps.map((step, index) => <li key={step} className="rounded-lg border border-border bg-background/40 p-2 text-xs">{index + 1}. {step.replaceAll("_", " ")} · {step === "research_game_speed" ? "Automated" : "Planned · not automated"}</li>)}</ol>
            {!draft.route.baseline.labs.steps.includes("slot2_research") && <button type="button" onClick={() => edit({ ...draft, route: { ...draft.route, baseline: { ...draft.route.baseline, labs: { ...draft.route.baseline.labs, steps: [...draft.route.baseline.labs.steps, "slot2_research"] } } } })} className="mt-3 rounded border border-border px-2 py-1 text-xs">Add planned slot 2 research</button>}
            <label className="mt-3 block text-xs">Execution for planned steps<select aria-label="Execution for planned steps" disabled value="planned" className="ml-2 rounded border border-border bg-background px-2 py-1"><option value="planned">Planned · not automated</option></select></label>
          </div>
        </section>
        {!!Object.keys(draft.route.overrides).length && <section className="rounded-2xl border border-border bg-card p-4"><h3 className="font-heading font-semibold">Account overrides</h3>{Object.entries(draft.route.overrides).map(([worker, override]) => <div key={worker} className="mt-2 flex items-center justify-between gap-2 text-xs"><span>{worker} · {override.account_id}</span><button type="button" onClick={() => edit(resetOverride(draft, worker))} className="rounded border border-border px-2 py-1">Reset override for {worker}</button></div>)}</section>}
      </div>
      <RouteInspector preview={preview} members={members} />
    </div>
    {history && <section aria-label="Route history" className="rounded-xl border border-border bg-card p-4"><h3 className="font-heading font-semibold">Route history</h3><div className="mt-2 flex flex-wrap gap-2">{history.map(item => <button key={item.revision} type="button" onClick={() => setRestoreRevision(item.revision)} className="rounded-md border border-border px-2 py-1 text-xs">Restore revision {item.revision}</button>)}</div></section>}
    {restoreRevision !== null && <div role="dialog" aria-label="Confirm route restore" className="rounded-xl border border-primary bg-card p-4"><p>Restore revision {restoreRevision} as a new revision?</p><div className="mt-3 flex gap-2"><button type="button" onClick={() => void restore()} disabled={busy} className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground">Confirm restore</button><button type="button" onClick={() => setRestoreRevision(null)} className="rounded-md border border-border px-3 py-2 text-sm">Cancel</button></div></div>}
  </section>;
}
