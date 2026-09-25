"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { fetchBuildRoute, fetchUpgrades } from "@/lib/api";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { Upgrade } from "@/lib/types";
import { VariantComparison } from "../VariantComparison";
import { useRerollWorkspace } from "../RerollWorkspace";
import { FleetRoutePreview } from "./FleetRoutePreview";
import { FlowBuilder } from "./FlowBuilder";

export default function StrategiesPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [route, setRoute] = useState<BuildRouteDocument | null>(null);
  const [catalog, setCatalog] = useState<Upgrade[] | null>(null);
  const [routeError, setRouteError] = useState<string | null>(null);
  const members = pool?.members ?? [];
  useEffect(() => {
    let active = true;
    void Promise.all([fetchBuildRoute(), fetchUpgrades()]).then(([nextRoute, upgrades]) => {
      if (!active) return;
      setRoute(nextRoute);
      setCatalog(upgrades);
      setSavedRevision(nextRoute.revision);
    }).catch(error => { if (active) setRouteError(error instanceof Error ? error.message : "Build Route unavailable"); });
    return () => { active = false; };
  }, []);

  return <div className="flex flex-col gap-5">
    <PageHeader title="Strategy Studio" meta={`${members.filter(member => !member.hidden).length} emulators · effective reroll plans`} />
    <p className="text-sm text-muted-foreground">See what each account can do next, what is projected, and why a decision was made.</p>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {routeError && <p role="alert" className="text-danger">Build Route unavailable: {routeError}</p>}
    {loading && !pool ? <p role="status">Loading fleet strategies…</p> : !members.some(member => !member.hidden) ? <p className="text-muted-foreground">No visible emulators in this reroll.</p> : null}
    {!!members.length && <FleetRoutePreview members={members} savedRevision={savedRevision} showHistory />}
    {route && catalog && <FlowBuilder key={route.revision} saved={route} catalog={catalog} members={members}
      onPublished={next => { setRoute(next); setSavedRevision(next.revision); }} />}
    {!!pool?.variant_comparison?.length && <details className="rounded-xl border border-border bg-card p-4">
      <summary className="cursor-pointer font-medium">Compare opening variants</summary>
      <p className="my-3 text-sm text-muted-foreground">Timing includes accounts that reached Tier 1 Wave 20. Reached counts show incomplete attempts; these samples do not establish a winning strategy.</p>
      <VariantComparison rows={pool.variant_comparison} />
    </details>}
  </div>;
}
