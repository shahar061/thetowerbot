"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import type { RerollCandidate, RerollMember, RerollRun } from "@/lib/fleet";
import { standingFor } from "@/lib/rerollState";
import { cn } from "@/lib/utils";

const eligible = (candidate: RerollCandidate) =>
  candidate.state === "ready" || candidate.state === "start_required";

function toggle(list: string[], name: string, on: boolean): string[] {
  return on ? [...list, name] : list.filter(item => item !== name);
}

/** Starting a new reroll stops the bot on every emulator not kept; the
 *  emulators stay on. The warning is its own step so it cannot be skimmed
 *  past on the way to the checkboxes. */
export function NewRerollDialog({ open, onClose, run, members, candidates, busy, error, onConfirm }: {
  open: boolean; onClose: () => void; run: RerollRun | null;
  members: RerollMember[]; candidates: RerollCandidate[];
  busy: boolean; error: string | null;
  onConfirm: (value: { keep: string[]; add: string[] }) => void;
}) {
  const [step, setStep] = useState<"warning" | "select">(run ? "warning" : "select");
  const [keep, setKeep] = useState<string[]>([]);
  const [add, setAdd] = useState<string[]>([]);
  const runNumber = run?.number ?? null;
  useEffect(() => {
    if (open) { setStep(runNumber !== null ? "warning" : "select"); setKeep([]); setAdd([]); }
  }, [open, runNumber]);

  const next = (run?.number ?? 0) + 1;
  // The parent polls every 5s and hands down fresh `members`/`candidates`.
  // A name selected against a stale snapshot can go missing or ineligible
  // (a candidate is taken, a member leaves the run) without the checkbox
  // state itself changing, so every read of `keep`/`add` is filtered
  // against the current props rather than trusted as-is.
  const chosenKeep = keep.filter(name => members.some(member => member.name === name));
  const chosenAdd = add.filter(name => candidates.some(candidate => candidate.name === name && eligible(candidate)));
  const stopping = members.length - chosenKeep.length;
  const change = (value: boolean) => { if (!value) onClose(); };

  if (step === "warning" && run) {
    return <ConfirmDialog open={open} onOpenChange={change} tone="warn" title={`Close ${run.name}?`}
      footer={<><Button variant="outline" onClick={onClose}>Cancel</Button>
        <Button onClick={() => setStep("select")}>Continue</Button></>}>
      <p>Starting a new reroll closes {run.name} for good. The bot stops on every emulator you don&apos;t keep playing, and they leave the reroll. The emulators stay on, and you can add them back later.</p>
      <p>You&apos;ll still be able to <strong>view their data</strong> in Archives and under Past rerolls.</p>
    </ConfirmDialog>;
  }

  return <ConfirmDialog open={open} onOpenChange={change} title={run ? `Start Reroll #${next}` : "Start a reroll"}
    footer={<><Button variant="outline" onClick={onClose}>Cancel</Button>
      <Button disabled={busy || !chosenKeep.length && !chosenAdd.length}
        onClick={() => onConfirm({ keep: chosenKeep, add: chosenAdd })}>
        {run && stopping ? `Start Reroll #${next} and stop ${stopping}` : `Start Reroll #${next}`}
      </Button></>}>
    {error && <p role="alert" className="rounded-lg border border-danger bg-danger-surface p-2 text-danger">{error.replaceAll("_", " ")}</p>}
    {run && members.length > 0 && <fieldset className="space-y-1.5">
      <legend className="font-semibold">Keep playing from {run.name}</legend>
      {members.map(member => <label key={member.name} className="flex items-center gap-2 rounded-lg border border-border p-2">
        <input type="checkbox" checked={chosenKeep.includes(member.name)} disabled={busy}
          onChange={event => setKeep(current => toggle(current, member.name, event.target.checked))} />
        <span><strong>{member.name}</strong> <span className="text-muted-foreground">· account {member.account_id ?? "—"}{member.wave != null ? ` · W${member.wave}` : ""}</span></span>
      </label>)}
    </fieldset>}
    <fieldset className="space-y-1.5">
      <legend className="font-semibold">Add fresh emulators</legend>
      {!candidates.length && <p className="text-muted-foreground">No host emulators available. Prepare one with Tower installed and unopened.</p>}
      {candidates.map(candidate => <label key={candidate.name} className={cn("flex items-center gap-2 rounded-lg border border-border p-2", !eligible(candidate) && "bg-muted/40 opacity-70")}>
        <input type="checkbox" checked={chosenAdd.includes(candidate.name)} disabled={busy || !eligible(candidate)}
          onChange={event => setAdd(current => toggle(current, candidate.name, event.target.checked))} />
        <span><strong>{candidate.name}</strong> <span className="text-muted-foreground">· {standingFor(candidate.state).label}</span></span>
      </label>)}
    </fieldset>
    {run && <p className="font-mono text-xs">Keep {chosenKeep.length} · Add {chosenAdd.length} · Stop {stopping}</p>}
  </ConfirmDialog>;
}
