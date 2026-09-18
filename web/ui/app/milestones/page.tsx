"use client";

import { useEffect, useMemo, useState } from "react";
import { BookOpen, Check, ChevronRight, CircleHelp, Flag, LockKeyhole, RotateCw, Trophy } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { AccountMetricsCards } from "@/components/AccountMetricsCards";
import { useAccountSelection } from "@/lib/AccountSelection";
import { fetchMilestoneRoadmap } from "@/lib/api";
import type { MilestoneNode, MilestoneRoadmap, MilestoneStatus } from "@/lib/milestoneRoadmap";
import { cn } from "@/lib/utils";

const STATUS: Record<MilestoneStatus, { label: string; tone: string }> = {
  verified: { label: "Verified", tone: "border-live bg-live-surface text-live" },
  claimed: { label: "Claim recorded", tone: "border-primary bg-primary/12 text-primary" },
  claimable: { label: "Wave reached", tone: "border-warn bg-warn-surface text-warn" },
  in_progress: { label: "In progress", tone: "border-primary/50 bg-primary/10 text-primary" },
  available: { label: "Available", tone: "border-primary/50 bg-primary/10 text-primary" },
  locked: { label: "Later", tone: "border-border bg-muted text-muted-foreground" },
  unknown: { label: "Not observed", tone: "border-border bg-card text-muted-foreground" },
};

function Icon({ node }: { node: MilestoneNode }) {
  if (node.status === "verified") return <Check className="size-6" aria-hidden="true" />;
  if (node.status === "locked") return <LockKeyhole className="size-5" aria-hidden="true" />;
  if (node.group === "Tiers") return <Flag className="size-6" aria-hidden="true" />;
  if (node.group === "Competition") return <Trophy className="size-6" aria-hidden="true" />;
  if (node.kind === "activity") return <ChevronRight className="size-6" aria-hidden="true" />;
  return <span className="font-mono text-lg font-black" aria-hidden="true">{node.wave ?? "★"}</span>;
}

function NodeDetail({ node, all }: { node: MilestoneNode; all: MilestoneNode[] }) {
  const prerequisites = node.requires.map(id => all.find(item => item.id === id)?.title ?? id);
  return <aside className="rounded-2xl border bg-card p-5 shadow-sm lg:sticky lg:top-20 lg:self-start" aria-live="polite">
    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
      <span>{node.group}</span><span aria-hidden="true">·</span>
      <span>{node.tier && node.wave ? `Tier ${node.tier} · wave ${node.wave}` : "Account milestone"}</span>
    </div>
    <h2 className="mt-3 text-xl font-bold">{node.title}</h2>
    <span className={cn("mt-3 inline-flex rounded-full border px-3 py-1 text-xs font-semibold", STATUS[node.status].tone)}>
      {STATUS[node.status].label}
    </span>
    <p className="mt-4 text-sm leading-relaxed text-muted-foreground">{node.description}</p>
    {node.progress && node.status === "in_progress" && <div className="mt-4">
      <div className="mb-1 flex justify-between text-xs"><span>Best recorded wave</span><strong>{node.progress.current} / {node.progress.target}</strong></div>
      <div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary" style={{ width: `${Math.min(100, node.progress.current / node.progress.target * 100)}%` }} /></div>
    </div>}
    {prerequisites.length > 0 && <p className="mt-4 text-xs text-muted-foreground">After {prerequisites.join(", ")}</p>}
    <p className="mt-4 border-t pt-4 text-xs text-muted-foreground">
      {node.status === "claimable" ? "The recorded wave was reached. Claim and unlock still need confirmation." :
        node.status === "claimed" ? "A matching milestone claim was recorded. The feature itself has not been verified yet." :
        node.status === "verified" ? "This account has a recorded observation of this feature." :
        node.status === "unknown" ? "No reliable account evidence has been recorded yet." :
        node.status === "locked" ? "Earlier tier progress has not been recorded for this account." :
        node.status === "available" ? "This feature is available without a recorded wave requirement." :
        "Progress comes from completed runs recorded for this account."}
    </p>
    <a href={node.source_url} target="_blank" rel="noopener noreferrer" className="mt-4 inline-flex items-center gap-2 text-xs font-medium text-primary underline">
      <BookOpen className="size-3.5" aria-hidden="true" /> Read game guide
    </a>
  </aside>;
}

