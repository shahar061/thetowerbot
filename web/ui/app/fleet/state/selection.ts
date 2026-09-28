import type { FleetStateAccount } from "@/lib/fleetState";

export type SelectionMode = "all" | "live" | "custom";
export interface Selection { mode: SelectionMode; ids: string[] }

export const SELECTION_KEY = "fleet-state:selection";
export const OPEN_KEY = "fleet-state:open";
export const DEFAULT_SELECTION: Selection = { mode: "all", ids: [] };

/** The viewer's saved filter. Storage can be missing or throw (private
 * window, blocked site data); then every emulator is shown. */
export function loadSelection(): Selection {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(SELECTION_KEY) ?? "null");
    if (raw && typeof raw === "object") {
      const { mode, ids } = raw as { mode?: unknown; ids?: unknown };
      if ((mode === "all" || mode === "live" || mode === "custom") && Array.isArray(ids)
        && ids.every(id => typeof id === "string")) return { mode, ids };
    }
  } catch { /* unreadable storage: fall back to showing everything */ }
  return DEFAULT_SELECTION;
}

export function saveSelection(selection: Selection): void {
  try { window.localStorage.setItem(SELECTION_KEY, JSON.stringify(selection)); } catch { /* per-viewer convenience only */ }
}

export function loadOpen(): Record<string, boolean> {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(OPEN_KEY) ?? "{}");
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      return Object.fromEntries(Object.entries(raw).filter(([, value]) => typeof value === "boolean"));
    }
  } catch { /* unreadable storage: every section takes its default */ }
  return {};
}

export function saveOpen(open: Record<string, boolean>): void {
  try { window.localStorage.setItem(OPEN_KEY, JSON.stringify(open)); } catch { /* per-viewer convenience only */ }
}

export const isLive = (account: FleetStateAccount): boolean => account.online && account.bot.live;

export function visibleAccounts(accounts: FleetStateAccount[], selection: Selection): FleetStateAccount[] {
  if (selection.mode === "all") return accounts;
  if (selection.mode === "live") return accounts.filter(isLive);
  const chosen = new Set(selection.ids);
  return accounts.filter(account => chosen.has(account.id));
}

/** Flip one chip. Choosing every emulator again is "All". */
export function toggleAccount(selection: Selection, id: string, accounts: FleetStateAccount[]): Selection {
  const shown = new Set(visibleAccounts(accounts, selection).map(account => account.id));
  if (shown.has(id)) shown.delete(id); else shown.add(id);
  const ids = accounts.map(account => account.id).filter(key => shown.has(key));
  return ids.length === accounts.length ? DEFAULT_SELECTION : { mode: "custom", ids };
}

/** A remembered choice wins. Otherwise Workshop is open, and Cards and Labs
 * are open only while one or two columns share the screen. */
export function sectionOpen(open: Record<string, boolean>, accountId: string, section: string,
  shownCount: number): boolean {
  const key = `${accountId}:${section}`;
  if (key in open) return open[key];
  return section === "workshop" || shownCount <= 2;
}
