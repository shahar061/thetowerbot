"use client";

import { useState } from "react";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import styles from "./routeCanvas.module.css";

type Gems = BuildRouteDocument["baseline"]["gems"];
type Labs = BuildRouteDocument["baseline"]["labs"];
type Kind = "gems" | "labs";

const GEM_OPTIONS = ["unlock_lab_slot_3", "unlock_lab_slot_4", "unlock_lab_slot_5", "card_slot", "cards"];
const LAB_OPTIONS = ["slot2_research"];
const LABELS: Record<string, string> = {
  unlock_lab_slot_2: "Unlock lab slot 2", unlock_lab_slot_3: "Unlock lab slot 3",
  unlock_lab_slot_4: "Unlock lab slot 4", unlock_lab_slot_5: "Unlock lab slot 5",
  card_slot: "Card slot", cards: "Cards", research_game_speed: "Game Speed research",
  slot2_research: "Slot 2 research",
};

export function ResourceBlocks({ kind, gems, labs, onGemsChange, onLabsChange }: {
  kind: Kind; gems: Gems; labs: Labs;
  onGemsChange: (gems: Gems) => void; onLabsChange: (labs: Labs) => void;
}): React.JSX.Element {
  const [dragged, setDragged] = useState<string | null>(null);
  const isGems = kind === "gems";
  const steps = isGems ? gems.steps : labs.steps;
  const options = isGems ? GEM_OPTIONS : LAB_OPTIONS;
  const fixed = isGems ? "unlock_lab_slot_2" : "research_game_speed";
  const color = isGems ? styles.gem : styles.lab;

  function update(next: string[]): void {
    if (isGems) onGemsChange({ ...gems, steps: next });
    else onLabsChange({ ...labs, steps: next });
  }
  function add(step: string, before?: string): void {
    if (!options.includes(step) && step !== fixed) return;
    const targetIndex = before ? steps.indexOf(before) : -1;
    const next = steps.filter(value => value !== step);
    next.splice(targetIndex < 1 ? next.length : Math.min(targetIndex, next.length), 0, step);
    update(next);
  }
  function move(step: string, direction: -1 | 1): void {
    const next = [...steps];
    const from = next.indexOf(step);
    const to = from + direction;
    if (from < 1 || to < 1 || to >= next.length) return;
    [next[from], next[to]] = [next[to], next[from]];
    update(next);
  }

  return <section className={`${styles.canvas} space-y-4`} aria-label={`${isGems ? "Gem" : "Lab"} block canvas`}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs font-semibold uppercase tracking-widest text-primary">{isGems ? "GEM ECONOMY" : "RESEARCH QUEUE"}</p>
        <h3 className="font-heading text-xl font-bold">{isGems ? "Spend the gems" : "Run the labs"}</h3>
        <p className="text-xs text-muted-foreground">Fleet-wide path. Drag blocks to arrange it; the first block is locked to protect current automation.</p></div>
      <span className="rounded-full border border-primary/40 bg-primary/10 px-3 py-1 text-xs text-primary">{isGems ? "100 gems reserved" : "Slot 1 dedicated"}</span>
    </div>
    {isGems && <label className="block max-w-xs text-xs font-semibold">Planned gem spend limit · % of observed balance
      <input type="number" aria-label="Gem spend limit" min={0} max={100} value={gems.spend_limit_pct}
        onChange={event => onGemsChange({ ...gems, spend_limit_pct: Number(event.target.value) })}
        className="mt-2 w-full rounded-lg border border-border bg-background px-3 py-2 [user-select:text]" />
      <span className="mt-1 block font-normal text-muted-foreground">Planning only. The bot still reserves and spends the first 100 gems on lab slot 2; this cap does not alter that unlock.</span>
    </label>}
    <div data-testid={`${isGems ? "gem" : "lab"}-path-drop`} aria-label={`${isGems ? "Gem" : "Lab"} path drop zone`}
      onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (dragged) add(dragged); setDragged(null); }}
      className={`${styles.drop} min-h-24`}>
      {steps.map((step, index) => <div key={step}>
        {index > 0 && <div className={styles.connector} aria-hidden="true" />}
        <div data-testid={`resource-step-${step}`} draggable={index > 0}
          onDragStart={event => { event.stopPropagation(); setDragged(step); }} onDragEnd={() => setDragged(null)}
          onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); if (dragged && dragged !== step) add(dragged, step); setDragged(null); }}
          className={`${styles.block} ${color} flex flex-wrap items-center justify-between gap-2 px-4 py-3`}>
          <div><span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{index === 0 ? "START · LOCKED" : `STEP ${index + 1}`}</span>
            <strong className="block text-sm">{LABELS[step] ?? step}</strong></div>
          <div className="flex items-center gap-1 text-xs"><span className="mr-2 text-muted-foreground">{index === 0 ? "Automated" : "Planned · not automated"}</span>
            {index > 0 && <><button type="button" aria-label={`Move ${LABELS[step]} up`} disabled={index === 1} onClick={() => move(step, -1)} className="rounded border border-border px-2 py-1 disabled:opacity-40">↑</button>
              <button type="button" aria-label={`Move ${LABELS[step]} down`} disabled={index === steps.length - 1} onClick={() => move(step, 1)} className="rounded border border-border px-2 py-1 disabled:opacity-40">↓</button>
              <button type="button" aria-label={`Remove ${LABELS[step]}`} onClick={() => update(steps.filter(value => value !== step))} className="rounded border border-border px-2 py-1">×</button></>}
          </div>
        </div>
      </div>)}
    </div>
    <div className="rounded-xl border border-border bg-card/90 p-4"><h4 className="text-sm font-semibold">Block palette</h4>
      <p className="mb-3 text-xs text-muted-foreground">Drag a block into the path or use Add. Planned steps are visual strategy only until automation supports them.</p>
      <div className="grid gap-2 sm:grid-cols-2">{options.filter(step => !steps.includes(step)).map(step => <div key={step} data-testid={`resource-palette-${step}`} draggable
        onDragStart={() => setDragged(step)} onDragEnd={() => setDragged(null)}
        className={`${styles.block} ${color} flex items-center justify-between gap-2 p-3 text-xs`}>
        <div><strong>{LABELS[step]}</strong><span className="block text-muted-foreground">Planned · not automated</span></div>
        <button type="button" aria-label={`Add ${LABELS[step]}`} onClick={() => add(step)} className="rounded border border-border px-2 py-1">+ Add</button>
      </div>)}</div>
    </div>
  </section>;
}
