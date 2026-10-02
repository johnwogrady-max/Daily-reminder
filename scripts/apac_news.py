"""Daily 8am Melbourne news briefing on Melbourne Airport / APAC and its
shareholders, written for APAC's General Counsel & Company Secretary.

Invoked by .github/workflows/apac-news.yml. Coverage is defined in
config/apac_news.yml. Pipeline:

  collect (Google News, Google Alerts feeds, ASX, optional Gmail label)
  -> drop items older than the lookback window or already reported
  -> Haiku filters for relevance and tags priority topics
  -> Sonnet writes the briefing, citing item ids like [12]
  -> citations are checked against the fetched set and replaced with
     "Source, date"; any line citing an unknown id is dropped
  -> written to _news.json (gitignored) for send_push.js

Seen-item state lives in .cache/apac_seen.json, persisted between runs by
actions/cache (never committed).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml
from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "apac_news.yml"
OUTPUT_PATH = REPO_ROOT / "_news.json"
SEEN_PATH = REPO_ROOT / ".cache" / "apac_seen.json"
SEEN_RETENTION_DAYS = 14

MELBOURNE = ZoneInfo("Australia/Melbourne")
RUN_HOUR = 8

FILTER_MODEL = "claude-haiku-4-5"
WRITER_MODEL = "claude-sonnet-5-5"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (APAC news briefing)"}


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: env var {name} not set", file=sys.stderr)
        sys.exit(1)
    return v


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- collection -------------------------------------------------------------


def _item(title: str, source: str, published: datetime | None, url: str,
          snippet: str, origin: str) -> dict:
    return {
        "title": " ".join((title or "").split()),
        "source": source or "",
        "published": published.isoformat() if published else "",
        "url": url or "",
        "snippet": " ".join(re.sub(r"<[^>]+>", " ", snippet or "").split())[:600],
        "origin": origin,
    }


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_google_news(queries: list[str]) -> list[dict]:
    items: list[dict] = []
    for q in queries:
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(f"{q} when:2d")
            + "&hl=en-AU&gl=AU&ceid=AU:en"
        )
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Google News query failed ({q}): {e}")
            continue
        feed = feedparser.parse(resp.content)
        for e in feed.entries:
            title = e.get("title", "")
            source = (e.get("source") or {}).get("title", "")
            # Google News titles end with " - Source"; strip it.
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3]
            items.append(_item(title, source, _entry_time(e), e.get("link", ""),
                               e.get("summary", ""), "google_news"))
    return items


def _unwrap_google_redirect(link: str) -> str:
    parsed = urlparse(link)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("url")
        if target:
            return target[0]
    return link


def fetch_alert_feeds(feed_urls: list[str]) -> list[dict]:
    items: list[dict] = []
    for feed_url in feed_urls:
        try:
            resp = requests.get(feed_url, headers=HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Alert feed failed: {e}")
            continue
        feed = feedparser.parse(resp.content)
        for e in feed.entries:
            link = _unwrap_google_redirect(e.get("link", ""))
            source = urlparse(link).netloc.removeprefix("www.")
            items.append(_item(e.get("title", ""), source, _entry_time(e), link,
                               e.get("summary", ""), "google_alerts"))
    return items


def fetch_asx(codes: list[str]) -> list[dict]:
    items: list[dict] = []
    for code in codes:
        url = (
            "https://asx.api.markitdigital.com/asx-research/1.0/companies/"
            f"{code.lower()}/announcements"
        )
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"ASX announcements failed ({code}): {e}")
            continue
        for a in (data.get("data") or {}).get("items", []):
            try:
                published = datetime.fromisoformat(a["date"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                published = None
            sensitive = " (price sensitive)" if a.get("isPriceSensitive") else ""
            items.append(_item(
                f"{code}: {a.get('headline', '')}{sensitive}",
                "ASX", published,
                f"https://www.asx.com.au/markets/trade-our-cash-market/announcements.{code.lower()}",
                "", "asx",
            ))
    return items


def _gmail_text(payload: dict) -> str:
    """Plain-text body of a Gmail message, falling back to stripped HTML."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            (plain if mime == "text/plain" else html if mime == "text/html" else []).append(text)
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain)
    return re.sub(r"<[^>]+>", " ", "\n".join(html))


