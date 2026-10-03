"use client";

import { useEffect } from "react";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import { duration, isAutomated, moveWithin, newResourceBlock, type AutomatedBlock, type LabBlock, type LabsReference, type LabsRow, type SlotNow } from "@/lib/labs";
import { detachLabSlot, editLabSlot, labSlotBlocks, trackForSlot, type SlotTrack } from "./labSlotDraft";

export type LabSlotPlannerProps = {
  labs: BuildRouteDocument["baseline"]["labs"];
  reference: LabsReference | null;
  automated: AutomatedBlock[];
  observed: LabsRow | null;
  focusSlot?: number;
  locked: boolean;
  onChange: (next: BuildRouteDocument["baseline"]["labs"]) => void;
};
const control = "min-h-11 min-w-0 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-50";
const button = "min-h-11 min-w-11 rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-40";

function completionText(job: SlotNow, historical: boolean, at: number): string {
  const deadline = job.completes_at;
  const known = deadline !== null && Number.isFinite(deadline) && deadline >= 0;
  if (historical) return `${known ? `Last expected completion: ${new Date(deadline * 1000).toLocaleString()}` : "Last expected completion unknown"}. Remaining time unknown; refresh observations.`;
  if ((known && deadline <= at) || (job.overdue_seconds !== null && job.overdue_seconds > 0)) {
    return "Due for verification. Completion not confirmed; the expected deadline has passed.";
  }
  return known ? `Expected completion: ${new Date(deadline * 1000).toLocaleString()} · about ${duration(deadline - at)} remaining`
    : "Expected completion unknown · remaining time unknown.";
}

function unlockText(slot: number, reference: LabsReference | null, historical: boolean): string {
  const gems = reference?.lab_slots.find(row => row.slot === slot)?.gems;
  const wave = reference?.labs_unlock_wave;
  const requirement = slot === 1 ? (wave != null && wave > 0 ? `Tier 1 Wave ${wave}` : null)
    : gems != null && Number.isFinite(gems) && gems >= 0 ? `${gems.toLocaleString()} gems · Lab ${slot - 1} owned` : null;
  return historical ? `If still locked: unlock requirement ${requirement ? `is ${requirement}` : "unknown"}.`
    : requirement ? `Unlock requirement: ${requirement}.` : "Unlock requirement unknown.";
}

