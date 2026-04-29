"""Daily 7am Melbourne briefing: emails + weather + calendar -> Telegram.

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

    # Threads we've already replied in within the last 24h are "handled" —
    # don't surface inbound messages from those threads as needing action.
    sent_recent = (
        svc.users()
        .messages()
        .list(userId="me", q="in:sent newer_than:1d", maxResults=100)
        .execute()
        .get("messages", [])
    )
    replied_thread_ids = {m["threadId"] for m in sent_recent}

    recent_q = "newer_than:1d in:inbox -category:promotions -category:social"
    recent_ids = (
        svc.users()
        .messages()
        .list(userId="me", q=recent_q, maxResults=20)
        .execute()
        .get("messages", [])
    )
    recent = [
        _message_meta(svc, m["id"])
        for m in recent_ids
        if m["threadId"] not in replied_thread_ids
    ]

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
        # SENT label is reliable across aliases / send-as; From-header
        # matching missed those.
        if "SENT" in last.get("labelIds", []):
            continue
        last_headers = {
            h["name"]: h["value"]
            for h in last.get("payload", {}).get("headers", [])
        }
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
    time_min = now.isoformat()
    time_max = (now + timedelta(days=7)).isoformat()

    cal_list = svc.calendarList().list(minAccessRole="reader").execute()
    calendars = [
        c for c in cal_list.get("items", [])
        if c.get("selected", False) and not c.get("deleted", False)
    ]

    events: list[dict] = []
    for cal in calendars:
        cal_id = cal["id"]
        cal_name = cal.get("summaryOverride") or cal.get("summary") or cal_id
        resp = (
            svc.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=40,
            )
            .execute()
        )
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
                    "calendar": cal_name,
                    "summary": e.get("summary", "(no title)"),
                    "start": start_str,
                    "end": end_str,
                    "location": e.get("location", ""),
                    "description": (e.get("description") or "")[:200],
                    "attendees": attendee_names,
                    "all_day": "date" in start and "dateTime" not in start,
                }
            )

    events.sort(key=lambda x: x["start"])
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


SYSTEM_PROMPT = """You write a daily 7am briefing for a busy professional in Melbourne, delivered via Telegram.

Rules:
- No character limit. Use the space to be genuinely useful, not padded.
- No markdown syntax (no *, _, #). Emoji for section headers only.
- Use exactly these five sections in order:

\u2614 <full sentence about umbrella>. Never just YES/NO. Be specific: cite rain probability, mm expected, and time window if relevant. E.g. 'No rain expected today, leave umbrella at home.' or 'Rain likely 3-6pm (70%, 4mm) - take umbrella.'

\U0001F324 <temp now>\u00b0C, <condition>. High <high>\u00b0 / Low <low>\u00b0. One sentence on what to wear or expect if notable (e.g. cold wind, humidity, strong UV).

\U0001F4E7 New emails (<N> in 24h):
Only include emails that need a reply, a decision, or carry important news (from boss, client, lawyer, doctor, school, bank, close personal contact). For each: name the sender clearly, subject, and 1 sentence on what action is needed or why it matters. Skip anything automated, transactional, or newsletter. If nothing needs action, say so plainly.

\u23F0 Follow up (<N> awaiting reply):
Threads from the last week where someone is waiting on you. For each: sender name, topic, how many days ago, and what you likely need to do. Be direct - if it looks urgent flag it. If nothing is pending, say so.

\U0001F4C5 This week:
List ALL events for the next 7 days, one per line. Format: <Day> <date> <time> - <title> [with <names> if relevant] [@ <location>]. Use short day names (Mon, Tue etc). For all-day events omit the time. If today has something starting within 2 hours, flag it prominently at the top of this section with a warning emoji. If no events, say clear.

Never hallucinate senders, meeting titles, attendees, or weather figures.
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
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
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
