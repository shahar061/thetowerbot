"use client";

import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { StatTile } from "@/components/StatTile";
import { SectionCard } from "@/components/ui/section-card";
import { duration } from "@/lib/format";
import { median } from "@/lib/useDerived";
import type { StatsPayload } from "@/lib/types";

// dataviz skill: chart surface/border tokens, so the Tooltip reads correctly
// in both themes instead of Recharts' unstyled white default.
const TOOLTIP_STYLE = {
  contentStyle: {
    background: "var(--popover)",
    color: "var(--popover-foreground)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-md)",
    fontSize: 12,
  },
  labelStyle: { color: "var(--muted-foreground)" },
  cursor: { stroke: "var(--border)" },
};

// Each panel plots a different quantity, so each takes its own slot from the
// validated categorical palette rather than four repetitions of slot 1. A
// single series still needs no legend - the panel title names what's plotted.
const WAVE = "var(--chart-1)";
const DURATION = "var(--chart-3)";
const TAPS = "var(--chart-2)";
const SCREENS = "var(--chart-4)";

function Panel({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <SectionCard title={title}>
      {note ? <p className="mb-2 text-xs text-muted-foreground">{note}</p> : null}
      <div className="h-64">{children}</div>
    </SectionCard>
  );
}

// An explicit "nothing here" state for a single panel, distinct from a chart
// with real zero-valued bars - a blank axis with no bars would otherwise read
// as ambiguous ("no data" vs "still loading"), so panels that can be empty
// even while other stored runs exist say so directly.
function EmptyPanel({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">{children}</div>;
}

export function StatsPanels({ stats, compact = false, view = "all", recentWaveWindow }: { stats: StatsPayload; compact?: boolean; view?: "all" | "wave" | "details"; recentWaveWindow?: number }): React.JSX.Element {
  const waves = stats.runs.map((r) => r.wave).filter((w): w is number => w != null);
  const historyMedianWave = median(waves);
  const medianWave = recentWaveWindow ? median(waves.slice(-recentWaveWindow)) : historyMedianWave;
  const medianLength = median(stats.runs.map((r) => r.duration));

  return (
    <div className="flex flex-col gap-4">


      {/* Derived client-side from the runs already on the page - the numbers
          you would otherwise read off four charts by eye. */}
      {view !== "details" && <div className={compact ? "grid grid-cols-2 gap-2" : "grid grid-cols-2 gap-3 sm:grid-cols-4"}>
        <StatTile label="runs" value={stats.runs.length} />
        <StatTile label={recentWaveWindow ? `recent ${recentWaveWindow} median wave` : "median wave"} value={medianWave == null ? "—" : Math.round(medianWave)}
          sub={recentWaveWindow && historyMedianWave != null ? `shown-history median W${Math.round(historyMedianWave)}` : undefined} />
        <StatTile label="best wave" value={waves.length ? Math.max(...waves) : "—"} tone="live" />
        <StatTile
          label="median length"
          value={medianLength == null ? "—" : duration(medianLength)}
        />
      </div>}

      <div className={compact ? "grid gap-4" : "grid gap-4 lg:grid-cols-2"}>
      {view !== "details" && <Panel title="Wave per run">
        <ResponsiveContainer>
          <LineChart data={stats.runs}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
            <XAxis dataKey="id" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip {...TOOLTIP_STYLE} />
            {/* Gives every point something to be read against, which a bare
                line does not. */}
            {medianWave != null ? (
              <ReferenceLine
                y={medianWave} stroke="var(--muted-foreground)" strokeDasharray="3 4"
                label={{
                  value: `median ${Math.round(medianWave)}`,
                  position: "insideTopRight",
                  fill: "var(--muted-foreground)",
                  fontSize: 10,
                }}
              />
            ) : null}
            <Line
              type="monotone" dataKey="wave" dot={stats.runs.length === 1}
              stroke={WAVE} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"
            />
          </LineChart>
        </ResponsiveContainer>
      </Panel>}

      {view !== "wave" && <><Panel title="Run length (s)">
        <ResponsiveContainer>
          <LineChart data={stats.runs}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
            <XAxis dataKey="id" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip {...TOOLTIP_STYLE} />
            <Line
              type="monotone" dataKey="duration" dot={false}
              stroke={DURATION} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"
            />
          </LineChart>
        </ResponsiveContainer>
      </Panel>

      <Panel
        title="Taps by action"
        note="Older runs logged the template filename (e.g. upgrade_damage.png); newer ones log the action name (e.g. Damage) - both can appear here."
      >
        {stats.taps.length ? (
          <ResponsiveContainer>
            <BarChart data={stats.taps}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
              <XAxis dataKey="action" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip {...TOOLTIP_STYLE} />
              <Bar dataKey="count" fill={TAPS} radius={[4, 4, 0, 0]} maxBarSize={24} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <EmptyPanel>No taps recorded yet.</EmptyPanel>
        )}
      </Panel>

      <Panel title="Events by screen">
        {stats.screens.length ? (
          <ResponsiveContainer>
            <BarChart data={stats.screens}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" />
              <XAxis dataKey="screen" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip {...TOOLTIP_STYLE} />
              <Bar dataKey="count" fill={SCREENS} radius={[4, 4, 0, 0]} maxBarSize={24} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <EmptyPanel>No screen events recorded yet.</EmptyPanel>
        )}
      </Panel></>}
      </div>
    </div>
  );
}
