import { ApiError, mutationHeaders } from "./api";
import { checkRuntimeCompatibility } from "./runtimeCompatibility";
import type { CardPolicy, StatusPayload } from "./types";

/** Every request is bound to this explicit selection; fleet details never read global selection. */
export interface CardClientContext {
  scope: string | null;
  accountId: string | null;
  worker: string | null;
  baseUrl?: string;
}
export interface CardTarget {
  card_id: string;
  min_level: number | null;
}
export type CardGoal =
  | { id: string; kind: "acquire"; targets: CardTarget[] }
  | { id: string; kind: "slots"; capacity: number; when_usable_card: boolean };
export interface CardLoadout {
  id: string;
  name: string;
  priority: string[];
}
export interface CardProgram {
  version: 1;
  goals: CardGoal[];
  loadouts: CardLoadout[];
  selected_loadout_id: string | null;
  gem_cap: number;
}
export interface CardEvidence {
  observed_at: number;
  evidence_ref: string;
  frame_digest?: string | null;
}
export interface CardItem {
  card_id: string;
  ownership: "owned" | "unowned" | "locked" | "unavailable" | "unknown";
  level: number | null;
  copies: number | null;
  copies_needed: number | null;
  maxed: boolean | null;
  equipped: boolean | null;
  observed_at?: number | null;
  evidence_ref?: string;
  field_evidence?: Record<string, CardEvidence>;
}
export interface CardSnapshot {
  revision?: number;
  frame_digest?: string | null;
  equipment_evidence?: CardEvidence | null;
  items: CardItem[];
  capacity: number | null;
  equipped: string[] | null;
  collection_complete: boolean;
  equipment_complete: boolean;
  observed_at: number;
}
export interface CardBudget {
  cycle_id: string;
  cap: number;
  spent: number;
  pending: number;
}
export interface CardPreconditions {
  expected_account_id: string;
  expected_generation: string;
  expected_epoch: number;
  expected_program_revision: string;
}
export type CardCommandKind =
  "refresh" | "buy" | "slot" | "apply" | "clear" | "cancel";
export interface CardCommandIntent {
  kind: CardCommandKind;
  quantity?: 1 | 10;
  loadout_id?: string;
  target_operation_id?: string;
  goal_id?: string;
}
export interface CardCommandRequest
  extends CardPreconditions, CardCommandIntent {
  idempotency_key: string;
  budget_cycle_id?: string;
}
export interface CardReward {
  position: number;
  card_id: string;
  quantity: number;
  level_before?: number | null;
  level_after?: number | null;
  copies_before?: number | null;
  copies_after?: number | null;
}
export interface CardOperation {
  operation_id: string;
  command: {
    kind: CardCommandKind;
    quantity: number;
    idempotency_key: string;
    scope: {
      account_id: string;
      lease_id: string;
      generation: string;
      epoch: number;
    };
    program_revision: string;
    source: "manual" | "automatic";
    budget_cycle_id: string;
    loadout_id: string | null;
    goal_id: string | null;
    target_operation_id: string | null;
  };
  status: string;
  reason: string | null;
  cancel_requested?: boolean;
  spent_gems: number | null;
  rewards: CardReward[];
  created_at: number;
  snapshot_before?: CardSnapshot | null;
  snapshot_after?: CardSnapshot | null;
}
export interface CardPreview {
  fresh: boolean;
  goals: { id: string; met: boolean | null }[];
  loadouts: {
    id: string;
    capacity?: number | null;
    capacity_limited?: boolean | null;
    capacity_excluded?: string[] | null;
    observed_equipped?: string[] | null;
    additions?: string[] | null;
    removals?: string[] | null;
    resolution: {
      desired: string[] | null;
      missing: string[];
      reason: string | null;
    };
  }[];
}
export interface CardCatalog {
  cards: { card_id: string; name: string; max_level: number | null }[];
  max_gem_slots: number;
}
export interface CardsProjection {
  scope?: { account_id: string; lease_id: string; generation: string; epoch: number } | null;
  account_id: string | null;
  snapshot: CardSnapshot | null;
  fresh: boolean;
  program: CardProgram | null;
  program_revision: string | null;
  policy: CardPolicy | null;
  active_budget: CardBudget | null;
  preconditions: CardPreconditions | null;
  config_owner: "local" | "fleet" | "unavailable";
  preview: CardPreview | null;
  decision: { kind: string; reason: string | null };
  capabilities: {
    inventory: boolean;
    buy_one: boolean;
    buy_ten: boolean;
    buy_slot: boolean;
    assign: boolean;
    reasons: Record<string, string>;
  };
  recent_operations: CardOperation[];
  read_only_reason: string | null;
}
export const emptyCardProgram = (): CardProgram => ({
  version: 1,
  goals: [],
  loadouts: [],
  selected_loadout_id: null,
  gem_cap: 0,
});
export const freshCommandKey = (): string => crypto.randomUUID();
export const safeInteger = (
  value: number,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): boolean => Number.isSafeInteger(value) && value >= min && value <= max;
