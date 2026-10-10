import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { BotSettings } from './BotSettings';
import type { Strategy } from '@/lib/types';
import type { createSingleStudioClient } from '@/lib/strategyClient';
const strategy: Strategy = {
  name: "default",
  actions: [
    { name: "Damage", template: "d.png", enabled: true, threshold: 0.9, brightness_ratio: 0.75 },
    { name: "Speed", template: "s.png", enabled: false, threshold: 0.8, brightness_ratio: 0.75 },
  ],
  affordability: "digits",
  interval: 2,
  menu_interval: 2,
  click_cooldown: 1,
  auto_navigate: false,
  max_runs: null,
  navigation_cooldown: 3,
  screen_confirmations: 2,
  tap_jitter_px: 8,
  timing_jitter: 0.15,
  tap_delay: 0.12,
  target_speed: null,
  shopping: {
    enabled: false,
    armed: false,
    visit_every_n_runs: 1,
    max_taps_per_visit: 40,
    workshop: [],
    cards: { enabled: false, gem_floor: 40, max_per_visit: 2, batch: "x1" },
  },
};
afterEach(cleanup);
test('saves cadence through bot settings, reverts drafts, and shows progress', async () => {
  const saved = { ...strategy, push_every_farm_runs: 10 };
  const saveSettings = vi.fn(async (patch: Partial<Strategy>) => ({ ...saved, ...patch }));
  const client = { readSettings: async () => saved, saveSettings } as unknown as ReturnType<typeof createSingleStudioClient>;
  render(<BotSettings client={client} onDirty={vi.fn()} push={{ phase: 'farming', every: 10, farms_remaining: 4 }} />);
  const input = await screen.findByRole('spinbutton', { name: 'Completed farming runs between pushes' });
  expect(screen.getByText(/6 of 10 farming runs completed/)).toBeVisible();
  fireEvent.change(input, { target: { value: '4' } }); fireEvent.blur(input);
  fireEvent.click(screen.getByRole('button', { name: 'Revert' }));
  expect(screen.getByRole('spinbutton', { name: 'Completed farming runs between pushes' })).toHaveValue(10);
  const reverted = screen.getByRole('spinbutton', { name: 'Completed farming runs between pushes' });
  fireEvent.change(reverted, { target: { value: '15' } }); fireEvent.blur(reverted);
  fireEvent.click(screen.getByRole('button', { name: 'Save bot settings' }));
  await waitFor(() => expect(saveSettings).toHaveBeenCalledWith(expect.objectContaining({ push_every_farm_runs: 15 })));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save bot settings' })).toBeDisabled());
  expect(saveSettings.mock.calls[0][0]).not.toHaveProperty('autopilot');
});
