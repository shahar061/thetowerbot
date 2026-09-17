import { ApiError } from "./api";

export function rerollCoordinatorUrl(error: unknown, currentUrl: string): string | null {
  if (!(error instanceof ApiError) || error.status !== 503
      || error.message !== "reroll_pool_unavailable") return null;
  const url = new URL(currentUrl);
  if (url.port === "8765") return null;
  url.port = "8765";
  url.pathname = "/fleet/reroll/";
  url.search = "";
  url.hash = "";
  return url.href;
}
