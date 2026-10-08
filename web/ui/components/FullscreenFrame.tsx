"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Maximize2, X } from "lucide-react";
import { cn } from "@/lib/utils";

export function FullscreenFrame({ children, className }: { children: ReactNode; className?: string }): React.JSX.Element {
  const frame = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fallback, setFallback] = useState(false);
  useEffect(() => {
    const changed = (): void => setFullscreen(document.fullscreenElement === frame.current);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  useEffect(() => {
    if (!fallback) return;
    const escape = (event: KeyboardEvent): void => { if (event.key === "Escape") setFallback(false); };
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", escape);
    return () => { document.body.style.overflow = previous; document.removeEventListener("keydown", escape); };
  }, [fallback]);
  const expanded = fullscreen || fallback;
  const toggle = async (): Promise<void> => {
    if (fullscreen) { try { await document.exitFullscreen(); } catch { /* Keep the exit control available. */ } }
    else if (fallback) setFallback(false);
    else if (frame.current?.requestFullscreen) {
      try { await frame.current.requestFullscreen(); } catch { setFallback(true); }
    } else setFallback(true);
  };
  return <div ref={frame} className={cn("relative", className,
    expanded && "!max-w-none !rounded-none !bg-black flex h-dvh w-screen flex-col items-center justify-center [&_canvas]:!h-full [&_canvas]:!w-auto [&_canvas]:!max-w-full [&_canvas]:object-contain [&_img]:!h-full [&_img]:!w-auto [&_img]:!max-w-full [&_img]:object-contain",
    fallback && "!fixed inset-0 z-[100]") }>
    {children}
    <button type="button" aria-label={expanded ? "Exit fullscreen" : "Fullscreen"}
      title={expanded ? "Exit fullscreen" : "Fullscreen"} onClick={() => { void toggle(); }}
      className="absolute left-2 top-2 z-20 rounded-md border bg-background/90 p-2 text-foreground hover:bg-accent">
      {expanded ? <X className="size-4" aria-hidden="true" /> : <Maximize2 className="size-4" aria-hidden="true" />}
    </button>
  </div>;
}