export default function MilestonesPage() {
  const { selected, loading: accountsLoading } = useAccountSelection();
  const selectedKey = selected?.key;
  const [data, setData] = useState<MilestoneRoadmap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [view, setView] = useState<"all" | "up_next" | "completed">("all");

  useEffect(() => {
    if (!selectedKey) { setData(null); setError(null); return; }
    let alive = true;
    setData(null);
    const load = () => fetchMilestoneRoadmap().then(value => {
      if (alive) { setData(value); setError(null); }
    }).catch((failure: Error) => { if (alive) setError(failure.message); });
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [selectedKey]);

  const next = data?.nodes.find(node => node.status === "in_progress" || node.status === "claimable") ?? null;
  const active = data?.nodes.find(node => node.id === activeId) ?? next ?? data?.nodes[0] ?? null;
  const visible = useMemo(() => (data?.nodes ?? []).filter(node =>
    view === "all" || (view === "completed" ? ["claimed", "verified"].includes(node.status)
      : ["in_progress", "claimable", "available", "unknown"].includes(node.status) && (node.tier ?? 1) <= 2)), [data, view]);
  const chapters = [...new Set(visible.map(node => node.tier ?? 1))];
  const completed = data?.nodes.filter(node => node.status === "claimed" || node.status === "verified").length ?? 0;

  if (accountsLoading) return <p className="text-sm text-muted-foreground">Loading account selection…</p>;
  if (!selected) return <div className="mx-auto max-w-xl rounded-2xl border bg-card p-8 text-center">
    <CircleHelp className="mx-auto size-9 text-muted-foreground" aria-hidden="true" />
    <h1 className="mt-3 text-xl font-bold">Choose a game account</h1>
    <p className="mt-2 text-sm text-muted-foreground">Select a running or archived account in the top bar to see its milestone path.</p>
  </div>;

  return <div className="mx-auto flex max-w-6xl flex-col gap-5">
    <PageHeader title="Milestone path" meta={selected.account_id ?? "Unattributed history"} />
    <p className="max-w-3xl text-sm text-muted-foreground">Follow the major game unlocks from your first cards and Labs through new tiers, Modules, research, and more. Progress uses this account&apos;s saved runs and verified observations.</p>
    <AccountMetricsCards accountKey={selected.key} />
    {error && <div role="alert" className="rounded-xl border border-warn bg-warn-surface p-4 text-sm text-warn">Could not load milestone progress: {error}</div>}
    {!data && !error && <p className="text-sm text-muted-foreground">Loading milestone path…</p>}
    {data && <>
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-xl border bg-card p-4"><p className="text-xs text-muted-foreground">Recorded milestones</p><p className="mt-1 text-2xl font-bold">{completed}<span className="text-sm font-normal text-muted-foreground"> / {data.nodes.length}</span></p></div>
        <div className="rounded-xl border bg-card p-4"><p className="text-xs text-muted-foreground">Next on the path</p><p className="mt-1 text-lg font-semibold">{next?.title ?? "Explore the path"}</p></div>
        <div className="rounded-xl border bg-card p-4"><p className="text-xs text-muted-foreground">Tier 1 best wave</p><p className="mt-1 text-2xl font-bold">{data.best_waves["1"] ?? "—"}</p></div>
      </div>
      <div className="flex flex-wrap gap-2" aria-label="Filter milestones">
        {(["all", "up_next", "completed"] as const).map(key => <button key={key} type="button" onClick={() => setView(key)}
          aria-pressed={view === key} className={cn("rounded-full border px-3 py-1.5 text-xs font-semibold", view === key ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent")}>{key === "all" ? "Full path" : key === "up_next" ? "Early path" : "Recorded"}</button>)}
      </div>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 rounded-2xl border bg-card/50 p-4 sm:p-7">
          {visible.length === 0 && <p className="py-12 text-center text-sm text-muted-foreground">No milestones in this view yet.</p>}
          {chapters.map(tier => <section key={tier} aria-label={`Tier ${tier} milestones`}>
            <div className="relative z-[1] my-6 rounded-full border bg-background px-4 py-2 text-center text-xs font-bold uppercase tracking-widest text-muted-foreground">Tier {tier} path</div>
            <div className="relative before:absolute before:bottom-0 before:left-1/2 before:top-0 before:w-1 before:-translate-x-1/2 before:rounded-full before:bg-border">
              {visible.filter(node => (node.tier ?? 1) === tier).map((node, index) => <div key={node.id} className="relative flex min-h-28 items-center justify-center py-3">
                <button type="button" onClick={() => setActiveId(node.id)} aria-label={`${node.title}, ${STATUS[node.status].label}`}
                  aria-current={active?.id === node.id ? "step" : undefined}
                  className={cn("relative z-[1] flex size-16 shrink-0 items-center justify-center rounded-full border-4 shadow-md transition-transform hover:scale-105 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary",
                    STATUS[node.status].tone, active?.id === node.id && "ring-4 ring-primary/25",
                    index % 3 === 0 ? "-translate-x-9 sm:-translate-x-16" : index % 3 === 1 ? "translate-x-9 sm:translate-x-16" : "")}
                ><Icon node={node} /></button>
                <button type="button" onClick={() => setActiveId(node.id)} className={cn("absolute top-1/2 w-[calc(50%-3.75rem)] -translate-y-1/2 rounded-xl border bg-background p-2 text-left text-xs shadow-sm sm:w-[calc(50%-6rem)]",
                  index % 3 === 0 ? "left-[calc(50%+1rem)] sm:left-[calc(50%+1.5rem)]" : "left-0",
                  active?.id === node.id && "border-primary")}
                ><strong className="block leading-tight">{node.title}</strong><span className="mt-1 block text-[10px] text-muted-foreground">{STATUS[node.status].label}</span></button>
              </div>)}
            </div>
          </section>)}
        </div>
        {active && <NodeDetail node={active} all={data.nodes} />}
      </div>
      <p className="flex items-center gap-2 text-xs text-muted-foreground"><RotateCw className="size-3.5" aria-hidden="true" /> Updated from saved account evidence every 30 seconds. The game guide supplies milestone order; the game and account observations determine actual unlocks.</p>
    </>}
  </div>;
}
