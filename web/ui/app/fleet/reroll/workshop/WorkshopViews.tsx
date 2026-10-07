"use client";

import { useRef, useState } from "react";
import type { RerollMember } from "@/lib/fleet";
import { cn } from "@/lib/utils";
import { WorkshopMatrix } from "./WorkshopMatrix";
import { WorkshopPlan } from "./WorkshopPlan";

type WorkshopTab = "upgrades" | "plan";
const TABS: { id: WorkshopTab; label: string }[] = [{ id: "upgrades", label: "Upgrades" }, { id: "plan", label: "Plan graph" }];

function rememberTab(tab: WorkshopTab): void {
  const url = new URL(window.location.href);
  if (tab === "plan") url.searchParams.set("tab", "plan"); else url.searchParams.delete("tab");
  window.history.replaceState(window.history.state, "", url);
}

export function WorkshopViews({ members, focusWorker = null, initialView, initialTab = "upgrades" }: {
  members: RerollMember[]; focusWorker?: string | null; initialView?: "table" | "cubes"; initialTab?: WorkshopTab;
}): React.JSX.Element {
  const [tab, setTab] = useState<WorkshopTab>(initialTab);
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const choose = (next: WorkshopTab): void => { setTab(next); rememberTab(next); };
  return <div className="flex flex-col gap-5">
    <div role="tablist" aria-label="Workshop views" className="flex gap-1 border-b border-border">
      {TABS.map((item, index) => <button key={item.id} type="button" role="tab" id={`workshop-tab-${item.id}`}
        aria-controls={`workshop-panel-${item.id}`} aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1}
        ref={element => { buttons.current[index] = element; }}
        className={cn("border-b-2 px-4 py-2 text-sm", tab === item.id ? "border-primary text-primary" : "border-transparent text-muted-foreground")}
        onClick={() => choose(item.id)}
        onKeyDown={event => {
          let next: number;
          if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
          else if (event.key === "ArrowLeft") next = (index + TABS.length - 1) % TABS.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = TABS.length - 1;
          else return;
          event.preventDefault(); choose(TABS[next].id); buttons.current[next]?.focus();
        }}>{item.label}</button>)}
    </div>
    <div role="tabpanel" id={`workshop-panel-${tab}`} aria-labelledby={`workshop-tab-${tab}`}>
      {tab === "plan" ? <WorkshopPlan members={members} focusWorker={focusWorker} />
        : <WorkshopMatrix members={members} focusWorker={focusWorker} initialView={initialView} />}
    </div>
  </div>;
}
