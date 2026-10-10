"use client";

import { SectionCard } from "@/components/ui/section-card";
import { useAutopilot } from "@/lib/useAutopilot";
import type { AutopilotSnapshot } from "@/lib/types";

const readable = (value: string) => value.replace(/_/g, " ");
export function AutopilotStatus() {
  const { snapshot, unreachable, now } = useAutopilot();
  return (
    <AutopilotStatusPanel
      snapshot={snapshot}
      unreachable={unreachable}
      now={now}
    />
  );
}

export function AutopilotStatusPanel({
  snapshot,
  unreachable = false,
  now = Date.now(),
}: {
  snapshot: AutopilotSnapshot | null;
  unreachable?: boolean;
  now?: number;
}) {
  const age = snapshot?.updated_at
    ? Math.max(0, Math.floor(now / 1000 - snapshot.updated_at))
    : null;
  const fresh = !unreachable && age !== null && age <= 15;
  const comparison = snapshot?.tier_comparison;
  const tiers = comparison?.tiers;
  return (
    <SectionCard
      title="Autopilot decisions"
      tone={!fresh ? "warn" : undefined}
      contentClassName="flex flex-col gap-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="rounded bg-muted px-2 py-1 font-mono text-xs uppercase">
          {snapshot ? readable(snapshot.phase) : "Waiting for observations"}
        </span>
        <span className={`text-xs ${fresh ? "text-live" : "text-warn"}`}>
          {unreachable
            ? "Disconnected · last known state"
            : age === null
              ? "No observation yet"
              : `${fresh ? "Fresh" : "Stale"} · ${age}s ago`}
        </span>
      </div>
      <p className="text-sm">
        {snapshot?.reason ||
          "The bot will explain its next decision after observing the game."}
      </p>
      {snapshot?.tournament && snapshot.tournament.stage !== "idle" && <p className="rounded border p-2 text-sm">Tournament · {readable(snapshot.tournament.stage)}{snapshot.tournament.league ? ` · ${snapshot.tournament.league}` : ""}{snapshot.tournament.reason ? ` · ${readable(snapshot.tournament.reason)}` : ""}</p>}
      <dl className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-xs text-muted-foreground">
            Next planned upgrade
          </dt>
          <dd className="mt-1 capitalize">
            {snapshot?.next_upgrade_id
              ? readable(snapshot.next_upgrade_id)
              : "Waiting"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Verified purchases</dt>
          <dd className="mt-1 font-mono">
            {snapshot?.verified_purchases ?? 0}
          </dd>
        </div>
      </dl>
      {snapshot?.last_purchase ? (
        <p className="text-xs text-muted-foreground">
          Last verified:{" "}
          {snapshot.last_purchase.name ??
            readable(snapshot.last_purchase.upgrade_id ?? "upgrade")}
          {snapshot.last_purchase.price != null
            ? ` · ${snapshot.last_purchase.price.toLocaleString()}`
            : ""}
        </p>
      ) : null}
      {comparison ? (
        <div className="border-t pt-3">
          <p className="text-sm font-medium">Farming tier comparison</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {typeof comparison.reason === "string"
              ? comparison.reason
              : "Waiting for completed runs."}
          </p>
          {typeof comparison.recommended_tier === "number" ? (
            <p className="mt-1 text-sm">
              Suggested farming tier: {comparison.recommended_tier} · change in
              game
            </p>
          ) : null}
          {Array.isArray(tiers) && tiers.length > 0 ? (
            <table className="mt-2 w-full text-left text-xs">
              <thead>
                <tr>
                  <th>Tier</th>
                  <th>Complete runs</th>
                  <th>Coins / hour</th>
                  <th>Median wave</th>
                </tr>
              </thead>
              <tbody>
                {tiers.map(
                  (row: {
                    tier: number;
                    runs: number;
                    coins_per_hour: number;
                    median_wave: number;
                  }) => (
                    <tr key={row.tier}>
                      <td className="py-1">{row.tier}</td>
                      <td>{row.runs}</td>
                      <td>{Math.round(row.coins_per_hour).toLocaleString()}</td>
                      <td>{row.median_wave}</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          ) : null}
        </div>
      ) : null}
    </SectionCard>
  );
}
