import { ApiError } from "./api";
import { isRerollPath } from "./workspace";

export function rerollCoordinatorUrl(error: unknown, currentUrl: string): string | null {
  if (!(error instanceof ApiError) || error.status !== 503
      || error.message !== "reroll_pool_unavailable") return null;
  const url = new URL(currentUrl);
  if (url.port === "8765") return null;
  url.port = "8765";
  url.pathname = isRerollPath(url.pathname) ? `${url.pathname.replace(/\/$/, "")}/` : "/fleet/reroll/";
  return url.href;
}
