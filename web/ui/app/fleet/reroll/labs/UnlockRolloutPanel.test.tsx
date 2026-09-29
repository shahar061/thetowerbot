import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { UnlockRolloutPanel } from "./UnlockRolloutPanel";
import type { UnlockRolloutRow } from "@/lib/labs";

const row = (slot: number, stage: UnlockRolloutRow["stage"], extra: Partial<UnlockRolloutRow> = {}): UnlockRolloutRow =>
  ({ slot, stage, canary_worker: null, dry_runs: 0, price: 100, halted_reason: null, evidence: [], ...extra });

test("reset is offered only on a halted slot", async () => {
  const onReset = vi.fn(async () => {});
  render(<UnlockRolloutPanel onReset={onReset} rows={[
    row(2, "canary", { canary_worker: "Air_1", dry_runs: 2 }),
    row(3, "halted", { price: 400, halted_reason: "Post-tap screen was not understood",
      evidence: ["/fleet/workers/Air_1/evidence/lab-unlock-slot3-1-0.png"] })]} />);
  expect(screen.getByRole("region", { name: "Lab slot rollout" })).toBeInTheDocument();
  expect(screen.getByText("Air_1")).toBeInTheDocument();
  expect(screen.getByText("Post-tap screen was not understood")).toBeInTheDocument();
  expect(screen.getByText("lab-unlock-slot3-1-0.png")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reset Lab 2 rollout" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 3 rollout" }));
  await waitFor(() => expect(onReset).toHaveBeenCalledWith(3));
});

test("a refused reset is shown, not swallowed", async () => {
  const onReset = vi.fn(async () => { throw new Error("slot_not_halted"); });
  render(<UnlockRolloutPanel rows={[row(2, "halted", { halted_reason: "x" })]} onReset={onReset} />);
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 2 rollout" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("slot_not_halted");
});
