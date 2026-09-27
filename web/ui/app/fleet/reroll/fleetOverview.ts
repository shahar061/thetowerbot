import type { RerollMember } from "@/lib/fleet";
import { validatedOverview } from "@/lib/fleet";
import type { LabsRow, LabsSnapshot } from "@/lib/labs";
import { memberIdentity } from "./statsHelpers";

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const nullableNumber = (value: unknown): boolean => value === null || finite(value);
const nullableString = (value: unknown): boolean => value === null || typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === "string");
const bool = (value: unknown): value is boolean => typeof value === "boolean";

function validSlot(value: unknown): boolean {
  if (!record(value) || !Number.isInteger(value.slot) || !record(value.now) ||
      !["researching", "idle", "locked", "owned_unread", "unknown"].includes(value.now.state as string) ||
      !nullableNumber(value.now.level) || !nullableNumber(value.now.completes_at) ||
      !nullableNumber(value.now.overdue_seconds) || !nullableNumber(value.now.read_at) || !bool(value.now.stale) ||
      !nullableString(value.now.research_id) || !nullableString(value.now.research_name) ||
      !(value.now.owned === null || bool(value.now.owned)) ||
      !["unknown", "historical", "current"].includes(value.now.evidence_status as string) ||
      !bool(value.automated) || !strings(value.why) || !nullableString(value.note) ||
      !(value.covered === null || bool(value.covered))) return false;
  if (!record(value.capabilities) ||
      !bool(value.capabilities.observe) || !bool(value.capabilities.plan) || !bool(value.capabilities.execute)) return false;
  if (value.next !== null && (!record(value.next) || typeof value.next.lab_id !== "string" ||
      typeof value.next.name !== "string" || !nullableNumber(value.next.level) ||
      !nullableNumber(value.next.price) || !nullableNumber(value.next.seconds))) return false;
  return true;
}

function validRow(value: unknown): value is LabsRow {
  if (!record(value) || typeof value.worker !== "string" || !nullableString(value.account_id) ||
      !nullableString(value.strategy_name) || !nullableNumber(value.read_at) ||
      !record(value.wallet) || !nullableNumber(value.wallet.coins) || !nullableNumber(value.wallet.gems) ||
      !["ok", "unknown"].includes(value.state as string) || !nullableString(value.reason) ||
      !Array.isArray(value.recent) || !value.recent.every(item => record(item) && finite(item.at) &&
        ["LAB", "CARD_BUY"].includes(item.kind as string) && nullableString(item.item) &&
        nullableString(item.category) && nullableString(item.currency) && nullableNumber(item.amount) && nullableString(item.reason)) ||
      !Number.isInteger(value.unknown_slots) ||
      !["unknown", "stale", "historical", "observed"].includes(value.freshness as string) ||
      !strings(value.blockers)) return false;
  if (value.plan === null) return true;
  const plan = value.plan;
  return record(plan) && nullableNumber(plan.wallet_coins) && finite(plan.jar) &&
    plan.account_id === value.account_id &&
    (plan.scope === undefined || plan.scope === null || (record(plan.scope) &&
      plan.scope.account_id === value.account_id && nullableString(plan.scope.lease_id) &&
      nullableString(plan.scope.generation) && finite(plan.scope.epoch))) &&
    Number.isInteger(plan.strategy_revision) && nullableNumber(plan.evaluated_at) &&
    Array.isArray(plan.slots) && plan.slots.every(validSlot) &&
    record(plan.gems) && nullableNumber(plan.gems.wallet) && nullableNumber(plan.gems.price) &&
    nullableNumber(plan.gems.have) && nullableNumber(plan.gems.need) && bool(plan.gems.automated) &&
    strings(plan.gems.why) && Array.isArray(plan.gems.steps) &&
    plan.gems.steps.every(item => record(item) && typeof item.block_id === "string" &&
      typeof item.type === "string" && typeof item.label === "string" && ["done", "current", "next"].includes(item.state as string) &&
      nullableNumber(item.price) && bool(item.automated)) &&
    (plan.gems.next === null || (record(plan.gems.next) && typeof plan.gems.next.block_id === "string" &&
      typeof plan.gems.next.type === "string" && typeof plan.gems.next.label === "string" && ["done", "current", "next"].includes(plan.gems.next.state as string) &&
      nullableNumber(plan.gems.next.price) && bool(plan.gems.next.automated)));
}