def fetch_gmail_label(label_name: str) -> list[dict]:
    if not label_name or not os.environ.get("GOOGLE_REFRESH_TOKEN"):
        return []
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=env("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("GOOGLE_CLIENT_ID"),
        client_secret=env("GOOGLE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(GoogleRequest())
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    label = next((l for l in labels if l["name"] == label_name), None)
    if not label:
        print(f"Gmail label {label_name!r} not found; skipping newsletters.")
        return []

    ids = (
        svc.users().messages()
        .list(userId="me", labelIds=[label["id"]], q="newer_than:2d", maxResults=15)
        .execute().get("messages", [])
    )
    items: list[dict] = []
    for m in ids:
        msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        try:
            published = parsedate_to_datetime(headers.get("Date", ""))
        except (TypeError, ValueError):
            published = None
        sender = headers.get("From", "").split("<")[0].strip().strip('"')
        item = _item(headers.get("Subject", ""), sender, published, "", "", "gmail")
        # Newsletters are long; keep enough for the filter to find the
        # relevant paragraph.
        item["snippet"] = " ".join(_gmail_text(msg["payload"]).split())[:6000]
        items.append(item)
    return items


# --- dedupe -----------------------------------------------------------------


def _fingerprint(item: dict) -> str:
    title = re.sub(r"[^a-z0-9 ]", "", item["title"].lower())
    return hashlib.sha1(title.encode()).hexdigest()[:16]


def load_seen() -> dict[str, str]:
    try:
        return json.loads(SEEN_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_seen(seen: dict[str, str]) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen))


def dedupe(items: list[dict], seen: dict[str, str], lookback_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out: list[dict] = []
    batch: set[str] = set()
    for item in items:
        if item["published"] and datetime.fromisoformat(item["published"]) < cutoff:
            continue
        fp = _fingerprint(item)
        if fp in seen or fp in batch:
            continue
        batch.add(fp)
        item["fp"] = fp
        out.append(item)
    for i, item in enumerate(out, start=1):
        item["id"] = i
    return out


# --- Claude -----------------------------------------------------------------


def _coverage_text(cfg: dict) -> str:
    lines = ["Tracked organisations:"]
    for e in cfg["entities"]:
        lines.append(f"- {e['label']}: {', '.join(e['names'])}")
    lines.append("\nPriority topics (always keep, however minor):")
    for t in cfg["priority_topics"]:
        lines.append(f"- {t['key']}: {t['label']} — {' '.join(t['description'].split())}")
    return "\n".join(lines)


FILTER_PROMPT = """You screen news items for a daily briefing.

Reader: {reader}

{coverage}

For each item decide whether it is genuinely about a tracked organisation
or priority topic, or about aviation regulation/policy that directly
affects Australian airport owners. Drop items that only mention a name in
passing, unrelated companies with similar names (e.g. other uses of
"IFM", "Morrison", "Future Fund"), sport, travel deals, and routine flight
delay stories with no legal, regulatory, governance or reputational angle.

Reply with JSON only, no prose:
{{"keep": [{{"id": <int>, "priority": "<priority topic key or empty string>"}}]}}
"""


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else {}


def filter_items(client: Anthropic, cfg: dict, items: list[dict]) -> list[dict]:
    if not items:
        return []
    system = FILTER_PROMPT.format(reader=" ".join(cfg["reader"].split()),
                                  coverage=_coverage_text(cfg))
    by_id = {i["id"]: i for i in items}
    kept: list[dict] = []
    for start in range(0, len(items), 80):
        chunk = [
            {"id": i["id"], "title": i["title"], "source": i["source"],
             "snippet": i["snippet"][:300 if i["origin"] != "gmail" else 6000]}
            for i in items[start:start + 80]
        ]
        resp = client.messages.create(
            model=FILTER_MODEL,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            decisions = _parse_json(text).get("keep", [])
        except ValueError:
            print("Filter returned unparseable JSON; keeping chunk unfiltered.")
            decisions = [{"id": c["id"], "priority": ""} for c in chunk]
        for d in decisions:
            item = by_id.get(d.get("id"))
            if item:
                item["priority"] = d.get("priority") or ""
                kept.append(item)
    return kept


WRITER_PROMPT = """You write a daily news briefing delivered as a phone notification.

Reader: {reader}
They know APAC, its shareholders and its live matters intimately. Do not
explain background they already know. Tell them what is new and why it
matters to a General Counsel / Company Secretary: disclosure and
confidentiality, board and shareholder reporting, litigation exposure,
regulatory and planning approvals, contracts and consents, reputation.

{coverage}

Rules:
- Use ONLY the items provided. Never add facts, figures, names or dates
  that are not in them. If an item is only a headline, say only what the
  headline supports.
- Cite every news line with its item id in square brackets, e.g. [12].
  Multiple items on the same story: cite all, e.g. [3][7].
- Plain text only. No markdown (no *, _, #). Emoji only as section headers.
- Hard limit 2,200 characters in total. Prefer fewer, sharper items.
- Mark priority-topic items with ★ at the start of the line.
- Each item: one line on what happened [id], then an indented line starting
  "→ " on why it matters to the reader. Skip the → line when there is
  nothing useful to say.
- Omit any section with nothing in it. If nothing at all is material,
  output exactly: "No material news today." followed by nothing else.

Sections, in this order:
⚖️ Top item: the single item most likely to need the reader's attention today (one line + → line). This must be the very first line.
📜 Legal & regulatory
🏛 Disputes & litigation
🤝 Shareholders: one line per shareholder with news
✈️ Airport & projects
🗓 Watch list: upcoming dates mentioned in the items
"""


def write_briefing(client: Anthropic, cfg: dict, items: list[dict]) -> str:
    if not items:
        return "No material news today."
    system = WRITER_PROMPT.format(reader=" ".join(cfg["reader"].split()),
                                  coverage=_coverage_text(cfg))
    payload = [
        {"id": i["id"], "title": i["title"], "source": i["source"],
         "published": i["published"], "priority": i.get("priority", ""),
         "text": i["snippet"]}
        for i in items
    ]
    today = datetime.now(MELBOURNE).strftime("%A %d %B %Y")
    resp = client.messages.create(
        model=WRITER_MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{
            "role": "user",
            "content": f"Today is {today}. Items:\n\n"
                       + json.dumps(payload, ensure_ascii=False),
        }],
    )
    return "\n".join(b.text.strip() for b in resp.content if b.type == "text").strip()


def _cite_label(item: dict) -> str:
    source = item["source"] or item["origin"]
    if item["published"]:
        when = datetime.fromisoformat(item["published"]).astimezone(MELBOURNE)
        return f"{source}, {when.day} {when.strftime('%b')}"
    return source


def resolve_citations(text: str, items: list[dict]) -> str:
    """Replace [id] markers with "(Source, date)". Drop any line citing an id
    that wasn't in the fetched set — the guard against invented stories."""
    by_id = {i["id"]: i for i in items}
    out: list[str] = []
    skip_why = False
    for line in text.splitlines():
        ids = [int(n) for n in re.findall(r"\[(\d+)\]", line)]
        if ids and any(n not in by_id for n in ids):
            print(f"Dropped line citing unknown item: {line!r}")
            skip_why = True
            continue
        if skip_why and line.strip().startswith("→"):
            continue
        skip_why = False
        if ids:
            labels = list(dict.fromkeys(_cite_label(by_id[n]) for n in ids))
            line = re.sub(r"\s*(\[\d+\])+", "", line).rstrip()
            line = f"{line} ({'; '.join(labels)})"
        out.append(line)
    # Drop section headings left empty by the removals above.
    cleaned = [
        line for n, line in enumerate(out)
        if not (line.startswith(SECTION_HEADINGS) and line.strip() in SECTION_TITLES
                and (n + 1 == len(out) or out[n + 1].startswith(SECTION_HEADINGS)
                     or not out[n + 1].strip()))
    ]
    return "\n".join(cleaned).strip()


SECTION_HEADINGS = ("⚖️", "📜", "🏛", "🤝", "✈️", "🗓")
SECTION_TITLES = {"📜 Legal & regulatory", "🏛 Disputes & litigation", "🤝 Shareholders",
                  "✈️ Airport & projects", "🗓 Watch list"}


# --- output -----------------------------------------------------------------


def headline(message: str) -> str:
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line.removeprefix("⚖️ Top item:").strip()[:200]
    return "No material news today."


def write_output(message: str) -> None:
    OUTPUT_PATH.write_text(json.dumps({
        "generated_at": datetime.now(MELBOURNE).isoformat(timespec="seconds"),
        "headline": headline(message),
        "body": message,
    }, ensure_ascii=False, indent=2))


def main() -> int:
    local_hour = datetime.now(MELBOURNE).hour
    force = os.environ.get("FORCE_RUN") == "1"
    dry_run = os.environ.get("DRY_RUN") == "1"
    if local_hour != RUN_HOUR and not force:
        print(f"Skipping: Melbourne local hour is {local_hour}, not {RUN_HOUR}.")
        return 0

    cfg = load_config()
    alert_feeds = [u.strip() for u in os.environ.get("NEWS_ALERT_FEEDS", "").split() if u.strip()]

    collected = {
        "google_news": fetch_google_news(cfg["news_queries"]),
        "google_alerts": fetch_alert_feeds(alert_feeds),
        "asx": fetch_asx(cfg.get("asx_codes", [])),
        "gmail": fetch_gmail_label(cfg.get("gmail_label", "")),
    }
    for origin, found in collected.items():
        print(f"Collected {len(found):>4} from {origin}")
    all_items = [i for found in collected.values() for i in found]

    seen = load_seen()
    fresh = dedupe(all_items, seen, cfg.get("lookback_hours", 36))
    print(f"{len(fresh)} new items after time window and dedupe")

    client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    kept = filter_items(client, cfg, fresh)
    print(f"{len(kept)} items kept by filter")

    if dry_run:
        # News is public; newsletter text is not, so show only its subject.
        for i in kept:
            flag = f" ★{i['priority']}" if i.get("priority") else ""
            print(f"  [{i['id']}] {i['source']}: {i['title']}{flag}")

    raw = write_briefing(client, cfg, kept)
    message = resolve_citations(raw, kept) or "No material news today."
    write_output(message)

    if dry_run:
        print("\n--- DRY RUN: briefing (not pushed, seen-list not updated) ---\n")
        print(message)
        return 0

    now = datetime.now(timezone.utc).isoformat()
    for i in fresh:
        seen[i["fp"]] = now
    save_seen(seen)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("did_run=true\n")
    print(f"Briefing generated ({len(message)} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
