import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { RerollMember } from "@/lib/fleet";
import type { MilestoneRoadmap } from "@/lib/milestoneRoadmap";
import { ProgressionAtlas } from "./ProgressionAtlas";

const member: RerollMember = { name: "Air_1", account_key: "worker:Air_1", account_id: "100", endpoint: "", lease_id: "a", state: "running", best_tier_1_wave: 24 };
const roadmap: MilestoneRoadmap = { schema_version: 1, account_id: "100", best_waves: { "1": 24 }, nodes: [{ id: "lab", title: "Laboratory", description: "Research becomes possible", group: "Features", kind: "unlock", tier: 1, wave: 20, requires: [], source_url: "", status: "claimable", progress: { current: 24, target: 20 }, wave_gate: { tier: 1, wave: 20, run_id: 1, reached_at: 120, play_seconds: 120, elapsed_seconds: 180 } }] };

test("pointer selection opens a docked fleet timing panel without making the map focusable", () => {
  const { container } = render(<ProgressionAtlas workers={[{ member, roadmap, error: null }, { member: { ...member, name: "Air_2", account_id: "200" }, roadmap: null, error: "Offline" }]} />);
  const map = container.querySelector("svg[aria-label='Interactive milestone map']")!;
  const node = container.querySelector("[data-atlas-node='lab']")!;
  expect(map).toHaveAttribute("focusable", "false");
  expect(node).not.toHaveAttribute("tabindex");
  fireEvent.pointerUp(node);
  const comparison = screen.getByRole("region", { name: "Laboratory comparison" });
  expect(within(comparison).getByText("Claimable")).toBeInTheDocument();
  expect(within(comparison).getByText("2m 0s")).toBeInTheDocument();
  expect(within(comparison).getByText("Evidence unavailable")).toBeInTheDocument();
  expect(within(comparison).getByText("Observed 24 / 20")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Fit atlas" })).toBeInTheDocument();
});

test("comparison shows each reached emulator's time and highlights the fastest", () => {
  const second = { ...roadmap, account_id: "200", nodes: [{ ...roadmap.nodes[0], status: "verified" as const,
    wave_gate: { tier: 1, wave: 20, run_id: 4, reached_at: 400, play_seconds: 300, elapsed_seconds: 360 } }] };
  const { container } = render(<ProgressionAtlas workers={[{ member, roadmap, error: null },
    { member: { ...member, name: "Air_2", account_id: "200" }, roadmap: second, error: null }]} />);
  fireEvent.pointerUp(container.querySelector("[data-atlas-node='lab']")!);
  const comparison = screen.getByRole("region", { name: "Laboratory comparison" });
  expect(within(comparison).getByText("2m 0s")).toBeInTheDocument();
  expect(within(comparison).getByText("5m 0s")).toBeInTheDocument();
  expect(within(comparison).getByText(/Fastest recorded/)).toBeInTheDocument();
});

test("list view exposes the same milestone with accessible comparison controls", () => {
  render(<ProgressionAtlas workers={[{ member, roadmap, error: null }]} />);
  fireEvent.click(screen.getByRole("button", { name: "List view" }));
  fireEvent.click(screen.getByRole("button", { name: /Inspect Laboratory/ }));
  expect(screen.getByRole("region", { name: "Laboratory comparison" })).toBeInTheDocument();
  expect(screen.getByText("0/1 verified")).toBeInTheDocument();
});

vi.mock("./atlas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));
