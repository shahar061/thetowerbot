"use client";

import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { GUIDE_BLOCKS, type GuideBlockType } from "../strategyBlocks";
import { BlockDiagram, DecisionLoop } from "./diagrams";
import { WALKTHROUGHS, type Walkthrough } from "./walkthroughs";
import styles from "./guide.module.css";

const EXAMPLES: Record<GuideBlockType, string> = {
  buy: "Buy Thorns: unlocked, costs 400, wallet 450 → buys. Wallet 300 → passes to the next block.",
  pool: "Pool Damage → Attack Speed, Damage capped at 2 buys: after 2 Damage buys, Attack Speed is picked. Add a target (Thorns → 51) to stop once the stat reaches 51%.",
  condition: "If current wave ≤ 20 → Then: economy pool. At wave 25 the Else path runs; if the wave is unknown, the decision waits.",
  fallback: "Paths: [Def. Abs pool, Wait]. Def. Abs costs 120 with 90 cash: the pool can't buy, so the Wait stops the decision and the 90 cash is kept.",
  budget: "Target 350, ceiling 400, spent 340: a 70-coin item is rejected (340 + 70 > 400); a 50-coin item is bought.",
  save_for: "Goal Thorns costs 409, wallet 300: records \"Saving for Thorns\" and lets later blocks try a cheaper buy. Only one goal saves at a time.",
  while_saving: "While saving for Thorns → Def. Abs at ≤ 80% of Thorns. Skipped when nothing is saving or the goal is a different upgrade.",
  wait: "Route [Save for Thorns (409), Wait]: with 300 coins nothing is affordable, so the Wait ends the decision and all 300 coins stay in the wallet.",
  native: "A saved copy from before blocks existed may hold 4 sealed policy blocks (starter, economy, objectives, fallback). They still run exactly as before; the Opening and Turtle templates now use the blocks above instead.",
};

function Route({ walk }: { walk: Walkthrough }): React.JSX.Element {
  const [scenario, setScenario] = useState(walk.scenarios[0]);
  return <section className={styles.walk} aria-label={walk.title}>
    <h3>{walk.title} <span>{walk.lane}</span></h3>
    <div className={styles.toggles}>{walk.scenarios.map(item => <button key={item.id} type="button" aria-pressed={item.id === scenario.id} onClick={() => setScenario(item)}>{item.label}</button>)}</div>
    <ol className={styles.route}>{walk.steps.map((step, index) => <li key={step.id} data-active={step.id === scenario.active}>
      <span className={styles.stepNo}>{index + 1}</span><div><strong>{step.title}</strong><p>{step.detail}</p></div></li>)}</ol>
    <p role="status" className={styles.reason}>{scenario.reason}</p>
  </section>;
}

export default function StrategyGuidePage(): React.JSX.Element {
  return <div className={`flex flex-col gap-8 ${styles.guide}`}>
    <PageHeader title="Strategy Studio guide" meta="How routes decide what to buy" />
    <Link href="/fleet/reroll/strategies/" className={styles.back}>← Back to Strategy Studio</Link>
    <section id="lanes"><h2>What a strategy is</h2>
      <p>A strategy is a route of blocks per spending lane. <b>Workshop</b> spends coins between runs; <b>Battle</b> spends cash during a run. Gems and Labs steps are planned and not automated yet.</p></section>
    <section id="loop"><h2>How strategies decide</h2><DecisionLoop />
      <ul className={styles.legend}><li className={styles.buy}><b>Buy</b> the block returns a purchase; after it is confirmed, facts refresh and the route starts again from the top.</li>
        <li className={styles.save}><b>Save</b> a goal is unaffordable; the saving is recorded and later blocks may still buy something cheap.</li>
        <li className={styles.pass}><b>Pass</b> nothing to do here; the next block is tried.</li>
        <li className={styles.wait}><b>Wait</b> missing evidence or an explicit wait; nothing is bought this time.</li></ul></section>
    <section id="blocks"><h2>Blocks</h2><div className={styles.cards}>{GUIDE_BLOCKS.map(block => <article key={block.type} id={`block-${block.type}`} className={styles.card}>
      <h3>{block.title}</h3><BlockDiagram type={block.type} title={block.title} /><p>{block.summary}</p><p className={styles.example}>{EXAMPLES[block.type]}</p></article>)}</div></section>
    <section id="walkthroughs"><h2>Walkthroughs</h2>{WALKTHROUGHS.map(walk => <Route key={walk.id} walk={walk} />)}</section>
    <section id="gotchas"><h2>Good to know</h2><ul className={styles.gotchas}>
      <li>Unknown facts never pick a branch: the route waits until the bot has verified them.</li>
      <li>Being unable to afford something is never a reason to move on to a different goal; use Save for goal and While saving to buy cheaply meanwhile.</li>
      <li>Weighted draws are seeded, so previews and workers make the same pick.</li>
      <li>Never Buy overrides every block, including items inside pools and goals.</li>
      <li>Turtle Battle waits when Defense % or enemy damage is unknown, because Defense Absolute coverage can&apos;t be checked.</li>
      <li>Give any block a name in its settings; the name replaces the generated title on the canvas.</li></ul></section>
  </div>;
}
