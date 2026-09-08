import type { AccountSnapshot, ClaimSnapshot, ConceptCatalog, StatsCollection } from "./account";
import type {
  AdvisorSnapshot,
  AdvisorDraftResult,
  AutopilotPreset,
  AutopilotCommand,
  AutopilotSnapshot,
  Upgrade,
  BotStatus,
  ControlPayload,
  DirectorPlanPayload,
  LedgerPayload,
  RunPurchasePayload,
  RunRow,
  Snapshot,
  StatsPayload,
  StatusPayload,
  Strategy,
  StrategyList,
  StoredEvent,
} from "./types";
import { checkRuntimeCompatibility } from "./runtimeCompatibility";

/** An HTTP failure that kept its status code.
 *
 * A bare Error flattens every rejection into one string, leaving callers to
 * match on prose to tell an expected answer from a real fault - a 409 from
 * /api/bot/start ("another tab already started one") means refresh, a 503
 * ("no emulator") means failure. The status is the only stable way to say
 * which is which. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { accept: "application/json", ...init?.headers } });
  // Parsed before the ok check and flattened through the same describeDetail
  // as the writes below: the server takes real trouble to distinguish
  // 404-absent from 422-corrupt with a message, and a read that reported
  // only "/api/strategies/crit -> 422" threw that reason away.
  const parsed = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      (parsed && describeDetail(parsed.detail)) ?? `${path} -> ${response.status}`,
    );
  }
  return parsed as T;
}

export const fetchStatus = () => getJson<StatusPayload>("/api/status", { cache: "no-store" });
export const fetchUpgrades = () => getJson<Upgrade[]>("/api/upgrades");
export const fetchAutopilot = () => getJson<AutopilotSnapshot>("/api/autopilot");
export const fetchAutopilotPresets = () => getJson<AutopilotPreset[]>("/api/autopilot/presets");
export const postAutopilotCommand = (command: AutopilotCommand) =>
  send<{ queued: boolean }>("/api/autopilot/command", "POST", command, "autopilot");
export const fetchRuns = (limit = 30) => getJson<RunRow[]>(`/api/runs?limit=${limit}`);
export const fetchRunEvents = (id: number) => getJson<StoredEvent[]>(`/api/runs/${id}/events`);
export const fetchRunPurchases = (id: number) =>
  getJson<RunPurchasePayload>(`/api/runs/${id}/purchases`, { cache: "no-store" });
export const fetchUnknown = () => getJson<Snapshot[]>("/api/unknown");
export const fetchStats = () => getJson<StatsPayload>("/api/stats");
export const fetchErrors = (limit = 100) => getJson<StoredEvent[]>(`/api/errors?limit=${limit}`);

export const fetchLedger = (
  opts: {
    includeRehearsals?: boolean;
    before?: number;
    /** A single kind, matching the route - not a list. */
    kind?: string;
    currency?: string;
  } = {},
) => {
  const params = new URLSearchParams();
  if (opts.includeRehearsals) params.set("include_rehearsals", "true");
  if (opts.before !== undefined) params.set("before", String(opts.before));
  if (opts.kind) params.set("kind", opts.kind);
  if (opts.currency) params.set("currency", opts.currency);
  const query = params.toString();
  return getJson<LedgerPayload>(`/api/ledger${query ? `?${query}` : ""}`);
};

export const fetchControl = () => getJson<ControlPayload>("/api/control");
export const fetchDirector = () => getJson<DirectorPlanPayload>("/api/director");
export const fetchAdvisor = (profile: string) =>
  getJson<AdvisorSnapshot>(`/api/advisor?profile=${encodeURIComponent(profile)}`);
export const importAdvisor = (body: { profile: string; filename: string; content: string }) =>
  send<AdvisorSnapshot>("/api/advisor/import", "POST", body, "advisor");
export const stageAdvisor = (body: { profile: string; import_id: string; recommendation_id: string; draft: Strategy }) =>
  send<AdvisorDraftResult>("/api/advisor/draft", "POST", body, "advisor");

/** FastAPI's `detail` is a plain string when our own ControlError becomes a
 * 422 (e.g. an out-of-range interval), but a list of structured items when
 * pydantic itself rejects a field's type before our validation ever runs
 * (e.g. a string where a float belongs). Flatten either into one line. */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) =>
        item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : JSON.stringify(item),
      )
      .join("; ");
  }
  return null;
}

/** Returns the full new state, or throws with the server's reason. */
export async function patchControl(
  patch: Partial<Strategy> & { paused?: boolean },
): Promise<ControlPayload> {
  const emergencyPause = Object.keys(patch).length === 1 && patch.paused === true;
  const headers = emergencyPause ? mutationHeaders() : await preflight("control");
  const response = await fetch("/api/control", {
    method: "PATCH",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(patch),
  });
  const body = await response.json();
  if (!response.ok)
    throw new ApiError(
      response.status,
      describeDetail(body.detail) ?? `PATCH /api/control -> ${response.status}`,
    );
  return body as ControlPayload;
}

type Capability = "control" | "lifecycle" | "strategies" | "autopilot" | "advisor";

function mutationHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "X-Tower-Api-Version": "1",
    "X-Tower-Backend-Hash": process.env.NEXT_PUBLIC_BACKEND_HASH ?? "",
  };
  if (process.env.NEXT_PUBLIC_DEV_UI !== "true") headers["X-Tower-Ui-Hash"] = process.env.NEXT_PUBLIC_UI_HASH ?? "";
  return headers;
}

async function preflight(capability: Capability): Promise<Record<string, string>> {
  let status: StatusPayload;
  try {
    status = await fetchStatus();
  } catch {
    throw new ApiError(412, "Unable to verify runtime compatibility; the command was not sent.");
  }
  const result = checkRuntimeCompatibility(status?.runtime);
  if (!result.compatible) throw new ApiError(412, `${result.reasons.join(" ")} The command was not sent.`);
  if (!status.runtime!.capabilities.includes(capability)) {
    throw new ApiError(412, `The backend does not provide the ${capability} capability; the command was not sent.`);
  }
  return mutationHeaders();
}

async function send<T>(path: string, method: string, body: unknown, capability: Capability, bypass = false): Promise<T> {
  const headers = bypass ? mutationHeaders() : await preflight(capability);
  const response = await fetch(path, {
    method,
    headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // 204 and empty bodies are not expected from any of these routes, but a
  // failed parse must still surface as the status, not as a JSON error.
  const parsed = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      (parsed && describeDetail(parsed.detail)) ?? `${method} ${path} -> ${response.status}`,
    );
  }
  return parsed as T;
}

export const fetchStrategies = () => getJson<StrategyList>("/api/strategies");
export const fetchStrategy = (name: string) =>
  getJson<Strategy>(`/api/strategies/${encodeURIComponent(name)}`);
export const saveStrategy = (name: string, body: Strategy) =>
  send<Strategy>(`/api/strategies/${encodeURIComponent(name)}`, "PUT", body, "strategies");
export const activateStrategy = (name: string) =>
  send<StrategyList>(`/api/strategies/${encodeURIComponent(name)}/activate`, "POST", undefined, "strategies");
export const deleteStrategy = (name: string) =>
  send<StrategyList>(`/api/strategies/${encodeURIComponent(name)}`, "DELETE", undefined, "strategies");

/** Queues one action for the scan loop's next pass - a game-speed nudge, say.
 *
 * Not patchControl: a patch is idempotent and a command is not. Retrying a
 * dropped patch leaves the same settings; retrying a dropped command taps
 * twice, so the two must not share a route. */
export const postCommand = (command: string) =>
  send<{ queued: string }>("/api/control/command", "POST", { command }, "control");

/** Starts the bot. A 409 (already running - another tab may have started
 * one) is a normal answer, not swallowed here: it surfaces as an ApiError
 * carrying status 409, which is what lets the control page treat it as a
 * cue to re-read the status rather than as a failure. */
export const startBot = () => send<BotStatus>("/api/bot/start", "POST", undefined, "lifecycle");

/** Ends the bot but keeps the dashboard serving. Distinct from `shutdown()`,
 * which ends the whole process - see that function's own comment. */
export const stopBot = () => send<BotStatus>("/api/bot/stop", "POST", undefined, "lifecycle", true);

/**
 * Ends the process, bot and dashboard together - the control page's "Shut
 * down" button, which confirms with "Shut down the bot AND the dashboard?".
 *
 * Explicitly NOT the "Stop bot" button sitting beside it: that is `stopBot`
 * above, which ends the bot and leaves the dashboard serving. This export
 * has already been pointed at the wrong control once, so the button it
 * belongs to is named here rather than described.
 */
export const shutdown = () => send<{ stopping: boolean }>("/api/shutdown", "POST", undefined, "lifecycle");

export const fetchAccount = () => getJson<AccountSnapshot>("/api/account", { cache: "no-store" });
export const fetchConcepts = () => getJson<ConceptCatalog>("/api/concepts");

/** Arms the read-only Home -> Settings -> Stats -> Home transaction. The scan
 *  loop walks it; this only returns the state the runner armed. A 409 means
 *  the runner refused (already running, paused, wrong screen) and a 503 that
 *  the reader it needs could not load - neither is queued for later. */
export const collectStats = () =>
  send<StatsCollection>("/api/account/collect", "POST", undefined, "lifecycle");

/** Arms one Home -> Missions -> claim -> Home walk. Same "arm now, scan loop
 *  walks it" contract as collectStats above: a 409 means another maintenance
 *  walk holds the menus (or the bot is not on a confirmed main menu), a 503
 *  means the OCR engine could not be built, and this backend advertises the
 *  route under the same `lifecycle` capability collectStats uses - there is
 *  no separate capability tag for claim walks. */
export const claimMissions = () =>
  send<ClaimSnapshot>("/api/missions/claim", "POST", undefined, "lifecycle");

/** Arms one Home -> Milestones -> Claim All -> Home walk. See claimMissions
 *  above for the shared contract. */
export const claimMilestones = () =>
  send<ClaimSnapshot>("/api/milestones/claim", "POST", undefined, "lifecycle");
