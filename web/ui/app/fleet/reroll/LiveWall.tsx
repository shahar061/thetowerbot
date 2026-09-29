"use client";

import { useEffect, useRef, useState } from "react";
import { MonitorOff, X } from "lucide-react";
import type { AccountChoice } from "@/lib/api";
import type { RerollMember } from "@/lib/fleet";
import { standingFor } from "@/lib/rerollState";
import { FleetCapture } from "./FleetCapture";
import { verifiedWorkerAccount } from "./FleetLiveCard";
import { SCREEN_ASPECT, wallLayout } from "./wallLayout";

function tileLabel(member: RerollMember): string {
  return [member.name, member.tier != null && `T${member.tier}`, member.wave != null && `W${member.wave}`,
    standingFor(member.state, member.error).label].filter(Boolean).join(" · ");
}

/** Tracks the wall's content box; jsdom and old browsers fall back to the window. */
function useAreaSize(area: React.RefObject<HTMLDivElement | null>): { width: number; height: number } {
  const [size, setSize] = useState(() => typeof window === "undefined"
    ? { width: 0, height: 0 } : { width: window.innerWidth, height: window.innerHeight });
  useEffect(() => {
    if (typeof ResizeObserver === "undefined" || !area.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(area.current);
    return () => observer.disconnect();
  }, [area]);
  return size;
}

/** Browsers only allow full screen from the click itself, so the opener calls this
 *  before the wall mounts. The wall is a fixed overlay, so the whole page going
 *  full screen shows just the wall. */
export function enterWallFullscreen(): void {
  if (!document.fullscreenElement) void document.documentElement.requestFullscreen?.().catch(() => {});
}

/** Every running emulator's screen side by side, full screen, for watching the whole fleet at once. */
export function LiveWall({ members, accounts, onClose }: {
  members: RerollMember[]; accounts: AccountChoice[]; onClose: () => void;
}): React.JSX.Element {
  const area = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const size = useAreaSize(area);
  const tiles = members.flatMap(member => {
    const account = accounts.find(choice => verifiedWorkerAccount(member, choice));
    return account ? [{ member, account }] : [];
  }).sort((a, b) => a.member.name.localeCompare(b.member.name));
  const notRunning = members.length - tiles.length;
  const { columns, tileWidth } = wallLayout(tiles.length, size.width, size.height);
  const width = Math.floor(tileWidth);

  useEffect(() => {
    // Esc while in browser full screen only leaves full screen, so that closes the wall too.
    const onFullscreenChange = () => { if (!document.fullscreenElement) close.current(); };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close.current(); };
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("fullscreenchange", onFullscreenChange);
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      window.removeEventListener("keydown", onKey);
      if (document.fullscreenElement === document.documentElement) void document.exitFullscreen?.().catch(() => {});
    };
  }, []);

  return <div role="dialog" aria-modal="true" aria-label="Live wall" className="fixed inset-0 z-50 flex flex-col bg-black text-white">
    <div className="flex h-9 shrink-0 items-center gap-3 px-3 text-xs text-white/70">
      <span className="font-semibold text-white">Live wall</span>
      <span>{tiles.length} {tiles.length === 1 ? "screen" : "screens"}</span>
      {notRunning > 0 && <span>{notRunning} not running</span>}
      <button type="button" aria-label="Close live wall" title="Close (Esc)" onClick={() => close.current()}
        className="ml-auto rounded-md p-1.5 text-white/70 hover:bg-white/10 hover:text-white">
        <X className="size-4" aria-hidden="true" />
      </button>
    </div>
    <div ref={area} className="grid min-h-0 flex-1 content-center justify-center gap-2 p-2"
      style={tiles.length ? { gridTemplateColumns: `repeat(${columns}, ${width}px)` } : undefined}>
      {tiles.length ? tiles.map(({ member, account }) => (
        <section key={`${member.name}:${account.key}:${account.account_id}`} aria-label={`${member.name} live screen`} className="flex flex-col">
          <h3 className="h-7 truncate text-xs leading-7 text-white/80">{tileLabel(member)}</h3>
          <div style={{ aspectRatio: SCREEN_ASPECT }} className="overflow-hidden rounded-md">
            <FleetCapture key={`${account.key}:${account.account_id}:${account.dashboard_url}:${member.lease_id}`} fill
              dashboardUrl={account.dashboard_url!} scope={account.key} instance={member.name} accountId={member.account_id!} />
          </div>
        </section>
      )) : <p role="status" className="flex flex-col items-center gap-3 text-sm text-white/70">
        <MonitorOff className="size-6" aria-hidden="true" />No emulator has a live screen right now.
      </p>}
    </div>
  </div>;
}
