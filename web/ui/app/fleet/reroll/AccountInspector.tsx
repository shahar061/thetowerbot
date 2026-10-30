"use client";

import Link from "next/link";
import { useId, useState } from "react";
import { X } from "lucide-react";
import { AccountExportReport } from "@/components/game-export/GameExportReports";
import type { RerollMember } from "@/lib/fleet";
import { SharedWorkshopLedger, WorkerBattlePurchases } from "./Purchases";
import { cn } from "@/lib/utils";

export function AccountInspector({ member, children, onClose }: {
  member: RerollMember; children: React.ReactNode; onClose: () => void;
}): React.JSX.Element {
  const [tab, setTab] = useState("Overview");
  const id = useId();
  return <section aria-label={`Inspect ${member.name}`} className="scroll-mt-6 rounded-xl border border-primary/40 bg-well/40 p-4">
    <div className="flex items-start justify-between gap-3"><div><h2 className="font-heading text-lg font-semibold">{member.name} <span className="text-sm font-normal text-muted-foreground">· Account {member.account_id ?? "unknown"}</span></h2><p className="mt-1 text-xs text-muted-foreground">Account details · your fleet remains above</p></div><button onClick={onClose} aria-label="Close account details" className="rounded-md p-2 text-muted-foreground hover:bg-accent"><X className="size-4" aria-hidden="true" /></button></div>
    <div role="tablist" aria-label="Account detail views" className="my-4 flex gap-1 border-b pb-2">
      {["Overview", "Workshop", "Battle", "Activity"].map((name, index, all) => <button key={name} role="tab" id={`${id}-${name}`} aria-selected={tab === name} aria-controls={`${id}-panel`} tabIndex={tab === name ? 0 : -1}
        onClick={() => setTab(name)} onKeyDown={event => { if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); const next = all[(index + (event.key === "ArrowRight" ? 1 : all.length - 1)) % all.length]; setTab(next); document.getElementById(`${id}-${next}`)?.focus(); } }}
        className={cn("rounded-md px-3 py-1.5 text-xs", tab === name ? "bg-primary/12 font-semibold text-primary" : "text-muted-foreground hover:bg-accent")}>{name}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${tab}`}>
      {tab === "Overview" && children}
      {tab === "Workshop" && <AccountExportReport key={`${member.account_key}:${member.account_id}`} context={{ scope: member.account_key ?? null, accountId: member.account_id ?? null }} section="workshop" />}
      {tab === "Battle" && <><p className="mb-3 text-xs text-muted-foreground">Confirmed run purchases. Live buying intent is not yet available for this worker.</p><WorkerBattlePurchases accountKey={member.account_key} expectedAccountId={member.account_id} /></>}
      {tab === "Activity" && <><SharedWorkshopLedger members={[member]} /><Link className="mt-3 inline-block text-xs text-primary hover:underline" href="/fleet/reroll/history/">Open fleet history →</Link></>}
    </div>
  </section>;
}
