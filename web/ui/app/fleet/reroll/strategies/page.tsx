"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PageHeader } from "@/components/PageHeader";
import { fetchBuildRoute, fetchFleetStrategies, fetchUpgrades } from "@/lib/api";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { Upgrade } from "@/lib/types";
import { VariantComparison } from "../VariantComparison";
import { useRerollWorkspace } from "../RerollWorkspace";
import { useFleetLabs } from "../FleetLabsContext";
import { overviewEvidence } from "../fleetOverview";
import { FleetRoutePreview } from "./FleetRoutePreview";
import { StrategyStudio } from "./StrategyStudio";
import type { StrategyDefinition, StrategyLibrary } from "@/lib/strategyStudio";

export default function StrategiesPage(): React.JSX.Element {
  return <Suspense fallback={<p role="status">Loading Strategy Studio…</p>}><StrategiesContent /></Suspense>;
}

function StrategiesContent(): React.JSX.Element {
  const query = useSearchParams();
  const { pool, loading, error } = useRerollWorkspace();
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [route, setRoute] = useState<BuildRouteDocument | null>(null);
  const [catalog, setCatalog] = useState<Upgrade[] | null>(null);
  const [library, setLibrary] = useState<StrategyLibrary | null>(null);
  const [routeError, setRouteError] = useState<string | null>(null);
  const [tab, setTab] = useState<"builder" | "roads">("builder");
  const { snapshot: labs, error: labsError, refresh: refreshLabs } = useFleetLabs();
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
  // Observation ownership and applied-plan previews remain separate: changing
  // a strategy revision does not erase a safely matched current/last lab job.
  const evidenceRows = labs && pool && !labsError ? members.filter(member => !member.hidden).map(member => overviewEvidence(member, labs)) : [];
  const labObservations = evidenceRows.flatMap(evidence => evidence.labs ? [evidence.labs] : []);
  const scopedLabs = labs && pool && !labsError ? { ...labs, workers: evidenceRows.flatMap(evidence => {
    const row = evidence.labs;
    return row ? [{ ...row, plan: evidence.appliedLabPlan,
      wallet: { ...row.wallet, coins: evidence.appliedLabPlan ? row.wallet.coins : null } }] : [];
  }) } : null;
  const requestedWorker = query?.get("worker") ?? null;
  const requestedAccount = query?.get("account") ?? null;
  const requestedSlot = Number(query?.get("slot"));
  const linkedMember = requestedWorker && requestedAccount ? members.find(member =>
    !member.hidden && member.name === requestedWorker && member.account_id === requestedAccount) : null;
  const assignment = linkedMember ? route?.assignments?.[linkedMember.name] : null;
  const assigned = assignment?.account_id === requestedAccount ? assignment : null;
  const exactInLibrary = assigned && library ? [...library.templates, ...library.strategies].find(item =>
    item.id === assigned.strategy_id && item.version === assigned.strategy_version) : null;
  const assignedSnapshot: StrategyDefinition | undefined = assigned && !exactInLibrary && assigned.baseline ? {
    id: `assigned-snapshot:${assigned.strategy_id}:${assigned.strategy_version}`,
    name: assigned.strategy_name, version: assigned.strategy_version,
    source_template: library?.strategies.find(item => item.id === assigned.strategy_id)?.source_template ?? "scratch",
    builtin: true, baseline: assigned.baseline,
  } : undefined;
  const invalidLink = !!requestedWorker && !!requestedAccount && !!pool && !!route && (!assigned || (!exactInLibrary && !assigned.baseline));
  const linkedSlot = assigned && !invalidLink && Number.isInteger(requestedSlot) && requestedSlot >= 1 && requestedSlot <= 5 ? requestedSlot : undefined;
  const linkIdentity = requestedWorker && requestedAccount ? JSON.stringify([
    requestedWorker, requestedAccount, requestedSlot, assigned?.strategy_id ?? null,
    assigned?.strategy_version ?? null,
  ]) : undefined;

  return <div className="flex flex-col gap-5">
    <PageHeader title="Strategy Studio" meta={`${members.filter(member => !member.hidden).length} emulators · effective reroll plans`} />
    <p className="text-sm text-muted-foreground">See what each account can do next, what is projected, and why. Open the Labs lane to plan all five slots; save and assignment stay separate.</p>
    {error && <p role="alert" className="text-danger">Fleet unavailable: {error}</p>}
    {routeError && <p role="alert" className="text-danger">Build Route unavailable: {routeError}</p>}
    {invalidLink && <p role="status" className="text-sm text-danger">Planning link no longer matches this worker, account, or assignment. Choose a current strategy before editing.</p>}
    {labsError && <p role="alert" aria-label="Labs data status" className="text-danger">Labs refresh failed: {labsError}. Last known lab observations may be stale; wallet split preview unavailable until a successful refresh.</p>}
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
    <div hidden={tab !== "builder"}>{route && catalog && library && <StrategyStudio library={library} saved={route} catalog={catalog} members={members} onLibrarySaved={setLibrary}
      initialLane={requestedWorker && requestedAccount ? "labs" : undefined}
      initialStrategyId={exactInLibrary?.id} assignedSnapshot={assignedSnapshot}
      initialWorker={requestedWorker ?? undefined} initialAccount={requestedAccount ?? undefined}
      initialSlot={linkedSlot} linkIdentity={linkIdentity}
      labsSnapshot={scopedLabs} labObservations={labObservations} onPublished={next => { setRoute(next); setSavedRevision(next.revision); void refreshLabs(); }} />}</div>
    {!!pool?.variant_comparison?.length && <details className="rounded-xl border border-border bg-card p-4">
      <summary className="cursor-pointer font-medium">Compare opening variants</summary>
      <p className="my-3 text-sm text-muted-foreground">Timing includes accounts that reached Tier 1 Wave 20. Reached counts show incomplete attempts; these samples do not establish a winning strategy.</p>
      <VariantComparison rows={pool.variant_comparison} />
    </details>}
  </div>;
}
