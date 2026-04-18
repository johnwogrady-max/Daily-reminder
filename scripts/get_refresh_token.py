"""One-time local helper to mint a Google OAuth refresh token.

Run once on your own machine (NOT in CI):

    pip install google-auth-oauthlib google-auth google-api-python-client
    python scripts/get_refresh_token.py

Prerequisites:
  1. Google Cloud project with Gmail API + Calendar API enabled.
  2. OAuth consent screen configured (External, Testing mode is fine;
     add your own Google account as a Test User).
  3. OAuth 2.0 Client ID of type 'Desktop app' created.
  4. Download its JSON and save it alongside this script as
     'client_secret.json'.

The script opens a browser, you approve, and it prints three values to
paste into GitHub Actions secrets:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

CLIENT_SECRET = Path(__file__).resolve().parent.parent / "client_secret.json"


def main() -> int:
    if not CLIENT_SECRET.exists():
        print(f"ERROR: {CLIENT_SECRET} not found.", file=sys.stderr)
        print(
            "Download your Desktop OAuth client JSON from Google Cloud Console "
            "and save it to that path.",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )

    with CLIENT_SECRET.open() as f:
        secret = json.load(f)
    installed = secret.get("installed") or secret.get("web") or {}

    print("\nPaste these into GitHub -> Settings -> Secrets and variables -> Actions:\n")
    print(f"GOOGLE_CLIENT_ID={installed.get('client_id', '')}")
    print(f"GOOGLE_CLIENT_SECRET={installed.get('client_secret', '')}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("\nKeep client_secret.json out of git. It's fine to delete it after this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
