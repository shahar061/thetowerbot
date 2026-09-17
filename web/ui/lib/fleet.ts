export type FleetClone = {
  instance?: string;
  state: "queued" | "staging" | "verifying" | "blocked" | "quarantined" | "dismissed" | "ready";
  reason: string;
  detail?: string | null;
  endpoint?: string;
  account_id?: string;
  evidence_ref?: string;
  registration_evidence_ref?: string;
  steps?: { at: number; state: string; reason: string; endpoint?: string }[];
};

export type FleetJob = {
  id: string;
  mode: "fresh" | "clone";
  source: string | null;
  requested_at: number;
  manager_result_url?: string;
  clones: FleetClone[];
};

export type FleetSnapshot = {
  capacity: { limit: number; used: number; available: number };
  sources: { instance: string; state: "parallel_session_qualified" | "blocked"; reason: string;
    evidence_at?: number; evidence_url?: string }[];
  jobs: FleetJob[];
  unavailable?: string;
};

export type FleetPreview = {
  mode: "fresh" | "clone";
  source: string | null;
  count: number;
  targets: string[];
  state: "eligible" | "blocked";
  reason?: string;
};

export type FleetSetup = {
  configured: boolean;
  settings: { capacity: number; name_prefix: string; qualification_id: string } | null;
  qualifications: { id: string; source_instance: string; evaluated_at?: number }[];
  host: { installed_prefix?: string; instance_count?: number; unavailable?: string };
};
