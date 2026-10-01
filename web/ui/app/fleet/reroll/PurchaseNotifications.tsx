"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchAccountWorkshopLevels, fetchAccountWorkshopPurchases } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import type { LedgerLine, WorkshopLevelRow } from "@/lib/types";
import { freshFleetEvidence } from "./FleetOverviewSummary";
import { CATEGORY_COLOR, gameNumber, levelText } from "./workshop/workshopFormat";

const POLL_MS = 5_000;
const DISPLAY_MS = 6_000;
const MAX_VISIBLE = 3;

type Source = { name: string; accountKey: string; accountId: string };
type Notice = { key: string; source: Source; line: LedgerLine; level: string; expiresAt: number | null };

function startQueued(notices: Notice[], now: number): Notice[] {
  return notices.map((notice, index) => index < MAX_VISIBLE && notice.expiresAt === null
    ? { ...notice, expiresAt: now + DISPLAY_MS } : notice);
}

function currentLevel(rows: WorkshopLevelRow[], item: string | null): string {
  const row = rows.find(candidate => candidate.name.toLowerCase() === item?.toLowerCase());
  if (!row || row.level_min === null) return "Current Lv ?";
  return `Current Lv ${levelText(row)}${row.level_min !== row.level_max ? " (est.)" : ""}`;
}

function nextMove(member: RerollMember | undefined, purchaseAt: number): string {
  if (!member) return "Calculating next move…";
  const afterPurchase = (at: number | null | undefined): boolean =>
    at != null && at > purchaseAt && freshFleetEvidence(at);
  const accountId = member.account_id;
  const battle = member.battle_evaluation;
  const workshop = member.workshop_evaluation;
  const resource = member.resource_evaluation;
  const battleDecision = battle?.decision;
  const workshopDecision = workshop?.decision;
  if (battleDecision && battleDecision.account_id === accountId && afterPurchase(battle?.evidence_at)) return battleDecision.reason;
  if (workshopDecision && workshopDecision.account_id === accountId && afterPurchase(workshop?.evidence_at)) return workshopDecision.reason;
  if (resource && resource.account_id === accountId && afterPurchase(resource.observed_at))
    return resource.lab_step.reason || resource.gem_step.reason;
  const plan = member.reroll_plan;
  if (plan && plan.account_id === accountId && afterPurchase(plan.observed_at)) return plan.reason;
  return "Calculating next move…";
}

function purchasePrice(line: LedgerLine): string {
  const price = line.price ?? (line.delta !== null && line.delta < 0 ? -line.delta : null);
  if (price === null) return "Price unknown";
  return `${gameNumber(price)} ${line.currency ?? "coins"}`;
}

