/** Which feed a screen view is showing: live video, or the bot's per-scan snapshots. */
export function FeedBadge({ live }: { live: boolean }): React.JSX.Element {
  return <span data-feed={live ? "live" : "snapshots"}
    className="pointer-events-none absolute left-2 top-2 rounded bg-background/90 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
    {live ? "Live" : "Snapshots"}
  </span>;
}
