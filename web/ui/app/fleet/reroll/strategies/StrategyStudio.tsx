"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Dialog } from "@base-ui/react/dialog";
import { ArrowDown, BookOpen, Copy, FlaskConical, Gem, GitBranch, Hammer, LockKeyhole, Maximize2, Minimize2, Monitor, Plus, Save, Shield, Sparkles, Swords, X } from "lucide-react";
import { assignFleetStrategy, previewBuildRoute, saveFleetStrategy } from "@/lib/api";
import type { BuildRouteDocument, BuildRoutePreview } from "@/lib/buildRoute";
import type { SpendingLane, StrategyBlock, StrategyDefinition, StrategyLibrary, StrategyWorker } from "@/lib/strategyStudio";
import type { Upgrade } from "@/lib/types";
import { ROOT_END, presetsForLane, findBlock, insertBlock, locateBlock, makeBlock, updateBlock, type BlockPreset, type BlockTarget } from "./strategyBlocks";
import { StrategyCanvas, type BlockDrag } from "./StrategyCanvas";
import { StrategyBlockInspector } from "./StrategyBlockInspector";
import { RouteInspector } from "./RouteInspector";
import styles from "./studio.module.css";

type Draft = StrategyDefinition & { dirty?: boolean };
const LANES = [{ id: "workshop", label: "Workshop", icon: Hammer }, { id: "battle", label: "In-game", icon: Swords },
  { id: "gems", label: "Gems", icon: Gem }, { id: "labs", label: "Labs", icon: FlaskConical }] as const;
const RESOURCES: Record<string, string> = { unlock_lab_slot_2: "Unlock lab slot 2", unlock_lab_slot_3: "Unlock lab slot 3", unlock_lab_slot_4: "Unlock lab slot 4",
  unlock_lab_slot_5: "Unlock lab slot 5", cards: "Cards", card_slot: "Card slot", research_game_speed: "Game Speed research", slot2_research: "Slot 2 research" };
const messageOf = (error: unknown): string => error instanceof Error ? error.message : "The request could not be completed.";

