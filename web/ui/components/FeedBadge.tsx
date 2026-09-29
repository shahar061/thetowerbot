/** Which feed a screen view is showing: live video, or the bot's per-scan snapshots.
 *  Views place it beside the screen, never over it: every corner of the game has UI. */
export function FeedBadge({ live }: { live: boolean }): React.JSX.Element {
  return <span data-feed={live ? "live" : "snapshots"}
    className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
    {live ? "Live" : "Snapshots"}
  </span>;
}
