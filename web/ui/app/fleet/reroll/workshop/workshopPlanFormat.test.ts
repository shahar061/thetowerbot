import { describe, expect, it } from "vitest";
import { valuePlan, weightedPlan } from "./planFixtures";
import { blockName, candidateCaption, linkWidth, matchedStep, percentLabel, rejectedLines, summarySentence } from "./workshopPlanFormat";

describe("summarySentence", () => {
  it("names the draw odds for a weighted buy", () => {
    expect(summarySentence(weightedPlan())).toBe(
      "Buy Defense Absolute for 198 coins. Turtle core drew it at 33% odds from 3 eligible upgrades.");
  });
  it("says there is no random draw for a value save", () => {
    expect(summarySentence(valuePlan())).toBe(
      "Saving for Coins / Kill Bonus (2.53K / 5.86K coins). Save for the best value ranked it best value at 293.0 coins per weight point. No random draw.");
  });
  it("reports a paused Workshop with the published reason", () => {
    const plan = weightedPlan({ override: "workshop_paused" });
    plan.evaluation = { ...plan.evaluation, decision: { ...plan.evaluation.decision!, state: "save_coins", item: null,
      upgrade_id: null, price: null, reason: "Workshop paused: saving coins for the next automated lab (2500 coins)." } };
    expect(summarySentence(plan)).toBe("Workshop paused: saving coins for the next automated lab (2500 coins).");
  });
  it("reports waiting when there is no decision", () => {
    const plan = weightedPlan();
    plan.evaluation = { ...plan.evaluation, decision: null, trace: { ...plan.evaluation.trace, reason: "Wait block reached" } };
    expect(summarySentence(plan)).toBe("Waiting: Wait block reached");
  });
});

describe("percentLabel", () => {
  const rows = weightedPlan().evaluation.trace.candidates!;
  it("shows weighted odds with one decimal", () => {
    expect(rows.map(row => percentLabel(row, "weighted", 75))).toEqual(["40.0%", "33.3%", "26.7%"]);
  });
  it("shows 100% for the value pick and weight share for the rest", () => {
    const value = valuePlan().evaluation.trace.candidates!;
    expect(value.map(row => percentLabel(row, "value", 46))).toEqual(["100%", "30%", "26%"]);
  });
  it("shows nothing for priority picks", () => {
    expect(percentLabel(rows[0], "priority", 0)).toBe("");
  });
});

describe("rejectedLines", () => {
  const names = { lifesteal: "Lifesteal", knockback_chance: "Knockback Chance", orbs: "Orbs", coins_per_kill_bonus: "Coins / Kill Bonus" };
  it("strips block prefixes, names upgrades, drops duplicates and the value ranking", () => {
    expect(rejectedLines([
      "core: lifesteal over wallet share", "core: lifesteal over wallet share",
      "knockback_chance: blocked by Never Buy", "bv2.value: value ranking coins_per_kill_bonus 293.0",
      "core: confirmed purchase counts unavailable",
      "Coins / Wave replaces Coins / Kill Bonus until best Tier 1 wave 60 (best 38)",
      "core: mystery_upgrade over price cap",
    ], names)).toEqual([
      { subject: "Lifesteal", reason: "over wallet share" },
      { subject: "Knockback Chance", reason: "blocked by Never Buy" },
      { subject: null, reason: "confirmed purchase counts unavailable" },
      { subject: null, reason: "Coins / Wave replaces Coins / Kill Bonus until best Tier 1 wave 60 (best 38)" },
      { subject: null, reason: "mystery_upgrade over price cap" },
    ]);
  });
});

describe("small helpers", () => {
  it("names a block by its label, else by its kind", () => {
    const steps = weightedPlan().evaluation.trace.steps!;
    expect(steps.map(blockName)).toEqual(["Unlock Defense Absolute", "Turtle core", "Wait"]);
    expect(matchedStep(steps)?.block_id).toBe("core");
    expect(matchedStep([])).toBeNull();
  });
  it("scales weighted links by odds and marks the value pick", () => {
    const [health, absolute] = weightedPlan().evaluation.trace.candidates!;
    expect(linkWidth(health, "weighted")).toBeCloseTo(10.8);
    expect(linkWidth(absolute, "value")).toBe(7);
    expect(linkWidth(health, "value")).toBe(1.5);
  });
  it("captions each selection", () => {
    expect(candidateCaption("weighted", 0.412)).toBe("Seeded draw · rolled 0.412");
    expect(candidateCaption("weighted", null)).toBe("Choice kept from earlier this visit");
    expect(candidateCaption("value", null)).toBe("Lowest price ÷ weight wins. No random draw.");
    expect(candidateCaption("priority", null)).toBe("Considered in order. The first that fits is chosen.");
  });
});
