# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions cron job that, at 7am Melbourne, builds a daily briefing from Gmail + Calendar + Weather, runs it through Claude, and delivers it as an end-to-end encrypted web push to an iOS PWA at `https://johnwogrady-max.github.io/Daily-reminder/`. The pipeline lives in `scripts/daily_alert.py`, the push sender in `scripts/send_push.js`, the PWA shell in `docs/`, all wired together by `.github/workflows/daily-alert.yml`. There is no application server, no test suite beyond a weather API smoke test, and no build step.

## Architecture (the parts that span files)

The pipeline is linear and runs once per invocation:

1. `google_credentials()` mints a fresh access token from a long-lived `GOOGLE_REFRESH_TOKEN` (scopes: `gmail.readonly`, `calendar.readonly`).
2. `fetch_emails()` makes three Gmail queries: (a) `in:sent newer_than:1d` to collect thread IDs the user has already replied in, (b) recent inbox (last 24h, excluding promotions/social) with those replied-in threads filtered out, and (c) follow-up threads (1–7d old where the last message does *not* carry the `SENT` label — used for the "awaiting reply" section). The `SENT` label check is deliberate: From-header matching missed replies sent from aliases / send-as addresses.
3. `fetch_events()` enumerates **every selected, non-deleted calendar** the user has reader access to, then merges 7 days of events sorted by start time. Do not collapse this back to `primary` — that was a deliberate fix (commit `764526d`).
4. `fetch_weather()` calls Google Maps Weather API (`weather.googleapis.com/v1`) for current conditions + 12h forecast at hardcoded Melbourne CBD lat/lon (`-37.8136, 144.9631`).
5. `summarise()` sends everything to Claude (`claude-sonnet-4-6`, `max_tokens=16000`, `thinking={"type": "adaptive"}`) using `SYSTEM_PROMPT` which strictly defines five sections (umbrella / weather / new emails / follow up / this week) with **no markdown** — the PWA renders plain text + emoji headers only.
6. `write_briefing_json()` writes `{generated_at, headline, body}` to `_briefing.json` at the repo root (gitignored). This file holds the real briefing for the next step and is **never committed** — the public repo only ever sees the static placeholder at `docs/briefing.json`.
7. The workflow then runs `scripts/send_push.js`, which reads `_briefing.json`, packs the full body into the encrypted web-push payload (truncated to ~2.8 KB to stay under the ~4 KB ciphertext limit), and ships it to the iPhone via the `web-push` npm package + VAPID keys. It exits 0 on `404`/`410` (subscription gone) so a stale subscription doesn't fail the run.
8. The service worker on the iPhone receives the encrypted push, stashes the full body in the Cache API at `./cached-briefing.json`, and shows a notification with just the headline. The PWA reads from that local cache when opened — the public site only serves a placeholder.

### Cron-hour gating (don't "simplify" this)

GitHub Actions cron is UTC-only, but Melbourne switches between AEDT (UTC+11) and AEST (UTC+10). The workflow registers **two** cron entries (`20:00 UTC` and `21:00 UTC`), and `main()` checks `datetime.now(MELBOURNE).hour != 7` to skip whichever one isn't 7am locally. Removing either cron, or removing the hour gate, will break daylight-saving handling. `FORCE_RUN=1` (set automatically on `workflow_dispatch`) bypasses the gate.

### Refresh token bootstrap

`scripts/get_refresh_token.py` is a **one-time local helper**, not part of the runtime path. It runs `InstalledAppFlow` against a `client_secret.json` (Desktop OAuth client, gitignored) and prints the three Google secrets to paste into GitHub Actions. Never run this in CI.

### PWA bootstrap (also one-time)

The iOS PWA needs three things wired before it works end-to-end:

1. A VAPID key pair generated locally with `npx web-push generate-vapid-keys` and stored as `VAPID_PUBLIC_KEY` + `VAPID_PRIVATE_KEY` GitHub secrets. Optional `VAPID_SUBJECT` (a `mailto:` URL) — defaults to `mailto:noreply@example.com`.
2. The same `VAPID_PUBLIC_KEY` pasted into the `VAPID_PUBLIC_KEY` constant at the top of `docs/app.js`. The public key on the client must match the one signing the push.
3. A `PUSH_SUBSCRIPTION` GitHub secret containing the JSON object the PWA produces when the user taps "Enable notifications" on the installed iOS PWA. iOS requires the PWA be installed via Add to Home Screen *before* push permission can be granted; permission requested in Safari proper is silently denied.

GitHub Pages must be enabled for the `/docs` folder of `main` (Settings → Pages). The PWA URL is `https://<owner>.github.io/<repo>/`.

### Privacy model

The repo can be public without leaking briefing content. The real body never lives on the public site:

- `_briefing.json` (gitignored) is written by Python, read by the Node push step, and discarded with the runner.
- Web-push payloads are end-to-end encrypted between the workflow and the subscribed device — push services (Apple, Google, Mozilla) cannot decrypt them.
- The service worker is the only place the body is decrypted; it stashes a copy in the device-local Cache API.
- `docs/briefing.json` is a generic placeholder shown if the PWA is opened before any push has landed (or on a different device).
- Notification preview on the lock screen shows the umbrella headline only. iOS Settings → Notifications → Briefing → Show Previews → "When Unlocked" hides even that until the phone is unlocked.

## Commands

```bash
# Install deps (Python 3.12 in CI)
pip install -r requirements.txt

# Run the briefing locally — requires ALL secrets in env, plus FORCE_RUN
# unless it's actually 7am in Melbourne
FORCE_RUN=1 \
ANTHROPIC_API_KEY=... \
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

**Always required**: `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_WEATHER_API_KEY`. `env()` exits with code 1 on the first missing one.

**Push delivery**: `VAPID_PUBLIC_KEY` + `VAPID_PRIVATE_KEY` + `PUSH_SUBSCRIPTION` (web push to the PWA — skip silently if `PUSH_SUBSCRIPTION` is absent); `VAPID_SUBJECT` (defaults to a placeholder mailto).

## Conventions worth knowing

- **No markdown in the briefing output.** The prompt forbids `*`, `_`, `#`. Emoji are used as section headers. The PWA's `<pre>` relies on this.
- **The headline is the umbrella line.** `headline()` extracts the first non-empty line of the briefing; that's the ~200-char string that appears in the iOS notification. Keep the umbrella section first in `SYSTEM_PROMPT`.
- **`docs/briefing.json` is a static placeholder, never updated by the workflow.** The real body is delivered only via the encrypted push payload. Don't reintroduce a step that writes real content to it. If you rename the field shape (`generated_at`, `headline`, `body`), update `docs/app.js` and `docs/service-worker.js` together.
- **The workflow has `permissions: contents: read`.** Don't add `contents: write` unless you are reintroducing a commit-back step (which would defeat the privacy model).
- **`did_run` output gates the push step.** `daily_alert.py` writes `did_run=true` to `GITHUB_OUTPUT` only when it actually generated a briefing (i.e. wasn't skipped by the hour gate). Without it, an off-hour cron would re-push the previous day's content.
- **Push payload size limit ~4 KB encrypted.** `send_push.js` truncates the body at ~2.8 KB plaintext with an explanatory tail. If briefings start getting cut, either trim the prompt or shrink the truncation marker.
- **Node 24 opt-in.** The workflow sets `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` to suppress Node 20 deprecation warnings (commit `089cb0b`). Keep it unless GitHub changes default runtimes.
- **Don't hallucinate in the prompt.** `SYSTEM_PROMPT` ends with an explicit "Never hallucinate senders, meeting titles, attendees, or weather figures" — preserve this if you edit the prompt.
