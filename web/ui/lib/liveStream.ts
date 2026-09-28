/** The browser half of a worker's live stream (WS /api/stream); see the live stream design spec. */

export const FIRST_FRAME_TIMEOUT_MS = 5_000;
export const LIVE_RETRY_MS = 30_000;
/** Deltas queued in the decoder beyond this are skipped until the next keyframe. */
export const MAX_DECODE_QUEUE = 30;
/**
 * The MAX_DECODE_QUEUE skip rule only starts applying once a binary message
 * has arrived while the decoder's queue was at or below this. Until then -
 * e.g. through a late joiner's replayed backlog - every frame from the first
 * keyframe on is decoded, so the backlog plays through quickly instead of
 * being skipped as "fell behind" before the decoder ever had a chance to catch up.
 */
export const CAUGHT_UP_DECODE_QUEUE = 2;

export type StreamConfig = { type: "config"; codec: string; width: number; height: number };
export type StreamFrame = { key: boolean; timestamp: number; data: Uint8Array };

export function streamUrl(dashboardUrl: string, scope?: string | null, expectedAccountId?: string | null): string {
  const url = new URL("api/stream", dashboardUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (scope) url.searchParams.set("scope", scope);
  if (expectedAccountId) url.searchParams.set("expected_account_id", expectedAccountId);
  return url.href;
}

/** A binary stream message: [flags u8 (bit 0 = keyframe)][pts u64 big-endian, µs][Annex-B access unit]. */
export function parseFrame(buffer: ArrayBuffer): StreamFrame {
  const view = new DataView(buffer);
  return { key: (view.getUint8(0) & 1) === 1, timestamp: Number(view.getBigUint64(1)), data: new Uint8Array(buffer, 9) };
}

/** WebCodecs exists only in secure contexts: localhost, or https such as Tailscale Serve. */
export function liveSupported(): boolean {
  return typeof window !== "undefined" && Boolean(window.isSecureContext) && typeof VideoDecoder !== "undefined";
}
