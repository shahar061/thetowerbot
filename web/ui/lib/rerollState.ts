import type { MachineState } from "@/components/StatusBadge";
import type { RerollMember } from "./fleet";

/**
 * What a reroll worker's `state` string actually means to the person watching.
 *
 * The pool, the supervisor and the host each contribute states to the same
 * field (`fleet/reroll_supervisor.py`, `fleet/reroll_pool.py`), and the page
 * used to render them by replacing underscores with spaces - so
 * `identity_changed`, `capacity_wait` and `running` all arrived on screen as
 * the same weight of grey text, and the only way to learn that one of them is
 * an emergency was to know the codebase.
 *
 * Every state is classified here once, on three axes a reader actually needs:
 * the four-colour machine vocabulary the rest of the dashboard already speaks,
 * whether a human is the thing being waited on, and whether the supervisor is
 * mid-move (in which case the card should read as busy rather than as settled
 * in this state).
 */
export type DeviceStanding = {
  tone: MachineState;
  label: string;
  /** One sentence: what this state is, and what - if anything - it wants. */
  hint: string;
  /** Nothing on this device moves until a person does something. */
  needsYou: boolean;
  /** A transition the supervisor is driving; it will resolve itself. */
  transient: boolean;
};

const STANDINGS: Record<string, DeviceStanding> = {
  running: {
    tone: "live", label: "Running", needsYou: false, transient: false,
    hint: "The worker is playing. Its plan and vitals below are live.",
  },
  starting: {
    tone: "warn", label: "Starting", needsYou: false, transient: true,
    hint: "The emulator is booting and the worker is attaching to it.",
  },
  stopping: {
    tone: "warn", label: "Stopping", needsYou: false, transient: true,
    hint: "The worker is shutting down; the slot frees when it does.",
  },
  capacity_wait: {
    tone: "idle", label: "Waiting for a slot", needsYou: false, transient: true,
    hint: "Every concurrent worker slot is taken. This one starts when one frees.",
  },
  paused: {
    tone: "idle", label: "Paused", needsYou: false, transient: false,
    hint: "Held by you. Start it to put it back in the rotation.",
  },
  ready: {
    tone: "idle", label: "Ready", needsYou: false, transient: false,
    hint: "Prepared and idle. Start it whenever there is a free slot.",
  },
  stopped: {
    tone: "idle", label: "Stopped", needsYou: false, transient: false,
    hint: "The emulator is not running.",
  },
  start_required: {
    tone: "idle", label: "Needs a start", needsYou: false, transient: false,
    hint: "The emulator is stopped. Starting the worker boots it first.",
  },
  needs_choice: {
    tone: "warn", label: "Needs your choice", needsYou: true, transient: false,
    hint: "Tier 1 Wave 60 is cleared and the first Ultimate Weapon is yours to pick - the bot will not choose a one-way upgrade.",
  },
  replace_manually: {
    tone: "error", label: "Replace manually", needsYou: true, transient: false,
    hint: "This account cannot be carried further. Remove this emulator, then add a freshly prepared one.",
  },
  unverified: {
    tone: "warn", label: "Unverified", needsYou: true, transient: false,
    hint: "No account identity has been confirmed on this device yet, so nothing it reports can be trusted to belong to one account.",
  },
  identity_changed: {
    tone: "error", label: "Identity changed", needsYou: true, transient: false,
    hint: "The account on this device is not the one the pool leased. Its history and this device's history no longer describe the same account.",
  },
  interrupted: {
    tone: "error", label: "Interrupted", needsYou: true, transient: false,
    hint: "The worker stopped without finishing. Check the journal before restarting it.",
  },
  // Only the supervisor's start path writes "failed": the worker process never
  // launched. A worker that launched and then exited reads as "stopped".
  failed: {
    tone: "error", label: "Failed to start", needsYou: true, transient: false,
    hint: "The worker couldn't be started. The error below says why; the journal has the details.",
  },
  tower_already_opened: {
    tone: "error", label: "Tower already opened", needsYou: true, transient: false,
    hint: "This emulator's Tower has been played before, so it cannot start a fresh reroll account. Prepare one with Tower installed and unopened.",
  },
  protected_template: {
    tone: "idle", label: "Protected template", needsYou: false, transient: false,
    hint: "The template emulator the others are cloned from. It is never enrolled.",
  },
  host_state_unavailable: {
    tone: "warn", label: "Host state unavailable", needsYou: true, transient: false,
    hint: "The host could not report this emulator's power state, so the pool cannot tell whether it is running.",
  },
  pool_state_unreadable: {
    tone: "warn", label: "Pool state unreadable", needsYou: true, transient: false,
    hint: "The pool file could not be read. Nothing here is current.",
  },
  reroll_process_state_unreadable: {
    tone: "warn", label: "Process state unreadable", needsYou: true, transient: false,
    hint: "The supervisor could not read the worker process's state.",
  },
  instance_not_in_pool: {
    tone: "warn", label: "Not in the pool", needsYou: true, transient: false,
    hint: "The supervisor was asked about an emulator the pool does not hold.",
  },
};

