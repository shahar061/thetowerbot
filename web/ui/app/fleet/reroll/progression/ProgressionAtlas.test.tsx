import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { RerollMember } from "@/lib/fleet";
import type { MilestoneRoadmap } from "@/lib/milestoneRoadmap";
import { ProgressionAtlas } from "./ProgressionAtlas";

const member: RerollMember = { name: "Air_1", account_key: "worker:Air_1", account_id: "100", endpoint: "", lease_id: "a", state: "running", best_tier_1_wave: 24 };
const roadmap: MilestoneRoadmap = { schema_version: 1, account_id: "100", best_waves: { "1": 24 }, nodes: [{ id: "lab", title: "Laboratory", description: "Research becomes possible", group: "Features", kind: "unlock", tier: 1, wave: 20, requires: [], source_url: "", status: "claimable", progress: { current: 24, target: 20 } }] };

test("keyboard node selection opens fleet evidence comparison without upgrading wave evidence", () => {
  render(<ProgressionAtlas workers={[{ member, roadmap, error: null }, { member: { ...member, name: "Air_2", account_id: "200" }, roadmap: null, error: "Offline" }]} />);
  fireEvent.keyDown(screen.getByRole("button", { name: /Select Laboratory/ }), { key: "Enter" });
  const comparison = screen.getByRole("region", { name: "Laboratory comparison" });
  expect(within(comparison).getByText("Claimable")).toBeInTheDocument();
  expect(within(comparison).getByText("Unknown", { selector: "strong" })).toBeInTheDocument();
  expect(within(comparison).getByText("24 / 20")).toBeInTheDocument();
  expect(within(comparison).getByText("Offline")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Fit atlas" })).toBeInTheDocument();
});

test("list view exposes the same milestone with accessible comparison controls", () => {
  render(<ProgressionAtlas workers={[{ member, roadmap, error: null }]} />);
  fireEvent.click(screen.getByRole("button", { name: "List view" }));
  fireEvent.click(screen.getByRole("button", { name: /Inspect Laboratory/ }));
  expect(screen.getByRole("region", { name: "Laboratory comparison" })).toBeInTheDocument();
  expect(screen.getByText("0/1 verified")).toBeInTheDocument();
});

vi.mock("./atlas.module.css", () => ({ default: new Proxy({}, { get: (_, key) => key }) }));