export function LabSlotPlanner({ labs, reference, automated, observed, locked, focusSlot, onChange }: LabSlotPlannerProps): React.JSX.Element {
  useEffect(() => {
    if (!focusSlot || focusSlot < 1 || focusSlot > 5) return;
    const section = document.getElementById(`strategy-lab-slot-${focusSlot}`);
    section?.focus();
    section?.scrollIntoView?.({ block: "start" });
  }, [focusSlot]);
  const blocks = labSlotBlocks(labs, reference);
  const choices = reference?.labs ?? [];
  const change = (slot: number, update: (track: SlotTrack, ids: Set<string>) => SlotTrack): void => {
    if (!locked) onChange(editLabSlot(labs, slot, reference, update));
  };
  return <div className="grid min-w-0 gap-4" aria-label="Five-slot lab planner">
    <div className="min-w-0 space-y-2 text-sm">
      <h3 className="font-semibold">Plan all five labs</h3>
      {focusSlot && <p className="font-medium">Planning Lab {focusSlot} · the other slot plans remain visible below.</p>}
      <p>Repeat each research one level at a time until its target, then use the next queued target. Running jobs are never replaced. Auto research next level is controlled in Strategy rules.</p>
      <p className="text-xs text-muted-foreground">Draft only. Save a version, then assign it separately. Base catalog estimates may differ from actual cost and time.</p>
      {!reference && <p role="status">Research catalog unavailable. Existing choices are preserved; additions are disabled.</p>}
      {!blocks && <p>Legacy lab program preserved. Load the catalog before editing slots.</p>}
    </div>
    {[1, 2, 3, 4, 5].map(slot => {
      const track = trackForSlot(blocks ?? [], slot);
      const shared = !!track && track.slots.length > 1;
      const ambiguous = (blocks ?? []).filter(block => block.type === "slot_track" && block.slots.includes(slot)).length > 1;
      const advanced = ambiguous || !!track?.children.some(block => block.type !== "research");
      const disabled = locked || shared || ambiguous || !blocks;
      const policy = track?.slot_policies?.[String(slot)];
      const now = observed?.plan?.slots.find(item => item.slot === slot)?.now;
      const historical = now?.stale || now?.evidence_status !== "current" || observed?.freshness !== "observed";
      return <section role="region" aria-label={`Lab ${slot}`} id={`strategy-lab-slot-${slot}`} tabIndex={focusSlot === slot ? -1 : undefined} key={slot} className={`min-w-0 rounded-xl border bg-card p-3 [overflow-wrap:anywhere] ${focusSlot === slot ? "border-primary ring-2 ring-primary/30" : "border-border"}`}>
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">Lab {slot}</h4>
          <span className="text-xs text-muted-foreground">{policy?.paused ?? track?.paused ? "Paused in draft" : "Queue enabled"}</span></header>
        <p className="mb-3 text-xs text-muted-foreground">{!now || now.state === "unknown" ? "Current job unknown" : <>
          {historical ? "Last observed · " : "Observed · "}{now.state === "researching"
            ? `${now.research_name ?? "Research name unknown"}${now.level == null ? "" : ` · level ${now.level}`}`
            : now.state === "locked" ? "Locked" : now.state === "idle" ? "Idle" : "Owned · job unreadable"}
          {now.read_at != null && ` · ${new Date(now.read_at * 1000).toLocaleString()}`}</>}</p>
        {now?.state === "researching" && <p className="mb-3 text-xs text-muted-foreground">{completionText(now, historical, Date.now() / 1000)}</p>}
        {now?.state === "locked" && <p className="mb-3 text-xs text-muted-foreground">{unlockText(slot, reference, historical)}</p>}
        {shared && <div className="mb-3 space-y-2 text-xs"><p>Shared with Labs {track.slots.filter(value => value !== slot).join(", ")}. Detach to edit only Lab {slot}; the other slots keep their program.</p>
          <button type="button" className={button} disabled={locked} onClick={() => onChange(detachLabSlot(labs, slot, reference))}>Edit Lab {slot} separately</button></div>}
        {advanced ? <p className="my-3 text-sm">Advanced program{ambiguous ? " · overlapping slot owners" : " · conditions, pools or waits"}. Use Advanced lab blocks below; the queue is preserved.</p>
          : <div className="grid min-w-0 gap-3">{(track?.children ?? []).map((item, index) => {
            if (item.type !== "research") return null;
            const pinned = slot === 1 && index === 0;
            const research = choices.find(choice => choice.id === item.lab_id);
            const price = item.lab_id === "labs.game-speed" ? reference?.game_speed.find(level => level.level === item.to_level) : null;
            const updateTarget = (patch: Partial<typeof item>): void => change(slot, current => ({ ...current,
              children: current.children.map(child => child.id === item.id ? { ...item, ...patch } : child) }));
            return <div key={item.id} className="grid min-w-0 gap-2 rounded-lg border border-border p-2">
              <label className="grid min-w-0 gap-1 text-xs">Research {index + 1}<select aria-label={`Lab ${slot} research ${index + 1}`} className={control}
                disabled={disabled || pinned || !reference} value={item.lab_id} onChange={event => updateTarget({ lab_id: event.target.value, to_level: 1 })}>
                {!research && <option value={item.lab_id}>{item.lab_id} · unavailable</option>}
                {choices.map(choice => <option key={choice.id} value={choice.id}>{choice.name}</option>)}</select></label>
              <label className="grid min-w-0 gap-1 text-xs">Target level<input aria-label={`Lab ${slot} target level ${index + 1}`} className={control} type="number" min={1} max={research?.max_level ?? undefined} step={1}
                disabled={disabled || pinned || !research} value={item.to_level} onChange={event => {
                  const value = Number(event.target.value);
                  if (event.target.value && Number.isFinite(value)) updateTarget({ to_level: Math.max(1, Math.min(Math.round(value), research?.max_level ?? Number.MAX_SAFE_INTEGER)) });
                }} /></label>
              {pinned && <p className="text-xs">Game Speed stays first through all supported levels.</p>}
              <p className="text-xs text-muted-foreground">{isAutomated("labs", item, [slot], automated) ? "Supported automation route" : "Planning only"} · {price ? `Target level estimate: ${price.coins.toLocaleString()} coins · ${duration(price.seconds)}` : "Cost unknown · time unknown"}</p>
              <div className="flex flex-wrap gap-2">
                {([-1, 1] as const).map(direction => {
                  const blocked = disabled || pinned || index + direction < (slot === 1 ? 1 : 0) || index + direction >= (track?.children.length ?? 0);
                  // aria-disabled keeps focus on this stable node after a move to an endpoint.
                  return <button key={direction} type="button" className={button} aria-disabled={blocked}
                    aria-label={`Move Lab ${slot} research ${index + 1} ${direction === -1 ? "up" : "down"}`} onClick={() => {
                      if (!blocked) change(slot, current => ({ ...current, children: moveWithin(current.children, item.id, direction) as LabBlock[] }));
                    }}>{direction === -1 ? "↑" : "↓"}</button>;
                })}
                <button type="button" className={button} disabled={disabled || pinned} aria-label={`Remove Lab ${slot} research ${index + 1}`}
                  onClick={() => change(slot, current => ({ ...current, children: current.children.filter(child => child.id !== item.id) }))}>Remove</button>
              </div>
            </div>;
          })}
          {!track?.children.length && <p className="text-xs text-muted-foreground">No queued research. Slot ownership is observed separately.</p>}
          <button type="button" className={button} disabled={disabled || !choices.length} onClick={() => change(slot, (current, ids) => ({ ...current,
            children: [...current.children, newResourceBlock("research", ids, { labId: choices.find(choice => choice.id !== "labs.game-speed")?.id ?? choices[0]?.id }) as LabBlock] }))}>Add research to Lab {slot}</button>
        </div>}
        <div className="mt-3 grid min-w-0 gap-2 border-t border-border pt-3">
          <label className="flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" aria-label={`Pause Lab ${slot}`} disabled={disabled} checked={policy?.paused ?? track?.paused ?? false}
            onChange={event => change(slot, current => ({ ...current, slot_policies: { ...current.slot_policies, [slot]: { ...current.slot_policies?.[slot], paused: event.target.checked } } }))} />Pause Lab {slot}</label>
          <label className="grid min-w-0 gap-1 text-xs">When blocked<select aria-label={`When Lab ${slot} is blocked`} className={control} disabled={disabled} value={policy?.on_blocked ?? track?.on_blocked ?? "wait"}
            onChange={event => change(slot, current => ({ ...current, slot_policies: { ...current.slot_policies, [slot]: { ...current.slot_policies?.[slot], on_blocked: event.target.value as "wait" | "skip" } } }))}>
            <option value="wait">Wait for this target</option><option value="skip">Try next target for now</option></select></label>
          <p className="text-xs text-muted-foreground">Skipping keeps the target queued. Slot 1 always waits for Game Speed first.</p>
        </div>
      </section>;
    })}
  </div>;
}
