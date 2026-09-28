import { ago, gameNumber } from "@/app/fleet/reroll/workshop/workshopFormat";
import type { StateCategory } from "@/lib/fleetState";

export const DASH = "—";
export const PRICE_UNKNOWN = "price unknown";

const known = (value: number | null | undefined): value is number =>
  value !== null && value !== undefined && Number.isFinite(value);

/** A balance or count in K/M/B/T. Null is a dash, never 0. */
export function amount(value: number | null | undefined): string {
  return known(value) ? gameNumber(value) : DASH;
}

/** A price. Null is a price nobody has observed. */
export function priceText(value: number | null | undefined): string {
  return known(value) ? gameNumber(value) : PRICE_UNKNOWN;
}

/** A whole number with separators: waves, levels, scan counters. */
export function whole(value: number | null | undefined): string {
  return known(value) ? Math.round(value).toLocaleString("en-US") : DASH;
}

/** 1h 36m, 4m 05s, 42s. */
export function span(seconds: number | null | undefined): string {
  if (!known(seconds)) return DASH;
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), rest = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

/** Seconds from now until an ISO time, never below zero. */
export function secondsUntil(iso: string, nowMs: number): number {
  const at = Date.parse(iso);
  return Number.isNaN(at) ? 0 : Math.max(0, (at - nowMs) / 1000);
}

/** "5m ago" for an ISO time; a dash when there is none. */
export function agoText(iso: string | null, nowMs: number): string {
  if (!iso) return DASH;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? DASH : ago(Math.max(0, (nowMs - at) / 1000));
}

export const CAT_COLOR: Record<StateCategory, string> = {
  attack: "var(--cat-attack)", defense: "var(--cat-defense)", utility: "var(--cat-utility)",
};
export const CAT_LABEL: Record<StateCategory, string> = {
  attack: "Attack", defense: "Defense", utility: "Utility",
};

/** One accent per emulator, by its position in the fleet. */
export const ACCENTS = [
  "oklch(0.72 0.19 300)", "oklch(0.78 0.13 195)", "oklch(0.74 0.18 350)",
  "oklch(0.72 0.16 265)", "oklch(0.80 0.15 75)", "oklch(0.76 0.16 150)",
] as const;
export const accentAt = (index: number): string => ACCENTS[index % ACCENTS.length];