export function StrategyStudio({ library: initialLibrary, saved, catalog, members, onPublished }: {
  library: StrategyLibrary; saved: BuildRouteDocument; catalog: Upgrade[]; members: StrategyWorker[];
  onPublished: (route: BuildRouteDocument) => void;
}): React.JSX.Element {
  const [library, setLibrary] = useState(initialLibrary);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [selectedId, setSelectedId] = useState(initialLibrary.templates.find(item => item.id === "turtle")?.id ?? initialLibrary.templates[0]?.id ?? "");
  const [lane, setLane] = useState<SpendingLane>("workshop");
  const [selection, setSelection] = useState<string | null>(null);
  const [target, setTarget] = useState<BlockTarget>(ROOT_END);
  const [category, setCategory] = useState<"Logic" | "Flow" | "Buy">("Logic");
  const [search, setSearch] = useState("");
  const [drag, setDrag] = useState<BlockDrag | null>(null);
  const [resourceDrag, setResourceDrag] = useState<string | null>(null);
  const [fullScreen, setFullScreen] = useState(false);
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyMode, setCopyMode] = useState<"copy" | "scratch">("copy");
  const [copyName, setCopyName] = useState("");
  const [pendingAdd, setPendingAdd] = useState<{ preset: BlockPreset; upgrade?: string; target: BlockTarget } | null>(null);
  const [assignOpen, setAssignOpen] = useState(false);
  const [targets, setTargets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<BuildRoutePreview | null>(null);
  const all = [...library.templates, ...library.strategies];
  const current = drafts[selectedId] ?? all.find(item => item.id === selectedId);
  const activeMembers = members.filter(member => !member.hidden && member.account_id);
  const names = useMemo(() => new Map(catalog.map(item => [item.id, item.name])), [catalog]);
  const programLane = lane === "workshop" || lane === "battle" ? lane : null;
  const blocks = current && programLane ? current.baseline[programLane].blocks ?? [] : [];
  const selectedBlock = findBlock(blocks, selection) ?? blocks[0];

  useEffect(() => {
    const key = (event: KeyboardEvent): void => {
      if (event.key === "Escape") { if (copyOpen) setCopyOpen(false); else if (assignOpen) setAssignOpen(false); else setFullScreen(false); }
    };
    document.addEventListener("keydown", key);
    const previous = document.body.style.overflow;
    if (fullScreen) document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", key); if (fullScreen) document.body.style.overflow = previous; };
  }, [fullScreen, copyOpen, assignOpen]);

  if (!current) return <p role="alert">No strategy templates available.</p>;
  const strategy = current;
  const locked = strategy.builtin;
  const resourceSteps = lane === "gems" || lane === "labs" ? strategy.baseline[lane].steps : [];
  const resourceOptions = lane === "gems" ? ["unlock_lab_slot_3", "unlock_lab_slot_4", "unlock_lab_slot_5", "card_slot", "cards"] : ["slot2_research"];
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
    setCopyMode("copy"); setCopyName(name); setPendingAdd(add ?? null); setCopyOpen(true); setError("");
  }
  function startScratch(): void {
    let name = "New strategy", number = 2;
    while (choices.some(item => item.name.toLowerCase() === name.toLowerCase())) name = `New strategy ${number++}`;
    setCopyMode("scratch"); setCopyName(name); setPendingAdd(null); setCopyOpen(true); setError("");
  }
  function createCopy(): void {
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
  function changeResource(step: string, before?: number): void {
    if (locked) { startCopy(); return; }
    if (lane !== "gems" && lane !== "labs") return;
    const next = resourceSteps.filter(item => item !== step);
    next.splice(Math.max(1, before ?? next.length), 0, step);
    edit({ ...strategy.baseline, [lane]: { ...strategy.baseline[lane], steps: next } }); setResourceDrag(null);
  }
  async function save(): Promise<void> {
    setBusy(true); setError("");
    try {
      const next = await saveFleetStrategy({ name: strategy.name, source_template: strategy.source_template,
        baseline: strategy.baseline, ...(strategy.version > 0 ? { strategy_id: strategy.id } : {}) }, library.revision);
      const result = next.strategies.find(item => item.name === strategy.name);
      if (!result) throw new Error("The saved strategy was not returned. Reload the library before retrying.");
      setLibrary(next); setDrafts(previous => { const copy = { ...previous }; delete copy[strategy.id]; delete copy[result.id]; return copy; });
      setSelectedId(result.id); setMessage(`Saved ${result.name} v${result.version}. Existing assignments stay on their saved version.`);
    } catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }
  async function assign(): Promise<void> {
    const workers = activeMembers.filter(member => targets.includes(member.name)).map(member => ({ worker: member.name, account_id: member.account_id! }));
    if (!workers.length || strategy.dirty) return;
    setBusy(true); setError("");
    try {
      const next = await assignFleetStrategy(strategy.id, strategy.version, workers, saved.revision);
      onPublished(next); setAssignOpen(false); setMessage(`${strategy.name} v${strategy.version} assigned to ${workers.length} emulator${workers.length === 1 ? "" : "s"}. Takes effect at the next safe decision.`);
    } catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }
  async function inspectDecisions(): Promise<void> {
    setBusy(true); setError("");
    const assignments = { ...saved.assignments };
    const overrides = { ...saved.overrides };
    activeMembers.forEach(member => { assignments[member.name] = { account_id: member.account_id!, strategy_id: strategy.id,
      strategy_version: Math.max(1, strategy.version), strategy_name: strategy.name, baseline: strategy.baseline }; delete overrides[member.name]; });
    try { setPreview(await previewBuildRoute({ ...saved, assignments, overrides }, saved.revision)); }
    catch (failure) { setError(messageOf(failure)); } finally { setBusy(false); }
  }

  return <section role="region" aria-label="Strategy Studio editor" data-fullscreen={fullScreen} className={`${styles.studio} ${fullScreen ? styles.fullscreen : ""}`}>
    <header className={styles.header}><div><p className={styles.eyebrow}>Strategy library</p><h2>Build your next move</h2></div>
      <button type="button" className={styles.button} onClick={() => setFullScreen(!fullScreen)}>{fullScreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}{fullScreen ? "Exit full screen" : "Full screen"}</button></header>
    <div className={styles.toolbar}>
      <label className={styles.strategyPicker}>Strategy<select aria-label="Strategy" disabled={busy} value={strategy.id} onChange={event => { setSelectedId(event.target.value); setSelection(null); setTarget(ROOT_END); setPreview(null); setError(""); }}>
        <optgroup label="Protected templates">{library.templates.map(item => <option key={item.id} value={item.id}>{item.name} · template</option>)}</optgroup>
        {!!choices.filter(item => !item.builtin).length && <optgroup label="Your strategies">{choices.filter(item => !item.builtin).map(item => <option key={item.id} value={item.id}>{item.name}{(drafts[item.id] ?? item).dirty ? " · draft" : ` · v${item.version}`}</option>)}</optgroup>}
      </select></label>
      <span className={styles.version}>{locked ? <LockKeyhole size={13} /> : <GitBranch size={13} />}{locked ? "Protected template" : strategy.dirty ? "Unsaved changes" : `Saved · v${strategy.version}`}</span>
      <div className={styles.toolbarActions}><Link className={styles.button} href="/fleet/reroll/strategies/guide/"><BookOpen size={15} />How it works</Link>
        <button className={styles.button} type="button" disabled={busy} onClick={startScratch}><Plus size={15} />Create from scratch</button>
        <button className={styles.button} type="button" disabled={busy} onClick={() => startCopy()}><Copy size={15} />Create copy</button>
        {!locked && <button className={styles.primaryButton} type="button" disabled={busy || !strategy.dirty} onClick={() => void save()}><Save size={15} />Save strategy</button>}
        <button className={locked ? styles.primaryButton : styles.button} type="button" disabled={busy || strategy.dirty || !activeMembers.length} onClick={() => { setTargets(activeMembers.map(member => member.name)); setAssignOpen(true); setError(""); }}><Monitor size={15} />Assign</button></div>
    </div>
    <div className={styles.sourceBanner}>{locked ? <LockKeyhole size={14} /> : <GitBranch size={14} />}<span>{locked ? "Built-in strategy · create a copy to make it yours." : `Based on ${strategy.source_template} · save a version, then assign it. Existing assignments never change with your draft.`}</span></div>
    {error && !copyOpen && !assignOpen && <p role="alert" className={styles.error}>{error}</p>}
    {message && <p role="status" className={styles.status}>{message}</p>}
    <div className={styles.workspace} inert={busy}>
      <aside className={styles.palette} aria-label="Block palette">
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
        </> : <>{resourceOptions.filter(step => !resourceSteps.includes(step)).map(step => <div key={step} className={`${styles.paletteBlock} ${styles.resource}`} draggable onDragStart={() => setResourceDrag(step)} onDragEnd={() => setResourceDrag(null)}>
          <span><strong>{RESOURCES[step]}</strong><small>Planned · not automated</small></span><button type="button" aria-label={`Add ${RESOURCES[step]}`} onClick={() => changeResource(step)}><Plus size={15} /></button></div>)}
          <p className={styles.hint}>Drag into the path. Current lab automation remains first.</p></>}
      </aside>
      <main className={styles.canvasPane}>
        <div role="tablist" aria-label="Spending lanes" className={styles.lanes}>{LANES.map(item => <button role="tab" aria-selected={lane === item.id} type="button" key={item.id} onClick={() => { setLane(item.id); setSelection(null); setTarget(ROOT_END); setPreview(null); }}><item.icon size={16} />{item.label}</button>)}</div>
        <div className={styles.canvasMeta}><span>{lane === "workshop" ? "Coins · after a run · research first" : lane === "battle" ? "Cash · during a run · verified rows" : lane === "gems" ? "Gems · reserve before spending" : "Coins · Game Speed before Workshop"}</span><span>{locked ? "Template" : "Editable copy"}</span></div>
        <div className={styles.canvas}>
          <div className={styles.trigger}><Sparkles size={14} />{lane === "battle" ? "When battle facts are ready" : "When the main menu is ready"}</div>
          {programLane ? <StrategyCanvas {...{ blocks, names, locked, target }} selected={selectedBlock?.id ?? null} onSelect={id => { setSelection(id); const at = locateBlock(blocks, id);
            // The goal branch is not an insertion point: add after its save-for block instead.
            const pos = at?.branch === "goal" && at.parent ? locateBlock(blocks, at.parent) : at;
            if (pos) setTarget({ ...pos, index: pos.index + 1 }); }} onTarget={setTarget} onDrop={drop} onMove={move} onDrag={setDrag} />
            : <div className={styles.path} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (resourceDrag) changeResource(resourceDrag); }}>{resourceSteps.map((step, index) => <div key={step}>
              <div className={styles.resourceConnector}><ArrowDown size={16} /></div><div className={`${styles.block} ${styles.resource}`} draggable={!locked && index > 0} onDragStart={() => setResourceDrag(step)} onDragEnd={() => setResourceDrag(null)} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); event.stopPropagation(); if (resourceDrag && index > 0) changeResource(resourceDrag, index); }}>
                <div className={styles.blockMain}><span className={styles.blockKind}>{index === 0 ? "CURRENT AUTOMATION" : "PLANNED · NOT AUTOMATED"}</span><strong>{RESOURCES[step]}</strong><span className={styles.blockDetail}>{index === 0 ? lane === "gems" ? "Reserve the first 100 gems for the second lab." : "Keep slot 1 on Game Speed until maxed. Never cancel research or rush with gems." : "Part of your spending path; waiting for automation support."}</span></div>
                {!locked && index > 0 && <div className={styles.resourceActions}><button type="button" disabled={index === 1} onClick={() => changeResource(step, index - 1)}>Move up</button><button type="button" aria-label={`Remove ${RESOURCES[step]}`} onClick={() => edit({ ...strategy.baseline, [lane]: { ...strategy.baseline[lane], steps: resourceSteps.filter(value => value !== step) } })}>Remove</button></div>}
              </div></div>)}</div>}
          <div className={styles.pathEnd}>One confirmed purchase → refresh facts → decide again</div>
        </div>
        <div className={styles.canvasFooter}><span>Preview this strategy on every current emulator. No assignments change.</span><button className={styles.button} type="button" disabled={busy || !activeMembers.length} onClick={() => void inspectDecisions()}>Preview across fleet</button></div>
      </main>
      {programLane ? <StrategyBlockInspector block={selectedBlock} lane={programLane} catalog={catalog} locked={locked}
        isGoal={selectedBlock ? locateBlock(blocks, selectedBlock.id)?.branch === "goal" : false} onCopy={() => startCopy()}
        onChange={block => editBlocks(updateBlock(blocks, block.id, () => block))} onRemove={() => { if (selectedBlock) editBlocks(updateBlock(blocks, selectedBlock.id, () => null)); setSelection(null); setTarget(ROOT_END); }} />
        : <aside className={styles.inspector} aria-label="Resource strategy settings"><p className={styles.eyebrow}>Resource path</p><h3>{lane === "gems" ? "Protect your lab fund" : "Keep research moving"}</h3><p className={styles.hint}>{lane === "gems" ? "Lab slot 2 is the first 100-gem purchase. Later cards and lab steps stay visibly planned." : "Game Speed uses lab slot 1 through all supported levels, before Workshop spending."}</p>
          {locked && <button className={styles.primaryButton} type="button" onClick={() => startCopy()}>Create copy to edit</button>}
          {lane === "gems" && <label className={styles.fields}>Planned gem spend limit (%)<input type="number" disabled={locked} min={0} max={100} value={strategy.baseline.gems.spend_limit_pct} onChange={event => edit({ ...strategy.baseline, gems: { ...strategy.baseline.gems, spend_limit_pct: Number(event.target.value) } })} /><span className={styles.hint}>Does not change the first 100-gem lab unlock.</span></label>}
        </aside>}
    </div>
    {programLane === "workshop" && <div className={styles.guardrails} inert={busy}>
      <label>Spend limit · % of available coins<input aria-label="Workshop spend limit" type="number" min={0} max={100} disabled={locked} value={strategy.baseline.workshop.coin_spend_limit_pct} onChange={event => edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, coin_spend_limit_pct: Number(event.target.value) } })} /></label>
      <div className={styles.banArea} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (locked) { startCopy(); return; } if (drag && "preset" in drag && drag.preset.startsWith("upgrade:")) { const id = drag.preset.slice(8); edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: [...new Set([...strategy.baseline.workshop.banned_upgrade_ids, id])] } }); } setDrag(null); }}>
        <strong>Never Buy <span>{strategy.baseline.workshop.banned_upgrade_ids.length}</span></strong><div className={styles.bannedItems}>{strategy.baseline.workshop.banned_upgrade_ids.map(id => <button key={id} type="button" disabled={locked} aria-label={`Allow ${names.get(id) ?? id}`} onClick={() => edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: strategy.baseline.workshop.banned_upgrade_ids.filter(value => value !== id) } })}>{names.get(id) ?? id}<X size={12} /></button>)}</div>
        <label>Add to Never Buy<select disabled={locked} value="" onChange={event => { if (event.target.value) edit({ ...strategy.baseline, workshop: { ...strategy.baseline.workshop, banned_upgrade_ids: [...new Set([...strategy.baseline.workshop.banned_upgrade_ids, event.target.value])] } }); }}><option value="">Drop an upgrade here, or choose…</option>{catalog.filter(item => !strategy.baseline.workshop.banned_upgrade_ids.includes(item.id)).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      </div>
    </div>}
    {preview && <div className={styles.preview}><p className={styles.hint}>Hypothetical fleet-wide assignment · compare before choosing which emulators to assign.</p><RouteInspector preview={preview} members={members} /></div>}
    <div className={styles.assignmentsSummary}><h3>Fleet assignments</h3>{activeMembers.map(member => { const assignment = saved.assignments?.[member.name]; return <div key={member.name}><Monitor size={15} /><strong>{member.name}</strong><span>{assignment && assignment.account_id === member.account_id ? `${assignment.strategy_name} · v${assignment.strategy_version}` : assignment ? "Account changed · assignment inactive" : "Current fleet route"}</span></div>; })}</div>
    <Dialog.Root open={copyOpen || assignOpen} onOpenChange={open => { if (!open && !busy) { setCopyOpen(false); setAssignOpen(false); } }}><Dialog.Portal><Dialog.Backdrop className={styles.modalBackdrop} /><Dialog.Popup className={`${styles.studio} ${styles.modal}`}>
      <div className={styles.modalHeading}><Dialog.Title>{copyOpen ? copyMode === "scratch" ? "Create strategy from scratch" : "Create your strategy" : "Assign saved strategy"}</Dialog.Title><button className={styles.iconButton} disabled={busy} type="button" aria-label="Close dialog" onClick={() => { setCopyOpen(false); setAssignOpen(false); }}><X size={18} /></button></div>
      {copyOpen ? <><Dialog.Description className={styles.hint}>{copyMode === "scratch" ? "Start with empty, non-spending purchase lanes. Add blocks, then save before assigning." : `Start from ${strategy.name}. The original stays protected.`}</Dialog.Description><label className={styles.fields}>Strategy name<input autoFocus maxLength={80} value={copyName} onChange={event => setCopyName(event.target.value)} onKeyDown={event => { if (event.key === "Enter") createCopy(); }} /></label></>
        : <><Dialog.Description className={styles.hint}>{strategy.name} · v{strategy.version}. Applies at the next safe decision.</Dialog.Description><button type="button" disabled={busy} className={styles.allFleet} aria-pressed={activeMembers.every(member => targets.includes(member.name))} onClick={() => setTargets(targets.length === activeMembers.length ? [] : activeMembers.map(member => member.name))}><Monitor size={17} />Entire current fleet</button>
          <div className={styles.memberChoices}>{activeMembers.map(member => <button key={member.name} type="button" aria-pressed={targets.includes(member.name)} onClick={() => setTargets(targets.includes(member.name) ? targets.filter(name => name !== member.name) : [...targets, member.name])}><Monitor size={17} /><strong>{member.name}</strong><span>{member.account_id}</span></button>)}</div></>}
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div className={styles.modalActions}><button type="button" className={styles.button} disabled={busy} onClick={() => { setCopyOpen(false); setAssignOpen(false); }}>Cancel</button><button type="button" className={styles.primaryButton} disabled={busy || (!copyOpen && !targets.length)} onClick={() => { if (copyOpen) createCopy(); else void assign(); }}>{copyOpen ? copyMode === "scratch" ? "Create blank strategy" : "Create editable copy" : `Assign to ${targets.length} emulator${targets.length === 1 ? "" : "s"}`}</button></div>
    </Dialog.Popup></Dialog.Portal></Dialog.Root>
  </section>;
}
