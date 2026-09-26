"use client";

import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Clock } from "lucide-react";
import { fetchAccountWorkshopLevels } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { deviceColor } from "@/lib/rerollState";
import type { WorkshopLevelRow, WorkshopLevels } from "@/lib/types";
import { memberIdentity } from "../statsHelpers";
import { RecentWorkshopBuys } from "./RecentWorkshopBuys";

const CATEGORIES = ["ATTACK", "DEFENSE", "UTILITY"] as const;
const STALE_SECONDS = 24 * 3600;
const CHEAPEST_MARKED = 3;
type Sort = "category" | "cheapest" | "gap";
type Result = { data: WorkshopLevels | null; error: string | null };

const SUFFIXES = ["", "K", "M", "B", "T", "q", "Q"];
/** Coin prices the way the game prints them: 331, 1.23K, 4.68T. */
export function gameNumber(value: number): string {
  let tier = 0;
  while (Math.abs(value) >= 1000 && tier < SUFFIXES.length - 1) { value /= 1000; tier += 1; }
  return tier === 0 ? String(Math.round(value)) : `${value.toFixed(2)}${SUFFIXES[tier]}`;
}

function ago(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/** Share of the ladder climbed; the lower bound of an ambiguous read. */
function progress(row: WorkshopLevelRow): number | null {
  if (row.status === "maxed") return 1;
  return row.level_min === null ? null : row.level_min / row.max_level;
}

function levelText(row: WorkshopLevelRow): string {
  if (row.level_min === null) return "?";
  return row.level_min === row.level_max ? String(row.level_min) : `${row.level_min}–${row.level_max}`;
}

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

export function WorkshopMatrix({ members, focusWorker = null }: { members: RerollMember[]; focusWorker?: string | null }): React.JSX.Element {
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => ({ name, account_key, account_id, lease_id })));
  const [results, setResults] = useState<Record<string, Result>>({});
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [hideMaxed, setHideMaxed] = useState(true);
  const [sort, setSort] = useState<Sort>("category");
  const [query, setQuery] = useState("");
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
      </div>
    </header>
    <div className="max-h-[75vh] overflow-auto">
      <table className="w-full min-w-[480px] border-separate border-spacing-0 text-sm">
        <thead className="sticky top-0 z-20 bg-card">
          <tr>
            <th className="sticky left-0 z-30 w-44 bg-card px-4 py-3 text-left text-xs font-medium text-muted-foreground">Upgrade</th>
            {shown.map(member => <th key={memberIdentity(member)} className="min-w-28 px-3 py-3 text-left">
              <button type="button" aria-pressed={focus === memberIdentity(member)} title={focus ? "Show all emulators" : `Focus ${member.name}`}
                onClick={() => setFocus(current => current ? null : memberIdentity(member))}
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
                  <button type="button" aria-expanded={open} onClick={() => setCollapsed(current => {
                    const next = new Set(current); if (!next.delete(category)) next.add(category); return next;
                  })} className="inline-flex items-center gap-1 text-xs font-semibold tracking-wide">
                    {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}{category} <span className="font-normal text-muted-foreground">({items.length})</span>
                  </button>
                </th>
                {shown.map(member => {
                  const shares = items.flatMap(item => { const share = byMember.get(memberIdentity(member))?.get(item.id); return share ? progress(share) ?? [] : []; });
                  return <td key={memberIdentity(member)} className="px-3 py-2 font-mono text-[11px] text-muted-foreground">
                    {shares.length ? `avg ${Math.round(shares.reduce((a, b) => a + b, 0) / shares.length * 100)}%` : "—"}
                  </td>;
                })}
              </tr>
              {open && items.map(row)}
            </Fragment>;
          })}
          {!pending && !visible.length && <tr><td colSpan={shown.length + 1} className="p-8 text-center text-sm text-muted-foreground">No upgrades match.</td></tr>}
        </tbody>
      </table>
    </div>
    {focused && <RecentWorkshopBuys member={focused} />}
    <p className="border-t px-4 py-3 text-xs text-muted-foreground">
      Levels are inferred by matching each Workshop stat the bot read to the upgrade’s per-level value table. A range (80–82) means the read’s
      rounding fits several levels, and “?” means the read fits none (for example a lab bonus). The next price assumes the lowest matching level and no Workshop discount labs.
    </p>
  </section>;
}
