"use client";

import {
  Activity, BookOpen, Compass, Contact, ChartLine, List, Power, Receipt, SlidersHorizontal, TriangleAlert,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { StatusBadge } from "@/components/StatusBadge";
import { ThemeToggle } from "@/components/ThemeToggle";
import { fetchErrors, fetchStrategies } from "@/lib/api";
import { useConnected } from "@/lib/useEventStream";
import { cn } from "@/lib/utils";

type Item = { href: string; label: string; icon: typeof Activity };

// Grouped by what you came here to do. "Watch" is the second-monitor half;
// "Configure" is the half that changes what the bot does.
const GROUPS: { label: string; items: Item[] }[] = [
  {
    label: "Watch",
    items: [
      { href: "/", label: "Live", icon: Activity },
      { href: "/runs/", label: "Runs", icon: List },
      { href: "/stats/", label: "Stats", icon: ChartLine },
      { href: "/errors/", label: "Errors", icon: TriangleAlert },
      { href: "/ledger/", label: "Ledger", icon: Receipt },
      { href: "/account/", label: "Account", icon: Contact },
      { href: "/director/", label: "Director", icon: Compass },
    ],
  },
  {
    label: "Configure",
    items: [
      { href: "/strategy/", label: "Strategy", icon: SlidersHorizontal },
      { href: "/control/", label: "Control", icon: Power },
    ],
  },
];

const GUIDE: Item = { href: "/guide/", label: "Guide", icon: BookOpen };

export function Sidebar() {
  const pathname = usePathname();
  // Read from the shared stream rather than opening one here: this component
  // is on every page, including the two that already subscribe via
  // useControlSync.
  const connected = useConnected();
  const [errorCount, setErrorCount] = useState<number | null>(null);
  const [active, setActive] = useState<string | null>(null);

  // Slow polls: neither of these changes often, and the rail is on every page.
  useEffect(() => {
    const load = () => {
      fetchErrors(100).then((rows) => setErrorCount(rows.length)).catch(() => {});
      fetchStrategies().then((list) => setActive(list.active)).catch(() => {});
    };
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, []);

  function link({ href, label, icon: Icon }: Item) {
    const isActive = pathname === href;
    const isErrors = href === "/errors/";
    return (
      <Link
        key={href}
        href={href}
        aria-current={isActive ? "page" : undefined}
        className={cn(
          "flex items-center gap-2.5 rounded-md border-l-2 border-transparent px-2.5 py-1.5 text-sm transition-colors",
          isActive
            ? "border-l-primary bg-primary/12 font-medium text-foreground"
            : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
        )}
      >
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        <span className="truncate">{label}</span>
        {/* On a second monitor this badge is most of the point of having a
            rail: it is the only thing that reports trouble from a page that
            is not about trouble. */}
        {isErrors && errorCount ? (
          <span className="ml-auto rounded-full bg-danger-surface px-1.5 font-mono text-[10px] font-bold text-danger">
            {errorCount > 99 ? "99+" : errorCount}
          </span>
        ) : null}
      </Link>
    );
  }

  return (
    <nav className="flex shrink-0 gap-1 overflow-x-auto border-b bg-sidebar p-2 md:h-dvh md:w-52 md:flex-col md:gap-0 md:overflow-visible md:border-b-0 md:border-r md:p-3">
      <div className="hidden pb-4 pl-2.5 md:block">
        <div className="font-mono text-xs font-bold uppercase tracking-[0.14em] text-primary">
          The Tower
        </div>
        {/* Which strategy is loaded is the one piece of bot state worth
            carrying on every page - it is what every rule on /strategy edits. */}
        <div className="mt-0.5 truncate font-mono text-[10px] text-faint-foreground">
          {active ?? "…"}
        </div>
      </div>

      {GROUPS.map((group) => (
        <div key={group.label} className="contents md:block">
          <div className="hidden px-2.5 pb-1.5 pt-3 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-faint-foreground md:block">
            {group.label}
          </div>
          {group.items.map(link)}
        </div>
      ))}

      <div className="contents md:mt-auto md:block">
        {link(GUIDE)}
        <div className="mt-3 hidden items-center gap-2 md:flex">
          <StatusBadge state={connected ? "live" : "warn"}>
            {connected ? "live" : "no bot"}
          </StatusBadge>
          <ThemeToggle />
        </div>
      </div>

      {/* On the mobile strip the badge rides beside the theme toggle - the
          rail is horizontal there and has no footer to sit in. */}
      <div className="ml-auto flex items-center gap-2 self-center md:hidden">
        <StatusBadge state={connected ? "live" : "warn"}>
          {connected ? "live" : "no bot"}
        </StatusBadge>
        <ThemeToggle />
      </div>
    </nav>
  );
}
