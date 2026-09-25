import type { GuideBlockType } from "../strategyBlocks";
import styles from "./guide.module.css";

export type Outcome = "buy" | "save" | "pass" | "wait";
const OUTCOMES: Record<GuideBlockType, Outcome[]> = {
  buy: ["buy", "pass"], pool: ["buy", "pass", "wait"], condition: ["pass", "wait"], fallback: ["buy", "wait", "pass"],
  budget: ["buy", "pass", "wait"], save_for: ["buy", "save", "pass"], while_saving: ["buy", "pass"], wait: ["wait"], native: ["buy", "save", "wait"],
};
const OUTCOME_TEXT: Record<Outcome, string> = { buy: "Buy → refresh", save: "Save → keep looking", pass: "Pass → next block", wait: "Wait → stop" };

export function DecisionLoop(): React.JSX.Element {
  // Each label is pre-split into lines: SVG <text> never wraps, and single-line
  // labels overflowed the 112-wide boxes into their neighbours.
  const nodes = [["Refresh", "facts"], ["Shared", "rules"], ["Walk blocks", "top-down"], ["First block", "that acts wins"], ["One confirmed", "purchase"]];
  return <svg role="img" aria-label="Decision loop" viewBox="0 0 640 220" width="100%" className={styles.diagram}>
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" className={styles.arrowHead} /></marker></defs>
    {nodes.map((label, index) => {
      const x = 12 + index * 126;
      return <g key={label.join(" ")}>
        <rect x={x} y={70} width={112} height={56} rx={12} className={index === 3 ? styles.nodeActive : styles.node} />
        <text x={x + 56} y={94} textAnchor="middle" className={styles.nodeText}>
          {label.map((line, lineIndex) => <tspan key={line} x={x + 56} dy={lineIndex === 0 ? 0 : 14}>{line}</tspan>)}
        </text>
        {index < nodes.length - 1 && <line x1={x + 112} y1={98} x2={x + 126} y2={98} className={styles.edge} markerEnd="url(#arrow)" />}
      </g>;
    })}
    <path d="M 572 126 C 572 196, 68 196, 68 126" className={styles.edgeLoop} markerEnd="url(#arrow)" fill="none" />
    <text x={320} y={206} textAnchor="middle" className={styles.caption}>then decide again</text>
    <text x={320} y={40} textAnchor="middle" className={styles.caption}>Reserves · lab-first · wave-60 stop · Never Buy apply before any block</text>
  </svg>;
}

export function BlockDiagram({ type, title }: { type: GuideBlockType; title: string }): React.JSX.Element {
  const outcomes = OUTCOMES[type];
  return <svg role="img" aria-label={`${title} outcomes`} viewBox="0 0 300 150" width="100%" className={styles.diagram}>
    <rect x={10} y={55} width={110} height={40} rx={10} className={styles.nodeActive} />
    <text x={65} y={80} textAnchor="middle" className={styles.nodeText}>{title}</text>
    {outcomes.map((outcome, index) => {
      const y = 20 + index * (110 / Math.max(outcomes.length - 1, 1));
      const ty = outcomes.length === 1 ? 75 : y;
      return <g key={outcome}>
        <path d={`M120 75 C 150 75, 150 ${ty}, 180 ${ty}`} className={`${styles.edge} ${styles[outcome]}`} fill="none" />
        <text x={186} y={ty + 4} className={`${styles.outcomeText} ${styles[outcome]}`}>{OUTCOME_TEXT[outcome]}</text>
      </g>;
    })}
  </svg>;
}
