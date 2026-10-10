"use client";

import { StatTile } from "@/components/StatTile";
import { StatusBadge, type MachineState } from "@/components/StatusBadge";
import { duration, money } from "@/lib/format";
import { median, useScanRate, useScreenAge, useTick } from "@/lib/useDerived";
import type { RunRow, StatusPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** One composite answer to "do I need to get up?".
 *
 * A real error outranks a dropped connection: a stale page still showing the
 * last known failure is more useful than one reporting only that it cannot
 * reach the bot. */
function health(status: StatusPayload | null, connected: boolean): { state: MachineState; label: string } {
  if (status?.last_error) return { state: "error", label: "ERROR" };
  if (!connected) return { state: "warn", label: "STALE" };
  if (!status) return { state: "idle", label: "—" };
  return status.bot.running ? { state: "live", label: "OK" } : { state: "idle", label: "IDLE" };
}

export function StatBar({
  status,
  connected,
  runs = [],
}: {
  status: StatusPayload | null;
  connected: boolean;
  /** Newest first, as /api/runs returns them. Feeds the wave tile's trend. */
  runs?: RunRow[];
}) {
  // The run clock and the time-on-screen counter both advance between polls.
  useTick(1000);

  const age = useScreenAge(status?.screen);
  const rate = useScanRate(status?.scans);
  const { state, label } = health(status, connected);

  const waves = runs.map((run) => run.wave).filter((wave): wave is number => wave != null);
  const lastWave = waves.length ? waves[0] : null;
  const par = median(waves.slice(1, 11));
  // Rounded: the median of an even-length window lands on a half, and
  // "+289.5 waves" reads as false precision for a whole-numbered counter.
  const delta = lastWave != null && par != null ? Math.round(lastWave - par) : null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge state={connected ? "live" : "warn"}>
          {connected ? "live" : "reconnecting"}
        </StatusBadge>
        <StatusBadge state={status?.bot.running ? "live" : "idle"}>
          {status?.bot.running ? "bot running" : "bot stopped"}
        </StatusBadge>
        {status?.bot.push_runs && status.bot.push_runs.every > 0 && (
          <StatusBadge state={status.bot.push_runs.blocker ? "warn" : "idle"}>
            {status.bot.push_runs.blocker
              ? status.bot.push_runs.blocker
              : status.bot.push_runs.phase === "farming"
                ? `farming · push in ${status.bot.push_runs.farms_remaining} runs`
                : status.bot.push_runs.phase === "returning"
                  ? `returning to tier ${status.bot.push_runs.farm_tier}`
                  : `push · ${status.bot.push_runs.target_tier ? `tier ${status.bot.push_runs.target_tier}` : "selecting tier"}`}
          </StatusBadge>
        )}
        {status?.last_error ? (
          <span className="font-mono text-xs text-danger">{status.last_error}</span>
        ) : null}
        {status && status.dropped > 0 ? (
          <StatusBadge state="warn">{status.dropped} dropped</StatusBadge>
        ) : null}
      </div>

      {/* The strip dims and breathes when the stream is down. Frozen numbers
          that still look live are worse than no numbers - see motion item 5. */}
      <div
        className={cn(
          "grid grid-cols-2 gap-3 transition-opacity sm:grid-cols-3 lg:grid-cols-7",
          !connected && "opacity-55 motion-safe:animate-breathe",
        )}
      >
        <StatTile
          className="lg:col-span-2"
          size="hero"
          label="screen"
          tone={state === "error" ? "danger" : "none"}
          value={status?.screen ?? "—"}
          // Time-on-screen is the anomaly detector: a bot parked on one screen
          // for five minutes is the failure mode, and nothing surfaced it.
          sub={status ? `for ${duration(age.seconds)}${age.exact ? "" : "+"}` : undefined}
        />
        <StatTile
          label="run"
          value={status?.run ? `#${status.run.id}` : "idle"}
          tone={status?.run ? "live" : "none"}
          sub={status?.run ? duration(status.run.elapsed) : undefined}
        />
        <StatTile
          label="wave"
          value={lastWave ?? "—"}
          trend={waves.length > 1 ? [...waves].reverse() : undefined}
          sub={delta == null ? undefined : `${delta >= 0 ? "▲ +" : "▼ "}${delta} vs median`}
          subTone={delta == null ? "none" : delta >= 0 ? "live" : "warn"}
        />
        <StatTile
          label="scan rate"
          value={rate == null ? "—" : rate.toFixed(1)}
          unit={rate == null ? undefined : "/s"}
          sub={status ? `${status.scans} total` : undefined}
        />
        <StatTile label="wallet" value={status ? money(status.wallet) : "—"} />
        <StatTile
          label="health"
          value={label}
          tone={state === "live" ? "live" : state === "warn" ? "warn" : state === "error" ? "danger" : "none"}
          sub={status ? `up ${duration(status.uptime)} · ${status.runs_completed} runs` : undefined}
        />
      </div>
    </div>
  );
}