export const cardContextKey = (context: CardClientContext): string =>
  JSON.stringify([
    context.scope,
    context.accountId,
    context.worker,
    context.baseUrl,
  ]);

export async function cardRequest<T>(
  context: CardClientContext,
  path: string,
  init: RequestInit = {},
): Promise<{ data: T; etag: string | null }> {
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
  if (context.scope) {
    headers.set("x-account-scope", context.scope);
    if (context.accountId)
      headers.set("x-expected-account-id", context.accountId);
  }
  const url = context.baseUrl ? new URL(path, context.baseUrl).href : path;
  const response = await fetch(url, { ...init, headers, cache: "no-store" });
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new ApiError(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : JSON.stringify(body?.detail ?? `HTTP ${response.status}`),
    );
  return { data: body as T, etag: response.headers.get("etag") };
}
export async function cardWrite<T>(
  context: CardClientContext,
  path: string,
  body: unknown,
  signal?: AbortSignal,
  method = "POST",
  extra: Record<string, string> = {},
): Promise<{ data: T; etag: string | null }> {
  const { data: status } = await cardRequest<StatusPayload>(
    context,
    "/api/status",
    { signal },
  );
  const compatible = checkRuntimeCompatibility(status.runtime);
  if (!compatible.compatible)
    throw new ApiError(412, compatible.reasons.join(" "));
  if (
    !status.runtime?.capabilities.includes(
      path.startsWith("/api/strategies") ? "strategies" : "control",
    )
  )
    throw new ApiError(412, "Runtime capability unavailable");
  return cardRequest<T>(context, path, {
    method,
    signal,
    headers: {
      "content-type": "application/json",
      ...mutationHeaders(),
      ...extra,
    },
    body: JSON.stringify(body),
  });
}
export const fetchCards = async (
  context: CardClientContext,
  signal?: AbortSignal,
): Promise<CardsProjection> =>
  (await cardRequest<CardsProjection>(context, "/api/cards", { signal })).data;
export const fetchCardCatalog = async (
  context: CardClientContext,
  signal?: AbortSignal,
): Promise<CardCatalog> =>
  (await cardRequest<CardCatalog>(context, "/api/cards/catalog", { signal }))
    .data;
export const submitCardCommand = async (
  context: CardClientContext,
  body: CardCommandRequest,
  signal?: AbortSignal,
): Promise<CardOperation> =>
  (await cardWrite<CardOperation>(context, "/api/cards/commands", body, signal))
    .data;
export const fetchCardOperation = async (
  context: CardClientContext,
  id: string,
  signal?: AbortSignal,
): Promise<CardOperation> =>
  (
    await cardRequest<CardOperation>(
      context,
      `/api/cards/operations/${encodeURIComponent(id)}`,
      { signal },
    )
  ).data;
export const previewCardProgram = async (
  context: CardClientContext,
  program: CardProgram,
  signal?: AbortSignal,
): Promise<CardPreview> =>
  (
    await cardRequest<CardPreview>(context, "/api/cards/preview", {
      method: "POST",
      signal,
      headers: { "content-type": "application/json", ...mutationHeaders() },
      body: JSON.stringify({ program }),
    })
  ).data;
export const startCardCycle = async (
  context: CardClientContext,
  body: CardPreconditions & { cycle_id: string; cap: number },
  signal?: AbortSignal,
): Promise<{
  budget: CardBudget;
  active: boolean;
  active_budget: CardBudget | null;
}> =>
  (
    await cardWrite<{
      budget: CardBudget;
      active: boolean;
      active_budget: CardBudget | null;
    }>(context, "/api/cards/budget-cycles", body, signal)
  ).data;

