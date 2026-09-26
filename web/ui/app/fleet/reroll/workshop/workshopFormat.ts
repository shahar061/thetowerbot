import type { WorkshopLevelRow } from "@/lib/types";

export const CATEGORIES = ["ATTACK", "DEFENSE", "UTILITY"] as const;
export const STALE_SECONDS = 24 * 3600;

/** The Workshop tab's theme hue, defined once in globals.css. */
export const CATEGORY_COLOR: Record<string, string> = {
  ATTACK: "var(--ws-attack)", DEFENSE: "var(--ws-defense)", UTILITY: "var(--ws-utility)",
};

const SUFFIXES = ["", "K", "M", "B", "T", "q", "Q"];
/** Coin prices the way the game prints them: 331, 1.23K, 4.68T. */
export function gameNumber(value: number): string {
  let tier = 0;
  while (Math.abs(value) >= 1000 && tier < SUFFIXES.length - 1) { value /= 1000; tier += 1; }
  return tier === 0 ? String(Math.round(value)) : `${value.toFixed(2)}${SUFFIXES[tier]}`;
}

export function ago(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/** Share of the ladder climbed; the lower bound of an ambiguous read. */
export function progress(row: WorkshopLevelRow): number | null {
  if (row.status === "maxed") return 1;
  return row.level_min === null ? null : row.level_min / row.max_level;
}

/** Levels bought so far; the lower bound of an ambiguous read, 0 when unread. */
export function levelsBought(row: WorkshopLevelRow): number {
  return row.status === "maxed" ? row.max_level : row.level_min ?? 0;
}

export function levelText(row: WorkshopLevelRow): string {
  if (row.level_min === null) return "?";
  return row.level_min === row.level_max ? String(row.level_min) : `${row.level_min}–${row.level_max}`;
}

/** Mean climbed share over the rows that have a read, or null when none do. */
export function averageShare(rows: (WorkshopLevelRow | undefined)[]): number | null {
  const shares = rows.flatMap(row => row ? progress(row) ?? [] : []);
  return shares.length ? shares.reduce((a, b) => a + b, 0) / shares.length : null;
}
