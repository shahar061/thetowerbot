# Telegram settings design

## Intent

Let a person control when the bot sends Telegram updates and which facts each update contains, from a Settings page at the bottom of both the single emulator and reroll fleet side menus. Show an accurate, clearly labeled sample message while they edit. Keep Telegram one-way.

## Existing behavior

`telegram_report.py` sends a fixed single-bot digest at startup and then hourly by default. The token and chat ID come from `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; the interval can be overridden by an environment variable or CLI flag. Fleet worker processes inherit those credentials and can each send a digest. The web app has no Telegram API or settings storage.

## User experience

The shared sidebar places **Settings** in its bottom section, above Guide and the theme/status controls. The single workspace links to `/settings/`; the fleet workspace links to `/fleet/reroll/settings/`. Both routes render the same settings component with a workspace-specific profile. The settings pages remain available when no game account is selected or no bot is running.

Each page shows:

- A Telegram connection state: configured if both environment values exist, otherwise which value is missing. Never return or display the token. The chat ID may be shown only in a masked form.
- An enable switch and a frequency control in minutes. The default profile is enabled, but sending requires credentials. The default frequency is 60 minutes unless the legacy interval environment variable supplies another initial value. Accept whole numbers from 1 to 1,440 minutes. Disabling stops future scheduled updates.
- Field switches for the selected workspace and a message preview that updates before saving. The preview uses fixed, visibly labeled sample data and performs no network send.
- Save, reset to saved values, loading, success, and validation/error states. An unsaved edit must not change live reporting.

Single emulator fields are status and uptime (always included), screen, scans, wallet, current run, completed runs, taps, skips, and last error. Fleet fields are fleet status and member count (always included), each emulator's name and state (always included), tier/wave, lifetime coins, milestone, and errors. Optional fields are enabled by default to retain the current single digest's detail and give a useful fleet digest. A missing value renders as unavailable; it must never be presented as zero.

The previews look like the following. Each preview is labeled **Sample message — no message sent** in the app; the label is outside the actual Telegram body.

```text
The Tower bot - running, up 3h 15m
Screen: BATTLE
Scans: 5,412
Wallet: $1,284,330
Run #42: 15m in
Runs completed: 3
Taps: buy_upgrade 91, retry 3
Skips: unaffordable 40
Last error: none
```

```text
Reroll fleet - running, 3 emulators
Air18 - running | Tier 1 Wave 72 | Lifetime coins: 14.2K | Milestone: Tier 1 Wave 50
Air19 - paused | Tier 1 Wave 31 | Lifetime coins unavailable | Milestone unavailable
Air20 - error | Tier/Wave unavailable | Lifetime coins: 8.1K | Error: game screen unavailable
```

## Sending behavior

There are two distinct profiles, `single` and `fleet`, each with its own enabled flag, interval, and selected fields. A standalone bot uses the single profile. A coordinator uses the fleet profile while at least one emulator is registered in its reroll pool and otherwise uses the single profile. It switches profiles as membership changes, without running two reporters. Fleet messages use the coordinator's reroll snapshot as the source. Reroll workers do not send their own periodic messages, avoiding duplicates. The coordinator must not send an empty single-bot digest while a fleet is active merely because it has credentials.

The first enabled message is sent when reporting starts, preserving the existing startup signal. Subsequent sends use the saved interval. Saving a profile while reporting is running applies it without a process restart: change the next due time from the save, and stop scheduling immediately when disabled. Re-enabling schedules the next send according to the new interval; it does not send a surprise message merely because Save was pressed. Transient snapshot and network failures are recorded and logged without stopping future attempts. Keep Telegram's 4,096-character limit and plain-text sending.

The token and chat ID remain environment configuration. The settings file contains no secrets. `--no-telegram` remains a hard override for the process. An explicit `--telegram-interval` remains an override for that process, and the UI indicates when the saved interval is overridden so it does not imply that Save changed the actual frequency. The legacy `TELEGRAM_SUMMARY_SECONDS` value supplies the initial single-profile default when no saved profile exists.

## Components and data flow

1. A small settings store owns validated `single` and `fleet` profiles. It persists JSON with atomic replacement beside a headless standalone bot's SQLite file or under the web coordinator's fleet root directory, including when that coordinator currently has no fleet members. The coordinator's single profile is used while its reroll pool is empty; it is not applied to fleet workers. Corrupt or inaccessible settings produce an explicit API error rather than silently erasing them.
2. `GET /api/telegram/settings?mode=single|fleet` returns the selected profile and safe credential/configuration status. `PUT /api/telegram/settings?mode=single|fleet` validates and saves that profile, then notifies the active reporter. `POST /api/telegram/preview?mode=single|fleet` accepts an unsaved profile and returns a mock example from the exact server renderer used for real messages; it never sends a message.
3. The reporter reads the active profile through the store and supports wakeup when it changes. The existing single snapshot renderer gains field selection. A separate fleet renderer formats a bounded number of emulator rows from the coordinator's reroll snapshot. A trailing count states how many rows were omitted if the message approaches Telegram's limit.
4. The Next.js API helper uses the local backend without account scoping. The two static-export routes share one client component. Sidebar mode detection uses the fleet route so the fleet Settings page retains fleet navigation.

## Errors and boundaries

- Reject unknown field names, invalid modes, non-integer or out-of-range intervals, and malformed request bodies with 422. Do not save a partially valid profile.
- A missing token or chat ID keeps the page editable but reports that sending is unavailable. A saved enabled profile begins sending when credentials are supplied and the process restarts.
- Telegram API failures never expose the token in logs or API responses. The preview never contacts Telegram.
- A fleet snapshot with no members still sends a meaningful fleet status and zero-member count. One bad member record cannot invalidate the whole message.
- Existing fleet workers may continue their old reporter until they restart after deployment; all newly launched workers use the new suppression rule.

## Verification

Run only focused tests for the settings store, both renderers, reporter rescheduling, Telegram settings API, API helper, settings component, and sidebar/account-shell routes. Check a sample single and fleet message against the preview structure. Verify that saving affects scheduling without restart and that fleet worker launches suppress individual digests. Do not run the broad repository test suite.
