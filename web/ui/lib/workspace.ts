/** Match a workspace boundary, including URLs without a trailing slash. */
export function isRerollPath(pathname: string): boolean {
  return pathname === "/fleet/reroll" || pathname.startsWith("/fleet/reroll/");
}

/** Pages that use the fleet rail and shell: the reroll workspace, plus the
 * fleet-wide Fleet State page that lives outside it. */
export function isFleetWorkspacePath(pathname: string): boolean {
  return isRerollPath(pathname) || pathname === "/fleet/state" || pathname.startsWith("/fleet/state/");
}
