"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { TournamentEditor } from "@/components/TournamentEditor";
import { Dialog } from "@base-ui/react/dialog";
import { BookOpen, Copy, FlaskConical, Gem, GitBranch, Hammer, LockKeyhole, Maximize2, Minimize2, Monitor, Plus, Save, Shield, Sparkles, Swords, X } from "lucide-react";
import { assignFleetStrategy, fetchStrategyLedger, previewBuildRoute, saveFleetStrategy } from "@/lib/api";
import type { BuildRouteDocument, BuildRoutePreview } from "@/lib/buildRoute";
import { isLabList, rulesOf, withRules, type LabsRow, type LabsSnapshot } from "@/lib/labs";
import type { SpendingLane, StrategyBlock, StrategyDefinition, StrategyLedgerEntry, StrategyLibrary, StrategyWorker } from "@/lib/strategyStudio";
import type { StrategyStudioClient } from "@/lib/strategyClient";
import type { Upgrade, Strategy } from "@/lib/types";
import { ROOT_END, presetsForLane, findBlock, insertBlock, locateBlock, makeBlock, updateBlock, type BlockPreset, type BlockTarget } from "@/app/fleet/reroll/strategies/strategyBlocks";
import { StrategyCanvas, type BlockDrag } from "@/app/fleet/reroll/strategies/StrategyCanvas";
import { StrategyBlockInspector } from "@/app/fleet/reroll/strategies/StrategyBlockInspector";
import { ResourceBlocks } from "@/app/fleet/reroll/strategies/ResourceBlocks";
import { LabListView } from "@/app/fleet/reroll/strategies/LabListView";
import { LabSlotPlanner } from "@/app/fleet/reroll/strategies/LabSlotPlanner";
import { StrategyRules } from "@/app/fleet/reroll/strategies/StrategyRules";
import { RouteInspector } from "@/app/fleet/reroll/strategies/RouteInspector";
import { StrategyHistory } from "@/app/fleet/reroll/strategies/StrategyHistory";
import { CardPlanEditor } from "@/components/cards/CardPlanEditor";
import { CardLoadoutEditor } from "@/components/cards/CardLoadoutEditor";
import { emptyCardProgram, fetchCardCatalog, cardDraftError, type CardCatalog } from "@/lib/cards";
import styles from "@/app/fleet/reroll/strategies/studio.module.css";

type Draft = StrategyDefinition & { dirty?: boolean };
type AssignmentReview = { linkIdentity: string; strategyId: string; strategyVersion: number; memberScope: string };
const LANES = [{ id: "workshop", label: "Workshop", icon: Hammer }, { id: "battle", label: "In-game", icon: Swords },
  { id: "gems", label: "Gems", icon: Gem }, { id: "labs", label: "Labs", icon: FlaskConical }, { id: "cards", label: "Cards", icon: BookOpen }] as const;
const messageOf = (error: unknown): string => error instanceof Error ? error.message : "The request could not be completed.";

