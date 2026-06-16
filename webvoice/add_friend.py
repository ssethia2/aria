"""Invite a friend to the webvoice trial.

Generates a random invite token, maps it to a stable user_id (so their memory persists),
writes it to friends.json (gitignored), and prints the link to send them.

  python3 webvoice/add_friend.py "Alice"                      # uses a placeholder host
  python3 webvoice/add_friend.py "Alice" https://aria.ngrok-free.app

They open the link on their phone, tap Start, and talk. Add to Home Screen for an app icon.
"""
import json
import re
import secrets
import sys
from pathlib import Path

FRIENDS = Path(__file__).resolve().parent / "friends.json"


def main():
    if len(sys.argv) < 2:
        print('usage: python3 webvoice/add_friend.py "<name>" [base_url]')
        raise SystemExit(2)
    name = sys.argv[1].strip()
    base = (sys.argv[2] if len(sys.argv) > 2 else "https://YOUR-DOMAIN").rstrip("/")
    user_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "friend"

    data = json.loads(FRIENDS.read_text()) if FRIENDS.exists() else {}
    # reuse an existing token for this user_id if present, so their memory stays theirs
    token = next((t for t, u in data.items() if u == user_id), None) or secrets.token_urlsafe(12)
    data[token] = user_id
    FRIENDS.write_text(json.dumps(data, indent=2))

    print(f"Added '{name}'  (user_id = {user_id})")
    print(f"Invite link:  {base}/?t={token}")
    if base == "https://YOUR-DOMAIN":
        print("(pass your real https URL as the 2nd arg to get a ready-to-send link)")


if __name__ == "__main__":
    main()
