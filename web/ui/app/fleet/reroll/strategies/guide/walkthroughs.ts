export type WalkStep = { id: string; title: string; detail: string };
export type Scenario = { id: string; label: string; active: string; reason: string };
export type Walkthrough = { id: string; title: string; lane: string; steps: WalkStep[]; scenarios: Scenario[] };

export const WALKTHROUGHS: Walkthrough[] = [
  { id: "turtle-workshop", title: "Turtle · Workshop", lane: "Coins, between runs", steps: [
      { id: "economy", title: "Budget · Early economy", detail: "Save for goal → weighted utility pool, 350 target / 400 ceiling" },
      { id: "objectives", title: "Save for goal · Objectives", detail: "Unlock Defense → Def. Abs (5) → Unlock Thorns → Thorns → 51%, then Cash Bonus · Coins/Kill · Health" },
      { id: "cheap", title: "While saving for Thorns · Cheap defense", detail: "Def. Abs at ≤ 80% of the Thorns price" },
      { id: "filler", title: "While saving · Filler", detail: "Utility / attack at ≤ 20% of wallet, capped" },
    ], scenarios: [
      { id: "early", label: "Early account", active: "economy", reason: "Utility spend 200 of 350: the budget is open, so the utility pool buys Unlock Cash Bonuses." },
      { id: "objectives", label: "Budget done", active: "objectives", reason: "Utility spend reached 350, so the budget passes; Defense Absolute (1 of 5) is affordable and bought." },
      { id: "saving", label: "Saving for Thorns", active: "cheap", reason: "Thorns costs 409 with 300 coins: saving. Defense Absolute costs 254, within 80% of the Thorns price (327), so it is bought." },
    ] },
  { id: "turtle-battle", title: "Turtle · Battle", lane: "Cash, during a run", steps: [
      { id: "emergency", title: "If Def. Abs coverage < 1.2", detail: "Buy Def. Abs, or wait if it is not purchasable" },
      { id: "early", title: "If wave ≤ 20", detail: "Cash/Wave → 10 · Coins/Kill → 1.25 · Cash Bonus → 1.25" },
      { id: "thorns", title: "Thorns steps by wave", detail: "≤ 40: 11% · ≤ 80: 21% · ≤ 160: 34% · then 51%" },
      { id: "survival", title: "Survival", detail: "Health · Damage · Attack Speed" },
    ], scenarios: [
      { id: "hit", label: "Heavy hits", active: "emergency", reason: "Enemy damage 200 × (1 − 50%) = 100; Def. Abs 100 covers only 1.0× < 1.2×, so Def. Abs is bought first." },
      { id: "wave15", label: "Wave 15", active: "early", reason: "Coverage is safe and wave 15 ≤ 20, so cash income comes first." },
      { id: "wave50", label: "Wave 50, Thorns 15%", active: "thorns", reason: "Wave 50 is in the ≤ 80 range, so Thorns is raised toward 21%." },
    ] },
];
