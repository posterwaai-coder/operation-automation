"""
One-off helper to (re)generate a Google OAuth refresh token for the Drive API.

Use this when the app fails with ``invalid_grant`` — that means the current
``GOOGLE_OAUTH_REFRESH_TOKEN`` is expired or revoked.

Prerequisites
-------------
1. In Google Cloud Console, create an OAuth 2.0 Client ID of type "Desktop app".
2. Download its client-secrets JSON and save it next to this file as
   ``oauth_client.json`` (already git-ignored).
3. Add your Google account as a test user on the OAuth consent screen — or,
   better, publish the consent screen to "Production" so the refresh token does
   not expire after 7 days (the usual cause of invalid_grant).

Run
---
    python get_refresh_token.py

A browser window opens for you to authorize. On success this prints the
client id / secret / refresh token to paste into your ``.env`` (or deployment
secrets) as:

    GOOGLE_OAUTH_CLIENT_ID=...
    GOOGLE_OAUTH_CLIENT_SECRET=...
    GOOGLE_OAUTH_REFRESH_TOKEN=...
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Must match the scopes requested by the app (see backend/main.py).
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth_client.json")


def main() -> None:
    if not os.path.exists(CLIENT_SECRETS_FILE):
        sys.exit(
            f"Missing {CLIENT_SECRETS_FILE}.\n"
            "Download your OAuth 'Desktop app' client secrets from Google Cloud "
            "Console and save them as oauth_client.json in the repo root."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES)
    # access_type=offline + prompt=consent forces Google to return a refresh token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        sys.exit(
            "No refresh token was returned. Revoke the app's access at "
            "https://myaccount.google.com/permissions and run this again."
        )

    print("\nSuccess! Add these to your .env / deployment secrets:\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
