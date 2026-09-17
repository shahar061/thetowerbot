export type FleetClone = {
  instance?: string;
  state: "queued" | "staging" | "verifying" | "blocked" | "quarantined" | "ready";
  reason: string;
  endpoint?: string;
  account_id?: string;
};

export type FleetJob = {
  id: string;
  source: string;
  requested_at: number;
  clones: FleetClone[];
};

export type FleetSnapshot = {
  sources: { instance: string; state: "qualified" | "blocked"; reason: string }[];
  jobs: FleetJob[];
  unavailable?: string;
};
