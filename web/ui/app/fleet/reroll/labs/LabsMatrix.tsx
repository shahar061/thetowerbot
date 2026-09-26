"use client";

import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { deviceColor } from "@/lib/rerollState";
import { duration, nowText, slotTone, type LabsReference, type LabsRow, type SlotPlan } from "@/lib/labs";
import { gameNumber } from "../workshop/WorkshopMatrix";

const PLANNED = "Planned · not automated";

function Badge({ automated }: { automated: boolean }): React.JSX.Element {
  return <span className={`inline-block rounded-full border px-2 py-0.5 text-[10px] ${automated
    ? "border-primary/50 text-primary" : "border-border text-muted-foreground"}`}>{automated ? "Automated" : PLANNED}</span>;
}

function NextTier({ slot, hidePlanned }: { slot: SlotPlan; hidePlanned: boolean }): React.JSX.Element {
  if (!slot.next) return <p className="text-muted-foreground">{slot.note ?? "Nothing planned"}</p>;
  if (hidePlanned && !slot.automated) return <p className="text-muted-foreground">—</p>;
  return <div className="space-y-0.5">
    <p className="font-medium">{slot.next.name}{slot.next.level !== null ? ` L${slot.next.level}` : ""}</p>
    <p className="font-mono">{slot.next.price !== null ? `${gameNumber(slot.next.price)} coins` : "Price unknown"}
      {slot.next.seconds !== null ? ` · ${duration(slot.next.seconds)}` : ""}</p>
    {slot.covered !== null && <p>{slot.covered ? "Covered" : "Not covered yet"}</p>}
    <Badge automated={slot.automated} />
    {slot.note && <p className="text-muted-foreground">{slot.note}</p>}
  </div>;
}

function SlotCell({ slot, at, hidePlanned }: { slot: SlotPlan; at: number; hidePlanned: boolean }): React.JSX.Element {
  const tone = slotTone(slot);
  return <div data-testid={`slot-${slot.slot}`} data-tone={tone} title={slot.why.join(" → ")}
    className={`min-w-36 space-y-1 rounded-md border p-2 text-xs ${tone === "ready" ? "border-amber-500 bg-amber-500/10" : "border-border"}`}>
    <p className="text-[10px] uppercase tracking-widest text-muted-foreground">Now</p>
    <p>{nowText(slot.now, at)}</p>
    <p className="text-[10px] uppercase tracking-widest text-muted-foreground">Next</p>
    <NextTier slot={slot} hidePlanned={hidePlanned} />
  </div>;
}

function GemCell({ row, hidePlanned }: { row: LabsRow; hidePlanned: boolean }): React.JSX.Element {
  const gems = row.plan?.gems;
  if (!gems) return <p className="text-xs text-muted-foreground">Unknown</p>;
  if (!gems.next) return <p className="text-xs text-muted-foreground">Gem path complete</p>;
  if (hidePlanned && !gems.automated) return <p className="text-xs text-muted-foreground">—</p>;
  return <div data-testid="gems" className="min-w-36 space-y-1 rounded-md border border-violet-400/60 bg-violet-400/10 p-2 text-xs">
    <p className="font-medium">{gems.next.label}</p>
    <p className="font-mono">{gems.have ?? "?"} / {gems.need ?? "?"} gems</p>
    <Badge automated={gems.automated} />
  </div>;
}

function Details({ row }: { row: LabsRow }): React.JSX.Element {
  return <div className="grid gap-4 p-3 md:grid-cols-2">
    <ol aria-label={`Gem path for ${row.worker}`} className="space-y-1 text-xs">
      {(row.plan?.gems.steps ?? []).map(step => <li key={step.block_id} data-state={step.state}
        className={`rounded-md border px-2 py-1 ${step.state === "current" ? "border-violet-400 bg-violet-400/10"
          : step.state === "done" ? "border-border text-muted-foreground line-through" : "border-border"}`}>
        {step.label}{step.price !== null ? ` · ${step.price} gems` : ""} · {step.state === "done" ? "done" : step.state === "current" ? "current" : "next"}
      </li>)}
    </ol>
    <ol aria-label={`Recent lab and card activity for ${row.worker}`} className="space-y-1 text-xs">
      {row.recent.length ? row.recent.map(item => <li key={`${item.at}:${item.kind}:${item.item}`} className="rounded-md border border-border px-2 py-1">
        {item.item ?? item.kind} · {item.amount !== null ? item.amount.toLocaleString("en-US") : "?"} {item.currency ?? ""}
        {item.reason ? ` · ${item.reason}` : ""}
      </li>) : <li className="text-muted-foreground">No lab or card activity recorded</li>}
    </ol>
  </div>;
}

