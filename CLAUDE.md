# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single-purpose GitHub Actions cron job that sends a 7am Melbourne daily briefing to Telegram. The whole pipeline lives in `scripts/daily_alert.py` (~300 lines) and is invoked by `.github/workflows/daily-alert.yml`. There is no application server, no test suite beyond a weather API smoke test, and no build step.

## Architecture (the parts that span files)

The pipeline is linear and runs once per invocation:

1. `google_credentials()` mints a fresh access token from a long-lived `GOOGLE_REFRESH_TOKEN` (scopes: `gmail.readonly`, `calendar.readonly`).
2. `fetch_emails()` makes two Gmail queries: recent inbox (last 24h, excluding promotions/social) and follow-up threads (1–7d old where the last message is *not* from the user — used for the "awaiting reply" section).
3. `fetch_events()` enumerates **every selected, non-deleted calendar** the user has reader access to, then merges 7 days of events sorted by start time. Do not collapse this back to `primary` — that was a deliberate fix (commit `764526d`).
4. `fetch_weather()` calls Google Maps Weather API (`weather.googleapis.com/v1`) for current conditions + 12h forecast at hardcoded Melbourne CBD lat/lon (`-37.8136, 144.9631`).
5. `summarise()` sends everything to Claude (`claude-opus-4-7`, `max_tokens=16000`, `thinking={"type": "adaptive"}`) using `SYSTEM_PROMPT` which strictly defines five sections (umbrella / weather / new emails / follow up / this week) with **no markdown** — Telegram receives plain text + emoji headers only.
6. `push()` POSTs to the Telegram Bot API.

### Cron-hour gating (don't "simplify" this)

GitHub Actions cron is UTC-only, but Melbourne switches between AEDT (UTC+11) and AEST (UTC+10). The workflow registers **two** cron entries (`20:00 UTC` and `21:00 UTC`), and `main()` checks `datetime.now(MELBOURNE).hour != 7` to skip whichever one isn't 7am locally. Removing either cron, or removing the hour gate, will break daylight-saving handling. `FORCE_RUN=1` (set automatically on `workflow_dispatch`) bypasses the gate.

### Refresh token bootstrap

`scripts/get_refresh_token.py` is a **one-time local helper**, not part of the runtime path. It runs `InstalledAppFlow` against a `client_secret.json` (Desktop OAuth client, gitignored) and prints the three Google secrets to paste into GitHub Actions. Never run this in CI.

## Commands

```bash
# Install deps (Python 3.12 in CI)
pip install -r requirements.txt

# Run the briefing locally — requires ALL secrets in env, plus FORCE_RUN
# unless it's actually 7am in Melbourne
FORCE_RUN=1 \
ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... \
GOOGLE_WEATHER_API_KEY=... \
python scripts/daily_alert.py

# Trigger the workflow manually (tick "force" to bypass the hour gate)
# via GitHub UI: Actions -> Daily morning alert -> Run workflow

# Mint a Google refresh token (LOCAL ONLY, needs client_secret.json at repo root)
pip install google-auth-oauthlib
python scripts/get_refresh_token.py
```

There is no linter, type-checker, or unit-test runner configured. The only automated check is `.github/workflows/test-weather.yml`, which curls the weather API and commits the response to `test-results/` with `[skip ci]`. It only runs when that workflow file itself changes (or via `workflow_dispatch`).

## Required secrets (GitHub Actions)

`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_WEATHER_API_KEY`. `env()` exits with code 1 on the first missing one.

## Conventions worth knowing

- **Telegram, not Pushover.** Delivery was migrated (commit `0ea5813`); there is no character cap and `SYSTEM_PROMPT` explicitly tells Claude to use the space, not pad it.
- **No markdown in the briefing output.** Telegram is sent as plain text; the prompt forbids `*`, `_`, `#`. Emoji are used as section headers.
- **Node 24 opt-in.** The workflow sets `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` to suppress Node 20 deprecation warnings (commit `089cb0b`). Keep it unless GitHub changes default runtimes.
- **Don't hallucinate in the prompt.** `SYSTEM_PROMPT` ends with an explicit "Never hallucinate senders, meeting titles, attendees, or weather figures" — preserve this if you edit the prompt.
