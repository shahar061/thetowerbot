"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { fetchBuildRoute, fetchFleetLabs, fetchFleetStrategies, fetchUpgrades } from "@/lib/api";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabsSnapshot } from "@/lib/labs";
import type { Upgrade } from "@/lib/types";
import { VariantComparison } from "../VariantComparison";
import { useRerollWorkspace } from "../RerollWorkspace";
import { FleetRoutePreview } from "./FleetRoutePreview";
import { StrategyStudio } from "./StrategyStudio";
import type { StrategyLibrary } from "@/lib/strategyStudio";

export default function StrategiesPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [route, setRoute] = useState<BuildRouteDocument | null>(null);
  const [catalog, setCatalog] = useState<Upgrade[] | null>(null);
  const [library, setLibrary] = useState<StrategyLibrary | null>(null);
  const [routeError, setRouteError] = useState<string | null>(null);
  const [tab, setTab] = useState<"builder" | "roads">("builder");
  const [labs, setLabs] = useState<LabsSnapshot | null>(null);
  const members = pool?.members ?? [];
  useEffect(() => {
    let active = true;
    void Promise.all([fetchBuildRoute(), fetchUpgrades(), fetchFleetStrategies()]).then(([nextRoute, upgrades, nextLibrary]) => {
      if (!active) return;
      setRoute(nextRoute);
      setCatalog(upgrades);
      setSavedRevision(nextRoute.revision);
      setLibrary(nextLibrary);
    }).catch(error => { if (active) setRouteError(error instanceof Error ? error.message : "Build Route unavailable"); });
    return () => { active = false; };
  }, []);
  // The labs snapshot only enriches the Studio (automation badges, wallet-split preview); its
  // absence must never block the page the way a Build Route failure does.
  useEffect(() => {
    let active = true;
    fetchFleetLabs().then(next => { if (active) setLabs(next); }, () => { if (active) setLabs(null); });
    return () => { active = false; };
  }, []);

  return <div className="flex flex-col gap-5">
    <PageHeader title="Strategy Studio" meta={`${members.filter(member => !member.hidden).length} emulators · effective reroll plans`} />
    <p className="text-sm text-muted-foreground">See what each account can do next, what is projected, and why a decision was made.</p>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {routeError && <p role="alert" className="text-danger">Build Route unavailable: {routeError}</p>}
    {loading && !pool ? <p role="status">Loading fleet strategies…</p> : !members.some(member => !member.hidden) ? <p className="text-muted-foreground">No visible emulators in this reroll.</p> : null}
    <div role="tablist" aria-label="Strategy Studio views" className="grid grid-cols-2 gap-2 rounded-2xl border border-border bg-card p-2">
      <button role="tab" aria-selected={tab === "builder"} type="button" onClick={() => setTab("builder")}
        className={`rounded-xl px-4 py-3 text-left text-sm font-semibold transition-colors ${tab === "builder" ? "bg-primary text-primary-foreground" : "hover:bg-background"}`}>
        Build route <span className="block text-xs font-normal opacity-75">Arrange the rules</span>
      </button>
      <button role="tab" aria-selected={tab === "roads"} type="button" onClick={() => setTab("roads")}
        className={`rounded-xl px-4 py-3 text-left text-sm font-semibold transition-colors ${tab === "roads" ? "bg-primary text-primary-foreground" : "hover:bg-background"}`}>
        Fleet roads <span className="block text-xs font-normal opacity-75">See each account&apos;s next step</span>
      </button>
    </div>
    {tab === "roads" && !!members.length && <FleetRoutePreview members={members} savedRevision={savedRevision} assignments={route?.assignments ?? undefined} showHistory />}
    <div hidden={tab !== "builder"}>{route && catalog && library && <StrategyStudio library={library} saved={route} catalog={catalog} members={members}
      labsSnapshot={labs} onPublished={next => { setRoute(next); setSavedRevision(next.revision); }} />}</div>
    {!!pool?.variant_comparison?.length && <details className="rounded-xl border border-border bg-card p-4">
      <summary className="cursor-pointer font-medium">Compare opening variants</summary>
      <p className="my-3 text-sm text-muted-foreground">Timing includes accounts that reached Tier 1 Wave 20. Reached counts show incomplete attempts; these samples do not establish a winning strategy.</p>
      <VariantComparison rows={pool.variant_comparison} />
    </details>}
  </div>;
}
