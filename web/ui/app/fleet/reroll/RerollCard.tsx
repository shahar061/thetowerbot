"use client";

import { useState, type ReactNode } from "react";
import { SectionCard } from "@/components/ui/section-card";

export function RerollCard({ title, children, tone }: {
  title: string; children: ReactNode; tone?: "live" | "warn" | "danger";
}) {
  const [collapsed, setCollapsed] = useState(false);
  return <SectionCard title={title} tone={tone} action={<button
    type="button" aria-label={`${collapsed ? "Expand" : "Collapse"} ${title}`}
    aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}
    className="rounded border px-2 py-1 text-lg leading-none"
  >{collapsed ? "⌄" : "⌃"}</button>}>
    {!collapsed && children}
  </SectionCard>;
}
