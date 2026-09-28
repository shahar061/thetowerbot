"use client";

import { useEffect, useRef } from "react";
import {
  CAUGHT_UP_DECODE_QUEUE, FIRST_FRAME_TIMEOUT_MS, MAX_DECODE_QUEUE, parseFrame, streamUrl, type StreamConfig,
} from "@/lib/liveStream";

/**
 * A worker's live H.264 stream, decoded in the browser (WebCodecs) and drawn
 * to a canvas. The host never decodes anything. Unmounting closes the stream.
 * Any failure calls onUnavailable once, so the parent can show the MJPEG
 * snapshots instead.
 */
export function LiveVideo({ dashboardUrl, scope, expectedAccountId, label, className, onUnavailable, onFrame }: {
  /** The worker to stream from. Defaults to this page's own origin (the device page). */
  dashboardUrl?: string;
  scope?: string | null;
  expectedAccountId?: string | null;
  label: string;
  className?: string;
  onUnavailable: () => void;
  onFrame?: () => void;
}): React.JSX.Element {
  const canvas = useRef<HTMLCanvasElement>(null);
  const callbacks = useRef({ onUnavailable, onFrame });
  useEffect(() => { callbacks.current = { onUnavailable, onFrame }; });

  useEffect(() => {
    const socket = new WebSocket(streamUrl(dashboardUrl ?? `${window.location.origin}/`, scope, expectedAccountId));
    socket.binaryType = "arraybuffer";
    let decoder: VideoDecoder | null = null;
    let waitingForKey = true;
    // Becomes true once a message has arrived while the decoder's queue was
    // small - only then does falling behind again mean skipping to the next
    // keyframe. Until then (e.g. a late joiner's replayed backlog), every
    // frame decodes so the backlog plays through quickly.
    let caughtUp = false;
    let drawn = false;
    let finished = false;
    let timer = 0;

    const stop = (): void => {
      finished = true;
      window.clearTimeout(timer);
      socket.onmessage = null;
      socket.onclose = null;
      if (socket.readyState < 2) socket.close();
      if (decoder && decoder.state !== "closed") decoder.close();
    };
    const fail = (): void => {
      if (finished) return;
      stop();
      callbacks.current.onUnavailable();
    };
    const draw = (frame: VideoFrame): void => {
      const target = canvas.current;
      const context = target?.getContext("2d");
      if (target && context) {
        if (target.width !== frame.displayWidth) target.width = frame.displayWidth;
        if (target.height !== frame.displayHeight) target.height = frame.displayHeight;
        context.drawImage(frame, 0, 0);
        if (!drawn) {
          drawn = true;
          callbacks.current.onFrame?.();
        }
      }
      frame.close();
    };

    timer = window.setTimeout(() => { if (!drawn) fail(); }, FIRST_FRAME_TIMEOUT_MS);
    socket.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
      try {
        if (typeof event.data === "string") {
          const config = JSON.parse(event.data) as StreamConfig;
          if (decoder && decoder.state !== "closed") decoder.close();
          decoder = new VideoDecoder({ output: draw, error: fail });
          decoder.configure({ codec: config.codec, codedWidth: config.width, codedHeight: config.height,
            optimizeForLatency: true });
          waitingForKey = true;
          caughtUp = false;
          return;
        }
        if (!decoder || decoder.state !== "configured") return;
        if (!caughtUp && decoder.decodeQueueSize <= CAUGHT_UP_DECODE_QUEUE) caughtUp = true;
        const frame = parseFrame(event.data);
        if (!frame.key && (waitingForKey || (caughtUp && decoder.decodeQueueSize > MAX_DECODE_QUEUE))) {
          // A delta is useless without everything since its keyframe: before
          // the first one, or once a caught-up decoder falls behind, wait for
          // the next. Before the decoder has ever caught up - e.g. a late
          // joiner's replayed backlog - every frame decodes instead, so the
          // backlog plays through quickly rather than being thrown away.
          waitingForKey = true;
          return;
        }
        waitingForKey = false;
        decoder.decode(new EncodedVideoChunk({ type: frame.key ? "key" : "delta", timestamp: frame.timestamp,
          data: frame.data }));
      } catch {
        fail();
      }
    };
    socket.onclose = fail;
    return stop;
  }, [dashboardUrl, scope, expectedAccountId]);

  return <canvas ref={canvas} role="img" aria-label={label} className={className} />;
}
