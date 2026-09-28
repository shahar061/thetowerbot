/** The browser half of a worker's live stream (WS /api/stream); see the live stream design spec. */

export const FIRST_FRAME_TIMEOUT_MS = 5_000;
export const LIVE_RETRY_MS = 30_000;
/** Deltas queued in the decoder beyond this are skipped until the next keyframe. */
export const MAX_DECODE_QUEUE = 30;
/**
 * The MAX_DECODE_QUEUE skip rule only starts applying once the server's
 * "live" marker (LiveMessage) has arrived - meaning a late joiner's replayed
 * backlog is done - AND a binary message has since arrived while the
 * decoder's queue was at or below this. Until then, every frame from the
 * first keyframe on is decoded, so the backlog plays through quickly instead
 * of being skipped as "fell behind" before the decoder ever had a chance to
 * catch up. (A fresh decoder reports queue 0 before it has been given any
 * work, which is why the "live" marker - not just a small queue reading on
 * its own - is what starts this: a queue of 0 on the very first replayed
 * frame doesn't mean the decoder is keeping up with the whole backlog.)
 */
export const CAUGHT_UP_DECODE_QUEUE = 2;
/**
 * Independent hard cap: skip deltas once the queue exceeds this, whether or
 * not the decoder has ever caught up. Without it, a device that's always too
 * slow to decode in real time would never set CAUGHT_UP_DECODE_QUEUE, so the
 * queue (and latency, and memory) would grow without bound. Sized a bit above
 * STREAM_GOP_FRAMES (180) worth of slack.
 */
export const CATCH_UP_DECODE_LIMIT = 240;

export type StreamConfig = { type: "config"; codec: string; width: number; height: number };
/** Sent exactly once per connection, right after the config and any replayed GOP cache. */
export type LiveMessage = { type: "live" };
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
