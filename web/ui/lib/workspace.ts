/** Match a workspace boundary, including URLs without a trailing slash. */
export function isRerollPath(pathname: string): boolean {
  return pathname === "/fleet/reroll" || pathname.startsWith("/fleet/reroll/");
}
