"use client";

import { AlertDialog } from "@base-ui/react/alert-dialog";
import { TriangleAlert } from "lucide-react";
import type * as React from "react";
import { cn } from "@/lib/utils";

/** A modal that must be answered. Base UI traps focus, closes on Escape and
 *  returns focus to the trigger; this only adds the dashboard's card styling. */
export function ConfirmDialog({ open, onOpenChange, title, tone = "default", children, footer }: {
  open: boolean;
  onOpenChange: (open: boolean, details?: unknown) => void;
  title: React.ReactNode;
  tone?: "default" | "warn";
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  return <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
    <AlertDialog.Portal>
      <AlertDialog.Backdrop className="fixed inset-0 z-40 bg-black/50" />
      <AlertDialog.Popup className="fixed left-1/2 top-1/2 z-50 flex max-h-[85vh] w-[min(36rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-3 overflow-y-auto rounded-xl bg-card p-4 text-sm text-card-foreground shadow-xl ring-1 ring-foreground/10">
        <AlertDialog.Title className={cn("flex items-center gap-2 text-base font-semibold", tone === "warn" && "text-warn")}>
          {tone === "warn" && <TriangleAlert aria-hidden className="size-5 shrink-0" />}
          {title}
        </AlertDialog.Title>
        <div className="flex flex-col gap-2">{children}</div>
        <div className="flex flex-wrap justify-end gap-2 pt-1">{footer}</div>
      </AlertDialog.Popup>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
