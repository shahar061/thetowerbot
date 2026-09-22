import { cn } from "@/lib/utils";

/** The shapes, one per currency the ledger can carry.
 *
 *  Shape is what identifies a currency; colour (--currency-<id> in
 *  globals.css) only reinforces it. Colour alone would fail colour-blind
 *  readers, and would collide with the page's status colours - a gold coin
 *  sits right beside --warn, and a green gem would read as --live's "gain".
 *
 *  Cut-outs are drawn in var(--card) so they read as holes on the card the
 *  glyph sits on, in both themes. */
const SHAPES: Record<string, React.ReactNode> = {
  coins: (
    <>
      <circle cx="8" cy="8" r="6.6" fill="currentColor" />
      <circle cx="8" cy="8" r="3.5" fill="none" stroke="var(--card)" strokeWidth="1.4" />
    </>
  ),
  gems: (
    <>
      <polygon points="8,1.4 14.6,6.3 8,14.6 1.4,6.3" fill="currentColor" />
      <polyline points="1.4,6.3 14.6,6.3" fill="none" stroke="var(--card)" strokeWidth="1.1" />
      <polyline points="5.2,6.3 8,14.6 10.8,6.3" fill="none" stroke="var(--card)" strokeWidth="0.9" />
    </>
  ),
  stones: (
    <>
      <polygon points="8,1.3 13.8,4.6 13.8,11.4 8,14.7 2.2,11.4 2.2,4.6" fill="currentColor" />
      <polygon points="8,5 10.6,6.5 10.6,9.5 8,11 5.4,9.5 5.4,6.5" fill="none" stroke="var(--card)" strokeWidth="1" />
    </>
  ),
  medals: (
    <polygon
      points="8,1.2 10,5.9 15,6.2 11.1,9.4 12.4,14.4 8,11.6 3.6,14.4 4.9,9.4 1,6.2 6,5.9"
      fill="currentColor"
    />
  ),
  cells: (
    <>
      <rect x="6" y="1" width="4" height="2.2" rx=".6" fill="currentColor" />
      <rect x="3.2" y="2.6" width="9.6" height="12.4" rx="2" fill="currentColor" />
      <polyline points="8.8,5 6.6,9 9.4,9 7.2,13" fill="none" stroke="var(--card)" strokeWidth="1.2" />
    </>
  ),
  keys: (
    <>
      <circle cx="5" cy="8" r="3.8" fill="currentColor" />
      <rect x="7.5" y="6.9" width="7.6" height="2.3" rx=".6" fill="currentColor" />
      <rect x="12" y="8.6" width="2" height="3" fill="currentColor" />
      <circle cx="5" cy="8" r="1.4" fill="var(--card)" />
    </>
  ),
  shards: (
    <>
      <polygon points="8,1.2 14.8,14.4 1.2,14.4" fill="currentColor" />
      <polyline points="8,1.2 8,14.4" fill="none" stroke="var(--card)" strokeWidth="1" />
    </>
  ),
  tickets: (
    <>
      <path
        d="M1.2 4 H14.8 V6.6 A1.6 1.6 0 0 0 14.8 9.4 V12 H1.2 V9.4 A1.6 1.6 0 0 0 1.2 6.6 Z"
        fill="currentColor"
      />
      <line x1="10.5" y1="4.6" x2="10.5" y2="11.4" stroke="var(--card)" strokeWidth="1" strokeDasharray="1.3 1.1" />
    </>
  ),
  bits: (
    <>
      <rect x="2" y="2" width="5.4" height="5.4" rx=".8" fill="currentColor" />
      <rect x="8.6" y="2" width="5.4" height="5.4" rx=".8" fill="currentColor" opacity=".55" />
      <rect x="2" y="8.6" width="5.4" height="5.4" rx=".8" fill="currentColor" opacity=".55" />
      <rect x="8.6" y="8.6" width="5.4" height="5.4" rx=".8" fill="currentColor" />
    </>
  ),
};

/** A currency this component has no shape for - a versioned resource id, or
 *  one added to currencies.py later. A hollow ring rather than nothing: the
 *  amount beside it still needs a marker, and a missing glyph would make it
 *  look like a different kind of number. */
const FALLBACK = <circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" strokeWidth="1.8" />;

export function CurrencyGlyph({
  currency,
  className,
}: {
  currency: string;
  className?: string;
}) {
  const known = currency in SHAPES;
  return (
    <svg
      viewBox="0 0 16 16"
      // Decorative: every place a glyph appears also carries the currency's
      // name for assistive tech, so announcing the shape would say it twice.
      aria-hidden="true"
      focusable="false"
      data-currency={currency}
      className={cn("inline-block size-3.5 shrink-0", className)}
      style={{ color: known ? `var(--currency-${currency})` : "var(--muted-foreground)" }}
    >
      {known ? SHAPES[currency] : FALLBACK}
    </svg>
  );
}
