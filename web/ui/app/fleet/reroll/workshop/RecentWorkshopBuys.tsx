import type { RerollMember } from "@/lib/fleet";

export function boughtAgo(at: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - at) / 60));
  return minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
}

/** The latest verified Workshop buys, with what they cost and why. */
export function RecentWorkshopBuys({ member }: { member: RerollMember }): React.JSX.Element {
  const buys = member.recent_workshop_purchases ?? [];
  return <section className="space-y-1 border-t px-4 py-3">
    <h4 className="text-sm font-medium">Recent Workshop buys · {member.name}</h4>
    {buys.length ? <ol aria-label={`Recent Workshop buys for ${member.name}`} className="space-y-1">
      {buys.map(buy => <li key={`${buy.at}:${buy.item}`} className="rounded-lg border border-border bg-background/40 px-2 py-1 text-xs">
        <div className="flex justify-between gap-2"><span className="font-medium">{buy.item}</span><span className="text-muted-foreground">{boughtAgo(buy.at)}</span></div>
        <p className="text-muted-foreground">{`${buy.cost ?? "?"} coins · ${buy.reason ?? "Reason not recorded"}`}</p>
      </li>)}
    </ol> : <p className="text-xs text-muted-foreground">No verified Workshop buys recorded yet.</p>}
  </section>;
}
