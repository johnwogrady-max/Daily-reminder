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


def _message_meta(svc, msg_id: str) -> dict:
    msg = (
        svc.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "unread": "UNREAD" in msg.get("labelIds", []),
        "internal_ts": int(msg.get("internalDate", 0)),
    }


def fetch_emails(creds: Credentials) -> dict:
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = svc.users().getProfile(userId="me").execute()
    my_email = (profile.get("emailAddress") or "").lower()

    recent_q = "newer_than:1d in:inbox -category:promotions -category:social"
    recent_ids = (
        svc.users()
        .messages()
        .list(userId="me", q=recent_q, maxResults=20)
        .execute()
        .get("messages", [])
    )
    recent = [_message_meta(svc, m["id"]) for m in recent_ids]

    thread_q = (
        "newer_than:7d older_than:1d in:inbox "
        "-category:promotions -category:social"
    )
    thread_ids = (
        svc.users()
        .threads()
        .list(userId="me", q=thread_q, maxResults=25)
        .execute()
        .get("threads", [])
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    follow_ups: list[dict] = []
    for t in thread_ids:
        thread = (
            svc.users()
            .threads()
            .get(
                userId="me",
                id=t["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        msgs = thread.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        last_headers = {
            h["name"]: h["value"]
            for h in last.get("payload", {}).get("headers", [])
        }
        last_from = (last_headers.get("From") or "").lower()
        if my_email and my_email in last_from:
            continue
        first_headers = {
            h["name"]: h["value"]
            for h in msgs[0].get("payload", {}).get("headers", [])
        }
        last_ts = int(last.get("internalDate", 0))
        age_days = max(0, (now_ms - last_ts) // (1000 * 60 * 60 * 24))
        follow_ups.append(
            {
                "from": last_headers.get("From", ""),
                "subject": first_headers.get("Subject", ""),
                "snippet": last.get("snippet", ""),
                "age_days": int(age_days),
                "thread_length": len(msgs),
                "unread": "UNREAD" in last.get("labelIds", []),
            }
        )

    return {"recent": recent, "follow_ups": follow_ups}


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
- Use exactly these five sections in order:

\u2614 <full phrase about umbrella>. Never just YES/NO. Examples: 'No rain expected today, leave umbrella at home.' or 'Rain likely 3-6pm (70%, 4mm), take umbrella.'
\U0001F324 <temp now>\u00b0C, <condition>, H<high>\u00b0/L<low>\u00b0

\U0001F4E7 New (<N> in 24h):
Be opinionated. Only flag emails that need a reply, a decision, or signal something important (boss, client, lawyer, bank, school, personal). Name the sender and say in ~6 words why it matters. Skip automated/transactional/newsletter. If nothing, say 'nothing needs action'.

\u23F0 Follow up (<N>):
Things from the last week still waiting on a reply from you (threads where the latest message is from someone else). Name sender, 5-word topic, days old. Skip anything that doesn't need a response. If nothing, say 'nothing pending'.

\U0001F4C5 This week:
List ALL events for the next 7 days. Format each as: <Day> <time> \u2014 <title> [with <names>] [@ <location>]. Short day names (Mon, Tue). All-day events omit time. Group multiple same-day events together. If no events say 'clear'.

Never hallucinate senders, meeting titles, or attendees.
"""


def summarise(emails: dict, events: list[dict], wx: dict) -> str:
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    user_payload = {
        "today_melbourne": datetime.now(MELBOURNE).strftime("%A %d %b %Y"),
        "weather": wx,
        "emails_last_24h": emails.get("recent", []),
        "threads_waiting_on_reply": emails.get("follow_ups", []),
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
