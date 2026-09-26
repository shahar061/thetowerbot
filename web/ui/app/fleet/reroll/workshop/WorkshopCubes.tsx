"use client";

import { ChevronDown, ChevronRight, Clock } from "lucide-react";
import type { RerollMember } from "@/lib/fleet";
import { deviceColor } from "@/lib/rerollState";
import type { WorkshopLevelRow } from "@/lib/types";
import { memberIdentity } from "../statsHelpers";
import { ago, averageShare, CATEGORIES, CATEGORY_COLOR, gameNumber, levelsBought, levelText, progress, STALE_SECONDS } from "./workshopFormat";

type Rows = Map<string, WorkshopLevelRow>;

const title = (category: string): string => category[0] + category.slice(1).toLowerCase();

/** Levels bought per emulator, stacked by Workshop tab. */
function TotalsChart({ members, catalog, rowsOf, errors, focused, onFocus }: {
  members: RerollMember[]; catalog: WorkshopLevelRow[]; rowsOf: (member: RerollMember) => Rows;
  errors: (member: RerollMember) => string | null; focused: boolean; onFocus: (member: RerollMember) => void;
}): React.JSX.Element {
  const ceiling = catalog.reduce((sum, item) => sum + item.max_level, 0);
  const totals = members.map(member => {
    const rows = rowsOf(member);
    return CATEGORIES.map(category => catalog.filter(item => item.category === category)
      .reduce((sum, item) => sum + (rows.has(item.id) ? levelsBought(rows.get(item.id)!) : 0), 0));
  });
  const top = Math.max(1, ...totals.map(parts => parts.reduce((a, b) => a + b, 0)));
  const step = 10 ** Math.max(0, Math.floor(Math.log10(top)) - 1) * 5;
  const scale = Math.ceil(top / step) * step;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(share => Math.round(scale * share));

  return <figure aria-label="Total Workshop levels per emulator" className="m-0 space-y-3 border-b p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <figcaption className="text-sm font-semibold">Total Workshop levels per emulator</figcaption>
      <ul className="flex gap-3.5 text-xs text-muted-foreground">
        {CATEGORIES.map(category => <li key={category} className="inline-flex items-center gap-1.5">
          <i className="size-2.5 rounded-[3px]" style={{ backgroundColor: CATEGORY_COLOR[category] }} />{title(category)}
        </li>)}
      </ul>
    </div>
    {members.map((member, index) => {
      const parts = totals[index];
      const sum = parts.reduce((a, b) => a + b, 0);
      const error = errors(member);
      return <div key={memberIdentity(member)} className="grid grid-cols-[5.5rem_1fr_5.5rem] items-center gap-2.5 text-xs">
        <button type="button" aria-pressed={focused} title={focused ? "Show all emulators" : `Focus ${member.name}`} onClick={() => onFocus(member)}
          className="inline-flex min-w-0 items-center gap-1.5 truncate text-left font-medium hover:underline">
          <i className="size-2 shrink-0 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} />{member.name}
        </button>
        {error ? <p className="text-[11px] text-danger">{error}</p>
          : <div className="flex h-[18px] gap-[2px]" style={{ width: `${sum / scale * 100}%` }}>
            {CATEGORIES.map((category, part) => parts[part] > 0 && <div key={category} data-testid={`total-${category}`}
              title={`${member.name} · ${title(category)}: ${parts[part].toLocaleString()} levels`}
              className="h-full min-w-[3px] first:rounded-l-[4px] last:rounded-r-[4px]"
              style={{ flexGrow: parts[part], backgroundColor: CATEGORY_COLOR[category] }} />)}
          </div>}
        <span className="text-right font-mono tabular-nums">{sum.toLocaleString()}
          {ceiling > 0 && <span className="block text-[10px] text-muted-foreground">{(sum / ceiling * 100).toFixed(1)}% of max</span>}
        </span>
      </div>;
    })}
    <div className="grid grid-cols-[5.5rem_1fr_5.5rem] gap-2.5 text-[10px] text-muted-foreground" aria-hidden="true">
      <span /><div className="relative h-3 border-t border-border">
        {ticks.map((tick, index) => <span key={index} className="absolute top-0.5 font-mono tabular-nums"
          style={{ left: `${tick / scale * 100}%`, transform: index === 0 ? "none" : index === ticks.length - 1 ? "translateX(-100%)" : "translateX(-50%)" }}>
          {tick.toLocaleString()}</span>)}
      </div><span />
    </div>
    <p className="text-xs text-muted-foreground">Levels bought across every upgrade, split by Workshop tab. Hover a segment for its count.</p>
  </figure>;
}