const OPENED_TOWER = "The Tower was already opened on this emulator, so it can't start a fresh account - it is probably a clone of one that was played. Remove it and add an emulator with The Tower installed but never opened.";

/** Error codes (from `fleet/reroll_supervisor.py` and `fleet/reroll_pool.py`)
 *  a person can act on, in their words. Keyed by the code before any ": detail". */
const FAILURE_HINTS: Record<string, string> = {
  worker_registration_missing_for_opened_tower: OPENED_TOWER,
  tower_already_opened: OPENED_TOWER,
  tower_state_unavailable: "The bot couldn't read The Tower's state over adb, usually because the emulator was still booting. Start it again.",
  tower_not_installed: "The Tower isn't installed on this emulator. Install it (without opening it), then add the emulator again.",
  android_boot_not_completed: "The emulator started but Android didn't finish booting within 3 minutes. Start it again.",
  protected_template: "This is the template emulator the others are cloned from. It is never played.",
  host_identity_changed_after_start: "The emulator came back from its start with a different identity than the pool recorded. Check the journal before retrying.",
  worker_registration_unverified: "The account registration saved for this emulator no longer matches it. Check the journal before retrying.",
  worker_identity_binding_changed: "The account registration saved for this emulator no longer matches it. Check the journal before retrying.",
};

export function failureHint(error?: string | null): string | null {
  return error ? FAILURE_HINTS[error.split(":")[0].trim()] ?? null : null;
}

/** The phrase the page shows for any state, including one this build predates.
 *
 *  An unknown state is deliberately treated as something to look at rather
 *  than as something benign: a state added on the Python side and not here is
 *  far more likely to be a new failure mode than a new happy path, and a UI
 *  that silently greys it out is the UI that hides it.
 */
export function standingFor(state: string, error?: string | null): DeviceStanding {
  const hint = state === "failed" ? failureHint(error) : null;
  if (hint) return { ...STANDINGS.failed, hint };
  return STANDINGS[state] ?? {
    tone: "warn", label: state.replaceAll("_", " "), needsYou: true, transient: false,
    hint: "This dashboard does not recognise that state. The journal is the authority on it.",
  };
}

/** Whether a card may be deleted from the device list. A Ready or Running
 *  device is healthy and stays; anything else (failed, paused, stopped, in
 *  transition) can be cleared away. A hidden card can always be restored. */
export function deletable(member: { state: string; hidden?: boolean }): boolean {
  return !!member.hidden || (member.state !== "ready" && member.state !== "running");
}

/** Sort key: what needs a person, first; then what is alive; then the rest.
 *  A pool of eight devices is read top-down once and then glanced at, so the
 *  card that wants something has to be the card at the top. */
export function attentionRank(state: string): number {
  const standing = standingFor(state);
  if (standing.needsYou) return standing.tone === "error" ? 0 : 1;
  if (standing.tone === "live") return 2;
  if (standing.transient) return 3;
  return 4;
}

/** The finish line, from `fleet/reroll_planner.py:STONES_WAVE`. Past it the
 *  account has stones and the Ultimate Weapon pick belongs to a human. */
export const STONES_WAVE = 60;

/** The reroll ladder, as the operator climbs it.
 *
 *  `id` is `RerollDecision.stage`, which is the build id the planner ran -
 *  `opening` below wave 20, `turtle` above it (see `reroll_planner._GOALS`).
 *  The third rung is not a build: it is the hand-off at wave 60. */
