import { cn } from "@/lib/utils";

const FILL = {
  live: "bg-live",
  warn: "bg-warn",
  danger: "bg-danger",
  primary: "bg-primary",
  chart: "bg-chart-1",
  muted: "bg-muted-foreground/50",
} as const;

/**
 * One proportion, drawn.
 *
 * The dashboard was full of pairs of numbers a reader had to divide in their
 * head - 1,240 coins against a 4,000 price, three running workers against a
 * limit of four - and a bar does that division in peripheral vision. It is a
 * real `<progress>`-shaped widget rather than two divs: the role and the
 * aria-valuenow are what make "62% of the way to affording it" reach a screen
 * reader at all, since the fill itself is decoration.
 *
 * `unknown` is a separate state on purpose, and renders as a hatched track
 * rather than an empty one. An empty bar says "zero"; a great deal of what
 * this app knows about a fresh account is not zero but unread, and the two
 * must not look alike.
 */
export function Meter({
  value,
  max,
  label,
  tone = "primary",
  unknown = false,
  className,
}: {
  value: number;
  max: number;
  /** Announced to assistive tech; the visible caption is the caller's job. */
  label: string;
  tone?: keyof typeof FILL;
  unknown?: boolean;
  className?: string;
}) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={unknown ? undefined : value}
      aria-valuetext={unknown ? "not read yet" : undefined}
      className={cn(
        "h-1.5 w-full overflow-hidden rounded-full bg-well",
        unknown && "bg-[repeating-linear-gradient(135deg,var(--muted)_0_4px,transparent_4px_8px)]",
        className,
      )}
    >
      {unknown ? null : (
        <div
          className={cn("h-full rounded-full transition-[width] duration-500", FILL[tone])}
          style={{ width: `${ratio * 100}%` }}
        />
      )}
    </div>
  );
}

/**
 * A count against a limit, as filled and empty pips.
 *
 * For the worker-slot limit, which is always a small integer and where "two of
 * four are free" is the whole message. A bar would round that into a
 * percentage; pips keep it countable at a glance.
 */
export function Pips({
  states,
  label,
}: {
  /** One entry per slot, in the order they should read. */
  states: ("running" | "starting" | "free")[];
  label: string;
}) {
  return (
    <div className="flex items-center gap-1" role="img" aria-label={label}>
      {states.map((state, index) => (
        <span
          key={index}
          className={cn(
            "h-4 w-2.5 rounded-[3px] border",
            state === "running" && "border-live bg-live/70",
            state === "starting" && "border-warn bg-warn/40 motion-safe:animate-breathe",
            state === "free" && "border-border-strong bg-transparent",
          )}
        />
      ))}
    </div>
  );
}
