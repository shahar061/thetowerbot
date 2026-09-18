export type AccountMetrics = {
  account_id: string | null;
  game_started: string | null;
  account_age_days: number | null;
  recent_cps: number | null;
  lifetime_coins: number | null;
  lifetime_coins_incomplete: boolean;
};

export function accountAge(days: number | null): string {
  if (days === null) return "Not recorded";
  if (days === 0) return "Less than 1 day";
  return `${days} ${days === 1 ? "day" : "days"}`;
}

export function coinsPerSecond(value: number | null): string {
  if (value === null) return "Not recorded";
  return new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 }).format(value);
}
