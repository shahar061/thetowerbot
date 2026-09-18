export type MilestoneStatus = "unknown" | "locked" | "in_progress" | "claimable" | "claimed" | "verified" | "available";

export type MilestoneNode = {
  id: string;
  title: string;
  group: string;
  description: string;
  kind: "unlock" | "activity" | "available";
  tier: number | null;
  wave: number | null;
  requires: string[];
  source_url: string;
  status: MilestoneStatus;
  progress: { current: number; target: number } | null;
};

export type MilestoneRoadmap = {
  schema_version: number;
  account_id: string | null;
  best_waves: Record<string, number>;
  nodes: MilestoneNode[];
};
