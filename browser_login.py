"""One-time login handoff — you log in, Aria reuses the session.

Opens a REAL (visible) browser to a site so you can sign in by hand (password,
2FA, captcha — all yours). On Enter, it saves the authenticated session to
browser_sessions/<name>.json. browse_and_report(session="<name>") then loads it
and acts already-logged-in — so Aria can explore Amazon/etc. as you, while
payment still stops at the human boundary.

Run on the host: `python3 browser_login.py amazon https://www.amazon.com`

SECURITY: the saved file is a live login session (like a cookie jar). It lives
on this host only, is gitignored, and expires — re-run when a site logs you out.
Aria never sees your password; she inherits the session you established.
"""
import os
import sys

SESSION_DIR = os.path.join(os.path.dirname(__file__), 'browser_sessions')


def session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, f"{name}.json")


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 browser_login.py <name> <url>\n"
              "  e.g. python3 browser_login.py amazon https://www.amazon.com")
        return
    name, url = sys.argv[1], sys.argv[2]
    os.makedirs(SESSION_DIR, exist_ok=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)   # visible — you drive it
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        print(f"\nA browser window opened at {url}.")
        print("Log in fully (password, 2FA, any captcha). When you see you're logged in,")
        input("come back here and press Enter to save the session... ")
        context.storage_state(path=session_path(name))
        browser.close()
    print(f"✅ Saved session '{name}'. Aria can now browse that site as you "
          f"(she still never completes payments).")


if __name__ == '__main__':
    main()
