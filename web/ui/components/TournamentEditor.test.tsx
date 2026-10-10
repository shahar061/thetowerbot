import { render, screen, fireEvent } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { TournamentEditor, tournamentDefaults } from "./TournamentEditor";

it("shows the free-only contract and preserves combat priorities when editing cash", () => {
  const change = vi.fn();
  render(<TournamentEditor onChange={change} />);
  expect(screen.getByRole('checkbox', { name: 'Enter with a free ticket' })).toBeChecked();
  expect(screen.getByText(/Never spend gems or buy coin upgrades/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Opening cash budget'), { target: { value: '50' } });
  expect(change).toHaveBeenLastCalledWith({ ...tournamentDefaults,
    opening_cash: { ...tournamentDefaults.opening_cash, cash_budget: 50 } });
});

it("retains null targets and lets the user change combat ordering", () => {
  const change = vi.fn();
  render(<TournamentEditor onChange={change} />);
  fireEvent.click(screen.getByRole('button', { name: 'Move attack_speed up' }));
  expect(change.mock.lastCall?.[0].rules.map((r: { upgrade_id: string }) => r.upgrade_id))
    .toEqual(['attack_speed', 'health', 'damage']);
});

it("adds only supported upgrades with a finite survival target and permits removal", () => {
  const change = vi.fn();
  render(<TournamentEditor onChange={change} />);
  expect(screen.queryByRole('option', { name: 'coins per wave' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Add upgrade' }));
  expect(change.mock.lastCall?.[0].rules.at(-1)).toEqual({ upgrade_id: 'defense_percent', enabled: true, target: 0 });
  fireEvent.click(screen.getByRole('button', { name: 'Remove damage' }));
  expect(change.mock.lastCall?.[0].rules.map((r: { upgrade_id: string }) => r.upgrade_id)).toEqual(['health', 'attack_speed']);
});