/** Verified Workshop receipts, independent of the selected account and visible over the live wall. */
export function PurchaseNotifications({ members }: { members: RerollMember[] }): React.JSX.Element {
  const [notices, setNotices] = useState<Notice[]>([]);
  const sourcesKey = JSON.stringify(members.filter(member => member.account_key && member.account_id)
    .map(member => ({ name: member.name, accountKey: member.account_key, accountId: member.account_id })));
  const sources = useMemo(() => JSON.parse(sourcesKey) as Source[], [sourcesKey]);

  useEffect(() => {
    let active = true;
    const startedAt = Date.now() / 1000;
    const seen = new Map<string, number>();
    const busy = new Set<string>();
    const identities = new Set(sources.map(source => `${source.accountKey}:${source.accountId}`));
    setNotices(current => current.filter(notice => identities.has(`${notice.source.accountKey}:${notice.source.accountId}`)));
    const refresh = async (): Promise<void> => {
      await Promise.all(sources.map(async source => {
        const identity = `${source.accountKey}:${source.accountId}`;
        if (busy.has(identity)) return;
        busy.add(identity);
        try {
          const payload = await fetchAccountWorkshopPurchases(source.accountKey, undefined, source.accountId);
          if (!active || (payload.account_id && payload.account_id !== source.accountId)) return;
          const newest = Math.max(0, ...payload.lines.map(line => line.id));
          const previous = seen.get(identity);
          seen.set(identity, Math.max(previous ?? 0, newest));
          // Establish the receipt cursor without losing buys that arrive during the first read.
          const fresh = payload.lines.filter(line => (previous === undefined ? line.ts > startedAt : line.id > previous)
            && line.kind === "WORKSHOP_BUY"
            && line.dry_run === 0 && (line.detail?.verdict === "bought" || line.detail?.verdict === "free"))
            .sort((a, b) => a.id - b.id);
          if (!fresh.length) return;
          const added = fresh.map(line => ({ key: `${identity}:${line.id}`, source, line,
            level: "Current Lv ?", expiresAt: null }));
          setNotices(current => startQueued([...current, ...added], Date.now()));
          try {
            const levels = await fetchAccountWorkshopLevels(source.accountKey, source.accountId);
            if (!active || levels.account_id !== source.accountId) return;
            const labels = new Map(fresh.map(line => [`${identity}:${line.id}`, currentLevel(levels.upgrades, line.item)]));
            setNotices(current => current.map(notice => labels.has(notice.key)
              ? { ...notice, level: labels.get(notice.key)! } : notice));
          } catch {
            // A level read is optional evidence; the purchase receipt still displays.
          }
        } catch {
          // A missed read is retried on the next interval without advancing the receipt cursor.
        } finally {
          busy.delete(identity);
        }
      }));
    };
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, POLL_MS);
    return () => { active = false; window.clearInterval(interval); };
  }, [sources]);

  useEffect(() => {
    const expiry = notices.flatMap(notice => notice.expiresAt ?? []).sort((a, b) => a - b)[0];
    if (expiry === undefined) return;
    const timer = window.setTimeout(() => setNotices(current => startQueued(
      current.filter(notice => notice.expiresAt === null || notice.expiresAt > Date.now()), Date.now(),
    )), Math.max(0, expiry - Date.now()));
    return () => window.clearTimeout(timer);
  }, [notices]);

  if (!notices.length) return <></>;
  return <div className="pointer-events-none fixed inset-x-3 bottom-3 z-[60] flex flex-col items-center gap-2 sm:bottom-5">
    {notices.slice(0, MAX_VISIBLE).map(notice => {
      const color = CATEGORY_COLOR[notice.line.category ?? ""] ?? "var(--primary)";
      const member = members.find(candidate => candidate.name === notice.source.name
        && candidate.account_key === notice.source.accountKey && candidate.account_id === notice.source.accountId);
      return <article key={notice.key} role="status" aria-label={`${notice.line.item ?? "Upgrade"} Workshop purchase`}
        className="w-full max-w-md overflow-hidden rounded-xl border-2 bg-card/95 text-card-foreground shadow-2xl backdrop-blur-md"
        style={{ borderColor: color, backgroundImage: `linear-gradient(135deg, color-mix(in oklch, ${color} 18%, var(--card)), var(--card) 70%)` }}>
        <div className="h-1" style={{ backgroundColor: color }} aria-hidden="true" />
        <div className="space-y-2 px-4 py-3">
          <div className="flex items-center justify-between gap-2 text-xs font-semibold uppercase tracking-wider">
            <span style={{ color }}>{notice.line.category ?? "Workshop"} · Purchased</span>
            <span className="text-muted-foreground">{notice.source.name}</span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <strong className="min-w-0 truncate text-lg">{notice.line.item ?? "Workshop upgrade"}</strong>
            <span className="shrink-0 rounded-full border px-2 py-1 text-xs font-semibold" style={{ borderColor: color, color }}>{notice.level}</span>
          </div>
          <p className="text-sm font-medium">Paid {purchasePrice(notice.line)}</p>
          <p className="border-t pt-2 text-sm" style={{ borderColor: color }}><span className="font-semibold">Next:</span> {nextMove(member, notice.line.ts)}</p>
        </div>
      </article>;
    })}
  </div>;
}
