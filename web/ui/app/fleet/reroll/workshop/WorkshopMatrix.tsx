"use client";

import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Clock } from "lucide-react";
import { fetchAccountWorkshopLevels } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { deviceColor } from "@/lib/rerollState";
import type { WorkshopLevelRow, WorkshopLevels } from "@/lib/types";
import { memberIdentity } from "../statsHelpers";
import { RecentWorkshopBuys } from "./RecentWorkshopBuys";
import { WorkshopCubes } from "./WorkshopCubes";
import { ago, averageShare, CATEGORIES, gameNumber, levelText, progress, STALE_SECONDS } from "./workshopFormat";

const CHEAPEST_MARKED = 3;
type Sort = "category" | "cheapest" | "gap";
export type WorkshopView = "table" | "cubes";
type Result = { data: WorkshopLevels | null; error: string | null };

export { gameNumber };

function Cell({ row, color, cheapest, now, focused }: {
  row?: WorkshopLevelRow; color: string; cheapest: boolean; now: number; focused: boolean;
}): React.JSX.Element {
  if (!row || row.status === "unseen") return <div className="rounded-md border border-dashed border-border/60 px-2 py-1.5 text-center text-xs text-muted-foreground" title="Not read yet">—</div>;
  const stale = row.observed_at !== null && now - row.observed_at > STALE_SECONDS;
  const share = progress(row);
  const title = [
    row.status === "ambiguous" && `The read "${row.raw_value}" matches levels ${levelText(row)}`,
    row.status === "unmatched" && `The read "${row.raw_value}" is not on this upgrade's value ladder (lab or card bonus, or a misread)`,
    row.observed_at !== null && `Read ${ago(now - row.observed_at)}`,
  ].filter(Boolean).join(" · ");
  return <div title={title} className={`min-w-0 space-y-1 ${stale ? "opacity-60" : ""}`}>
    <div className="flex items-baseline justify-between gap-1 font-mono text-xs tabular-nums">
      {row.status === "maxed" ? <span className="font-semibold text-muted-foreground">MAX</span>
        : <span className={row.status === "exact" ? "" : "underline decoration-dotted underline-offset-2"}>{levelText(row)}<span className="text-muted-foreground"> / {row.max_level}</span></span>}
      {stale && <Clock aria-label="Stale read" className="size-3 shrink-0 text-muted-foreground" />}
    </div>
    <div className="h-[3px] overflow-hidden rounded-full bg-muted" aria-hidden="true">
      {share !== null && <div className="h-full rounded-full" style={{ width: `${Math.max(share * 100, share > 0 ? 2 : 0)}%`, backgroundColor: color }} />}
    </div>
    {row.next_coins !== null && <div className="flex items-center gap-1 font-mono text-[11px] tabular-nums text-muted-foreground">
      {cheapest && <span aria-label="Among the cheapest next upgrades" className="size-1.5 rounded-full" style={{ backgroundColor: color }} />}
      <span className={cheapest ? "text-foreground" : ""}>{gameNumber(row.next_coins)}</span>
    </div>}
    {focused && row.raw_value !== null && <div className="text-[10px] text-muted-foreground">read {row.raw_value}{row.observed_at !== null && ` · ${ago(now - row.observed_at)}`}</div>}
  </div>;
}

const VIEW_STORAGE_KEY = "workshop-view";

/** The layout last picked on this browser, so navigating back to the Workshop reopens it. */
function storedView(): WorkshopView {
  try {
    return window.localStorage.getItem(VIEW_STORAGE_KEY) === "cubes" ? "cubes" : "table";
  } catch {
    return "table";
  }
}

/** Keeps the chosen layout in ?view= (refresh, shared links) and in localStorage (returning via the nav). */
function rememberView(view: WorkshopView): void {
  try { window.localStorage.setItem(VIEW_STORAGE_KEY, view); } catch { /* storage blocked: the URL still carries it */ }
  const url = new URL(window.location.href);
  if (view === "cubes") url.searchParams.set("view", view); else url.searchParams.delete("view");
  window.history.replaceState(window.history.state, "", url);
}

