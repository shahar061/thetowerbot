import { CurrencyGlyph } from "@/components/CurrencyGlyph";
import { cn } from "@/lib/utils";

/** The signed number, on its own. Kept a separate export so a caller that
 *  needs the text - a title, an aria-label - words it exactly as the chip
 *  prints it. */
export function signed(delta: number): string {
  if (delta > 0) return `+${delta.toLocaleString("en-US")}`;
  if (delta < 0) return `-${Math.abs(delta).toLocaleString("en-US")}`;
  return "0";
}

/** One currency's movement: glyph plus signed amount.
 *
 *  Three states the ledger already distinguishes, and this keeps distinct
 *  rather than collapsing into a number:
 *
 *  - `null`  "moved by an amount nobody read" - an unreadable price, or a
 *            mission reward whose digits did not OCR. Shown as `?`, dashed.
 *  - `0`     "provably nothing moved" - a skip, a rehearsal. Shown faint.
 *  - signed  a real movement. A gain takes the --live tint; a spend stays
 *            quiet, because spending is the normal case and not an alarm.
 *
 *  Direction is carried by the sign and the tint, never by the glyph: the
 *  glyph answers "which currency", and letting it also answer "which way"
 *  would give two colour systems to one chip. */
export function CurrencyAmount({
  currency,
  delta,
  className,
}: {
  currency: string;
  delta: number | null;
  className?: string;
}) {
  const unknown = delta === null;
  const label = unknown ? `unknown amount of ${currency}` : `${signed(delta)} ${currency}`;
  return (
    <span
      title={label}
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-[5px] py-px pr-1.5 pl-1 font-mono tabular-nums",
        unknown && "text-muted-foreground outline-1 -outline-offset-1 outline-dashed outline-border-strong",
        !unknown && delta > 0 && "bg-live-surface text-live",
        !unknown && delta < 0 && "bg-muted text-muted-foreground",
        !unknown && delta === 0 && "text-faint-foreground",
        className,
      )}
    >
      <CurrencyGlyph currency={currency} />
      {/* The number in its own element, so it reads back as exactly the
          text the old page printed. */}
      <span>{unknown ? "?" : signed(delta)}</span>
      <span className="sr-only">{currency}</span>
    </span>
  );
}
