"use client";

import { useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/ui/section-card";
import { cn } from "@/lib/utils";

export function RerollCard({ title, children, tone, action, defaultCollapsed }: {
  title: string; children: ReactNode; tone?: "live" | "warn" | "danger";
  /** Controls that belong to the panel itself - a filter row, a count. They
   *  sit beside the collapse toggle and disappear with the body. */
  action?: ReactNode;
  defaultCollapsed?: boolean;
}) {
  const [collapsed, setCollapsed] = useState(!!defaultCollapsed);
  return <SectionCard title={title} tone={tone} action={<div className="flex items-center gap-2">
    {collapsed ? null : action}
    <Button
      variant="ghost" size="icon-sm" aria-label={`${collapsed ? "Expand" : "Collapse"} ${title}`}
      aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}
    ><ChevronDown className={cn("transition-transform", !collapsed && "rotate-180")} aria-hidden="true" /></Button>
  </div>}>
    {!collapsed && children}
  </SectionCard>;
}
