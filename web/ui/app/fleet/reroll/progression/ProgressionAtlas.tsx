"use client";

import { useEffect, useId, useMemo, useRef, useState, type PointerEvent } from "react";
import { Compass, Crosshair, GitBranch, List, Maximize2, Minus, Plus, Shield, Sparkles, Swords } from "lucide-react";
import { buildFleetGraph, currentObjective, STATUS_LABELS, type AtlasNode, type WorkerRoadmap } from "./fleetRoadmap";
import { deviceColor } from "@/lib/rerollState";
import styles from "./atlas.module.css";

const VIEW_WIDTH = 1200;
const VIEW_HEIGHT = 670;
const known = (status: string): boolean => ["claimable", "claimed", "verified", "available"].includes(status);

export function ProgressionAtlas({ workers, pending = false, initialWorker = null }: { workers: WorkerRoadmap[]; pending?: boolean; initialWorker?: string | null }): React.JSX.Element {
  const graph = useMemo(() => buildFleetGraph(workers), [workers]);
  const [selected, setSelected] = useState<string | null>(null);
  const [focusedWorker, setFocusedWorker] = useState<string | null>(initialWorker);
  const [list, setList] = useState(false);
  const [manualCamera, setCamera] = useState<{ x: number; y: number; scale: number } | null>(null);
  const drag = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [viewWidth, setViewWidth] = useState(VIEW_WIDTH);
  useEffect(() => {
    const element = svg.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setViewWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [list, graph.nodes.length]);
  const firstObjective = currentObjective(graph.nodes, focusedWorker);
  const camera = manualCamera ?? { x: viewWidth / 2 - (firstObjective?.x ?? viewWidth / 2), y: VIEW_HEIGHT / 2 - (firstObjective?.y ?? VIEW_HEIGHT / 2), scale: 1 };
  const pattern = useId().replace(/:/g, "");
  const chosen = graph.nodes.find((node) => node.id === selected);
  const denominator = workers.filter(({ member }) => member.account_id && member.account_key).length;
  const focus = workers.find(({ member }) => member.name === focusedWorker);
  const selectedPlan = focus?.member.reroll_plan?.account_id === focus?.member.account_id ? focus?.member.reroll_plan : null;
  function select(node: AtlasNode): void { setSelected(node.id); }
  function fit(): void {
    const scale = Math.min(1, viewWidth / graph.width, VIEW_HEIGHT / graph.height);
    setCamera({ x: (viewWidth - graph.width * scale) / 2, y: (VIEW_HEIGHT - graph.height * scale) / 2, scale });
  }
  function zoom(factor: number): void {
    setCamera(() => {
      const current = camera;
      const scale = Math.max(.18, Math.min(2.2, current.scale * factor));
      return { scale, x: viewWidth / 2 - (viewWidth / 2 - current.x) * scale / current.scale, y: VIEW_HEIGHT / 2 - (VIEW_HEIGHT / 2 - current.y) * scale / current.scale };
    });
  }
  function objective(): void {
    const node = currentObjective(graph.nodes, focusedWorker);
    if (node) { select(node); setCamera({ x: viewWidth / 2 - node.x, y: VIEW_HEIGHT / 2 - node.y, scale: 1 }); }
  }
  function pointerDown(event: PointerEvent<SVGSVGElement>): void {
    if ((event.target as Element).closest('[role="button"]')) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, px: camera.x, py: camera.y };
  }
  function pointerMove(event: PointerEvent<SVGSVGElement>): void {
    if (!drag.current || !svg.current) return;
    const factor = viewWidth / svg.current.getBoundingClientRect().width;
    const x = drag.current.px + (event.clientX - drag.current.x) * factor;
    const y = drag.current.py + (event.clientY - drag.current.y) * factor;
    setCamera(() => ({ ...camera, x: Math.max(-graph.width * camera.scale + 150, Math.min(viewWidth - 150, x)), y: Math.max(-graph.height * camera.scale + 150, Math.min(VIEW_HEIGHT - 150, y)) }));
  }
  return <div className={styles.atlas}>
    <section aria-label="Reroll ladder" className={styles.ladder}>
      <div className={styles.chapter}><span className={styles.chapterIcon}><Swords size={20} /></span><div><small>01 / OPENING</small><strong>Tier 1 · Wave 20</strong><span>Establish the first foothold</span></div></div>
      <span className={styles.ladderLine} aria-hidden="true" />
      <div className={styles.chapter}><span className={styles.chapterIcon}><Shield size={20} /></span><div><small>02 / TURTLE</small><strong>Tier 1 · Wave 60</strong><span>Survive toward the stone reward</span></div></div>
      <span className={styles.ladderLine} aria-hidden="true" />
      <div className={styles.chapter}><span className={styles.chapterIcon}><Sparkles size={20} /></span><div><small>03 / THE CHOICE</small><strong>Ultimate Weapon</strong><span>Manual choice · operator required</span></div></div>
      <p>Reroll route · wave progress does not verify a feature unlock or a claimed reward.</p>
    </section>
    <section className={styles.workerBar} aria-label="Fleet evidence">
      <button className={!focusedWorker ? styles.activeWorker : ""} onClick={() => { setFocusedWorker(null); setCamera(null); }}>All workers <span>{workers.length}</span></button>
      {workers.map(({ member, error, roadmap }, index) => <button key={member.name} className={focusedWorker === member.name ? styles.activeWorker : ""} onClick={() => { setFocusedWorker(member.name); setCamera(null); }} aria-pressed={focusedWorker === member.name}>
        <i style={{ background: deviceColor(member.name) }}>{index + 1}</i><span>{member.name}<small>{error ? "Evidence unavailable" : roadmap ? `T1 best ${roadmap.best_waves["1"] ?? "unknown"}` : "Awaiting evidence"}</small></span>
      </button>)}
    </section>
    {workers.some((worker) => worker.error) && <div className={styles.errors}>{workers.filter((worker) => worker.error).map(({ member, error }) => <p key={member.name}><strong>{member.name}</strong> <span>{error}</span></p>)}</div>}
    {focus && <section aria-label="Strategy intent" className={styles.intent}><GitBranch size={18} /><strong>{focus.member.name} · strategy intent</strong><span>{selectedPlan?.goal ?? "No identity-matched strategy observed"}</span><span aria-hidden="true">⇢</span><span>{selectedPlan?.item ? `Next workshop objective: ${selectedPlan.item}` : "Next purchase unknown"}</span><small>Recommendation · not an unlock prerequisite</small>
      {!!selectedPlan?.next_purchases?.length && <div className={styles.objectives} aria-label="Projected strategy objectives">{selectedPlan.next_purchases.filter((purchase) => purchase.account_id === focus.member.account_id).slice(0, 4).map((purchase) => <span key={`${purchase.position}-${purchase.upgrade_id}`}><small>{purchase.category} / PROJECTED {purchase.position}</small><strong>{purchase.item}</strong></span>)}</div>}
    </section>}
    <section className={styles.mapPanel} aria-label="Milestone atlas">
      <div className={styles.toolbar}><div><Compass size={18} /><strong>Unlock constellations</strong><span>{graph.nodes.length} milestones · {graph.groups.length} categories</span></div>
        <div><button aria-label="Map view" aria-pressed={!list} onClick={() => setList(false)}><GitBranch size={16} /> Map</button><button aria-label="List view" aria-pressed={list} onClick={() => setList(true)}><List size={16} /> List</button></div>
      </div>
      {!graph.nodes.length ? <div className={styles.empty}>{pending ? "Assembling the fleet atlas…" : "No milestone catalog available. A verified worker roadmap is needed to draw the atlas."}</div> : list ? <div className={styles.list}>
        {graph.groups.map((group) => <section key={group.name}><h2>{group.name} <span>{group.count}</span></h2><div>{graph.nodes.filter((node) => node.group === group.name).map((node) => <button key={node.id} onClick={() => select(node)} aria-label={`Inspect ${node.title}`}><strong>{node.title}</strong><span>{node.tier && node.wave ? `T${node.tier} · W${node.wave}` : node.kind}</span><span>{node.verified}/{denominator} verified</span></button>)}</div></section>)}
      </div> : <div className={styles.canvas}>
        <svg ref={svg} viewBox={`0 0 ${viewWidth} ${VIEW_HEIGHT}`} aria-label="Interactive milestone map" onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
          <defs><pattern id={pattern} width="32" height="32" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".7" fill="currentColor" opacity=".13" /></pattern><radialGradient id={`${pattern}-island`}><stop offset="0%" stopColor="var(--atlas-accent)" stopOpacity=".1" /><stop offset="65%" stopColor="var(--atlas-accent)" stopOpacity=".035" /><stop offset="100%" stopColor="var(--atlas-accent)" stopOpacity="0" /></radialGradient></defs>
          <rect width="100%" height="100%" fill={`url(#${pattern})`} />
          <g transform={`translate(${camera.x} ${camera.y}) scale(${camera.scale})`}>
            {graph.groups.map((group, index) => <g key={group.name} className={styles.cluster}>
              <ellipse cx={group.x + group.width / 2} cy={group.y + group.height / 2} rx={group.width / 2 - 25} ry={group.height / 2 - 25} fill={`url(#${pattern}-island)`} />
              <circle cx={group.x + group.width / 2} cy={group.y + group.height / 2} r="82" className={styles.islandSeal} />
              <text x={group.x + group.width / 2} y={group.y + group.height / 2 - 16} textAnchor="middle" className={styles.clusterCount}>CONSTELLATION {String(index + 1).padStart(2, "0")}</text>
              <text x={group.x + group.width / 2} y={group.y + group.height / 2 + 8} textAnchor="middle" className={styles.clusterTitle}>{group.name.toUpperCase()}</text>
              <text x={group.x + group.width / 2} y={group.y + group.height / 2 + 32} textAnchor="middle" className={styles.clusterCount}>{group.count} MILESTONES</text>
            </g>)}
            {graph.edges.map((edge) => { const from = graph.nodes.find((node) => node.id === edge.from)!; const to = graph.nodes.find((node) => node.id === edge.to)!; const reached = to.accounts.some((account) => (!focusedWorker || account.member.name === focusedWorker) && account.status === "verified"); return <path key={`${edge.from}-${edge.to}`} d={`M ${from.x} ${from.y} C ${from.x} ${from.y + 80}, ${to.x} ${to.y - 80}, ${to.x} ${to.y}`} className={reached ? styles.reachedEdge : styles.edge} />; })}
            {graph.nodes.map((node) => {
              const active = node.id === selected;
              const account = node.accounts.find((entry) => entry.member.name === focusedWorker);
              const illuminated = account ? known(account.status) : node.accounts.some((entry) => known(entry.status));
              const unknown = account ? account.status === "unknown" : node.accounts.every((entry) => entry.status === "unknown");
              return <g key={node.id} transform={`translate(${node.x} ${node.y})`} role="button" tabIndex={0} aria-label={`Select ${node.title}, ${node.verified} of ${denominator} verified`} aria-pressed={active} onFocus={() => {
                const x = node.x * camera.scale + camera.x;
                const y = node.y * camera.scale + camera.y;
                if (x < 60 || x > viewWidth - 60 || y < 60 || y > VIEW_HEIGHT - 90) setCamera({ x: viewWidth / 2 - node.x, y: VIEW_HEIGHT / 2 - node.y, scale: 1 });
              }} onClick={() => select(node)} onKeyDown={(event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(node); } }} className={`${styles.node} ${active ? styles.selected : ""} ${illuminated ? styles.illuminated : ""} ${unknown ? styles.unknown : ""}`}>
                <title>{node.title} · {node.accounts.map((entry) => `${entry.member.name}: ${STATUS_LABELS[entry.status]}`).join("; ")}</title>
                <rect className={styles.hitTarget} x="-122" y="-49" width="244" height="135" rx="10" />
                <circle className={styles.orbit} r="43" />
                <path className={styles.frame} d={node.kind === "unlock" ? "M0 -35 L31 -18 L31 18 L0 35 L-31 18 L-31 -18 Z" : "M0 -31 L31 0 L0 31 L-31 0 Z"} />
                <text className={styles.glyph} y="6" textAnchor="middle">{node.kind === "unlock" ? "✦" : node.kind === "activity" ? "◇" : "○"}</text>
                {camera.scale >= .55 && <><text className={styles.nodeTitle} y="65" textAnchor="middle">{node.title.length > 31 ? `${node.title.slice(0, 29)}…` : node.title}</text><text className={styles.nodeMeta} y="85" textAnchor="middle">{node.tier && node.wave ? `T${node.tier} · W${node.wave}  /  ` : ""}{node.verified}/{denominator} verified</text></>}
                {node.accounts.filter((entry) => entry.member.account_id && entry.member.account_key).map((entry) => { const index = workers.findIndex((worker) => worker.member.name === entry.member.name); const angle = (-145 + index * 36) * Math.PI / 180; return <g key={entry.member.name} transform={`translate(${Math.cos(angle) * 44} ${Math.sin(angle) * 44})`} opacity={focusedWorker && focusedWorker !== entry.member.name ? .3 : 1}><circle r="10" fill={known(entry.status) ? deviceColor(entry.member.name) : "var(--card)"} stroke={deviceColor(entry.member.name)} strokeWidth="1.5" /><text y="3.5" textAnchor="middle" fontSize="10" fill={known(entry.status) ? "#ffffff" : "var(--foreground)"} fontWeight="700">{index + 1}</text></g>; })}
              </g>;
            })}
          </g>
        </svg>
        <div className={styles.mapHint}>Drag to explore · select a node to compare{camera.scale < .55 && " · zoom in for labels"}</div>
        <div className={styles.controls}><button aria-label="Zoom out" onClick={() => zoom(.8)}><Minus size={17} /></button><span>{Math.round(camera.scale * 100)}%</span><button aria-label="Zoom in" onClick={() => zoom(1.25)}><Plus size={17} /></button><button aria-label="Fit atlas" onClick={fit}><Maximize2 size={17} /></button><button aria-label="Focus current objective" onClick={objective}><Crosshair size={17} /></button></div>
      </div>}
      <div className={styles.legend}><span><i className={styles.solidLine} /> Confirmed prerequisite</span><span><i className={styles.legendNode} /> Filled marker: reached or available</span><span><i className={styles.unknownNode} /> Unknown evidence</span><span>Verified counts require explicit verification</span></div>
    </section>
    {chosen ? <section aria-label={`${chosen.title} comparison`} className={styles.comparison}>
      <header><div><small>{chosen.group} / FLEET COMPARISON</small><h2>{chosen.title}</h2><p>{chosen.description}</p></div><button onClick={() => setSelected(null)} aria-label="Close milestone comparison">×</button></header>
      <div className={styles.comparisonGrid}>{chosen.accounts.map((account, index) => {
        const plan = account.member.reroll_plan?.account_id === account.member.account_id ? account.member.reroll_plan : null;
        return <article key={account.member.name}><h3><i style={{ background: deviceColor(account.member.name) }}>{index + 1}</i>{account.member.name}<small>{account.member.account_id ?? "Unverified account"}</small></h3>
          <strong className={styles.status} data-status={account.status}>{STATUS_LABELS[account.status]}</strong>
          <dl><dt>Observed progress</dt><dd>{account.progress ? `${account.progress.current} / ${account.progress.target}` : "Unknown"}</dd><dt>Prerequisites still unverified</dt><dd>{account.missing.length ? account.missing.map((id) => graph.nodes.find((node) => node.id === id)?.title ?? id).join(", ") : account.requires.length ? "None" : "No catalog prerequisites"}</dd><dt>Next workshop decision</dt><dd>{plan?.item ? `${plan.item} · ${plan.state.replace(/_/g, " ")}` : "Unknown"}</dd><dt>Milestone observation age</dt><dd>Not supplied by roadmap</dd>{plan && <><dt>Strategy observed</dt><dd>{new Date(plan.observed_at * 1000).toLocaleString()}</dd></>}</dl>
          {account.error && <p className={styles.workerError}>{account.error}</p>}
        </article>;
      })}</div>
    </section> : graph.nodes.length > 0 && <div className={styles.selectionPrompt}><Crosshair size={20} /><span>Select a milestone to compare every worker’s evidence, prerequisites, and next decision.</span></div>}
  </div>;
}
