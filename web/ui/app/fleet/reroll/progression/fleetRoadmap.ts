import type { RerollMember } from "@/lib/fleet";
import type { MilestoneNode, MilestoneRoadmap, MilestoneStatus } from "@/lib/milestoneRoadmap";

export type WorkerRoadmap = { member: RerollMember; roadmap: MilestoneRoadmap | null; error: string | null };
export type NodeAccount = { member: RerollMember; status: MilestoneStatus; progress: MilestoneNode["progress"]; requires: string[]; missing: string[]; error: string | null };
export type AtlasNode = MilestoneNode & { x: number; y: number; accounts: NodeAccount[]; verified: number };
export type FleetGraph = { nodes: AtlasNode[]; edges: { from: string; to: string }[]; groups: { name: string; x: number; y: number; width: number; height: number; count: number }[]; width: number; height: number };
export const STATUS_LABELS: Record<MilestoneStatus, string> = { unknown: "Unknown", locked: "Locked", in_progress: "In progress", claimable: "Claimable", claimed: "Claimed", verified: "Verified", available: "Available" };

export async function loadFleetRoadmaps(members: RerollMember[], fetcher: (key: string) => Promise<MilestoneRoadmap>, onResult?: (worker: WorkerRoadmap) => void): Promise<WorkerRoadmap[]> {
  const requests = new Map<string, Promise<MilestoneRoadmap>>();
  async function readMember(member: RerollMember): Promise<WorkerRoadmap> {
    if (!member.account_key || !member.account_id) return { member, roadmap: null, error: "Unverified account — waiting for worker identity." };
    try {
      let request = requests.get(member.account_key);
      if (!request) { request = fetcher(member.account_key); requests.set(member.account_key, request); }
      const roadmap = await request;
      if (roadmap.account_id !== member.account_id) return { member, roadmap: null, error: "Account identity changed — roadmap withheld." };
      return { member, roadmap, error: null };
    } catch (error) {
      return { member, roadmap: null, error: error instanceof Error ? error.message : "Roadmap unavailable" };
    }
  }
  return Promise.all(members.map(async (member) => {
    const result = await readMember(member);
    onResult?.(result);
    return result;
  }));
}

/** Positions are organizational only. Only catalogue `requires` creates edges. */
export function buildFleetGraph(workers: WorkerRoadmap[]): FleetGraph {
  const catalog = new Map<string, MilestoneNode>();
  workers.forEach(({ roadmap }) => roadmap?.nodes.forEach((node) => { if (!catalog.has(node.id)) catalog.set(node.id, node); }));
  const order = (node: MilestoneNode): number => (node.tier ?? 0) * 100000 + (node.wave ?? 0);
  const entries = [...new Set([...catalog.values()].map((node) => node.group))].map((name) => {
    const items = [...catalog.values()].filter((node) => node.group === name).sort((a, b) => order(a) - order(b) || a.id.localeCompare(b.id));
    let rings = 1;
    while (3 * rings * (rings + 1) < items.length) rings++;
    return { name, items, size: rings * 460 + 280 };
  }).sort((a, b) => order(a.items[0]) - order(b.items[0]) || a.name.localeCompare(b.name));
  const groups: FleetGraph["groups"] = [];
  const nodes: AtlasNode[] = [];
  const columnWidths = [0, 1, 2].map((column) => Math.max(0, ...entries.filter((_, index) => index % 3 === column).map((entry) => entry.size)));
  let top = 70;
  entries.forEach(({ name, items, size }, groupIndex) => {
    const column = groupIndex % 3;
    const left = 70 + columnWidths.slice(0, column).reduce((sum, width) => sum + width + 90, 0);
    const centerX = left + columnWidths[column] / 2;
    const rowHeight = Math.max(...entries.slice(Math.floor(groupIndex / 3) * 3, Math.floor(groupIndex / 3) * 3 + 3).map((entry) => entry.size));
    const centerY = top + rowHeight / 2;
    groups.push({ name, x: centerX - size / 2, y: centerY - size / 2, width: size, height: size, count: items.length });
    items.forEach((node, index) => {
      let ring = 1;
      let position = index;
      while (position >= 6 * ring) { position -= 6 * ring; ring++; }
      const count = Math.min(6 * ring, items.length - 3 * (ring - 1) * ring);
      const angle = -Math.PI / 2 + position * Math.PI * 2 / count + (ring % 2 === 0 ? Math.PI / count : 0);
      const radius = 230 * ring;
      const accounts: NodeAccount[] = workers.map(({ member, roadmap, error }) => {
        const own = roadmap?.nodes.find((entry) => entry.id === node.id);
        return { member, status: own?.status ?? "unknown", progress: own?.progress ?? null, requires: own?.requires ?? node.requires,
          missing: (own?.requires ?? node.requires).filter((id) => !roadmap?.nodes.some((entry) => entry.id === id && ["verified", "claimed", "available"].includes(entry.status))), error };
      });
      nodes.push({ ...node, x: centerX + Math.cos(angle) * radius, y: centerY + Math.sin(angle) * radius, accounts, verified: accounts.filter((account) => account.status === "verified").length });
    });
    if (column === 2 || groupIndex === entries.length - 1) top += rowHeight + 90;
  });
  return { nodes, groups, width: Math.max(900, columnWidths.filter(Boolean).reduce((sum, width) => sum + width + 90, 50)), height: Math.max(top + 20, 540), edges: nodes.flatMap((node) => node.requires.filter((id) => catalog.has(id)).map((from) => ({ from, to: node.id }))) };
}

/** Focus actual unfinished evidence first, then the earliest known locked milestone. */
export function currentObjective(nodes: AtlasNode[], worker: string | null): AtlasNode | undefined {
  const candidates = (statuses: string[]): AtlasNode[] => nodes.filter((node) => node.accounts.some((account) => (!worker || account.member.name === worker) && statuses.includes(account.status)));
  const earliest = (items: AtlasNode[]): AtlasNode | undefined => [...items].sort((a, b) => (a.tier ?? 0) - (b.tier ?? 0) || (a.wave ?? 0) - (b.wave ?? 0) || a.id.localeCompare(b.id))[0];
  return earliest(candidates(["in_progress", "claimable"])) ?? earliest(candidates(["locked"])) ?? nodes[0];
}
