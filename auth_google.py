"""One-time (re-)auth for the PRIMARY Google account.

Runs the OAuth browser flow with the CURRENT scope set (Gmail + Calendar, see
ADR 0006) and overwrites token.json on success. Safe to run while the bot is
up: the old token keeps working until the new one atomically replaces it — no
deletion, no race with the engine's Gmail polling.

Run once after pulling a scope change: `python3 auth_google.py`
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

from skills.email_manager import SCOPES

BASE = os.path.dirname(__file__)


def main():
    creds_path = os.path.join(BASE, 'credentials.json')
    token_path = os.path.join(BASE, 'token.json')
    if not os.path.exists(creds_path):
        print("Error: credentials.json not found!")
        return

    print("Opening browser to (re-)authorize the PRIMARY Google account with scopes:")
    for s in SCOPES:
        print(f"  - {s}")
    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, 'w') as f:
        f.write(creds.to_json())
    print(f"✅ token.json updated — Gmail keeps working, Calendar is now connected.")


if __name__ == '__main__':
    main()