/** API data is untrusted even when fetchFleetLabs has a TypeScript return type. */
export function validatedLabsSnapshot(value: unknown): LabsSnapshot | null {
  if (!record(value) || !Array.isArray(value.workers) || !value.workers.every(validRow) ||
      !Array.isArray(value.automated) || !value.automated.every(item => record(item) &&
        ["labs", "gems"].includes(item.lane as string) && typeof item.type === "string" && Number.isInteger(item.slot) &&
        (item.lab_id === undefined || typeof item.lab_id === "string")) ||
      !record(value.reference) || !Array.isArray(value.reference.labs) ||
      !value.reference.labs.every(item => record(item) && typeof item.id === "string" &&
        typeof item.name === "string" && nullableNumber(item.max_level) && bool(item.priced)) ||
      !Array.isArray(value.reference.game_speed) || !value.reference.game_speed.every(item => record(item) &&
        finite(item.level) && finite(item.coins) && finite(item.seconds) && finite(item.max_speed)) ||
      !Array.isArray(value.reference.lab_slots) || !value.reference.lab_slots.every(item => record(item) &&
        finite(item.slot) && finite(item.gems)) ||
      !Array.isArray(value.reference.card_slots) || !value.reference.card_slots.every(item => record(item) &&
        finite(item.slot) && finite(item.gems)) ||
      !finite(value.reference.card_gems) || !finite(value.reference.labs_unlock_wave) ||
      !Array.isArray(value.reference.sources) || !value.reference.sources.every(item => record(item) &&
        typeof item.url === "string" && typeof item.checked === "string")) return null;
  if (new Set(value.workers.map(row => row.worker)).size !== value.workers.length) return null;
  return value as LabsSnapshot;
}

export function fleetScope(members: RerollMember[]): string {
  return JSON.stringify(members.map(member => [memberIdentity(member), validatedOverview(member)?.attempt_id ?? null]).sort((a, b) =>
    JSON.stringify(a).localeCompare(JSON.stringify(b))));
}

export function overviewEvidence(member: RerollMember, snapshot: LabsSnapshot | null) {
  const account = member.account_id;
  const revision = member.route_revision_applied;
  const row = account && snapshot?.workers.find(item => item.worker === member.name && item.account_id === account &&
    (item.plan?.account_id === undefined || item.plan.account_id === account) &&
    (!item.plan?.scope || (item.plan.scope.account_id === account && item.plan.scope.lease_id === member.lease_id))) || null;
  const appliedLabPlan = row?.plan && revision != null && row.plan.strategy_revision === revision ? row.plan : null;
  const labs = row?.plan && !appliedLabPlan ? { ...row,
    reason: [row.reason, "Applied plan revision unverified or different; next actions are planning only"].filter(Boolean).join(" · "),
    plan: { ...row.plan,
      slots: row.plan.slots.map(slot => ({ ...slot, automated: false,
        capabilities: slot.capabilities && { ...slot.capabilities, execute: false } })),
      gems: { ...row.plan.gems, automated: false,
        next: row.plan.gems.next && { ...row.plan.gems.next, automated: false } },
    },
  } : row;
  const matched = <T extends { account_id: string; revision: number | null }>(value: T | null | undefined): T | null =>
    account && revision != null && value?.account_id === account && value.revision === revision ? value : null;
  return { overview: validatedOverview(member), labs, appliedLabPlan, workshop: matched(member.workshop_evaluation),
    battle: matched(member.battle_evaluation), resource: matched(member.resource_evaluation) };
}