function Lane({ member, row, color, cheapest, now }: {
  member: RerollMember; row?: WorkshopLevelRow; color: string; cheapest: boolean; now: number;
}): React.JSX.Element {
  const name = <span className="inline-flex min-w-0 items-center gap-1.5 truncate">
    <i className="size-[7px] shrink-0 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} />{member.name}
  </span>;
  if (!row || row.status === "unseen") return <li className="grid grid-cols-[4rem_1fr_6.5rem] items-center gap-2 text-[11px]" title="Not read yet">
    {name}<div className="h-2 rounded border border-dashed border-border" /><span className="text-right text-muted-foreground">—</span>
  </li>;
  const stale = row.observed_at !== null && now - row.observed_at > STALE_SECONDS;
  const share = progress(row);
  const tip = [
    row.status === "ambiguous" && `The read "${row.raw_value}" matches levels ${levelText(row)}`,
    row.status === "unmatched" && `The read "${row.raw_value}" is not on this upgrade's value ladder (lab or card bonus, or a misread)`,
    row.observed_at !== null && `Read ${ago(now - row.observed_at)}`,
  ].filter(Boolean).join(" · ");
  return <li title={tip} className={`grid grid-cols-[4rem_1fr_6.5rem] items-center gap-2 text-[11px] ${stale ? "opacity-55" : ""}`}>
    {name}
    <div className="h-2 overflow-hidden rounded" style={{ backgroundColor: `color-mix(in oklch, ${color}, transparent 85%)` }} aria-hidden="true">
      {share !== null && <div className="h-full rounded" style={{ width: `${Math.max(share * 100, share > 0 ? 2 : 0)}%`, backgroundColor: color }} />}
    </div>
    <span className="flex items-baseline justify-end gap-1.5 font-mono tabular-nums">
      {stale && <Clock aria-label="Stale read" className="size-3 shrink-0 self-center text-muted-foreground" />}
      {row.status === "maxed" ? <span className="font-semibold text-muted-foreground">MAX</span>
        : <span><span className={row.status === "exact" ? "" : "underline decoration-dotted underline-offset-2"}>{levelText(row)}</span>
          <span className="text-muted-foreground">/{row.max_level}</span></span>}
      {row.next_coins !== null && <span className={`inline-flex items-center gap-1 ${cheapest ? "font-semibold text-foreground" : "text-muted-foreground"}`}>
        {cheapest && <i aria-label="Among the cheapest next upgrades" className="size-[5px] rounded-full" style={{ backgroundColor: color }} />}
        {gameNumber(row.next_coins)}
      </span>}
    </span>
  </li>;
}

/** One card per Workshop tab, one cube per upgrade, one bar per emulator. */
export function WorkshopCubes({ members, catalog, visible, rowsOf, cheapest, errors, now, focused, onFocus, collapsed, onToggle }: {
  members: RerollMember[]; catalog: WorkshopLevelRow[]; visible: WorkshopLevelRow[]; rowsOf: (member: RerollMember) => Rows;
  cheapest: (member: RerollMember) => Set<string>; errors: (member: RerollMember) => string | null; now: number;
  focused: boolean; onFocus: (member: RerollMember) => void; collapsed: Set<string>; onToggle: (category: string) => void;
}): React.JSX.Element {
  return <div>
    <TotalsChart members={members} catalog={catalog} rowsOf={rowsOf} errors={errors} focused={focused} onFocus={onFocus} />
    <div className="max-h-[75vh] space-y-4 overflow-auto p-4">
      {CATEGORIES.map(category => {
        const items = visible.filter(item => item.category === category);
        if (!items.length) return null;
        const color = CATEGORY_COLOR[category];
        const open = !collapsed.has(category);
        return <section key={category} aria-label={`${title(category)} upgrades`} className="space-y-3.5 rounded-xl border p-3.5"
          style={{ borderColor: `color-mix(in oklch, ${color}, transparent 55%)`, backgroundColor: `color-mix(in oklch, ${color}, transparent 93%)` }}>
          <header className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2">
            <button type="button" aria-expanded={open} onClick={() => onToggle(category)} className="inline-flex items-center gap-1.5 text-xs font-semibold tracking-wide">
              {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              <i className="size-2 rounded-[2px]" style={{ backgroundColor: color }} />{category}
              <span className="font-normal text-muted-foreground">({items.length} upgrades)</span>
            </button>
            <p className="flex flex-wrap gap-x-3.5 gap-y-1 text-[11px] text-muted-foreground" title="Average share of each upgrade's max level">
              avg climbed:{members.map(member => {
                const share = averageShare(items.map(item => rowsOf(member).get(item.id)));
                return <span key={memberIdentity(member)} className="inline-flex items-center gap-1.5">
                  <i className="size-[7px] rounded-full" style={{ backgroundColor: deviceColor(member.name) }} />{member.name}
                  <b className="font-mono font-semibold text-foreground">{share === null ? "—" : `${Math.round(share * 100)}%`}</b>
                </span>;
              })}
            </p>
          </header>
          {open && <div className="grid grid-cols-[repeat(auto-fill,minmax(16rem,1fr))] gap-3">
            {items.map(item => <article key={item.id} aria-label={item.name} className="space-y-2 rounded-[10px] border border-border bg-card p-3">
              <header className="flex items-baseline justify-between gap-2">
                <h3 className="text-sm font-semibold">{item.name}</h3>
                <span className="font-mono text-[10px] font-semibold tracking-[.12em]" style={{ color }}>{category.slice(0, 3)}</span>
              </header>
              <ul className="space-y-1.5">
                {members.map(member => <Lane key={memberIdentity(member)} member={member} row={rowsOf(member).get(item.id)} color={color}
                  cheapest={cheapest(member).has(item.id)} now={now} />)}
              </ul>
            </article>)}
          </div>}
        </section>;
      })}
    </div>
  </div>;
}
