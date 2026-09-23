"use client";

import Link from "next/link";
import { useState } from "react";
import { ChevronDown, RotateCcw, Trash2 } from "lucide-react";
import { Meter } from "@/components/Meter";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { accountAge, coinsPerSecond } from "@/lib/accountMetrics";
import type { RerollMember } from "@/lib/fleet";
import { deletable, LADDER, ladderProgress, STONES_WAVE, standingFor } from "@/lib/rerollState";
import { cn } from "@/lib/utils";
import { PlanStrip } from "./PlanStrip";
import { WorkerBattlePurchases } from "./Purchases";

function display(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function observed(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

/** How long ago the worker last reported, in the coarsest unit that is still
 *  true. A pool card's greatest lie is fresh-looking numbers from a worker
 *  that stopped reporting twenty minutes ago, and the only cure is to print
 *  the age of the reading beside it. */
function freshness(value: string | number | null | undefined): { text: string; stale: boolean } | null {
  if (value === null || value === undefined) return null;
  const at = new Date(typeof value === "number" ? value * 1000 : value).getTime();
  if (Number.isNaN(at)) return null;
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  const stale = seconds > 180;
  if (seconds < 60) return { text: `${seconds}s ago`, stale };
  if (seconds < 3600) return { text: `${Math.round(seconds / 60)}m ago`, stale };
  return { text: `${Math.round(seconds / 3600)}h ago`, stale };
}

/** One number with its name over it. Smaller and denser than the StatTile the
 *  Live page uses: a device card carries a dozen of these and they are read as
 *  a block, not one at a time. */
function Vital({ label, value, tone }: { label: string; value: string | number | null | undefined; tone?: "live" | "warn" }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint-foreground" title={label}>
        {label}
      </div>
      <div className={cn("mt-0.5 truncate font-mono text-[13px]", tone === "live" && "text-live", tone === "warn" && "text-warn")}>
        {display(value)}
      </div>
    </div>
  );
}

/**
 * Where this account stands on the reroll ladder.
 *
 * The ladder is the entire reason the pool exists - every worker is climbing
 * the same three rungs to wave 60 and a human's Ultimate Weapon pick - and
 * before this the only trace of it on a device card was a goal sentence
 * buried in the plan text. A reader could not tell a worker two waves from
 * the finish line from one that has never completed a run.
 */
function LadderRail({ best, stage }: { best: number | null | undefined; stage?: string }) {
  const progress = ladderProgress(best);
  // The planner's own stage wins when it has spoken: it knows which build it
  // selected, and `best` is only this page's approximation of the same fact.
  const rung = stage === "stones" ? 2 : stage === "turtle" ? Math.max(1, progress.rung) : progress.rung;
  const current = LADDER[rung];

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium">{current.goal}</span>
        <span className="font-mono text-[11px] text-faint-foreground">
          {progress.wave === null
            ? "no verified run yet"
            : progress.remaining === null
              ? `T1 W${progress.wave} · finished`
              : `T1 W${progress.wave} · ${progress.remaining} to go`}
        </span>
      </div>

      <div className="relative mt-2">
        <Meter
          label="Reroll ladder progress"
          value={progress.percent}
          max={100}
          unknown={progress.wave === null}
          tone={progress.percent >= 100 ? "live" : "primary"}
          className="h-2"
        />
        {/* The rung boundary at wave 20, where build_selection swaps the
            opening build for the turtle one. Drawn on the track rather than
            written underneath it: the reader wants to see which side of it
            this account is on, not read a number and compare. */}
        <span
          aria-hidden="true"
          className="absolute top-0 h-2 w-px bg-background"
          style={{ left: "33%" }}
        />
      </div>

      <ol className="mt-1.5 flex justify-between gap-2 text-[10px]">
        {LADDER.map((step, index) => (
          <li
            key={step.id}
            className={cn(
              "flex items-center gap-1 font-mono uppercase tracking-[0.08em]",
              index < rung && "text-live",
              index === rung && "text-foreground",
              index > rung && "text-faint-foreground",
            )}
            title={step.blurb}
          >
            <span
              aria-hidden="true"
              className={cn(
                "size-1.5 rounded-full",
                index < rung && "bg-live",
                index === rung && "bg-primary",
                index > rung && "bg-border-strong",
              )}
            />
            {step.title}
            {step.target ? <span className="text-faint-foreground">W{step.target}</span> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * One emulator in the pool, top to bottom in the order a person asks about it:
 * is it alive, does it want anything from me, how far has it got, what is it
 * about to spend, and only then every reading behind those answers.
 */
export function DeviceCard({
  member,
  accountKey,
  accountId,
  collapsed,
  onToggle,
  onStart,
  onPause,
  onRemove,
  onHide,
  onJournal,
  onOpenAccount,
  busy,
}: {
  member: RerollMember;
  accountKey?: string | null;
  accountId?: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onStart: () => void;
  onPause: () => void;
  onRemove: () => void;
  /** Deletes the card from the list (or restores it); the worker is untouched. */
  onHide: () => void;
  onJournal: () => void;
  onOpenAccount?: () => void;
  busy: boolean;
}) {
  const standing = standingFor(member.state, member.error);
  const age = freshness(member.observed_at);
  const region = `worker-${member.name}`;
  const [confirming, setConfirming] = useState<"remove" | null>(null);

  return (
    <article
      className={cn(
        "flex min-w-0 flex-col gap-3 overflow-hidden rounded-xl border border-l-4 bg-card p-3 text-sm ring-1 ring-foreground/10 transition-colors",
        standing.tone === "live" && "border-l-live",
        standing.tone === "warn" && "border-l-warn",
        standing.tone === "error" && "border-l-danger",
        standing.tone === "idle" && "border-l-border-strong",
        standing.needsYou && "ring-2 ring-warn/30",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate font-heading text-[15px] font-semibold">{member.name}</h3>
            <StatusBadge state={standing.tone}>{standing.label}</StatusBadge>
          </div>
          <p className="mt-0.5 font-mono text-[11px] text-faint-foreground">
            {member.endpoint}
            {age ? (
              <>
                {" · "}
                <span className={age.stale ? "text-warn" : undefined} title={observed(member.observed_at)}>
                  seen {age.text}
                </span>
              </>
            ) : null}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {accountId && onOpenAccount ? (
            <Link
              href="/"
              onClick={onOpenAccount}
              className="text-xs text-primary underline-offset-2 hover:underline"
            >
              Open account {accountId}
            </Link>
          ) : null}
          {deletable(member) ? <Button
            variant="ghost"
            size="icon-sm"
            disabled={busy}
            aria-label={member.hidden ? `Restore ${member.name}` : `Delete ${member.name} from the list`}
            title={member.hidden ? "Show this device in the list again" : "Delete from the list - the worker and emulator keep running"}
            onClick={onHide}
          >
            {member.hidden ? <RotateCcw aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
          </Button> : null}
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${member.name}`}
            aria-expanded={!collapsed}
            aria-controls={region}
            onClick={onToggle}
          >
            <ChevronDown className={cn("transition-transform", !collapsed && "rotate-180")} aria-hidden="true" />
          </Button>
        </div>
      </div>

      {/* What the device is doing this second, before any history. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
        <span>{display(member.game_screen)}</span>
        {member.milestone ? <span className="text-faint-foreground">· {member.milestone}</span> : null}
        {member.current_run_id != null ? <span className="text-faint-foreground">· run #{member.current_run_id}</span> : null}
      </div>

      {/* A state that stops until a person acts says so in full, in its own
          tinted box. This is the one thing on the card that must survive
          being glanced at from across the room. */}
      {standing.needsYou ? (
        <div
          className={cn(
            "rounded-lg border p-2.5",
            standing.tone === "error" ? "border-danger/40 bg-danger-surface" : "border-warn/40 bg-warn-surface",
          )}
        >
          {/* The state's name is already on the badge above; repeating it
              here made one condition look like two findings. What the banner
              adds is what to DO about it, and the reading behind it. */}
          <p className={cn("text-[13px]", standing.tone === "error" ? "text-danger" : "text-warn")}>
            {standing.hint}
          </p>
          {member.uw_result ? (
            <p className="mt-1.5 font-mono text-xs text-foreground">{member.uw_result}</p>
          ) : null}
          {member.error ? (
            <p className="mt-1.5 break-words font-mono text-xs text-danger [overflow-wrap:anywhere]">{member.error}</p>
          ) : null}
        </div>
      ) : null}

      <div id={region} hidden={collapsed} className="flex min-w-0 flex-col gap-3">
        <LadderRail best={member.best_tier_1_wave} stage={member.reroll_plan?.stage} />

        {/* Two columns on a phone: three left "Workshop upgrades bought"
            truncated to a word and a half at 420px. */}
        <div className="grid grid-cols-2 gap-x-3 gap-y-2.5 min-[420px]:grid-cols-3 sm:grid-cols-4">
          <Vital label="Tier / wave" value={member.tier != null && member.wave != null ? `${member.tier} / ${member.wave}` : null} />
          <Vital label="Best T1 wave" value={member.best_tier_1_wave} tone={(member.best_tier_1_wave ?? 0) >= STONES_WAVE ? "live" : undefined} />
          <Vital label="Run duration (s)" value={member.run_duration_seconds} />
          <Vital label="Battle cash" value={member.battle_cash} />
          <Vital label="Run coins" value={member.run_coins} />
          <Vital label="Lifetime coins" value={member.lifetime_coins} tone={member.lifetime_coins_incomplete ? "warn" : undefined} />
          <Vital label="Recent CPS" value={coinsPerSecond(member.recent_cps ?? null)} />
          <Vital label="Account age" value={accountAge(member.account_age_days ?? null)} />
          <Vital label="Workshop upgrades bought" value={member.workshop_upgrades_bought} />
          <Vital label="Verified account ID" value={member.account_id ?? accountId} />
          {/* The Ultimate Weapon reading is the payload of the attention
              banner above when there is one, so it is not repeated here -
              the same sentence twice on one card reads as two findings. */}
          {standing.needsYou && member.uw_result ? null : <Vital label="UW result" value={member.uw_result} />}
          <Vital label="Last observation" value={observed(member.observed_at)} />
        </div>

        {/* The four wallets, as chips rather than four more vitals: they are
            read together ("has it got stones yet?") and never individually. */}
        <div className="flex flex-wrap gap-1.5 font-mono text-[11px]">
          {([["gems", member.wallet_gems], ["stones", member.wallet_stones], ["medals", member.wallet_medals]] as const).map(
            ([name, value]) => (
              <span
                key={name}
                className={cn(
                  "rounded-md border px-2 py-0.5",
                  value ? "border-border-strong text-foreground" : "border-border text-faint-foreground",
                )}
              >
                {display(value)} <span className="text-faint-foreground">{name}</span>
              </span>
            ),
          )}
        </div>

        {member.lifetime_coins_incomplete ? (
          <p className="text-xs text-muted-foreground">
            Lifetime coins await an unreadable run result; showing the last verified baseline.
          </p>
        ) : null}

        {member.reroll_plan ? (
          <PlanStrip plan={member.reroll_plan} worker={member.name} />
        ) : member.state === "running" ? (
          <p className="text-sm text-muted-foreground">Workshop plan updates when this worker reaches the main menu.</p>
        ) : null}

        <WorkerBattlePurchases accountKey={accountKey} />

        <details className="rounded-lg border border-border">
          <summary className="cursor-pointer px-2.5 py-1.5 text-xs font-medium text-muted-foreground">
            Evidence and controls
          </summary>
          <div className="space-y-2 border-t border-border px-2.5 py-2 text-xs">
            <p className="break-words [overflow-wrap:anywhere]">Recent runs: {member.recent_runs?.join(", ") || "—"}</p>
            <p className="break-words [overflow-wrap:anywhere]">Evidence: {display(member.evidence)}</p>
            <p className="break-words [overflow-wrap:anywhere]">Error: {display(member.error)}</p>
            <p className="text-faint-foreground">{standing.hint}</p>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="xs" variant="outline" disabled={busy} onClick={onStart}>Start</Button>
              <Button size="xs" variant="outline" disabled={busy} onClick={onPause}>Pause</Button>
              <Button size="xs" variant="outline" onClick={onJournal}>Show journal</Button>
              <Button size="xs" variant="outline" disabled={busy} onClick={() => setConfirming("remove")}>Remove</Button>
              {member.leave_error && <span className="text-danger">Couldn&apos;t stop this bot when the reroll changed: {member.leave_error}</span>}
            </div>
          </div>
        </details>
      </div>
      <ConfirmDialog open={confirming === "remove"} onOpenChange={open => setConfirming(open ? "remove" : null)}
        title={`Remove ${member.name} from the reroll?`}
        footer={<><Button variant="outline" onClick={() => setConfirming(null)}>Cancel</Button>
          <Button onClick={() => { setConfirming(null); onRemove(); }}>Remove {member.name}</Button></>}>
        <p>Its worker stops and its emulator shuts down. You can add it back later from the Add list, and it continues with the same account.</p>
      </ConfirmDialog>
    </article>
  );
}
