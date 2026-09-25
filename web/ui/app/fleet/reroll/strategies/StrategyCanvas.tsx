"use client";

import { ArrowDown, ChevronDown, ChevronUp, GitBranch, GripVertical, Pause, Plus, ShoppingBag, Sparkles } from "lucide-react";
import type { StrategyBlock } from "@/lib/strategyStudio";
import { blockDetail, blockTitle, childGroups, type BlockTarget } from "./strategyBlocks";
import styles from "./studio.module.css";

export type BlockDrag = { preset: string } | { id: string };

export function StrategyCanvas({ blocks, names, selected, locked, target, onSelect, onTarget, onDrop, onMove, onDrag,
  parent = null, branch = "root" }: {
  blocks: StrategyBlock[]; names: Map<string, string>; selected: string | null; locked: boolean; target: BlockTarget;
  onSelect: (id: string) => void; onTarget: (target: BlockTarget) => void; onDrop: (target: BlockTarget) => void;
  onMove: (id: string, direction: -1 | 1) => void; onDrag: (value: BlockDrag | null) => void;
  parent?: string | null; branch?: BlockTarget["branch"];
}): React.JSX.Element {
  const insertion = (index: number): React.JSX.Element => <button type="button" aria-label={parent ? `Insert into ${branch} at ${index + 1}` : `Insert block at ${index + 1}`}
    onClick={() => onTarget({ parent, branch, index })} onDragOver={event => event.preventDefault()}
    onDrop={event => { event.preventDefault(); event.stopPropagation(); onDrop({ parent, branch, index }); }}
    className={`${styles.connector} ${target.parent === parent && target.branch === branch && target.index === index ? styles.target : ""}`}>
    <Plus size={13} /><span>Add here</span>
  </button>;
  return <div className={parent ? styles.nestedPath : styles.path} aria-label={parent ? `${branch} path` : "Strategy block path"}>
    {!blocks.length && <p className={styles.emptyPath}>Choose a block from the side palette.</p>}
    {blocks.map((block, index) => {
      const title = blockTitle(block, names);
      const Icon = block.type === "condition" ? GitBranch : block.type === "wait" ? Pause : block.type === "pool" ? Sparkles : ShoppingBag;
      const kind = block.type === "condition" || block.type === "pool" ? "logic" : block.type === "fallback" || block.type === "wait" ? "flow" : "buy";
      return <div key={block.id}>
        {insertion(index)}
        <div className={`${styles.block} ${styles[kind]} ${selected === block.id ? styles.selectedBlock : ""}`}
          data-testid={`block-${block.id}`} draggable={!locked}
          onDragStart={event => { event.stopPropagation(); onDrag({ id: block.id }); event.dataTransfer?.setData("text/plain", block.id); }}
          onDragEnd={() => onDrag(null)}>
          <button type="button" aria-pressed={selected === block.id} onClick={() => onSelect(block.id)} className={styles.blockMain}>
            <span className={styles.blockKind}><Icon size={14} />{block.type === "native" ? "BUILT-IN POLICY" : kind.toUpperCase()}<GripVertical size={14} className={styles.grip} /></span>
            <strong>{title}</strong><span className={styles.blockDetail}>{blockDetail(block)}</span>
            {block.type === "native" && block.phase === "objectives" && <span className={styles.chips}>{block.policy === "turtle"
              ? <><span>Def. Abs · base target 5 buys</span><span>Thorns · 51% stat</span></>
              : <><span>Damage + Attack Speed</span><span>Unlocks + economy</span></>}</span>}
          </button>
          {!locked && <div className={styles.blockTools}><button type="button" disabled={index === 0} onClick={() => onMove(block.id, -1)} aria-label={`Move ${title} up`}><ChevronUp size={14} /></button>
            <button type="button" disabled={index === blocks.length - 1} onClick={() => onMove(block.id, 1)} aria-label={`Move ${title} down`}><ChevronDown size={14} /></button></div>}
          {childGroups(block).map(group => <div key={group.branch} className={styles.branch}>
            <div className={styles.branchHeader}><span><ArrowDown size={12} />{group.label}</span><button type="button" onClick={() => onTarget({ parent: block.id, branch: group.branch, index: group.blocks.length })}>Add to {group.label}</button></div>
            <StrategyCanvas {...{ names, selected, locked, target, onSelect, onTarget, onDrop, onMove, onDrag }} blocks={group.blocks} parent={block.id} branch={group.branch} />
          </div>)}
        </div>
      </div>;
    })}
    {insertion(blocks.length)}
  </div>;
}