export const LADDER = [
  { id: "opening", title: "Opening", goal: "Reach Tier 1 Wave 20", target: 20,
    blurb: "Buy attack paced by Coins/Wave, then Coins/Wave, then unlock Thorns." },
  { id: "turtle", title: "Turtle", goal: "Reach Tier 1 Wave 60", target: STONES_WAVE,
    blurb: "Keep defense ahead of enemy damage and add cash and coin income." },
  { id: "stones", title: "Ultimate Weapon", goal: "You pick the first Ultimate Weapon", target: null,
    blurb: "Stones are earned. Choose Golden Tower or Black Hole yourself; if neither is offered, replace the emulator." },
] as const;

export type LadderProgress = {
  /** Index into LADDER, or 0 when nothing has been verified yet. */
  rung: number;
  /** 0-100 across the whole ladder, for one continuous bar. */
  percent: number;
  /** Best verified Tier 1 wave, or null when no run has been read. */
  wave: number | null;
  /** Waves left to the next rung, null once the ladder is finished. */
  remaining: number | null;
};

/** Where one account stands on the ladder, from the single number that
 *  decides it everywhere else: the best VERIFIED Tier 1 wave. A null wave is
 *  "no run has been read yet", which is not the same as wave zero and is why
 *  `wave` survives into the result rather than being clamped on the way in. */
export function ladderProgress(best: number | null | undefined): LadderProgress {
  const wave = best ?? null;
  const reached = Math.max(0, wave ?? 0);
  if (reached >= STONES_WAVE) return { rung: 2, percent: 100, wave, remaining: null };
  if (reached >= 20) {
    const span = (reached - 20) / (STONES_WAVE - 20);
    return { rung: 1, percent: 33 + span * 67, wave, remaining: STONES_WAVE - reached };
  }
  return { rung: 0, percent: (reached / 20) * 33, wave, remaining: 20 - reached };
}

/** `decision.decide()`'s five states, as a reader meets them.
 *
 *  These are the whole point of the plan strip: "save_coins" and
 *  "observe_price" are both "not buying yet" but they want completely
 *  different things from the operator - one is arithmetic, the other is a
 *  screenshot - and the page has to say which. */
export const DECISIONS: Record<string, { label: string; tone: MachineState; hint: string }> = {
  buy: { label: "Buying", tone: "live",
    hint: "Affordable and within the spend reserve. The worker buys it on its next Workshop visit." },
  save_coins: { label: "Saving coins", tone: "idle",
    hint: "The price is known and the coins are not there yet. Nothing to do but earn." },
  observe_price: { label: "Reading the price", tone: "warn",
    hint: "Nobody has read this row's price, and no constant can stand in for it. The worker goes and looks." },
  observe_balance: { label: "Reading the balance", tone: "warn",
    hint: "The price is known and the wallet is not. The worker goes and looks." },
  needs_operator: { label: "Needs you", tone: "warn",
    hint: "Nothing on the plan is both ready and unheld, so there is no next purchase to name." },
};

export function decisionFor(state: string) {
  return DECISIONS[state] ?? { label: state.replaceAll("_", " "), tone: "warn" as MachineState,
    hint: "This dashboard does not recognise that decision state." };
}

/** A stable hue per emulator name.
 *
 *  The journal and the shared Workshop ledger both label rows by which device
 *  produced them, and a reader scanning either one is matching on colour
 *  before they read the name. Deriving the hue from the name means the two
 *  panels agree without either knowing about the other - and that the colour
 *  survives a device being removed and re-added.
 *
 *  The coordinator sends its own colour on journal entries; that one wins
 *  where it exists, and this is the fallback and the ledger's only source.
 */
const DEVICE_COLORS = ["#2563eb", "#b45309", "#7c3aed", "#047857", "#be185d", "#0e7490"];

export function deviceColor(name: string): string {
  let hash = 0;
  for (const char of name) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0;
  return DEVICE_COLORS[hash % DEVICE_COLORS.length];
}

/** Seconds as `2h 14m` - play time to wave 20 runs for hours. */
export function hoursMinutes(seconds: number): string {
  const minutes = Math.max(0, Math.floor(seconds / 60));
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}

/** The card's one line on this account's race to Tier 1 Wave 20. */
export function playToW20(member: RerollMember): string | null {
  if (member.play_seconds_to_t1w20 != null) return `W20 in ${hoursMinutes(member.play_seconds_to_t1w20)}`;
  if (member.play_seconds_so_far != null) return `${hoursMinutes(member.play_seconds_so_far)} played, not yet W20`;
  return null;
}
