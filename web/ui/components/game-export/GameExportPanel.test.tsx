import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { GameExportPanel } from './GameExportPanel';
import type { GameExportProjection } from '@/lib/gameExport';
afterEach(cleanup);
const projection = { available: true, account_id: 'A', source: 'game_export', exported_at: 1000,
  captured_at: 1001, age_seconds: 700, stale: true, source_online: false,
  current_run: { active: true, tier: 3, wave: 100, observed_wave: 100, estimated_wave: null },
} as GameExportProjection;
it('makes missing official evidence explicit without showing fabricated zero values', () => {
  render(<GameExportPanel data={{ available: false, account_id: 'A', source: 'game_export' }} section="workshop" />);
  expect(screen.getByText(/no official game export/i)).toBeInTheDocument();
  expect(screen.queryByText(/level 0/i)).not.toBeInTheDocument();
});
it('shows exporter age and keeps observed wave distinct from a safe estimate', () => {
  render(<GameExportPanel data={{ ...projection, stale: false, source_online: true, age_seconds: 20,
    current_run: { ...projection.current_run!, estimated_wave: 104 } }} section="tracker" />);
  expect(screen.getByText(/20s old/i)).toBeInTheDocument();
  expect(screen.getByText('Observed wave')).toBeInTheDocument();
  expect(screen.getByText('Estimated wave')).toBeInTheDocument();
});
it('never renders an estimated wave for stale or disconnected source', () => {
  render(<GameExportPanel data={{ ...projection, current_run: { ...projection.current_run!, estimated_wave: 999 } }} section="tracker" />);
  expect(screen.getByText(/stale/i)).toBeInTheDocument();
  expect(screen.getByText(/offline/i)).toBeInTheDocument();
  expect(screen.queryByText('999')).not.toBeInTheDocument();
});
it('exposes full completed report fields including fractional and unknown native values', () => {
  render(<GameExportPanel data={{ ...projection, runs: [{ id: 'r1', account_id: 'A', exported_at: 1000,
    battle_at: 900, tier: 3, wave: 100, coins: 12.75,
    report: { BossDamage: 1.125, UnknownNativeEnum: 1001, CustomReportMetric: 8.5 } }] }} section="runs" />);
  expect(screen.getByText('BossDamage')).toBeInTheDocument();
  expect(screen.getByText('1.125')).toBeInTheDocument();
  expect(screen.getByText('UnknownNativeEnum')).toBeInTheDocument();
  expect(screen.getByText('1,001')).toBeInTheDocument();
});
it('distinguishes sampled journal intervals from exact completed lab timestamps', () => {
  render(<GameExportPanel data={{ ...projection, journal: [
    { id: 'one', account_id: 'A', kind: 'snapshot_change', summary: 'Workshop changed between exports', observed_from: 900, observed_to: 1000, changes: { level: [1, 2] } },
    { id: 'two', account_id: 'A', kind: 'lab_completed', summary: 'Lab completion confirmed', occurred_at: 950, changes: {} },
  ] }} section="journal" />);
  expect(screen.getByText(/Observed between/)).toBeInTheDocument();
  expect(screen.getByText(/Game event:/)).toBeInTheDocument();
});
it('expires a cached estimate after the 300-second projection horizon even before stale threshold', () => {
  render(<GameExportPanel data={{ ...projection, source_online: true, stale: false, age_seconds: 301,
    current_run: { active: true, observed_wave: 100, estimated_wave: 150 } }} section="tracker" />);
  expect(screen.queryByText('Estimated wave')).not.toBeInTheDocument();
});
it('shows collector activation diagnostics when official evidence is unavailable', () => {
  render(<GameExportPanel data={{ available: false, account_id: 'A', source: 'game_export',
    reason: 'export_source_changed', collection_error: 'Worker source scope is missing; reload the worker' }} section="tracker" />);
  expect(screen.getByRole('alert')).toHaveTextContent('Worker source scope is missing');
});
it('computes statistics only from retained reports belonging to this account', () => {
  render(<GameExportPanel data={{ ...projection, runs: [
    { id: 'one', account_id: 'A', exported_at: 1000, wave: 55, coins: 12.75 },
    { id: 'other', account_id: 'OTHER', exported_at: 1000, wave: 999, coins: 1e9 },
  ] }} section="stats" />);
  expect(screen.getByText('Imported battle statistics')).toBeInTheDocument();
  expect(screen.getByText('Highest reported wave')).toBeInTheDocument();
  expect(screen.getAllByText('55')).toHaveLength(2);
  expect(screen.queryByText('999')).not.toBeInTheDocument();
  expect(screen.getByText(/Totals cover retained imported reports, not lifetime/)).toBeInTheDocument();
});
it('uses readable inventory labels, Yes/No booleans and explicit unknown identities', () => {
  render(<GameExportPanel data={{ ...projection, cards: { slots: 5, items: [
    { index: 1, id: 'damage', name: 'Damage', level: 2, count: 5, unlocked: true, active: false, mastery_unlocked: false },
    { index: 10, id: null, name: null, level: 0, unlocked: null, active: null },
  ] } }} section="cards" />);
  for (const label of ['Level', 'Spare copies', 'Owned', 'Equipped', 'Mastery']) expect(screen.getByRole('columnheader', { name: label })).toBeInTheDocument();
  expect(screen.getByText('Yes')).toBeInTheDocument();
  expect(screen.getAllByText('No')).toHaveLength(2);
  expect(screen.getByText('Unmapped entry')).toBeInTheDocument();
  expect(screen.getByText('Identity unavailable · Native index 10')).toBeInTheDocument();
});
it('formats verified research fractions as percentages while leaving unsupported values explicit', () => {
  render(<GameExportPanel data={{ ...projection, labs: { slots: [], research: [
    { index: 1, id: 'damage', name: 'Damage research', percent_complete: 0.215 },
    { index: 2, id: 'health', name: 'Health research', percent_complete: 50 },
  ] } }} section="labs" />);
  expect(screen.getByText('21.5%')).toBeInTheDocument();
  expect(screen.getByText('Unknown (raw 50)')).toBeInTheDocument();
});
