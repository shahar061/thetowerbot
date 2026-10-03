import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CardPlanEditor } from "./CardPlanEditor";

it("visibly explains the usable-card condition beside its accessible switch", () => {
  const onChange = vi.fn();
  render(<CardPlanEditor program={{ version: 1, gem_cap: 100, loadouts: [], selected_loadout_id: null,
    goals: [{ id: "slot", kind: "slots", capacity: 2, when_usable_card: true }] }}
    catalog={{ cards: [], max_gem_slots: 21 }} onChange={onChange} />);
  expect(screen.getByText("Only when another usable card exists")).toBeVisible();
  const toggle = screen.getByRole("switch", { name: "Only when another usable card exists" });
  expect(toggle).toHaveAttribute("aria-checked", "true");
  fireEvent.click(toggle);
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ goals: [expect.objectContaining({ when_usable_card: false })] }));
});
