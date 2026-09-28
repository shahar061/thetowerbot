import { STATE_CATEGORIES, type FleetStateRunUpgrades } from "@/lib/fleetState";
import { CAT_COLOR, CAT_LABEL, whole } from "./stateFormat";

/** Total, the attack/defense/utility split and the top 8 upgrades as bars. */
export function RunUpgradesChart({ data }: { data: FleetStateRunUpgrades | null }): React.JSX.Element {
  if (data === null) return <p className="fs-none">No run recorded yet</p>;
  const top = data.items.slice(0, 8);
  const max = Math.max(1, ...top.map(item => item.levels));
  return (
    <div className="fs-upg">
      <div className="fs-upt"><b>{whole(data.total)}</b><small>levels bought {data.scope === "current" ? "this run" : "last run"}</small></div>
      {data.total > 0 && (
        <div className="fs-split" role="img"
          aria-label={STATE_CATEGORIES.map(c => `${CAT_LABEL[c]} ${data.by_category[c]}`).join(", ")}>
          {STATE_CATEGORIES.map(c => <i key={c} style={{ flex: data.by_category[c], background: CAT_COLOR[c] }} />)}
        </div>)}
      <div className="fs-leg">
        {STATE_CATEGORIES.map(c => (
          <span key={c}><i style={{ background: CAT_COLOR[c] }} aria-hidden="true" />{CAT_LABEL[c]} <b>{data.by_category[c]}</b></span>))}
      </div>
      {top.map(item => (
        <div className="fs-ub" key={item.id}>
          <span>{item.name}</span>
          <div className="b"><i style={{ width: `${(item.levels / max) * 100}%`, background: item.category ? CAT_COLOR[item.category] : "var(--muted-foreground)" }} /></div>
          <em>{item.levels}</em>
        </div>))}
    </div>);
}