export function StrategyStudio({ library: initialLibrary, saved, catalog, members, onPublished, labsSnapshot = null, labObservations,
  initialLane, initialStrategyId, assignedSnapshot, initialWorker, initialAccount, initialSlot, linkIdentity, onLibrarySaved, client, mode = "fleet", onDraftChange }: {
  client?: StrategyStudioClient; mode?: "single" | "fleet"; onDraftChange?: (dirty: boolean) => void;
  library: StrategyLibrary; saved: BuildRouteDocument; catalog: Upgrade[]; members: StrategyWorker[];
  onPublished: (route: BuildRouteDocument) => void; labsSnapshot?: LabsSnapshot | null; labObservations?: LabsRow[];
  initialLane?: SpendingLane; initialStrategyId?: string; assignedSnapshot?: StrategyDefinition;
  initialWorker?: string; initialAccount?: string; initialSlot?: number; linkIdentity?: string;
  onLibrarySaved?: (library: StrategyLibrary) => void;
}): React.JSX.Element {
  const transport = useMemo(() => client ?? { saveVersion: saveFleetStrategy, assign: assignFleetStrategy,
    preview: previewBuildRoute, ledger: fetchStrategyLedger }, [client]);
  const defaultStrategyId = initialLibrary.templates.find(item => item.id === "turtle")?.id ?? initialLibrary.templates[0]?.id ?? "";
  const linkedStrategyId = assignedSnapshot?.id ?? initialStrategyId ?? defaultStrategyId;
  const firstObservedMember = members.find(member => !member.hidden && member.account_id);
  const linkedObservationAccount = initialWorker && initialAccount ? JSON.stringify([initialWorker, initialAccount])
    : firstObservedMember ? JSON.stringify([firstObservedMember.name, firstObservedMember.account_id]) : "";
  const [library, setLibrary] = useState(initialLibrary);
  useEffect(() => setLibrary(initialLibrary), [initialLibrary]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  useEffect(() => onDraftChange?.(Object.values(drafts).some(d => !!d.dirty)), [drafts, onDraftChange]);
  const [selectedIdState, setSelectedId] = useState(linkedStrategyId);
  const [laneState, setLane] = useState<SpendingLane>(initialLane ?? "workshop");
  const [seenLinkIdentity, setSeenLinkIdentity] = useState(linkIdentity ?? "");
  const [selection, setSelection] = useState<string | null>(null);
  const [target, setTarget] = useState<BlockTarget>(ROOT_END);
  const [category, setCategory] = useState<"Logic" | "Flow" | "Buy">("Logic");
  const [search, setSearch] = useState("");
  const [drag, setDrag] = useState<BlockDrag | null>(null);
  const [fullScreen, setFullScreen] = useState(false);
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyIdentity, setCopyIdentity] = useState<string | null>(null);
  const [copyMode, setCopyMode] = useState<"copy" | "scratch">("copy");
  const [copyName, setCopyName] = useState("");
  const [pendingAdd, setPendingAdd] = useState<{ preset: BlockPreset; upgrade?: string; target: BlockTarget } | null>(null);
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignScope, setAssignScope] = useState("");
  const [assignReview, setAssignReview] = useState<AssignmentReview | null>(null);
  const [observationAccountState, setObservationAccount] = useState(linkedObservationAccount);
  const [targets, setTargets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<BuildRoutePreview | null>(null);
  const [previewScope, setPreviewScope] = useState("");
  const [ledger, setLedger] = useState<StrategyLedgerEntry[] | null>(null);
  const [ledgerError, setLedgerError] = useState("");
  const linkChanged = seenLinkIdentity !== (linkIdentity ?? "");
  const snapshotChanged = selectedIdState.startsWith("assigned-snapshot:") && selectedIdState !== assignedSnapshot?.id;
  const selectedId = linkChanged || snapshotChanged ? linkedStrategyId : selectedIdState;
  const lane = linkChanged ? initialLane ?? "workshop" : laneState;
  const observationAccount = linkChanged ? linkedObservationAccount : observationAccountState;
  const all =[...library.templates, ...library.strategies, ...(assignedSnapshot ? [assignedSnapshot] : [])];
  const current = drafts[selectedId] ?? all.find(item => item.id === selectedId);
  const activeMembers = members.filter(member => !member.hidden && member.account_id);
  const memberScope = JSON.stringify(activeMembers.map(member => [member.name, member.account_id]).sort());
  const currentMemberScope = useRef(memberScope);
  currentMemberScope.current = memberScope;
  const currentLinkIdentity = useRef(linkIdentity ?? "");
  currentLinkIdentity.current = linkIdentity ?? "";
  const currentStrategyPin = useRef("");
  currentStrategyPin.current = current ? JSON.stringify([current.id, current.version]) : "";
  const observationMember = activeMembers.find(member => JSON.stringify([member.name, member.account_id]) === observationAccount);
  const observedLabs = observationMember ? (labObservations ?? labsSnapshot?.workers)?.find(row => row.worker === observationMember.name && row.account_id === observationMember.account_id) ?? null : null;
  const names = useMemo(() => new Map(catalog.map(item => [item.id, item.name])), [catalog]);
  const programLane = lane === "workshop" || lane === "battle" ? lane : null;
  const blocks = current && programLane ? current.baseline[programLane].blocks ?? [] : [];
  const selectedBlock = findBlock(blocks, selection) ?? blocks[0];
  const [cardCatalog, setCardCatalog] = useState<CardCatalog>({ cards: [], max_gem_slots: 21 });
  const [cardCatalogError, setCardCatalogError] = useState("");
  useEffect(() => {
    if (lane !== "cards" && lane !== "gems") return;
    const abort = new AbortController();
    void fetchCardCatalog({ scope: null, accountId: null, worker: null }, abort.signal).then(value => {
      if (!abort.signal.aborted) { setCardCatalog(value); setCardCatalogError(""); }
    }).catch(error => { if (!abort.signal.aborted) setCardCatalogError(messageOf(error)); });
    return () => abort.abort();
  }, [lane]);

  useEffect(() => {
    const key = (event: KeyboardEvent): void => {
      if (event.key === "Escape") { if (copyOpen) setCopyOpen(false); else if (assignOpen) setAssignOpen(false); else setFullScreen(false); }
    };
    document.addEventListener("keydown", key);
    const previous = document.body.style.overflow;
    if (fullScreen) document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", key); if (fullScreen) document.body.style.overflow = previous; };
  }, [fullScreen, copyOpen, assignOpen]);
  // History is a side panel: its failure must never block editing, so it
  // reports into its own error slot rather than the studio's.
  const refreshLedger = useCallback((): void => {
    transport.ledger().then(next => { setLedger(next.entries); setLedgerError(""); },
      failure => setLedgerError(messageOf(failure)));
  }, [transport]);
  useEffect(refreshLedger, [refreshLedger]);
  useEffect(() => {
    if (!linkChanged && !snapshotChanged) return;
    setSelectedId(linkedStrategyId);
    setLane(initialLane ?? "workshop");
    setObservationAccount(linkedObservationAccount);
    setSelection(null);
    setTarget(ROOT_END);
    setPreview(null);
    setError("");
    setAssignOpen(false);
    setCopyOpen(false);
    setAssignReview(null);
    setAssignScope("");
    setTargets([]);
    setCopyIdentity(null);
    setPendingAdd(null);
    setSeenLinkIdentity(linkIdentity ?? "");
  }, [linkChanged, snapshotChanged, linkedStrategyId, initialLane, linkedObservationAccount, linkIdentity]);

  if (!current) return <p role="alert">No strategy templates available.</p>;
  const strategy = current;
  const compatibility = strategy.kind === "legacy";
  const importedPolicy = strategy.legacy_snapshot?.strategy as Partial<Strategy> | undefined;
  const locked = strategy.builtin || compatibility;
  const viewingAssignedSnapshot = strategy.id === assignedSnapshot?.id;
  const assignmentReviewed = !!assignReview && !linkChanged && !snapshotChanged && !viewingAssignedSnapshot && !strategy.dirty
    && assignReview.linkIdentity === (linkIdentity ?? "") && assignReview.strategyId === strategy.id
    && assignReview.strategyVersion === strategy.version && assignReview.memberScope === memberScope;
  const known = new Set(all.map(item => item.id));
  const choices = [...all, ...Object.values(drafts).filter(item => !known.has(item.id))];

  function edit(baseline: BuildRouteDocument["baseline"]): void {
    if (locked || busy) return;
    setDrafts(previous => ({ ...previous, [strategy.id]: { ...strategy, baseline, dirty: true } }));
    setPreview(null); setError(""); setMessage("");
  }
  function editBlocks(next: StrategyBlock[]): void {
    if (!programLane) return;
    edit({ ...strategy.baseline, [programLane]: { ...strategy.baseline[programLane], mode: "blocks", blocks: next } });
  }
  function startCopy(add?: typeof pendingAdd): void {
    const base = `${strategy.name} copy`;
    let name = base, number = 2;
    while (choices.some(item => item.name.toLowerCase() === name.toLowerCase())) name = `${base} ${number++}`;
    setCopyMode("copy"); setCopyName(name); setPendingAdd(add ?? null); setCopyIdentity(linkIdentity ?? ""); setCopyOpen(true); setError("");
  }
  function startScratch(): void {
    let name = "New strategy", number = 2;
    while (choices.some(item => item.name.toLowerCase() === name.toLowerCase())) name = `New strategy ${number++}`;
    setCopyMode("scratch"); setCopyName(name); setPendingAdd(null); setCopyIdentity(linkIdentity ?? ""); setCopyOpen(true); setError("");
  }
  function createCopy(): void {
    if (linkChanged || snapshotChanged || copyIdentity !== (linkIdentity ?? "")) return;
    const name = copyName.trim();
    if (!name || choices.some(item => item.name.toLowerCase() === name.toLowerCase())) { setError("Choose a unique strategy name."); return; }
    const baseline = structuredClone(copyMode === "scratch" ? saved.baseline : strategy.baseline);
    if (copyMode === "scratch") {
      baseline.workshop = { ...baseline.workshop, mode: "blocks", blocks: [] };
      baseline.battle = { ...baseline.battle, mode: "blocks", blocks: [], branches: [] };
    }
    const draft: Draft = { ...structuredClone(strategy), id: `draft.${crypto.randomUUID()}`, name, version: 0,
      source_template: copyMode === "scratch" ? "scratch" : strategy.source_template || strategy.id,
      baseline, builtin: false, dirty: true };
    if (pendingAdd && programLane) {
      const block = makeBlock(pendingAdd.preset, programLane, pendingAdd.upgrade);
      draft.baseline = { ...draft.baseline, [programLane]: { ...draft.baseline[programLane], mode: "blocks", blocks: insertBlock(draft.baseline[programLane].blocks ?? [], block, pendingAdd.target) } };
      setSelection(block.id);
    }
    setDrafts(previous => ({ ...previous, [draft.id]: draft })); setSelectedId(draft.id);
    setCopyOpen(false); setPendingAdd(null); setError(""); setMessage("Editable copy created. Save it before assigning to emulators.");
  }
  function add(preset: BlockPreset, upgrade?: string, at = target): void {
    if (!programLane) return;
    if (locked) { startCopy({ preset, upgrade, target: at }); return; }
    const block = makeBlock(preset, programLane, upgrade);
    editBlocks(insertBlock(blocks, block, at)); setSelection(block.id);
    setTarget({ ...at, index: Math.min(at.index, blocks.length) + 1 });
  }
  function drop(at: BlockTarget): void {
    if (!drag) return;
    if ("preset" in drag) {
      if (drag.preset.startsWith("upgrade:")) add("buy", drag.preset.slice(8), at);
      else add(drag.preset as BlockPreset, undefined, at);
    } else if (!locked) {
      const block = findBlock(blocks, drag.id), from = locateBlock(blocks, drag.id);
      if (!block || !from || (at.parent && findBlock([block], at.parent))) { setDrag(null); return; }
      const index = from.parent === at.parent && from.branch === at.branch && from.index < at.index ? at.index - 1 : at.index;
      editBlocks(insertBlock(updateBlock(blocks, block.id, () => null), block, { ...at, index }));
      setSelection(block.id);
    }
    setDrag(null);
  }
  function move(id: string, direction: -1 | 1): void {
    const block = findBlock(blocks, id), from = locateBlock(blocks, id);
    if (!block || !from || from.index + direction < 0) return;
    editBlocks(insertBlock(updateBlock(blocks, id, () => null), block, { ...from, index: from.index + direction }));
  }
  async function save(): Promise<void> {
    const invalidCards = strategy.baseline.cards ? cardDraftError(strategy.baseline.cards, null) : null;
    if (invalidCards) { setError(invalidCards); return; }
    setBusy(true); setError("");
    try {
      const next = await transport.saveVersion({ name: strategy.name, source_template: strategy.source_template,
        baseline: strategy.baseline, ...(strategy.version > 0 ? { strategy_id: strategy.id } : {}) }, library.revision);
      const result = next.strategies.find(item => item.name === strategy.name);
      if (!result) throw new Error("The saved strategy was not returned. Reload the library before retrying.");
      setLibrary(next); onLibrarySaved?.(next);
      setDrafts(previous => { const copy = { ...previous }; delete copy[strategy.id]; delete copy[result.id]; return copy; });
      setSelectedId(result.id); setMessage(`Saved ${result.name} v${result.version}. Existing assignments stay on their saved version.`);
      refreshLedger();
    } catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }
  async function assign(): Promise<void> {
    const workers = activeMembers.filter(member => targets.includes(member.name)).map(member => ({ worker: member.name, account_id: member.account_id! }));
    if (!assignOpen || !assignmentReviewed || !workers.length || workers.length !== new Set(targets).size || assignScope !== memberScope) return;
    setBusy(true); setError("");
    try {
      const next = await transport.assign(strategy.id, strategy.version, workers, saved.revision);
      if (currentMemberScope.current !== memberScope) { setError("Accounts changed while assigning. Refresh the fleet to verify assignments."); return; }
      if (currentLinkIdentity.current !== assignReview?.linkIdentity
        || currentStrategyPin.current !== JSON.stringify([strategy.id, strategy.version])) {
        setError("Strategy changed while assigning. Refresh the fleet to verify assignments."); return;
      }
      onPublished(next); setAssignOpen(false); setMessage(`${strategy.name} v${strategy.version} assigned to ${workers.length} emulator${workers.length === 1 ? "" : "s"}. Takes effect at the next safe decision.`);
      refreshLedger();
    } catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }
  async function inspectDecisions(): Promise<void> {
    setBusy(true); setError("");
    const assignments = { ...saved.assignments };
    const overrides = { ...saved.overrides };
    activeMembers.forEach(member => { assignments[member.name] = { account_id: member.account_id!, strategy_id: strategy.id,
      strategy_version: Math.max(1, strategy.version), strategy_name: strategy.name, baseline: strategy.baseline }; delete overrides[member.name]; });
    try {
      const next = await transport.preview({ ...saved, assignments, overrides }, saved.revision);
      if (currentMemberScope.current === memberScope) { setPreview(next); setPreviewScope(memberScope); }
    }
    catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }

  return <section role="region" aria-label="Strategy Studio editor" data-fullscreen={fullScreen} className={`${styles.studio} ${fullScreen ? styles.fullscreen : ""}`}>
    <header className={styles.header}><div><p className={styles.eyebrow}>Strategy library</p><h2>Build your next move</h2></div>
      <button type="button" className={styles.button} onClick={() => setFullScreen(!fullScreen)}>{fullScreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}{fullScreen ? "Exit full screen" : "Full screen"}</button></header>
    <div className={styles.toolbar}>
      <label className={styles.strategyPicker}>Strategy<select aria-label="Strategy" disabled={busy} value={strategy.id} onChange={event => { setSelectedId(event.target.value); setSelection(null); setTarget(ROOT_END); setPreview(null); setError(""); }}>
        <optgroup label="Protected templates">{library.templates.map(item => <option key={item.id} value={item.id}>{item.name} · template</option>)}</optgroup>
        {assignedSnapshot && <optgroup label="Assigned snapshot"><option value={assignedSnapshot.id}>{assignedSnapshot.name} · assigned v{assignedSnapshot.version}</option></optgroup>}
        {!!choices.filter(item => !item.builtin).length && <optgroup label="Your strategies">{choices.filter(item => !item.builtin).map(item => <option key={item.id} value={item.id}>{item.name}{(drafts[item.id] ?? item).dirty ? " · draft" : ` · v${item.version}`}</option>)}</optgroup>}
      </select></label>
      <span className={styles.version}>{locked ? <LockKeyhole size={13} /> : <GitBranch size={13} />}{viewingAssignedSnapshot ? `Assigned snapshot · v${strategy.version}` : compatibility ? "Imported policy" : locked ? "Protected template" : strategy.dirty ? "Unsaved changes" : `Saved · v${strategy.version}`}</span>
      <div className={styles.toolbarActions}><Link className={styles.button} href="/fleet/reroll/strategies/guide/"><BookOpen size={15} />How it works</Link>
        <button className={styles.button} type="button" disabled={busy} onClick={startScratch}><Plus size={15} />Create from scratch</button>
        <button className={styles.button} type="button" disabled={busy || compatibility} onClick={() => startCopy()}><Copy size={15} />Create copy</button>
        {!locked && <button className={styles.primaryButton} type="button" disabled={busy || !strategy.dirty} onClick={() => void save()}><Save size={15} />{mode === "single" ? "Save version" : "Save strategy"}</button>}
        <button className={locked ? styles.primaryButton : styles.button} type="button" disabled={busy || viewingAssignedSnapshot || strategy.dirty || !activeMembers.length} onClick={() => {
          setTargets(activeMembers.map(member => member.name)); setAssignScope(memberScope);
          setAssignReview({ linkIdentity: linkIdentity ?? "", strategyId: strategy.id, strategyVersion: strategy.version, memberScope });
          setAssignOpen(true); setError("");
        }}><Monitor size={15} />{mode === "single" ? "Apply to this emulator" : "Assign"}</button></div>
    </div>
    <div className={styles.sourceBanner}>{locked ? <LockKeyhole size={14} /> : <GitBranch size={14} />}<span>{viewingAssignedSnapshot ? `This is the exact assigned v${strategy.version} baseline. Create a copy to edit it; newer saved versions remain separate.` : compatibility ? "Imported policy · immutable purchase snapshot." : locked ? "Built-in strategy · create a copy to make it yours." : `Based on ${strategy.source_template} · save a version, then assign it. Existing assignments never change with your draft.`}</span></div>
    {error && !copyOpen && !assignOpen && <p role="alert" className={styles.error}>{error}</p>}
    {message && <p role="status" className={styles.status}>{message}</p>}
    {compatibility ? <section className="p-5 space-y-3" aria-label="Imported policy">
      <h3 className="font-semibold">Imported {strategy.legacy_snapshot?.profile} · v{strategy.version}</h3>
      <p className="text-sm text-muted-foreground">This version preserves the original purchase rules exactly. Apply it to keep that behavior, or create a new native plan from a template. Bot timing and claims remain specific to this emulator.</p>
      <dl className="grid gap-4 sm:grid-cols-2 text-sm">
        <div><dt className="font-semibold">Battle purchases</dt><dd>{importedPolicy?.autopilot?.enabled ? `${importedPolicy.autopilot.preset} · ${importedPolicy.autopilot.rules.length} OCR priorities` : 'Original image-based priorities'}</dd>
          <ol className="list-decimal pl-5 mt-2">{(importedPolicy?.autopilot?.enabled ? importedPolicy.autopilot.rules.map(rule => ({ name: names.get(rule.upgrade_id) ?? rule.upgrade_id, enabled: rule.enabled })) : importedPolicy?.actions ?? []).filter(rule => rule.enabled).map((rule, index) => <li key={index}>{rule.name}</li>)}</ol></div>
        <div><dt className="font-semibold">Workshop purchases</dt><dd>{importedPolicy?.shopping?.enabled ? `${importedPolicy.shopping.workshop.length} ordered rules · original budgets and reserves preserved` : 'Disabled in the original profile'}</dd>
          <ol className="list-decimal pl-5 mt-2">{importedPolicy?.shopping?.workshop.map((rule, index) => <li key={index}>{rule.name}</li>)}</ol></div>
        <div><dt className="font-semibold">Cards</dt><dd>{importedPolicy?.shopping?.cards.enabled ? 'Original purchase limits and gem reserve' : 'Purchases disabled in the original profile'}</dd></div>
        <div><dt className="font-semibold">Progression</dt><dd>Original tier thresholds and build choice preserved</dd></div>
      </dl>
    </section> : <>
    <div className={styles.workspace} inert={busy} style={lane === "labs" || lane === "cards" ? { gridTemplateColumns: "minmax(0, 1fr)" } : undefined}>
      {lane !== "labs" && lane !== "cards" && <aside className={styles.palette} aria-label="Block palette">
        <div className={styles.paletteHeading}><h3>Blocks</h3><span>drag to connect</span></div>
        {programLane ? <>
          <div className={styles.smallTabs} role="tablist" aria-label="Block categories">{(["Logic", "Flow", "Buy"] as const).map(group => <button key={group} role="tab" type="button" aria-selected={category === group} onClick={() => setCategory(group)}>{group}</button>)}</div>
          {presetsForLane(programLane).filter(item => item.group === category).map(item => <div key={item.id} className={`${styles.paletteBlock} ${styles[item.group.toLowerCase()]}`} draggable onDragStart={event => { setDrag({ preset: item.id }); event.dataTransfer?.setData("text/plain", item.id); }} onDragEnd={() => setDrag(null)}>
            <span><strong>{item.label}</strong><small>{item.detail}</small></span><button type="button" aria-label={`Add ${item.label}`} onClick={() => add(item.id)}><Plus size={16} /></button></div>)}
          {category === "Buy" && <>
            <input className={styles.search} type="search" aria-label="Search upgrade blocks" placeholder="Find upgrade…" value={search} onChange={event => setSearch(event.target.value)} />
            <div className={styles.upgradePalette}>{catalog.filter(item => (programLane === "workshop" || !item.unlock) && item.name.toLowerCase().includes(search.toLowerCase())).map(item => <div key={item.id} className={`${styles.paletteBlock} ${styles[item.category.toLowerCase()]}`} draggable onDragStart={() => setDrag({ preset: `upgrade:${item.id}` })} onDragEnd={() => setDrag(null)}>
              <span><strong>{item.name}</strong><small>{item.category.toLowerCase()}</small></span><button type="button" aria-label={`Add ${item.name}`} onClick={() => add("buy", item.id)}><Plus size={14} /></button></div>)}</div>
          </>}
          <p className={styles.hint}>Drop into a connector, or choose an insertion point and use +.</p>
          <div className={styles.sharedRules}><Shield size={15} /><strong>Shared rules</strong><p>Never Buy, unlocks and affordability apply to every path.</p></div>
        </> : <p className={styles.hint}>Add and arrange {lane} blocks on the canvas. Only blocks marked Automated run today; the rest are planned.</p>}
      </aside>}
      <main className={styles.canvasPane}>
        <div role="tablist" aria-label="Spending lanes" className={styles.lanes}>{LANES.map(item => <button role="tab" aria-selected={lane === item.id} type="button" key={item.id} onClick={() => { setLane(item.id); setSelection(null); setTarget(ROOT_END); setPreview(null); }}><item.icon size={16} />{item.label}</button>)}</div>
        <div className={styles.canvasMeta}><span>{lane === "workshop" ? "Coins · after a run · research first" : lane === "battle" ? "Cash · during a run · verified rows" : lane === "cards" ? "Cards · independent account budgets · between runs" : lane === "gems" ? "Gems · reserve before spending" : "Coins · Game Speed before Workshop"}</span><span>{locked ? "Template" : "Editable copy"}</span></div>
        <div className={styles.canvas}>
          <div className={styles.trigger}><Sparkles size={14} />{lane === "battle" ? "When battle facts are ready" : "When the main menu is ready"}</div>
          {programLane ? <StrategyCanvas {...{ blocks, names, locked, target }} selected={selectedBlock?.id ?? null} onSelect={id => { setSelection(id); const at = locateBlock(blocks, id);
            // The goal branch is not an insertion point: add after its save-for block instead.
            const pos = at?.branch === "goal" && at.parent ? locateBlock(blocks, at.parent) : at;
            if (pos) setTarget({ ...pos, index: pos.index + 1 }); }} onTarget={setTarget} onDrop={drop} onMove={move} onDrag={setDrag} />
            : lane === "cards" ? <section className="space-y-4" aria-label="Library Cards program">
              <p>Saving a Cards program creates a library revision. Assign it from Fleet Cards; each account keeps its own budget. Saving never enables automation.</p>
              <Link href={mode === "single" ? "/cards/" : "/fleet/reroll/cards/"}>{mode === "single" ? "View this account’s Cards" : "Compare accounts and assign Cards"}</Link>
              {cardCatalogError && <p role="alert">Card catalog unavailable: {cardCatalogError}. Existing references are retained.</p>}
              {strategy.baseline.cards ? <>
                <CardPlanEditor program={strategy.baseline.cards} catalog={cardCatalog} disabled={locked || busy} onChange={cards => edit({ ...strategy.baseline, cards })} />
                <CardLoadoutEditor program={strategy.baseline.cards} catalog={cardCatalog} disabled={locked || busy} onChange={cards => edit({ ...strategy.baseline, cards })} />
              </> : <button type="button" disabled={locked || busy} onClick={() => edit({ ...strategy.baseline, cards: emptyCardProgram() })}>Add Cards program</button>}
            </section> : lane === "labs" ? <div className="min-w-0 space-y-4">
              <label className="grid min-w-0 gap-1 text-xs">Account observations
                <select aria-label="Lab observation account" className="min-h-11 min-w-0 w-full" value={observationMember ? observationAccount : ""}
                  onChange={event => setObservationAccount(event.target.value)}>
                  <option value="">Choose an account</option>{activeMembers.map(member => <option key={member.name} value={JSON.stringify([member.name, member.account_id])}>{member.name} · {member.account_id}</option>)}
                </select></label>
              {!observationMember && observationAccount && <p role="status" className="text-sm">Account changed. Choose an account for observations; your strategy draft is preserved.</p>}
              {isLabList(strategy.baseline.labs) ? <LabListView labs={strategy.baseline.labs} reference={labsSnapshot?.reference ?? null} observed={observedLabs} /> : <>
              <LabSlotPlanner labs={strategy.baseline.labs} reference={labsSnapshot?.reference ?? null} automated={labsSnapshot?.automated ?? []}
                observed={observedLabs} locked={locked || busy} focusSlot={initialSlot} onChange={labs => edit({ ...strategy.baseline, labs })} />
              <details className="min-w-0 rounded-xl border border-border p-3 [&_button]:min-h-11 [&_select]:min-h-11 [&_input:not([type=checkbox])]:min-h-11">
                <summary className="min-h-11 cursor-pointer py-3 text-sm font-semibold">Advanced lab blocks</summary>
                <p className="mb-3 text-xs text-muted-foreground">Conditions, pools and shared tracks keep their full program. Changes here affect every slot in a shared track.</p>
                <ResourceBlocks kind="labs" gems={strategy.baseline.gems} labs={strategy.baseline.labs} locked={locked}
                  automated={labsSnapshot?.automated ?? []} catalog={labsSnapshot?.reference ?? null} rules={rulesOf(strategy.baseline)}
                  onGemsChange={() => {}} onLabsChange={labs => { if (locked) startCopy(); else edit({ ...strategy.baseline, labs }); }} />
              </details>
              </>}
            </div> : <ResourceBlocks cardProgram={strategy.baseline.cards} cardCatalog={cardCatalog} kind="gems" gems={strategy.baseline.gems} labs={strategy.baseline.labs} locked={locked}
                automated={labsSnapshot?.automated ?? []} catalog={labsSnapshot?.reference ?? null} hideGemSpendLimit
                rules={rulesOf(strategy.baseline)}
                onGemsChange={gems => { if (locked) startCopy(); else edit({ ...strategy.baseline, gems }); }}
                onLabsChange={labs => { if (locked) startCopy(); else edit({ ...strategy.baseline, labs }); }} />}
          <div className={styles.pathEnd}>One confirmed purchase → refresh facts → decide again</div>
        </div>
        <div className={styles.canvasFooter}><span>{mode === "single" ? "Preview this strategy for this emulator. No assignment changes." : "Preview this strategy on every current emulator. No assignments change."}</span><button className={styles.button} type="button" disabled={busy || viewingAssignedSnapshot || !activeMembers.length} onClick={() => void inspectDecisions()}>{mode === "single" ? "Preview this emulator" : "Preview across fleet"}</button></div>
      </main>
      {programLane ? <StrategyBlockInspector block={selectedBlock} lane={programLane} catalog={catalog} locked={locked}
        isGoal={selectedBlock ? locateBlock(blocks, selectedBlock.id)?.branch === "goal" : false} onCopy={() => startCopy()}
        onChange={block => editBlocks(updateBlock(blocks, block.id, () => block))} onRemove={() => { if (selectedBlock) editBlocks(updateBlock(blocks, selectedBlock.id, () => null)); setSelection(null); setTarget(ROOT_END); }} />
        : <aside className={styles.inspector} style={lane === "labs" || lane === "cards" ? { gridColumn: "1 / -1" } : undefined} aria-label="Resource strategy settings"><p className={styles.eyebrow}>Resource path</p><h3>{lane === "gems" ? "Protect your lab fund" : "Keep research moving"}</h3><p className={styles.hint}>{lane === "gems" ? "Lab slot 2 is the first 100-gem purchase. Later cards and lab steps stay visibly planned." : "Game Speed uses lab slot 1 through all supported levels, before Workshop spending."}</p>
          {locked && <button className={styles.primaryButton} type="button" onClick={() => startCopy()}>Create copy to edit</button>}
        </aside>}
    </div>
    {programLane === "workshop" && <div className={styles.guardrails} inert={busy}>
      <label>Spend limit · % of available coins<input aria-label="Workshop spend limit" type="number" min={10} max={100} disabled={locked} value={strategy.baseline.workshop.coin_spend_limit_pct}
        onChange={event => { const rules = rulesOf(strategy.baseline); edit(withRules(strategy.baseline, { ...rules, coins: { ...rules.coins, workshop_spend_limit_pct: Number(event.target.value) } })); }} /></label>
      <div className={styles.banArea} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (locked) { startCopy(); return; } if (drag && "preset" in drag && drag.preset.startsWith("upgrade:")) { const id = drag.preset.slice(8); edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: [...new Set([...strategy.baseline.workshop.banned_upgrade_ids, id])] } }); } setDrag(null); }}>
        <strong>Never Buy <span>{strategy.baseline.workshop.banned_upgrade_ids.length}</span></strong><div className={styles.bannedItems}>{strategy.baseline.workshop.banned_upgrade_ids.map(id => <button key={id} type="button" disabled={locked} aria-label={`Allow ${names.get(id) ?? id}`} onClick={() => edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: strategy.baseline.workshop.banned_upgrade_ids.filter(value => value !== id) } })}>{names.get(id) ?? id}<X size={12} /></button>)}</div>
        <label>Add to Never Buy<select disabled={locked} value="" onChange={event => { if (event.target.value) edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: [...new Set([...strategy.baseline.workshop.banned_upgrade_ids, event.target.value])] } }); }}><option value="">Drop an upgrade here, or choose…</option>{catalog.filter(item => !strategy.baseline.workshop.banned_upgrade_ids.includes(item.id)).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      </div>
    </div>}
    <details className="min-w-0 border-b border-border px-3 py-4 sm:px-6" open inert={busy}>
      <summary className="cursor-pointer text-sm font-semibold">Strategy rules</summary>
      <div className="space-y-2 text-sm"><strong>Tier promotion</strong>
        <p>Tiers without a threshold stay on their current tier.</p>
        {Object.entries(strategy.baseline.tier_promotion ?? {}).map(([tier, wave]) => <label key={tier} className="flex gap-2 items-center">
          Tier {tier} · best wave
          <input aria-label={`Leave tier ${tier} at wave`} type="number" min={1} disabled={locked} value={wave}
            onChange={event => { if (event.target.value) edit({ ...strategy.baseline, tier_promotion: { ...strategy.baseline.tier_promotion, [tier]: Number(event.target.value) } }); }} />
          <button type="button" disabled={locked} onClick={() => { const next = { ...strategy.baseline.tier_promotion }; delete next[tier]; edit({ ...strategy.baseline, tier_promotion: next }); }}>Remove tier {tier}</button>
        </label>)}
        <button type="button" disabled={locked} onClick={() => { const tier = Math.max(0, ...Object.keys(strategy.baseline.tier_promotion ?? {}).map(Number)) + 1;
          edit({ ...strategy.baseline, tier_promotion: { ...strategy.baseline.tier_promotion, [tier]: 250 } }); }}>Add tier threshold</button>
      </div>
      <StrategyRules rerollMode={mode === "fleet"} rules={rulesOf(strategy.baseline)} locked={locked} rows={labsSnapshot?.workers ?? []}
        labList={isLabList(strategy.baseline.labs)} onChange={rules => edit(withRules(strategy.baseline, rules))} />
    </details>
    </>}
    {!compatibility && <TournamentEditor value={strategy.baseline.tournament} onChange={tournament => edit({ ...strategy.baseline, tournament })} disabled={locked} />}
    {preview && previewScope === memberScope && <div className={styles.preview}><p className={styles.hint}>{mode === "single" ? "Preview for this emulator · no purchases will be made." : "Hypothetical fleet-wide assignment · compare before choosing which emulators to assign."}</p><RouteInspector preview={preview} members={members} /></div>}
    <div className={styles.assignmentsSummary}><h3>{mode === "single" ? "Emulator assignment" : "Fleet assignments"}</h3>{activeMembers.map(member => { const assignment = saved.assignments?.[member.name]; return <div key={member.name}><Monitor size={15} /><strong>{member.name}</strong><span>{assignment && assignment.account_id === member.account_id ? `${assignment.strategy_name} · v${assignment.strategy_version}` : assignment ? "Account changed · assignment inactive" : mode === "single" ? "Local profile" : "Current fleet route"}</span></div>; })}</div>
    <StrategyHistory entries={ledger} error={ledgerError} />
    <Dialog.Root key={linkIdentity ?? "default"} open={(copyOpen || assignOpen) && !linkChanged && !snapshotChanged} onOpenChange={open => { if (!open && !busy) { setCopyOpen(false); setAssignOpen(false); } }}><Dialog.Portal><Dialog.Backdrop className={styles.modalBackdrop} /><Dialog.Popup className={`${styles.studio} ${styles.modal}`}>
      <div className={styles.modalHeading}><Dialog.Title>{copyOpen ? copyMode === "scratch" ? "Create strategy from scratch" : "Create your strategy" : "Assign saved strategy"}</Dialog.Title><button className={styles.iconButton} disabled={busy} type="button" aria-label="Close dialog" onClick={() => { setCopyOpen(false); setAssignOpen(false); }}><X size={18} /></button></div>
      {copyOpen ? <><Dialog.Description className={styles.hint}>{copyMode === "scratch" ? "Start with empty, non-spending purchase lanes. Add blocks, then save before assigning." : `Start from ${strategy.name}. The original stays protected.`}</Dialog.Description><label className={styles.fields}>Strategy name<input autoFocus maxLength={80} value={copyName} onChange={event => setCopyName(event.target.value)} onKeyDown={event => { if (event.key === "Enter") createCopy(); }} /></label></>
        : <><Dialog.Description className={styles.hint}>{strategy.name} · v{strategy.version}. Applies at the next safe decision.</Dialog.Description>{mode === "fleet" && <button type="button" disabled={busy} className={styles.allFleet} aria-pressed={activeMembers.every(member => targets.includes(member.name))} onClick={() => setTargets(targets.length === activeMembers.length ? [] : activeMembers.map(member => member.name))}><Monitor size={17} />Entire current fleet</button>}
          <div className={styles.memberChoices}>{mode === "single" ? <p>Only this emulator: <strong>{activeMembers[0]?.name}</strong> · {activeMembers[0]?.account_id}</p> : activeMembers.map(member => <button key={member.name} type="button" aria-pressed={targets.includes(member.name)} onClick={() => setTargets(targets.includes(member.name) ? targets.filter(name => name !== member.name) : [...targets, member.name])}><Monitor size={17} /><strong>{member.name}</strong><span>{member.account_id}</span></button>)}</div></>}
      {error && <p className={styles.error} role="alert">{error}</p>}
      {assignOpen && assignScope !== memberScope && <p role="alert" className={styles.error}>Accounts changed. Close this dialog and review the current fleet before assigning.</p>}
      {assignOpen && assignScope === memberScope && !assignmentReviewed && <p role="alert" className={styles.error}>Strategy changed. Close this dialog and review the current assignment.</p>}
      <div className={styles.modalActions}><button type="button" className={styles.button} disabled={busy} onClick={() => { setCopyOpen(false); setAssignOpen(false); }}>Cancel</button><button type="button" className={styles.primaryButton} disabled={busy || (copyOpen ? copyIdentity !== (linkIdentity ?? "") : (!targets.length || !assignmentReviewed))} onClick={() => { if (copyOpen) createCopy(); else void assign(); }}>{copyOpen ? copyMode === "scratch" ? "Create blank strategy" : "Create editable copy" : (mode === "single" ? "Apply saved version" : `Assign to ${targets.length} emulator${targets.length === 1 ? "" : "s"}`)}</button></div>
    </Dialog.Popup></Dialog.Portal></Dialog.Root>
  </section>;
}
