import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { StarterRolloutPanel } from "./StarterRolloutPanel";
import type { RehearsedLabRow, StarterRolloutRow } from "@/lib/labs";

const slot = (key: string, stage: StarterRolloutRow["stage"], extra: Partial<StarterRolloutRow> = {}): StarterRolloutRow =>
  ({ key, stage, canary_worker: null, dry_runs: 0, halted_reason: null, evidence: [], ...extra });
const lab = (extra: Partial<RehearsedLabRow> = {}): RehearsedLabRow =>
  ({ lab_id: "labs.attack-speed", name: "Attack Speed", status: "rehearsed", level: 1, price: 30,
     catalog_price: 30, seconds: 15, mismatch: false, worker: "Air_1", misses: 0, evidence: [], ...extra });

test("slots and labs render with actions only where they apply", async () => {
  const onResetSlot = vi.fn(async () => {});
  const onLab = vi.fn(async () => {});
  render(<StarterRolloutPanel rows={[slot("start:2", "canary", { canary_worker: "Air_1", dry_runs: 2 }),
    slot("start:3", "halted", { halted_reason: "Start was not proven", evidence: ["/e/lab-start-slot3-1-0.png"] })]}
    labs={[lab(), lab({ lab_id: "labs.labs-speed", name: "Labs Speed", status: "unfindable", misses: 3 }),
      lab({ lab_id: "labs.ban-perks", name: "Ban Perks", status: "needs_review", catalog_price: null, price: 5000 })]}
    onResetSlot={onResetSlot} onLab={onLab} />);
  expect(screen.getByRole("region", { name: "Lab starter rollout" })).toBeInTheDocument();
  expect(screen.getByText("Start was not proven")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reset Lab 2 starts" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 3 starts" }));
  await waitFor(() => expect(onResetSlot).toHaveBeenCalledWith("start:3"));
  fireEvent.click(screen.getByRole("button", { name: "Search again for Labs Speed" }));
  await waitFor(() => expect(onLab).toHaveBeenCalledWith("labs.labs-speed", "reset"));
  fireEvent.click(screen.getByRole("button", { name: "Accept price for Ban Perks" }));
  await waitFor(() => expect(onLab).toHaveBeenCalledWith("labs.ban-perks", "accept"));
  expect(screen.queryByRole("button", { name: "Search again for Attack Speed" })).not.toBeInTheDocument();
});

test("a refused action is shown", async () => {
  render(<StarterRolloutPanel rows={[slot("start:2", "halted", { halted_reason: "x" })]} labs={[]}
    onResetSlot={vi.fn(async () => { throw new Error("not_halted"); })} onLab={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Reset Lab 2 starts" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("not_halted");
});