type ActionAttempt<T> = { contextKey: string; epoch: number; body: T };
/** Exact immutable intent survives uncertain network outcomes, never selection changes. */
export class CardActionSession<T = CardCommandRequest> {
  private epoch = 0;
  private contextKey: string | null = null;
  private attempt: ActionAttempt<T> | null = null;
  private busy = false;
  begin(contextKey: string, body: T): ActionAttempt<T> | null {
    if (
      this.busy ||
      this.attempt ||
      (this.contextKey !== null && this.contextKey !== contextKey)
    )
      return null;
    this.contextKey = contextKey;
    this.busy = true;
    return (this.attempt = {
      contextKey,
      epoch: this.epoch,
      body: structuredClone(body),
    });
  }
  failed(attempt: ActionAttempt<T>): void {
    if (this.isCurrent(attempt)) this.busy = false;
  }
  complete(attempt: ActionAttempt<T>): void {
    if (this.isCurrent(attempt)) {
      this.busy = false;
      this.attempt = null;
    }
  }
  retry(contextKey: string): ActionAttempt<T> | null {
    if (this.busy || !this.attempt || this.attempt.contextKey !== contextKey)
      return null;
    this.busy = true;
    return this.attempt;
  }
  isCurrent(attempt: ActionAttempt<T>): boolean {
    return this.attempt === attempt && attempt.epoch === this.epoch;
  }
  switchAccount(contextKey: string): void {
    this.contextKey = contextKey;
    this.epoch++;
    this.attempt = null;
    this.busy = false;
  }
}

/** Wire validation only; goal/budget decisions remain exclusively server-owned. */
export function cardDraftError(
  program: CardProgram,
  policy: CardPolicy | null,
): string | null {
  if (!safeInteger(program.gem_cap))
    return "Plan gem cap must be a nonnegative safe integer. Enter a smaller cap before saving.";
  if (
    policy &&
    (!safeInteger(policy.gem_floor) ||
      !safeInteger(policy.max_per_visit, 1, 50))
  )
    return "Reserve must be a safe integer; cards per visit must be between 1 and 50.";
  if (program.loadouts.some((loadout) => !loadout.name.trim()))
    return "Every loadout needs a name.";
  return null;
}

export interface CardAssignmentState {
  account_id: string; worker: string | null; strategy_id: string; strategy_version: number;
  overlay_id: string; published_revision: number; program: CardProgram;
  status: "pending" | "active"; applied_revision: number | null;
}
export interface FleetCardsRow {
  account_id: string; worker: string | null; cards: CardsProjection; assignment: CardAssignmentState | null;
}
export interface FleetCardsSnapshot { revision: number; accounts: FleetCardsRow[] }
export interface FleetCardResult<T = unknown> {
  account_id: string; worker?: string | null; status: "accepted" | "pending" | "conflict" | "unavailable";
  result?: T; reason?: unknown; http_status?: number; assignment?: CardAssignmentState;
}
const fleetContext: CardClientContext = { scope: null, accountId: null, worker: null };
export const fetchFleetCards = async (signal?: AbortSignal): Promise<FleetCardsSnapshot> =>
  (await cardRequest<FleetCardsSnapshot>(fleetContext, "/api/fleet/cards", { signal })).data;
async function fleetCardsWrite<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return (await cardWrite<T>(fleetContext, `/api/fleet/cards/${path}`, body, signal)).data;
}
export const assignFleetCardProgram = (body: { expected_revision: number; strategy_id: string; strategy_version: number; targets: { account_id: string; worker?: string }[] }, signal?: AbortSignal): Promise<{ revision: number; results: FleetCardResult[] }> => fleetCardsWrite("assignments", body, signal);
export const refreshFleetCards = (targets: (CardPreconditions & { worker: string; idempotency_key: string; kind: "refresh" })[], signal?: AbortSignal): Promise<{ results: FleetCardResult<CardOperation>[] }> => fleetCardsWrite("commands", { targets }, signal);
export const setFleetCardsAutomation = (targets: (CardPreconditions & { worker: string; enabled: boolean })[], signal?: AbortSignal): Promise<{ results: FleetCardResult[] }> => fleetCardsWrite("automation", { targets }, signal);
export const selectFleetCardLoadout = (body: { expected_revision: number; account_id: string; loadout_id: string | null }, signal?: AbortSignal): Promise<{ revision: number; assignment: CardAssignmentState }> => fleetCardsWrite("loadouts", body, signal);
export type FleetAccountAction =
  | { action: "command"; body: CardCommandRequest & { worker: string } }
  | { action: "cycle"; body: CardPreconditions & { worker: string; cap: number; cycle_id: string } }
  | { action: "preview"; body: CardPreconditions & { worker: string; program: CardProgram } }
  | { action: "operation"; body: CardPreconditions & { worker: string; operation_id: string } };
export const fleetCardAccountRequest = <T>(request: FleetAccountAction, signal?: AbortSignal): Promise<FleetCardResult<T>> => fleetCardsWrite(`account-${request.action}`, request.body, signal);
/** Includes the existing FactScope lease; equal program content does not establish authority. */
export const fleetCardAuthority = (row: FleetCardsRow): string => JSON.stringify([row.account_id, row.worker, row.cards.scope, row.cards.preconditions, row.cards.config_owner, row.cards.program_revision, row.cards.program, row.assignment?.overlay_id]);
