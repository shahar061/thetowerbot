"use client";

import { useState } from "react";
import type { MatchBox } from "@/lib/types";
import { accountScope } from "@/lib/accountScope";

export function DeviceView({
  boxes,
  size,
}: {
  boxes: MatchBox[];
  size: { width: number; height: number } | null;
}) {
  const [overlay, setOverlay] = useState(true);
  const best = boxes.length ? boxes.reduce((a, b) => (b.score > a.score ? b : a)) : null;
  const scope = accountScope();

  return (
    <div className="flex flex-col">
      {/* The boxes are absolutely positioned in percentages of the frame's own
          pixel dimensions, so the image can be any size on screen and the
          overlay follows it - no coordinate maths in two languages. */}
      <div className="relative w-full overflow-hidden rounded-t-xl bg-well">
        {size ? (
          <>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={`/api/frame${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`} alt="device screen" className="block w-full" />
            {overlay
              ? boxes.map((box) => (
                  <div key={`${box.name}-${box.x}-${box.y}`}>
                    <div
                      title={`${box.name} ${box.score.toFixed(3)}`}
                      // The state is the assertable thing here, not the hue -
                      // the palette moved once already and took a test with it.
                      data-state={box.tapped ? "tapped" : "matched"}
                      className={`absolute border-[1.5px] ${
                        box.tapped ? "border-live bg-live/12" : "border-warn bg-warn/12"
                      }`}
                      style={{
                        left: `${(box.x / size.width) * 100}%`,
                        top: `${(box.y / size.height) * 100}%`,
                        width: `${(box.w / size.width) * 100}%`,
                        height: `${(box.h / size.height) * 100}%`,
                      }}
                    >
                      {/* The name used to live only in a title attribute, which
                          is useless at the distance this panel is read from. */}
                      <span
                        className={`absolute -top-[13px] left-[-1.5px] rounded-sm px-1 text-[9px] font-bold leading-[13px] whitespace-nowrap ${
                          box.tapped ? "bg-live text-live-foreground" : "bg-warn text-warn-foreground"
                        }`}
                      >
                        {box.name} {box.score.toFixed(2)}
                      </span>
                    </div>
                    {/* The label itself is a button - tapping it opens an
                        info panel instead of buying anything - so the real
                        tap lands on the buy square beside it, at (tap_x,
                        tap_y). Drawn as a small dot, visually distinct from
                        the box outline, so it reads as "here", not as
                        another rectangle. */}
                    <div
                      title={`${box.name} tap point`}
                      className="absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
                      style={{
                        left: `${(box.tap_x / size.width) * 100}%`,
                        top: `${(box.tap_y / size.height) * 100}%`,
                      }}
                    >
                      {/* One expanding ring when the tap actually lands. The
                          one place in the app where motion is load-bearing:
                          you see the tap rather than reading that it happened. */}
                      {box.tapped ? (
                        <span className="absolute -inset-1 rounded-full border-2 border-live motion-safe:animate-ripple" />
                      ) : null}
                    </div>
                  </div>
                ))
              : null}

            <label className="absolute bottom-2 left-2 flex items-center gap-1.5 rounded-md bg-background/70 px-2 py-1 text-[11px] text-muted-foreground backdrop-blur-sm">
              <input
                type="checkbox"
                checked={overlay}
                onChange={(e) => setOverlay(e.target.checked)}
                className="accent-primary"
              />
              matches
            </label>
          </>
        ) : (
          // size is null until the first publish() - every other panel on
          // this page has something to say before its first data arrives;
          // this was the one bare blank spot.
          <p className="aspect-[9/16] p-4 text-sm text-muted-foreground">
            Waiting for the first frame…
          </p>
        )}
      </div>

      {/* The strongest match, spelled out. The overlay says where; this says
          what and how confidently, without hovering anything. */}
      <div className="truncate border-t px-3 py-2 font-mono text-[11px] text-muted-foreground">
        {best ? (
          <>
            {best.name} {best.score.toFixed(3)}{" "}
            <span className="text-faint-foreground">
              → ({best.tap_x},{best.tap_y})
            </span>
          </>
        ) : (
          <span className="text-faint-foreground">no matches this scan</span>
        )}
      </div>
    </div>
  );
}
