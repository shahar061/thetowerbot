import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlanGraph } from "./PlanGraph";
import { valuePlan, weightedPlan } from "./planFixtures";

describe("PlanGraph", () => {
  it("shows budget, blocks, weighted odds and the drawn buy", () => {
    render(<PlanGraph plan={weightedPlan()} stale={false} />);
    const budget = within(screen.getByRole("region", { name: "Budget" }));
    expect(budget.getByText("640")).toBeInTheDocument();
    expect(budget.getByText("480")).toBeInTheDocument();
    const blocks = within(screen.getByRole("region", { name: "Strategy blocks" }));
    expect(blocks.getByRole("listitem", { name: "Unlock Defense Absolute: done" })).toBeInTheDocument();
    expect(blocks.getByRole("listitem", { name: "Turtle core: matched" })).toBeInTheDocument();
    expect(blocks.getByRole("listitem", { name: "Wait: not reached" })).toBeInTheDocument();
    const candidates = within(screen.getByRole("region", { name: "Candidates" }));
    const drawn = candidates.getByRole("listitem", { name: "Defense Absolute" });
    expect(drawn.textContent).toContain("33.3%");
    expect(drawn.textContent).toContain("drawn");
    expect(candidates.getByRole("listitem", { name: "Health" }).textContent).toContain("40.0%");
    expect(candidates.getByText("Seeded draw · rolled 0.412")).toBeInTheDocument();
    expect(candidates.getAllByText("over wallet share")).toHaveLength(1);
    const decision = within(screen.getByRole("region", { name: "Decision" }));
    expect(decision.getByText("Buy")).toBeInTheDocument();
    expect(decision.getByText("Defense Absolute")).toBeInTheDocument();
  });

  it("shows the value ranking without odds and says there is no draw", () => {
    render(<PlanGraph plan={valuePlan()} stale={false} />);
    const candidates = within(screen.getByRole("region", { name: "Candidates" }));
    expect(candidates.getByRole("listitem", { name: "Coins / Kill Bonus" }).textContent).toContain("100%");
    expect(candidates.getByRole("listitem", { name: "Health" }).textContent).toContain("weight share");
    expect(screen.getAllByText(/No random draw/).length).toBeGreaterThan(0);
    expect(within(screen.getByRole("region", { name: "Decision" })).getByText("Save coins")).toBeInTheDocument();
    expect(screen.getByText(/3.33K short/)).toBeInTheDocument();
  });

  it("dims a stale plan behind a banner", () => {
    render(<PlanGraph plan={weightedPlan()} stale />);
    expect(screen.getByRole("status").textContent).toContain("Strategy changed since this decision");
  });

  it("labels a paused Workshop and shows the underlying pick", () => {
    const plan = weightedPlan({ override: "workshop_paused" });
    plan.evaluation = { ...plan.evaluation, decision: { ...plan.evaluation.decision!, state: "save_coins", item: null,
      upgrade_id: null, price: null, reason: "Workshop paused: saving coins for labs" } };
    render(<PlanGraph plan={plan} stale={false} />);
    const decision = within(screen.getByRole("region", { name: "Decision" }));
    expect(decision.getByText("Workshop paused")).toBeInTheDocument();
    expect(decision.getByText("Underlying pick: Defense Absolute")).toBeInTheDocument();
  });

  it("renders a legacy trace with no steps or candidates", () => {
    const plan = weightedPlan({ strategy: { id: null, name: "Baseline", mode: "legacy_planner" } });
    const trace = { ...plan.evaluation.trace };
    delete trace.steps;
    delete trace.candidates;
    delete trace.selection;
    plan.evaluation = { ...plan.evaluation, trace };
    render(<PlanGraph plan={plan} stale={false} />);
    expect(screen.getByText("This strategy doesn't use blocks (legacy planner).")).toBeInTheDocument();
    expect(screen.getByText("No candidates recorded for this decision.")).toBeInTheDocument();
  });

  it("says no blocks were evaluated when a blocks strategy has no steps", () => {
    const plan = weightedPlan();
    plan.evaluation = { ...plan.evaluation, trace: { ...plan.evaluation.trace, steps: [] } };
    render(<PlanGraph plan={plan} stale={false} />);
    expect(screen.getByText("No blocks were evaluated for this decision.")).toBeInTheDocument();
    expect(screen.queryByText(/doesn't use blocks/)).not.toBeInTheDocument();
  });

  it("measures the decision against the spend ceiling, not the full wallet", () => {
    const plan = valuePlan();
    // Wallet 2940 holds the price, but the ceiling (wallet - jar) 2530 does not.
    plan.evaluation = { ...plan.evaluation, decision: { ...plan.evaluation.decision!, price: 2700, wallet_coins: 2940 } };
    render(<PlanGraph plan={plan} stale={false} />);
    const decision = within(screen.getByRole("region", { name: "Decision" }));
    expect(decision.getByText("spendable 2.53K")).toBeInTheDocument();
    expect(decision.getByText("170 short")).toBeInTheDocument();
    expect(decision.queryByText("affordable")).not.toBeInTheDocument();
  });
});
