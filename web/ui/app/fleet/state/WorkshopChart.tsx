import type { WorkshopSkill } from "@/lib/fleetState";
import { cn } from "@/lib/utils";
import { DASH, priceText, whole } from "./stateFormat";

export function workshopLevelText(skill: WorkshopSkill): string {
  if (skill.level === null) return DASH;
  if (skill.level_min != null && skill.level_max != null && skill.level_min !== skill.level_max) {
    return `${whole(skill.level_min)}–${whole(skill.level_max)}`;
  }
  return whole(skill.level);
}

export function estimatedPrice(skill: WorkshopSkill): boolean {
  return skill.next_cost_source === "catalog_estimate" || skill.next_cost_source === "stat_ladder";
}

/** Same horizontal level bars as run purchases, with explicit unknowns and prices. */
export function WorkshopChart({ label, skills, nextId, color }: {
  label: string; skills: WorkshopSkill[]; nextId: string | null; color: string;
}): React.JSX.Element {
  const max = Math.max(1, ...skills.filter(skill => !skill.locked).map(skill => skill.level ?? 0));
  return <div className="fs-workshop-chart" role="region" aria-label={`${label} levels`} tabIndex={0}>
    <p className="fs-note">Workshop levels · next purchase price</p>
    {skills.length === 0 && <p className="fs-none">Nothing read yet</p>}
    {skills.map(skill => {
      const next = skill.id === nextId && !skill.locked;
      const known = !skill.locked && skill.level !== null;
      const range = skill.level_min != null && skill.level_max != null && skill.level_min !== skill.level_max;
      return <div key={skill.id} className={cn("fs-workshop-bar", next && "next", skill.locked && "locked")}>
        <div className="fs-workshop-bar-head">
          <span>{skill.name}{next && <span className="fs-tn">NEXT</span>}</span>
          <span className="n">{skill.locked ? "locked" : skill.status === "maxed" ? "MAX" : priceText(skill.next_cost)}</span>
        </div>
        <div className="fs-workshop-bar-value">
          {known ? <div className="b" role="meter" aria-label={`${skill.name} level`}
            aria-valuemin={0} aria-valuemax={max} aria-valuenow={skill.level!}
            aria-valuetext={range ? `Between ${skill.level_min} and ${skill.level_max}` : undefined}>
            <i style={{ width: `${(skill.level! / max) * 100}%`, background: color }} />
          </div> : <span className="fs-dim">{skill.locked ? "Next unlock" : "Level unknown"}</span>}
          <span className="n">{skill.locked ? DASH : workshopLevelText(skill)}</span>
        </div>
        {!skill.locked && skill.next_cost !== null && estimatedPrice(skill) && <small className="fs-price-source">Estimated price</small>}
      </div>;
    })}
  </div>;
}
