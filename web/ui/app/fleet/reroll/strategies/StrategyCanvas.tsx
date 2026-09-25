"use client";

import { ArrowDown, ChevronDown, ChevronUp, GitBranch, GripVertical, Pause, PiggyBank, Plus, ShoppingBag, Sparkles, Wallet } from "lucide-react";
import type { StrategyBlock } from "@/lib/strategyStudio";
import { blockDetail, blockTitle, childGroups, type BlockTarget } from "./strategyBlocks";
import styles from "./studio.module.css";

export type BlockDrag = { preset: string } | { id: string };

const phaseNames: Record<string, string> = { starter: "Survival Starter", economy: "Early Economy",
  objectives: "Upgrade objectives", fallback: "Cheap fallback" };
const completionText: Record<string, string> = { starter: "When no eligible starter upgrades remain or Tier 1 reaches Wave 20",
  economy: "When the utility allocation is reached", objectives: "When no targeted objective remains",
  fallback: "When no cheap fallback is eligible" };

export function StrategyCanvas({ blocks, names, selected, locked, target, onSelect, onTarget, onDrop, onMove, onDrag,
  parent = null, branch = "root" }: {
  blocks: StrategyBlock[]; names: Map<string, string>; selected: string | null; locked: boolean; target: BlockTarget;
  onSelect: (id: string) => void; onTarget: (target: BlockTarget) => void; onDrop: (target: BlockTarget) => void;
  onMove: (id: string, direction: -1 | 1) => void; onDrag: (value: BlockDrag | null) => void;
  parent?: string | null; branch?: BlockTarget["branch"];
}): React.JSX.Element {
  // The save-for goal holds exactly one block, edited in the inspector: no drop targets.
  const insertion = (index: number): React.JSX.Element | null => branch === "goal" ? null : <button type="button" aria-label={parent ? `Insert into ${branch} at ${index + 1}` : `Insert block at ${index + 1}`}
    onClick={() => onTarget({ parent, branch, index })} onDragOver={event => event.preventDefault()}
    onDrop={event => { event.preventDefault(); event.stopPropagation(); onDrop({ parent, branch, index }); }}
    className={`${styles.connector} ${target.parent === parent && target.branch === branch && target.index === index ? styles.target : ""}`}>
    <Plus size={13} /><span>Add here</span>
  </button>;
  return <div className={parent ? styles.nestedPath : styles.path} aria-label={parent ? `${branch} path` : "Strategy block path"}>
    {!blocks.length && <div className={styles.emptyPath}><strong>No purchases configured</strong>{!parent && <p>Choose a block from the side palette to add the first step.</p>}</div>}
    {blocks.map((block, index) => {
      const title = blockTitle(block, names);
      const Icon = block.type === "budget" ? Wallet : block.type === "save_for" || block.type === "while_saving" ? PiggyBank :
        block.type === "condition" ? GitBranch : block.type === "wait" ? Pause : block.type === "pool" ? Sparkles : ShoppingBag;
      const kind = block.type === "condition" || block.type === "pool" ? "logic"
        : block.type === "fallback" || block.type === "wait" || block.type === "budget" || block.type === "save_for" || block.type === "while_saving" ? "flow" : "buy";
      const nextBlock = blocks[index + 1];
      return <div key={block.id}>
        {insertion(index)}
        <div className={`${styles.block} ${styles[kind]} ${selected === block.id ? styles.selectedBlock : ""}`}
          data-testid={`block-${block.id}`} draggable={!locked}
          onDragStart={event => { event.stopPropagation(); onDrag({ id: block.id }); event.dataTransfer?.setData("text/plain", block.id); }}
          onDragEnd={() => onDrag(null)}>
          <button type="button" aria-pressed={selected === block.id} onClick={() => onSelect(block.id)} className={styles.blockMain}>
            <span className={styles.blockKind}><Icon size={14} />{block.type === "native" ? "LEGACY BUILT-IN" : kind.toUpperCase()}<GripVertical size={14} className={styles.grip} /></span>
            <strong>{title}</strong><span className={styles.blockDetail}>{blockDetail(block)}</span>
            {block.type === "pool" && (block.targets || block.level_caps) && <span className={styles.chips}>
              {Object.entries(block.targets ?? {}).map(([id, value]) => <span key={`t-${id}`}>{names.get(id) ?? id} → {value}</span>)}
              {Object.entries(block.level_caps ?? {}).map(([id, cap]) => <span key={`c-${id}`}>{names.get(id) ?? id} ≤ {cap.base}{cap.per_level_of ? ` + ${cap.step ?? 1}/${names.get(cap.per_level_of) ?? cap.per_level_of}` : ""}</span>)}
            </span>}
            {block.type === "native" && block.phase === "objectives" && <span className={styles.chips}>{block.policy === "turtle"
              ? <><span>Def. Abs · base target 5 buys</span><span>Thorns · 51% stat</span></>
              : <><span>Damage + Attack Speed</span><span>Unlocks + economy</span></>}</span>}
          </button>
          {!locked && <div className={styles.blockTools}><button type="button" disabled={index === 0} onClick={() => onMove(block.id, -1)} aria-label={`Move ${title} up`}><ChevronUp size={14} /></button>
            <button type="button" disabled={index === blocks.length - 1} onClick={() => onMove(block.id, 1)} aria-label={`Move ${title} down`}><ChevronDown size={14} /></button></div>}
          {childGroups(block).map(group => <div key={group.branch} className={styles.branch}>
            <div className={styles.branchHeader}><span><ArrowDown size={12} />{group.label}</span>{group.branch !== "goal" && <button type="button" onClick={() => onTarget({ parent: block.id, branch: group.branch, index: group.blocks.length })}>Add to {group.label}</button>}</div>
            <StrategyCanvas {...{ names, selected, locked, target, onSelect, onTarget, onDrop, onMove, onDrag }} blocks={group.blocks} parent={block.id} branch={group.branch} />
          </div>)}
        </div>
        {block.type === "native" && nextBlock?.type === "native" && <p className={styles.hint}>
          {block.phase === "starter" && block.policy === "turtle" ? "Turtle skips Survival Starter" : completionText[block.phase] ?? "When this phase completes"}
          {" → "}{phaseNames[nextBlock.phase] ?? nextBlock.phase}
        </p>}
      </div>;
    })}
    {insertion(blocks.length)}
  </div>;
}
