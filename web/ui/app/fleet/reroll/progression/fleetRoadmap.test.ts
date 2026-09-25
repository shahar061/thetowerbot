import { expect, test, vi } from "vitest";
import type { RerollMember } from "@/lib/fleet";
import type { MilestoneNode, MilestoneRoadmap } from "@/lib/milestoneRoadmap";
import { buildFleetGraph, loadFleetRoadmaps, currentObjective } from "./fleetRoadmap";

export const workers: RerollMember[] = [
  { name: "Air_1", account_key: "worker:Air_1", account_id: "100", endpoint: "", lease_id: "a", state: "running" },
  { name: "Air_2", account_key: "worker:Air_2", account_id: "200", endpoint: "", lease_id: "b", state: "running" },
];
export const parent: MilestoneNode = { id: "parent", title: "Laboratory", group: "Features", description: "Unlock research", kind: "unlock", tier: 1, wave: 20, requires: [], source_url: "", status: "claimable", progress: { current: 24, target: 20 } };
export const roadmap = (account_id = "100"): MilestoneRoadmap => ({ schema_version: 1, account_id, best_waves: { "1": 24 }, nodes: [parent, { ...parent, id: "child", title: "Second lab", requires: ["parent", "unlisted"], status: "locked" }] });

test("scopes reads, deduplicates account keys, and isolates unavailable workers", async () => {
  const fetcher = vi.fn(async (key: string) => { if (key === "worker:Air_2") throw new Error("Worker offline"); return roadmap(); });
  const results = await loadFleetRoadmaps([...workers, { ...workers[0], name: "Alias" }], fetcher);
  expect(fetcher.mock.calls).toEqual([["worker:Air_1"], ["worker:Air_2"]]);
  expect(results[0].roadmap?.account_id).toBe("100");
  expect(results[1].error).toBe("Worker offline");
  expect(results[2].roadmap?.account_id).toBe("100");
});

test("rejects another account's roadmap and never fetches unverified membership", async () => {
  const fetcher = vi.fn(async () => roadmap("other"));
  const results = await loadFleetRoadmaps([workers[0], { ...workers[1], account_id: null }], fetcher);
  expect(results[0].roadmap).toBeNull();
  expect(results[0].error).toMatch(/identity changed/i);
  expect(results[1].error).toMatch(/unverified account/i);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test("uses only real prerequisites and keeps claimable, unknown and per-worker progress separate", () => {
  const first = { tier: 1, wave: 20, run_id: 1, reached_at: 120, play_seconds: 120, elapsed_seconds: 150 };
  const graph = buildFleetGraph([
    { member: workers[0], roadmap: { ...roadmap(), nodes: [{ ...parent, wave_gate: first }, { ...parent, id: "child", title: "Second lab", requires: ["parent", "unlisted"], status: "locked" }] }, error: null },
    { member: workers[1], roadmap: { ...roadmap("200"), nodes: [{ ...parent, status: "unknown", progress: { current: 5, target: 20 } }] }, error: null },
  ]);
  const node = graph.nodes.find((node) => node.id === "parent")!;
  expect(node.accounts.map((account) => account.status)).toEqual(["claimable", "unknown"]);
  expect(node.accounts.map((account) => account.progress?.current)).toEqual([24, 5]);
  expect(node.accounts.map((account) => account.wave_gate?.play_seconds ?? null)).toEqual([120, null]);
  expect(node.verified).toBe(0);
  expect(graph.edges).toEqual([{ from: "parent", to: "child" }]);
  expect(graph.nodes.find((node) => node.id === "child")!.accounts[1].status).toBe("unknown");
  expect(buildFleetGraph([{ member: workers[1], roadmap: null, error: "Offline" }]).nodes).toEqual([]);
});

test("lays out early-game categories as two-dimensional islands and chooses actual current evidence", () => {
  const data = { ...roadmap(), nodes: [
    { ...parent, id: "cards", group: "Cards", wave: 400, status: "locked" as const },
    { ...parent, id: "workshop", group: "Workshop", wave: 10, status: "verified" as const },
    { ...parent, id: "goal", group: "Unlocks", wave: 20, status: "in_progress" as const },
  ] };
  const graph = buildFleetGraph([{ member: workers[0], roadmap: data, error: null }]);
  expect(graph.groups.map((group) => group.name)).toEqual(["Workshop", "Unlocks", "Cards"]);
  expect(new Set(graph.groups.map((group) => group.x)).size).toBe(3);
  expect(currentObjective(graph.nodes, "Air_1")?.id).toBe("goal");
  expect(graph.edges).toEqual([]);
});
