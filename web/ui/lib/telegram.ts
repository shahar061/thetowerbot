export type TelegramMode = "single" | "fleet";

export type TelegramProfile = {
  enabled: boolean;
  interval_minutes: number;
  fields: string[];
};

export type TelegramSettingsResponse = {
  mode: TelegramMode;
  profile: TelegramProfile;
  configured: boolean;
  token_present: boolean;
  chat_id_present: boolean;
  chat_id_masked: string | null;
  suppressed: boolean;
  interval_overridden: boolean;
  effective_interval_seconds: number | null;
};

export type TelegramField = { id: string; label: string; description: string };

export const TELEGRAM_FIELDS: Record<TelegramMode, TelegramField[]> = {
  single: [
    { id: "screen", label: "Screen", description: "The game screen the bot last saw." },
    { id: "scans", label: "Scans", description: "How many times the bot has checked the game." },
    { id: "wallet", label: "Wallet", description: "The last observed battle cash balance." },
    { id: "run", label: "Current run", description: "Run number and elapsed time, when available." },
    { id: "runs_completed", label: "Completed runs", description: "Runs finished since this bot started." },
    { id: "taps", label: "Taps", description: "Most common actions the bot took." },
    { id: "skips", label: "Skips", description: "Most common reasons the bot did not act." },
    { id: "last_error", label: "Last error", description: "The latest recorded error, if any." },
  ],
  fleet: [
    { id: "tier_wave", label: "Tier and wave", description: "Latest observed progress for each emulator." },
    { id: "lifetime_coins", label: "Lifetime coins", description: "Observed lifetime coins for each account." },
    { id: "milestone", label: "Milestone", description: "Latest recorded milestone for each emulator." },
    { id: "errors", label: "Errors", description: "Current worker errors, when present." },
  ],
};
