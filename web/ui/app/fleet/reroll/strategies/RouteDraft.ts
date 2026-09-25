import type { BuildRouteDocument } from "@/lib/buildRoute";

export type DraftState = { route: BuildRouteDocument; expectedRevision: number };

export function beginDraft(saved: BuildRouteDocument): DraftState {
  return { route: structuredClone(saved), expectedRevision: saved.revision };
}

export function changeWorkshop(state: DraftState, change: Partial<BuildRouteDocument["baseline"]["workshop"]>): DraftState {
  const workshop = { ...state.route.baseline.workshop, ...change };
  return { ...state, route: { ...state.route, baseline: { ...state.route.baseline, workshop } } };
}

export function movePriority(state: DraftState, id: string, direction: -1 | 1): DraftState {
  const ids = [...state.route.baseline.workshop.priority_ids];
  const from = ids.indexOf(id);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= ids.length) return state;
  [ids[from], ids[to]] = [ids[to], ids[from]];
  return changeWorkshop(state, { priority_ids: ids, mode: "priorities" });
}

export function setBan(state: DraftState, id: string, banned: boolean): DraftState {
  const ids = new Set(state.route.baseline.workshop.banned_upgrade_ids);
  if (banned) ids.add(id); else ids.delete(id);
  return changeWorkshop(state, { banned_upgrade_ids: [...ids].sort() });
}

export function setSpendLimit(state: DraftState, pct: number): DraftState {
  return changeWorkshop(state, { coin_spend_limit_pct: pct });
}

export function setDrawChance(state: DraftState, pct: number): DraftState {
  return changeWorkshop(state, { draw_chance_pct: pct });
}

export function setWeight(state: DraftState, id: string, weight: number): DraftState {
  return changeWorkshop(state, { weights: { ...state.route.baseline.workshop.weights, [id]: weight } });
}

export function bindOverride(state: DraftState, worker: string, accountId: string): DraftState {
  return { ...state, route: { ...state.route, overrides: {
    ...state.route.overrides, [worker]: { account_id: accountId, patches: {} },
  } } };
}

export function resetOverride(state: DraftState, worker: string): DraftState {
  const overrides = { ...state.route.overrides };
  delete overrides[worker];
  return { ...state, route: { ...state.route, overrides } };
}

export function scopedWorkshop(state: DraftState, worker: string | null, accountId: string | null
                               ): BuildRouteDocument["baseline"]["workshop"] {
  const baseline = state.route.baseline.workshop;
  if (!worker || !accountId) return baseline;
  const override = state.route.overrides[worker];
  if (!override || override.account_id !== accountId) return baseline;
  return { ...baseline, ...(override.patches[baseline.id] ?? {}) } as typeof baseline;
}

export function patchWorkshop(state: DraftState, worker: string | null, accountId: string | null,
                              change: Partial<BuildRouteDocument["baseline"]["workshop"]>): DraftState {
  if (!worker || !accountId) return changeWorkshop(state, change);
  const baseline = state.route.baseline.workshop;
  const existing = state.route.overrides[worker];
  const patches = existing?.account_id === accountId ? existing.patches : {};
  return { ...state, route: { ...state.route, overrides: {
    ...state.route.overrides,
    [worker]: { account_id: accountId, patches: {
      ...patches, [baseline.id]: { ...(patches[baseline.id] ?? {}), ...change },
    } },
  } } };
}

export function draftErrors(route: BuildRouteDocument, knownIds: Set<string>): string[] {
  const rule = route.baseline.workshop;
  const errors: string[] = [];
  if (!Number.isInteger(route.baseline.gems.spend_limit_pct) || route.baseline.gems.spend_limit_pct < 0 || route.baseline.gems.spend_limit_pct > 100)
    errors.push("Gem spend limit must be between 0 and 100%.");
  if ([rule.coin_spend_limit_pct, rule.draw_chance_pct].some(value => !Number.isInteger(value) || value < 0 || value > 100))
    errors.push("Percentages must be between 0 and 100.");
  if (Object.values(rule.weights).some(value => !Number.isInteger(value) || value < 1))
    errors.push("Weights must be positive whole numbers.");
  if (rule.draw_chance_pct > 0 && (rule.mode !== "priorities" || !rule.priority_ids.length))
    errors.push("Weighted luck needs at least one route priority.");
  if (Object.keys(rule.weights).some(id => !rule.priority_ids.includes(id)))
    errors.push("A weighted upgrade is no longer in the priority list.");
  if ([...rule.priority_ids, ...rule.banned_upgrade_ids, ...Object.keys(rule.weights)].some(id => !knownIds.has(id)))
    errors.push("A Workshop upgrade is missing from the catalog.");
  if (rule.priority_ids.some(id => rule.banned_upgrade_ids.includes(id)))
    errors.push("A priority is in Never Buy; remove it from one list.");
  const ruleIds = new Set([rule.id, ...route.baseline.battle.branches.flatMap(branch =>
    [branch.id, ...branch.phases.map(phase => phase.id)])]);
  if (Object.entries(route.dependencies).some(([id, dependencies]) =>
    !ruleIds.has(id) || dependencies.some(dependency => !ruleIds.has(dependency))))
    errors.push("Broken dependency: a linked route node no longer exists.");
  for (const [worker, override] of Object.entries(route.overrides)) {
    const patch = override.patches[rule.id];
    if (!patch) continue;
    const effective = { ...rule, ...patch } as typeof rule;
    if ([effective.coin_spend_limit_pct, effective.draw_chance_pct].some(value =>
      !Number.isInteger(value) || value < 0 || value > 100))
      errors.push(`${worker}: percentages must be between 0 and 100.`);
    if (effective.priority_ids.some(id => effective.banned_upgrade_ids.includes(id)))
      errors.push(`${worker}: a priority is in Never Buy.`);
    if (effective.draw_chance_pct > 0 && (effective.mode !== "priorities" || !effective.priority_ids.length))
      errors.push(`${worker}: weighted luck needs a route priority.`);
  }
  if (route.baseline.battle.mode === "phases") {
    if (!route.baseline.battle.branches.some(branch => branch.min_best_tier_1_wave === null))
      errors.push("Battle flow needs a fallback branch.");
    for (const branch of route.baseline.battle.branches) {
      const ordered = [...branch.phases].sort((a, b) => a.start_wave - b.start_wave);
      if (ordered.some((phase, index) => phase.start_wave < 1 ||
        (phase.end_wave !== null && phase.end_wave < phase.start_wave) ||
        (index > 0 && (ordered[index - 1].end_wave === null ||
          ordered[index - 1].end_wave! >= phase.start_wave))))
        errors.push(`${branch.id}: battle phases overlap or have invalid waves.`);
      if (ordered.some(phase => phase.draw_chance_pct > 0 && !phase.priority_ids.length))
        errors.push(`${branch.id}: weighted phase needs upgrade priorities.`);
    }
  }
  return errors;
}
