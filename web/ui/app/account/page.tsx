"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { AccountMetricsCards } from "@/components/AccountMetricsCards";
import { SectionCard } from "@/components/ui/section-card";
import { useAccountSelection } from "@/lib/AccountSelection";
import { claimMilestones, claimMissions, collectStats, fetchAccount, fetchConcepts } from "@/lib/api";
import { ACCOUNT_SECTIONS, describeCollection, evidenceAge, readerSupported } from "@/lib/account";
import type { AccountConcept, AccountFact, AccountSection, AccountSnapshot, ClaimSnapshot, ConceptCatalog, StatsCollection } from "@/lib/account";
import { ScreenReadings } from "./ScreenReadings";

const date = (seconds: number) => new Date(seconds * 1000).toLocaleString();

function FactEvidence({ fact, concept, now }: { fact: AccountFact; concept?: AccountConcept; now: number }) {
  const evidence = fact.evidence;
  return <details className="rounded-md border p-3 text-sm">
    <summary className="cursor-pointer space-y-1">
      <span className="font-medium">{concept?.name ?? fact.concept_id}</span>
      <span className="float-right ml-2 font-mono text-primary">{fact.value === null ? "Unknown" : String(fact.value)}{concept?.unit ? ` ${concept.unit}` : ""}</span>
      <span className="block text-xs text-muted-foreground">{fact.status} · {evidenceAge(fact, now)}</span>
    </summary>
    <dl className="mt-3 grid gap-2 break-words text-xs text-muted-foreground">
      <div><dt className="text-foreground">Concept</dt><dd>{fact.concept_id}</dd></div>
      <div><dt className="text-foreground">Saved observation</dt><dd>{date(evidence.observed_at)} · {Math.round(evidence.confidence * 100)}% confidence</dd></div>
      <div><dt className="text-foreground">Raw OCR</dt><dd>{evidence.raw_name} → {evidence.raw_value ?? "No raw value"}</dd></div>
      <div><dt className="text-foreground">Source frame digest</dt><dd className="break-all font-mono">{evidence.frame_digest}</dd></div>
      <div><dt className="text-foreground">Source region</dt><dd>{evidence.rect.join(", ")} (x, y, width, height) · frame {evidence.frame_width} × {evidence.frame_height}</dd></div>
      <div><dt className="text-foreground">Frame image</dt><dd>{evidence.frame_ref ? `Reference: ${evidence.frame_ref}` : "Image not retained; digest and OCR evidence only."}</dd></div>
    </dl>
  </details>;
}

/** Arms the read-only Settings -> Stats walk. Deliberately not a Refresh:
 *  this moves the emulator, so it says so, reports where a stopped run
 *  stopped, and never claims a value was read. */
