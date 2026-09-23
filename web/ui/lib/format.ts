import type { BotEvent } from "./types";

export const clock = (ts: number): string =>
  new Date(ts * 1000).toTimeString().slice(0, 8);

export const money = (n: number | null | undefined): string =>
  n === null || n === undefined ? "-" : "$" + n;

/** Seconds as m:ss, for clocks that run for minutes rather than hours. */
export function duration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** One event, split into the fixed-width label the feed shows as a type chip
 * and the part actually worth reading.
 *
 * The feed colours the chip and leaves the message at full contrast; colouring
 * the whole line by type turns a busy feed into 400px of one hue. `describe()`
 * below reassembles the two into the single line the filter matches on, so
 * there is still exactly one place that knows how an event reads. */
export interface EventLine {
  kind: string;
  body: string;
}

export function splitEvent(event: BotEvent): EventLine {
  switch (event.type) {
    case "BattlePurchased":
      return {
        kind: "BUY",
        body: `Confirmed ${event.item} price=${money(event.price)}${event.value == null ? "" : ` value=${event.value}`}`,
      };
    case "Tapped":
      return {
        kind: "TAP",
        body: `${event.action} (${event.x},${event.y}) score=${event.score.toFixed(3)} price=${money(event.price)}`,
      };
    case "Skipped":
      return {
        kind: "SKIP",
        body: `${event.action} reason=${event.reason}${event.detail ? " " + event.detail : ""}`,
      };
    case "AutopilotDecided":
      return {
        kind: "PLAN",
        body: `${event.phase}${event.upgrade_id ? ` ${event.upgrade_id}` : ""} ${event.reason}`,
      };
    case "ScreenChanged": {
      // A live event carries `curr`. A replayed history row does not:
      // sinks/store.py's to_row() moves `curr` into the events table's
      // `screen` column before blobbing the rest, so a stored row has
      // `screen` instead. Prefer `curr`, fall back to `screen`.
      const curr = (event as { curr?: string; screen?: string }).curr ??
        (event as { curr?: string; screen?: string }).screen;
      return { kind: "SCREEN", body: `${event.prev} -> ${curr} (${event.confidence.toFixed(3)})` };
    }
    case "ScanCompleted":
      return {
        kind: "SCAN",
        body: `${event.screen} ${Math.round(event.duration_ms)}ms wallet=${money(event.wallet)}`,
      };
    case "RunStarted":
      return { kind: "RUN", body: `#${event.run_id} started${event.purpose ? ` (${event.purpose})` : ""}` };
    case "RunEnded":
      return {
        kind: "RUN",
        body: `#${event.run_id} ${event.abandoned ? "abandoned" : "ended"} wave=${event.wave ?? "?"} coins=${event.coins ?? "?"}`,
      };
    case "Navigated":
      return { kind: "NAV", body: event.target };
    case "UnknownScreen":
      return { kind: "UNKNWN", body: `best=${event.best_anchor} ${event.best_score.toFixed(3)}` };
    case "BotError":
      return { kind: "ERROR", body: event.message };
    case "ControlChanged": {
      const parts = Object.entries(event.changed)
        .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
        .join(" ");
      return { kind: "CTRL", body: `${parts} (${event.source})` };
    }
    case "Purchased":
      return {
        kind: "BUY",
        // "rehearsal" spelled out rather than a dry_run flag: this line is
        // read at a glance on a second monitor, and a purchase that did not
        // happen must not look like one that did.
        body: `${event.item} ${money(event.price)}${event.dry_run ? " (rehearsal)" : ""}`,
      };
    case "PurchaseSkipped":
      return {
        kind: "NOBUY",
        body: `${event.item} reason=${event.reason}${event.detail ? " " + event.detail : ""}`,
      };
    case "ShoppingStarted":
      return { kind: "SHOP", body: `visit #${event.visit}${event.dry_run ? " (rehearsal)" : ""}` };
    case "ShoppingEnded":
      return {
        kind: "SHOP",
        body: `visit #${event.visit} ${event.aborted ? "aborted" : "done"} bought=${event.bought} spent=${event.spent ?? "unknown"}${event.reason ? " " + event.reason : ""}`,
      };
    case "ShoppingUnavailable":
      return { kind: "SHOP", body: event.reason };
    case "SpeedAdjusted": {
      // A manual nudge from the dashboard carries neither reading nor target
      // - it means "one step from wherever it is now", so it never read the
      // widget. Rendering the arrow alone is the honest line for that; making
      // one up so both sources look alike would be the dishonest one.
      const known = event.reading != null && event.target != null;
      const journey = known
        ? ` x${event.reading!.toFixed(1)} -> x${event.target!.toFixed(1)}`
        : "";
      return { kind: "SPEED", body: `${event.direction}${journey} (${event.source})` };
    }
    case "FloatingGemClaimed":
      return {
        kind: "GEM",
        body: `+${event.delta} at (${event.point[0]},${event.point[1]}) ${event.gems_before} -> ${event.gems_after}`,
      };
    case "ClaimUncertain":
      return {
        kind: "CLAIM?",
        body: `${event.target} reason=${event.reason}${event.detail ? " " + event.detail : ""}`,
      };
    case "PageChanged":
      return {
        kind: "PAGE",
        body: `${event.prev_page} -> ${event.curr_page} (${event.confidence.toFixed(3)})`,
      };
    default:
      // An event type the UI predates. Showing its name beats dropping it.
      return { kind: (event as { type: string }).type, body: "" };
  }
}

/** One event as one line of the feed.
 *
 * Kept as the filter's haystack and as the plain-text rendering: padding the
 * label to six characters reproduces the column layout this returned before
 * the feed learned to draw chips. */
export function describe(event: BotEvent): string {
  const { kind, body } = splitEvent(event);
  return body ? `${kind.padEnd(6)} ${body}` : kind;
}
