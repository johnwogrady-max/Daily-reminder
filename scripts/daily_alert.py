"""Daily 7am Melbourne briefing: emails + weather + calendar -> Pushover.

Invoked by .github/workflows/daily-alert.yml. Reads secrets from env.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

MELBOURNE = ZoneInfo("Australia/Melbourne")
LAT, LON = -37.8136, 144.9631
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: env var {name} not set", file=sys.stderr)
        sys.exit(1)
    return v


def google_credentials() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=env("GOOGLE_REFRESH_TOKEN"),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=env("GOOGLE_CLIENT_ID"),
        client_secret=env("GOOGLE_CLIENT_SECRET"),
        scopes=GOOGLE_SCOPES,
    )
    creds.refresh(GoogleRequest())
    return creds


def fetch_emails(creds: Credentials) -> list[dict]:
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    q = "newer_than:1d in:inbox -category:promotions -category:social"
    listing = svc.users().messages().list(userId="me", q=q, maxResults=30).execute()
    out: list[dict] = []
    for m in listing.get("messages", []):
        msg = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append(
            {
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": msg.get("snippet", ""),
                "unread": "UNREAD" in msg.get("labelIds", []),
            }
        )
    return out


def fetch_events(creds: Credentials) -> list[dict]:
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=7)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=40,
        )
        .execute()
    )
    events: list[dict] = []
    for e in resp.get("items", []):
        start = e.get("start", {})
        end = e.get("end", {})
        start_str = start.get("dateTime") or start.get("date") or ""
        end_str = end.get("dateTime") or end.get("date") or ""
        attendee_list = e.get("attendees") or []
        attendee_names = [
            a.get("displayName") or a.get("email", "").split("@")[0]
            for a in attendee_list
            if not a.get("self")
        ]
        events.append(
            {
                "summary": e.get("summary", "(no title)"),
                "start": start_str,
                "end": end_str,
                "location": e.get("location", ""),
                "description": (e.get("description") or "")[:200],
                "attendees": attendee_names,
                "all_day": "date" in start and "dateTime" not in start,
            }
        )
    return events


def fetch_weather() -> dict:
    key = env("GOOGLE_WEATHER_API_KEY")
    base = "https://weather.googleapis.com/v1"
    params = {
        "key": key,
        "location.latitude": LAT,
        "location.longitude": LON,
    }
    cur = requests.get(f"{base}/currentConditions:lookup", params=params, timeout=20)
    cur.raise_for_status()
    fc = requests.get(
        f"{base}/forecast/hours:lookup",
        params={**params, "hours": 12},
        timeout=20,
    )
    fc.raise_for_status()
    return {"current": cur.json(), "forecast": fc.json()}


SYSTEM_PROMPT = """You write a daily 7am push notification briefing for a busy professional in Melbourne.

Hard rules:
- TOTAL length <= 1000 characters. No preamble, no sign-off, no markdown.
- Use exactly these four sections in order:

\u2614 YES or NO \u2014 one sentence umbrella call, cite rain % or mm expected.
\U0001F324 <temp now>\u00b0C, <condition>, H<high>\u00b0/L<low>\u00b0

\U0001F4E7 Emails (<N> in 24h):
Be opinionated. Only flag emails that need a reply, a decision, or signal something important (e.g. from a boss, client, lawyer, bank, school). Name the sender and say in 5 words why it matters. Skip anything automated, transactional, or informational. If nothing needs action say 'nothing needs action'.

\U0001F4C5 This week:
List ALL events for the next 7 days. Format each as: <Day> <time> \u2014 <title> [with <names> if attendees] [@ <location> if set]. Use short day names (Mon, Tue etc). For all-day events omit the time. Group multiple events on the same day together. If no events say 'clear'.

Never hallucinate senders, meeting titles, or attendees.
"""


def summarise(emails: list[dict], events: list[dict], wx: dict) -> str:
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    user_payload = {
        "today_melbourne": datetime.now(MELBOURNE).strftime("%A %d %b %Y"),
        "weather": wx,
        "emails": emails,
        "events": events,
    }
    resp = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Write today's briefing. Raw data follows as JSON:\n\n"
                    + __import__("json").dumps(user_payload, default=str)
                ),
            }
        ],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(p.strip() for p in parts if p.strip()).strip()


def push(message: str) -> None:
    r = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": env("PUSHOVER_APP_TOKEN"),
            "user": env("PUSHOVER_USER_KEY"),
            "title": "Daily briefing",
            "message": message,
            "priority": 0,
        },
        timeout=20,
    )
    r.raise_for_status()


def main() -> int:
    local_hour = datetime.now(MELBOURNE).hour
    force = os.environ.get("FORCE_RUN") == "1"
    if local_hour != 7 and not force:
        print(f"Skipping: Melbourne local hour is {local_hour}, not 7. Set FORCE_RUN=1 to override.")
        return 0

    creds = google_credentials()
    emails = fetch_emails(creds)
    events = fetch_events(creds)
    wx = fetch_weather()
    message = summarise(emails, events, wx)
    if not message:
        message = "(Claude returned empty output. Check workflow logs.)"
    push(message)
    print("Pushed briefing:")
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
