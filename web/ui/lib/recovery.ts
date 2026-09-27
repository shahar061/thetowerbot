export type RecoverySettings = {
  mode: "off" | "shadow" | "assist"; model: string; deadline_seconds: number;
  max_calls: number; max_actions: number; cooldown_seconds: number;
  incident_limit_microusd: number; daily_limit_microusd: number;
};

export type RecoveryBudgetLimits = {
  incident_limit_microusd: number; daily_limit_microusd: number; max_calls: number;
};

export type RecoverySettingsResponse = {
  settings: RecoverySettings; shadow_worker: string | null; settings_revision: number;
  policy: { active: RecoveryBudgetLimits & { revision: number; disabled_request_id: string | null };
    requested: RecoveryBudgetLimits; requested_policy_conflict: boolean };
  daily_budget?: { day: string; reserved_microusd: number; settled_microusd: number;
    total_microusd: number; remaining_microusd: number; disabled: boolean;
    budget_incident_request_id: string | null };
  budget_observed_at_utc?: string;
  assist_allowed_actions: string[];
  status: { producer: "unknown" | "available"; workers: unknown[] };
};

/** Convert user dollars without floating-point rounding or exponent syntax. */
export function parseUsdMicros(value: string): number | null {
  if (!/^(0|[1-9]\d*)(?:\.(\d{1,6}))?$/.test(value)) return null;
  const [dollars, fraction = ""] = value.split(".");
  if (dollars.length > 9) return null;
  const micros = Number(dollars) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  return Number.isSafeInteger(micros) ? micros : null;
}

/** Preserve every significant micro-dollar digit while showing cents. */
export function formatUsd(micros: number): string {
  if (!Number.isSafeInteger(micros) || micros < 0) return "Unknown";
  const whole = Math.floor(micros / 1_000_000);
  const fraction = String(micros % 1_000_000).padStart(6, "0")
    .replace(/0+$/, "").padEnd(2, "0");
  return `$${whole}.${fraction}`;
}