export function LabsMatrix({ rows, reference, focus, at = Date.now() / 1000 }: {
  rows: LabsRow[]; reference: LabsReference; focus: string | null; at?: number;
}): React.JSX.Element {
  const [hidePlanned, setHidePlanned] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const shown = focus ? rows.filter(row => row.worker === focus) : rows;
  const slots = shown.flatMap(row => row.plan?.slots ?? []);
  const toggle = (worker: string): void => setOpen(current => { const next = new Set(current); if (!next.delete(worker)) next.add(worker); return next; });

  return <section aria-label="Labs and gems" className="space-y-4">
    <div aria-label="Labs summary" className="flex flex-wrap gap-2 text-xs">
      <span className="rounded-full border border-border bg-card px-3 py-1">{shown.length} emulators</span>
      <span className="rounded-full border border-amber-500/50 bg-amber-500/10 px-3 py-1">{slots.filter(slot => slotTone(slot) === "ready").length} automated labs ready to start</span>
      <span className="rounded-full border border-border bg-card px-3 py-1">{slots.filter(slot => slot.now.state === "unknown").length} slots unread</span>
      <label className="ml-auto flex items-center gap-1.5"><input type="checkbox" checked={hidePlanned} onChange={event => setHidePlanned(event.target.checked)} />Hide not-automated</label>
    </div>
    <div className="hidden overflow-auto rounded-xl border border-border bg-card md:block">
      <table aria-label="Lab slots per emulator" className="w-full border-separate border-spacing-0 text-sm">
        <thead><tr><th className="px-3 py-2 text-left text-xs">Emulator</th>
          {[1, 2, 3, 4, 5].map(slot => <th key={slot} className="px-3 py-2 text-left text-xs">Lab {slot}</th>)}
          <th className="px-3 py-2 text-left text-xs">Gems</th></tr></thead>
        <tbody>{shown.map(row => <Fragment key={row.worker}>
          <tr aria-label={row.worker} className="border-t border-border/60 align-top">
            <th scope="row" className="px-3 py-2 text-left text-xs font-normal">
              <button type="button" aria-label={`Details for ${row.worker}`} aria-expanded={open.has(row.worker)} onClick={() => toggle(row.worker)} className="inline-flex items-center gap-1">
                {open.has(row.worker) ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                <span className="font-medium" style={{ color: deviceColor(row.worker) }}>{row.worker}</span></button>
              <p className="text-muted-foreground">{row.strategy_name ?? "No strategy"}</p>
              <p className="font-mono text-muted-foreground">{row.wallet.coins !== null ? `${gameNumber(row.wallet.coins)} coins` : "coins ?"} · {row.wallet.gems ?? "?"} gems</p>
              {row.plan && row.plan.jar > 0 && <p className="text-muted-foreground">Lab jar {gameNumber(row.plan.jar)}</p>}
              {row.reason && <p className="text-danger">{row.reason}</p>}
            </th>
            {row.plan ? row.plan.slots.map(slot => <td key={slot.slot} className="px-2 py-2"><SlotCell slot={slot} at={at} hidePlanned={hidePlanned} /></td>)
              : <td colSpan={5} className="px-3 py-2 text-xs text-muted-foreground">Unknown</td>}
            <td className="px-2 py-2"><GemCell row={row} hidePlanned={hidePlanned} /></td>
          </tr>
          {open.has(row.worker) && <tr><td colSpan={7}><Details row={row} /></td></tr>}
        </Fragment>)}</tbody>
      </table>
    </div>
    <div className="space-y-3 md:hidden">{shown.map(row => <article key={row.worker} aria-label={`Labs for ${row.worker}`} className="space-y-2 rounded-xl border border-border bg-card p-3">
      <h3 className="font-medium" style={{ color: deviceColor(row.worker) }}>{row.worker}</h3>
      {row.reason && <p className="text-xs text-danger">{row.reason}</p>}
      <div className="grid grid-cols-2 gap-2">{(row.plan?.slots ?? []).map(slot => <SlotCell key={slot.slot} slot={slot} at={at} hidePlanned={hidePlanned} />)}</div>
      <GemCell row={row} hidePlanned={hidePlanned} />
    </article>)}</div>
    <div className="grid gap-4 md:grid-cols-2">
      <section aria-label="Game Speed prices" className="rounded-xl border border-border bg-card p-3 text-xs">
        <h3 className="mb-2 font-semibold">Game Speed</h3>
        <table className="w-full"><thead><tr><th className="text-left">Level</th><th className="text-left">Coins</th><th className="text-left">Time</th><th className="text-left">Max speed</th></tr></thead>
          <tbody>{reference.game_speed.map(level => <tr key={level.level}><td>{level.level}</td><td>{gameNumber(level.coins)}</td><td>{duration(level.seconds)}</td><td>×{level.max_speed.toFixed(1)}</td></tr>)}</tbody></table>
      </section>
      <section aria-label="Slot prices" className="rounded-xl border border-border bg-card p-3 text-xs">
        <h3 className="mb-2 font-semibold">Lab and card slots</h3>
        <p>Lab slots: {reference.lab_slots.map(price => `${price.slot}: ${price.gems.toLocaleString("en-US")}`).join(" · ")} gems</p>
        <p>Card slots: {reference.card_slots.map(price => `${price.slot}: ${price.gems.toLocaleString("en-US")}`).join(" · ")} gems</p>
        <p>Card: {reference.card_gems} gems · Labs unlock at Tier 1 wave {reference.labs_unlock_wave}</p>
        <ul className="mt-2 space-y-0.5 text-muted-foreground">{reference.sources.map(source => <li key={source.url}><a className="underline" href={source.url} target="_blank" rel="noreferrer">{source.url.replace("https://", "")}</a> · checked {source.checked}</li>)}</ul>
      </section>
    </div>
  </section>;
}
