import type { FleetStateAccount } from "./fleetState";

export interface SingleAccountState {
  generated_at: string;
  account_id: string | null;
  account: FleetStateAccount | null;
}

export type LabShareMode = "when_affordable" | "save_pct" | "labs_first" | "just_in_time";
export interface LabSavingsSettings {
  account_id: string | null;
  worker: string | null;
  revision: number | null;
  share_mode: LabShareMode | null;
  share_pct: number | null;
  auto_start: boolean;
  editable: boolean;
  reason: string | null;
}
export interface LabSavingsInput {
  expected_account_id: string;
  expected_revision: number;
  share_mode: LabShareMode;
  share_pct: number;
}