function CollectStats({ collection, onArmed }: { collection?: StatsCollection; onArmed: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = collection?.status === "running";
  return <SectionCard title="Collect stats" tone={collection?.result?.status === "failed" ? "warn" : undefined}>
    <p className="text-sm text-muted-foreground">A read-only transaction: the bot opens Settings, reads the Stats panel and returns to the main menu. It buys nothing and changes nothing in the game, and it holds all other automation while it walks.</p>
    {!collection
      ? <p className="mt-3 text-sm">This backend cannot walk the game. Open Settings → Stats yourself, then refresh.</p>
      : <>
        <p className="mt-3 text-sm">{describeCollection(collection)}</p>
        <button disabled={busy || running}
          onClick={() => { setBusy(true); setError(null); collectStats().then(onArmed).catch((e: Error) => setError(e.message)).finally(() => setBusy(false)); }}
          className="mt-3 rounded-md border px-3 py-2 text-sm transition-colors hover:border-border-strong hover:bg-muted active:translate-y-px disabled:pointer-events-none disabled:opacity-50">
          {running ? "Collecting…" : busy ? "Starting…" : "Collect stats"}
        </button>
      </>}
    {error && <p role="alert" className="mt-3 text-sm text-danger">Could not start a collection: {error}. Nothing was tapped.</p>}
  </SectionCard>;
}

/** Arms one Home -> Missions -> claim -> Home walk, or the Milestones
 *  equivalent, depending on which `action` it is given. Shares
 *  CollectStats's busy/error handling above: neither button here knows a
 *  running/idle state ahead of a click, so all it tracks is whether this
 *  request is in flight and, if it failed, why - the same ApiError message
 *  `send()` already built via describeDetail. */
function ClaimButton({ label, busyLabel, action }: { label: string; busyLabel: string; action: () => Promise<ClaimSnapshot> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return <div>
    <button disabled={busy}
      onClick={() => { setBusy(true); setError(null); action().catch((e: Error) => setError(e.message)).finally(() => setBusy(false)); }}
      className="rounded-md border px-3 py-2 text-sm transition-colors hover:border-border-strong hover:bg-muted active:translate-y-px disabled:pointer-events-none disabled:opacity-50">
      {busy ? busyLabel : label}
    </button>
    {error && <p role="alert" className="mt-3 text-sm text-danger">Could not start a claim: {error}. Nothing was tapped.</p>}
  </div>;
}

export default function AccountPage() {
  const { selected } = useAccountSelection();
  const [account, setAccount] = useState<AccountSnapshot | null>(null);
  const [catalog, setCatalog] = useState<ConceptCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState("all");
  const [now, setNow] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    // Independent requests: a catalog outage must not hide saved evidence.
    fetchAccount().then(value => { if (active) { setAccount(value); setError(null); } })
      .catch((e: Error) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    fetchConcepts().then(value => { if (active) { setCatalog(value); setCatalogError(false); } })
      .catch(() => { if (active) setCatalogError(true); });
    setNow(Date.now() / 1000);
    const timer = setInterval(() => setNow(Date.now() / 1000), 60_000);
    return () => { active = false; clearInterval(timer); };
  }, [reload]);
  const unavailable = !account?.revision && (!!error || !!account?.errors?.account);
  const awaitingAccount = !account && !error;
  const state = account?.revision ?? account?.unknown_state;
  const sections = Object.entries(ACCOUNT_SECTIONS) as [AccountSection, string][];
  const facts = sections.flatMap(([key]) => state?.[key] ?? []);
  const concepts = catalog?.concepts ?? [];
  const names = new Map(concepts.map(c => [c.concept_id, c]));
  const known = new Set(facts.filter(f => f.value !== null).map(f => f.concept_id));
  const filtered = concepts.filter(c => (domain === "all" || c.domain === domain) && `${c.name} ${c.concept_id}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="mx-auto flex max-w-6xl flex-col gap-4">
    <PageHeader title="Account inspector" meta={account?.revision ? `revision ${account.revision.revision_id}` : unavailable ? "account state unavailable" : awaitingAccount ? "loading account" : "no saved revision"}
      action={<button disabled={loading} onClick={() => setReload(n => n + 1)} className="rounded-md border px-3 py-2 text-sm transition-colors hover:border-border-strong hover:bg-muted active:translate-y-px disabled:pointer-events-none disabled:opacity-50">{loading ? "Loading…" : "Refresh"}</button>} />
    <p className="max-w-3xl text-sm text-muted-foreground">Saved account evidence, recent game screen observations, and the inputs still missing. Workshop values and account totals are not individual upgrade levels.</p>
    <AccountMetricsCards accountKey={selected?.key} />
    {error && <p role="alert" className="rounded-md border border-danger p-3 text-danger">Could not load account: {error}. {account ? "Previously loaded evidence remains below; refresh failed." : "Account state is unavailable, not empty."}</p>}
    {account?.error && <p role="alert" className="rounded-md border border-danger p-3 text-danger">Account service warning: {account.error}. Saved evidence may be incomplete.</p>}
    {account && <>
      <div className="grid gap-4 sm:grid-cols-3">
        <SectionCard title="Saved observations"><p className="font-mono text-3xl">{unavailable ? "Unavailable" : facts.length}</p><p className="text-xs text-muted-foreground">{unavailable ? "Saved state could not be restored; observation count is unknown." : account.revision ? "Expand a value to inspect its evidence." : "Nothing verified yet; this is not a zero-value account."}</p></SectionCard>
        <SectionCard title="Account identity"><p>{state?.account_id ?? "Unknown account"}</p><p className="text-xs text-muted-foreground">Game version: {state?.game_version ?? "unknown"}</p><p className="break-all text-xs text-muted-foreground">Registry: {state?.registry_version ?? "unknown"}</p></SectionCard>
        <SectionCard title="Reader capability"><p>Saved Workshop values</p><p className="text-xs text-muted-foreground">{account.persistence_available ? "Persistence available" : "Persistence unavailable"}. {account.screen_readings ? "Settings and Stats screen observations are available separately for this session." : "Other permanent readers have no supported ingestion in this account API."}</p></SectionCard>
      </div>
      {selected?.running && <><CollectStats collection={account.collection} onArmed={() => setReload(n => n + 1)} />
      <SectionCard title="Claim rewards">
        <p className="text-sm text-muted-foreground">Two separate device walks: Home → Missions → claim → Home, and Home → Milestones → Claim All → Home. Each holds all other automation while it walks.</p>
        <div className="mt-3 flex flex-wrap gap-3">
          <ClaimButton label="Claim missions" busyLabel="Claiming…" action={claimMissions} />
          <ClaimButton label="Claim milestones" busyLabel="Claiming…" action={claimMilestones} />
        </div>
      </SectionCard></>}
      {selected?.running && <ScreenReadings data={account.screen_readings} />}
      <SectionCard title="Missing optimizer inputs" tone="warn">
        <p className="text-sm text-muted-foreground">Unknown does not mean locked, unavailable in the game, or zero. Catalog membership does not prove ownership or execution support.</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{sections.map(([key, label]) => <div key={key} className="rounded-md bg-muted/40 p-3 text-sm"><p className="font-medium">{label}</p><p className="text-xs text-muted-foreground">{state?.[key]?.length ? `${state[key]!.length} saved observations; coverage may be partial` : key === "workshop_stats" ? unavailable ? "Unknown · account state unavailable" : "Not yet scanned / no verified values" : "Unknown · reader unavailable in this API"}</p></div>)}</div>
      </SectionCard>
      <SectionCard title="Observed account values">
        <p className="mb-3 text-xs text-muted-foreground">Freshness uses saved evidence age: stale after 24 hours (display threshold only). Unchanged observations may not create a revision; this is not the last live scan time.</p>
        {facts.length === 0 ? <p className="text-sm">{unavailable ? "Saved observations are unavailable. Resolve the account storage error before assessing coverage." : "No saved observations. A supported workshop scan must verify values before they appear here."}</p> : sections.map(([key, label]) => state?.[key]?.length ? <div className="mb-4" key={key}><h3 className="mb-2 text-sm font-medium">{label}</h3><div className="grid gap-2 md:grid-cols-2">{state[key]!.map(f => <FactEvidence key={f.concept_id} fact={f} concept={names.get(f.concept_id)} now={now} />)}</div></div> : null)}
      </SectionCard>
    </>}
    <SectionCard title="Inventory coverage & unlock dependencies">
      {catalogError && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh concept metadata. {catalog ? "Previously loaded catalog shown." : "Inventory coverage and dependency metadata unavailable."}</p>}
      {catalog && <>
        {state && state.registry_version !== catalog.registry_version && <p role="alert" className="mb-3 text-warn">Registry versions differ; catalog descriptions may not match saved observations.</p>}
        <div className="flex flex-col gap-2 sm:flex-row">
          <label className="flex-1 text-xs text-muted-foreground">Search inventory<input value={query} onChange={e => setQuery(e.target.value)} placeholder="Name or concept ID" className="mt-1 block w-full rounded-md border bg-background p-2 text-sm text-foreground" /></label>
          <label className="text-xs text-muted-foreground">Domain<select value={domain} onChange={e => setDomain(e.target.value)} className="mt-1 block w-full rounded-md border bg-background p-2 text-sm text-foreground"><option value="all">All domains</option>{[...new Set(concepts.map(c => c.domain))].sort().map(d => <option key={d}>{d}</option>)}</select></label>
        </div>
        <p className="my-3 text-xs text-muted-foreground">{filtered.length} concepts · Dependencies describe catalog rules, not confirmed account unlocks.</p>
        <div className="grid gap-2 md:grid-cols-2">{filtered.map(c => <details key={c.concept_id} className="min-w-0 rounded-md border p-3 text-sm"><summary className="cursor-pointer"><span className="font-medium">{c.name}</span><span className="mt-1 block text-xs text-muted-foreground">{c.domain} · {known.has(c.concept_id) ? "Saved value available" : readerSupported(c) ? unavailable ? "Unknown · account state unavailable" : awaitingAccount ? "Awaiting account state" : "Not yet scanned / no verified value" : "Unknown · reader unavailable"}</span></summary>
          <div className="mt-3 space-y-2 break-words text-xs text-muted-foreground"><p className="break-all">{c.concept_id}</p><p>Account unlock status: unknown. A value observation does not establish ownership or unlock state.</p><p>Prerequisites: {c.prerequisites === null ? "Unknown in catalog" : c.prerequisites.length ? c.prerequisites.map(id => names.get(id)?.name ?? id).join(", ") : "None listed in catalog"}.</p><p>Unlocks: {c.unlocks.length ? c.unlocks.map(id => names.get(id)?.name ?? id).join(", ") : "No relationships recorded"}.</p><p>Rule verification: {c.rule_verified ? "verified in catalog" : "unverified"}. Execution and purchase eligibility are not certified by this view.</p></div>
        </details>)}</div>
        {filtered.length === 0 && <p className="text-sm">No matching concepts.</p>}
      </>}
    </SectionCard>
  </div>;
}