export function WorkshopMatrix({ members, focusWorker = null, initialView }: {
  members: RerollMember[]; focusWorker?: string | null; initialView?: WorkshopView;
}): React.JSX.Element {
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const [results, setResults] = useState<Record<string, Result>>({});
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [hideMaxed, setHideMaxed] = useState(true);
  const [sort, setSort] = useState<Sort>("category");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<WorkshopView>(() => initialView ?? storedView());
  const [focus, setFocus] = useState<string | null>(() => {
    const target = members.find(member => member.name === focusWorker);
    return target ? memberIdentity(target) : null;
  });
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    let active = true;
    const scoped = JSON.parse(identity) as RerollMember[];
    const load = async (): Promise<void> => {
      await Promise.all(scoped.map(async member => {
        let result: Result;
        if (!member.account_key || !member.account_id) result = { data: null, error: "Waiting for a verified account" };
        else {
          try {
            const data = await fetchAccountWorkshopLevels(member.account_key, member.account_id);
            if (data.account_id !== member.account_id) throw new Error("Account changed; waiting for matching Workshop reads");
            result = { data, error: null };
          } catch (failure) { result = { data: null, error: failure instanceof Error ? failure.message : "Workshop levels unavailable" }; }
        }
        if (active) setResults(current => ({ ...current, [memberIdentity(member)]: result }));
      }));
      if (active) setNow(Date.now() / 1000);
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [identity]);

  const shown = focus ? members.filter(member => memberIdentity(member) === focus) : members;
  const focused = focus ? members.find(member => memberIdentity(member) === focus) ?? null : null;
  const rowsOf = (member: RerollMember): Map<string, WorkshopLevelRow> =>
    new Map((results[memberIdentity(member)]?.data?.upgrades ?? []).map(row => [row.id, row]));
  const byMember = new Map(members.map(member => [memberIdentity(member), rowsOf(member)]));
  const catalog = members.map(member => results[memberIdentity(member)]?.data?.upgrades).find(Boolean) ?? [];

  const cheapest = new Map(members.map(member => {
    const priced = [...(byMember.get(memberIdentity(member))?.values() ?? [])].filter(row => row.next_coins !== null)
      .sort((a, b) => (a.next_coins ?? 0) - (b.next_coins ?? 0)).slice(0, CHEAPEST_MARKED);
    return [memberIdentity(member), new Set(priced.map(row => row.id))];
  }));
  const cells = (id: string): (WorkshopLevelRow | undefined)[] => shown.map(member => byMember.get(memberIdentity(member))?.get(id));
  const minPrice = (id: string): number => Math.min(Infinity, ...cells(id).flatMap(row => row?.next_coins ?? []));
  const gap = (id: string): number => {
    const levels = cells(id).flatMap(row => row?.level_min ?? []);
    return levels.length > 1 ? Math.max(...levels) - Math.min(...levels) : -1;
  };
  const needle = query.trim().toLowerCase();
  const visible = catalog.filter(item => (!needle || item.name.toLowerCase().includes(needle))
    && !(hideMaxed && cells(item.id).every(row => !row || row.status === "maxed" || row.status === "unseen")));
  const sorted = sort === "category" ? null
    : [...visible].sort((a, b) => sort === "cheapest" ? minPrice(a.id) - minPrice(b.id) : gap(b.id) - gap(a.id));

  const toggleFocus = (member: RerollMember): void => setFocus(current => current ? null : memberIdentity(member));
  const toggleCategory = (category: string): void => setCollapsed(current => {
    const next = new Set(current); if (!next.delete(category)) next.add(category); return next;
  });

  const reads = members.flatMap(member => [...(byMember.get(memberIdentity(member))?.values() ?? [])].flatMap(row => row.observed_at ?? []));
  const oldest = reads.length ? Math.min(...reads) : null;
  const pending = members.some(member => !results[memberIdentity(member)]);

  const row = (item: WorkshopLevelRow): React.JSX.Element => <tr key={item.id} className="border-t border-border/60">
    <th scope="row" className="sticky left-0 z-10 bg-card px-4 py-2 text-left text-sm font-normal">{item.name}</th>
    {shown.map(member => <td key={memberIdentity(member)} className="px-3 py-2 align-top">
      <Cell row={byMember.get(memberIdentity(member))?.get(item.id)} color={deviceColor(member.name)} now={now} focused={focus !== null}
        cheapest={cheapest.get(memberIdentity(member))?.has(item.id) ?? false} />
    </td>)}
  </tr>;

  return <section aria-label="Workshop levels" className="overflow-hidden rounded-xl border border-border bg-card">
    <header className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
      <p className="text-xs text-muted-foreground">
        {pending ? "Loading Workshop reads…" : oldest === null ? "No Workshop reads yet" : `Oldest read ${ago(now - oldest)}`}
        {" · "}<span className="inline-flex items-center gap-1"><i className="inline-block size-1.5 rounded-full bg-foreground" /> 3 cheapest next upgrades per emulator</span>
      </p>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <input aria-label="Search upgrades" placeholder="Search…" value={query} onChange={event => setQuery(event.target.value)}
          className="h-8 w-36 rounded-md border border-border bg-transparent px-2 text-sm" />
        <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={hideMaxed} onChange={event => setHideMaxed(event.target.checked)} /> Hide maxed &amp; unread</label>
        <select aria-label="Sort upgrades" value={sort} onChange={event => setSort(event.target.value as Sort)} className="h-8 rounded-md border border-border bg-card px-2 text-xs">
          <option value="category">By category</option>
          <option value="cheapest">Cheapest next</option>
          <option value="gap">Biggest level gap</option>
        </select>
        <div role="group" aria-label="Layout" className="inline-flex gap-0.5 rounded-md border border-border p-0.5">
          {(["table", "cubes"] as const).map(option => <button key={option} type="button" aria-pressed={view === option}
            onClick={() => { setView(option); rememberView(option); }}
            className={`rounded px-2.5 py-1 text-xs capitalize ${view === option ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:text-foreground"}`}>
            {option}
          </button>)}
        </div>
      </div>
    </header>
    {view === "cubes" ? <WorkshopCubes members={shown} catalog={catalog} visible={sorted ?? visible}
      rowsOf={member => byMember.get(memberIdentity(member)) ?? new Map()}
      cheapest={member => cheapest.get(memberIdentity(member)) ?? new Set()}
      errors={member => results[memberIdentity(member)]?.error ?? null}
      now={now} focused={focus !== null} onFocus={toggleFocus} collapsed={collapsed} onToggle={toggleCategory} />
    : <div className="max-h-[75vh] overflow-auto">
      <table className="w-full min-w-[480px] border-separate border-spacing-0 text-sm">
        <thead className="sticky top-0 z-20 bg-card">
          <tr>
            <th className="sticky left-0 z-30 w-44 bg-card px-4 py-3 text-left text-xs font-medium text-muted-foreground">Upgrade</th>
            {shown.map(member => <th key={memberIdentity(member)} className="min-w-28 px-3 py-3 text-left">
              <button type="button" aria-pressed={focus === memberIdentity(member)} title={focus ? "Show all emulators" : `Focus ${member.name}`}
                onClick={() => toggleFocus(member)}
                className="inline-flex items-center gap-1.5 rounded-md text-xs font-medium hover:underline">
                <i className="size-2.5 rounded-full" style={{ backgroundColor: deviceColor(member.name) }} />{member.name}
              </button>
              {results[memberIdentity(member)]?.error && <p className="mt-1 text-[10px] font-normal text-danger">{results[memberIdentity(member)].error}</p>}
            </th>)}
          </tr>
        </thead>
        <tbody>
          {sorted ? sorted.map(row) : CATEGORIES.map(category => {
            const items = visible.filter(item => item.category === category);
            if (!items.length) return null;
            const open = !collapsed.has(category);
            return <Fragment key={category}>
              <tr className="bg-muted/30">
                <th scope="rowgroup" className="sticky left-0 z-10 bg-muted px-4 py-2 text-left">
                  <button type="button" aria-expanded={open} onClick={() => toggleCategory(category)} className="inline-flex items-center gap-1 text-xs font-semibold tracking-wide">
                    {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}{category} <span className="font-normal text-muted-foreground">({items.length})</span>
                  </button>
                </th>
                {shown.map(member => {
                  const share = averageShare(items.map(item => byMember.get(memberIdentity(member))?.get(item.id)));
                  return <td key={memberIdentity(member)} className="px-3 py-2 font-mono text-[11px] text-muted-foreground">
                    {share === null ? "—" : `avg ${Math.round(share * 100)}%`}
                  </td>;
                })}
              </tr>
              {open && items.map(row)}
            </Fragment>;
          })}
          {!pending && !visible.length && <tr><td colSpan={shown.length + 1} className="p-8 text-center text-sm text-muted-foreground">No upgrades match.</td></tr>}
        </tbody>
      </table>
    </div>}
    {view === "cubes" && !pending && !visible.length && <p className="p-8 text-center text-sm text-muted-foreground">No upgrades match.</p>}
    {focused && <RecentWorkshopBuys member={focused} />}
    <p className="border-t px-4 py-3 text-xs text-muted-foreground">
      Levels are inferred by matching each Workshop stat the bot read to the upgrade’s per-level value table. A range (80–82) means the read’s
      rounding fits several levels, and “?” means the read fits none (for example a lab bonus). The next price assumes the lowest matching level and no Workshop discount labs.
    </p>
  </section>;
}
