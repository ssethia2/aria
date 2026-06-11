"""Netflix Household skill — autonomous "Update Household" automation.

Authenticates the secondary account (token_netflix.json), finds the latest household
email, extracts the CTA link, and uses a headless Playwright browser to click confirm.
Exposes the update_netflix_household tool (used by interact.py and aria_server.py).
Requires: `playwright install chromium`. Authorize the account first via auth_netflix.py.
"""
import os
import base64
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from langchain_core.tools import tool
from playwright.sync_api import sync_playwright

def get_netflix_gmail_service():
    """Builds a Gmail service securely scoped to the secondary Netflix token."""
    token_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'token_netflix.json')
    if not os.path.exists(token_path):
        print("Missing token_netflix.json. User must authenticate the secondary account first.")
        return None
        
    creds = Credentials.from_authorized_user_file(token_path)
    try:
        return build('gmail', 'v1', credentials=creds)
    except HttpError as error:
        print(f"Failed to build secondary Gmail service for Netflix: {error}")
        return None

# Subject-phrase matches only — a bare from:info@mailer.netflix.com would let any
# marketing email trigger browser automation. The engine reuses this (+ newer_than:1d).
HOUSEHOLD_EMAIL_QUERY = ('subject:"update your Netflix Household" OR '
                         'subject:"Important update about your Netflix Household"')

# Strongest signal first: Netflix's real CTA links carry these path fragments.
CTA_HREF_HINTS = ['update-primary-location', 'household', 'verify-location', 'travel']
CTA_TEXT_HINTS = ['update', 'confirm', 'household', 'yes, this was me', "i'm traveling"]
SKIP_HREF_HINTS = ['unsubscribe', 'help.netflix', 'privacy', 'browse']


def extract_netflix_link(service) -> str:
    """Finds the most recent Netflix Household update email and extracts the secure CTA link."""
    try:
        results = service.users().messages().list(
            userId='me', q=HOUSEHOLD_EMAIL_QUERY, maxResults=5).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return None
            
        msg_id = messages[0]['id']
        msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        
        payload = msg.get('payload', {})
        parts = payload.get('parts', [])
        
        html_data = None
        if not parts:
            if payload.get('mimeType') == 'text/html':
                html_data = payload.get('body', {}).get('data')
        else:
            for part in parts:
                if part.get('mimeType') == 'text/html':
                    html_data = part.get('body', {}).get('data')
                    break
                    
        if not html_data:
            return None
            
        html_content = base64.urlsafe_b64decode(html_data).decode('utf-8')
        soup = BeautifulSoup(html_content, 'html.parser')
        
        links = [l for l in soup.find_all('a', href=True)
                 if not any(s in l['href'].lower() for s in SKIP_HREF_HINTS)]

        # 1) The href itself is the strongest signal (real CTA paths).
        for link in links:
            if any(h in link['href'].lower() for h in CTA_HREF_HINTS):
                return link['href']
        # 2) Fall back to anchor-text matching.
        for link in links:
            text = link.get_text().lower()
            if any(h in text for h in CTA_TEXT_HINTS):
                return link['href']
        # 3) No blind first-link grab: clicking a random tracking URL is worse than
        #    an honest failure (that's how we once "updated" via a /browse link).
        return None

    except Exception as e:
        print(f"Error fetching Netflix link from email: {e}")
        return None

# Post-visit page-state markers, checked in priority order.
SUCCESS_MARKERS = ["household is updated", "household updated", "you're all set",
                   "primary location updated", "successfully updated",
                   # Exact phrasing from the live page (screenshot, 2026-06-10):
                   "updated your netflix household", "you've updated"]
EXPIRED_MARKERS = ["link expired", "link is no longer valid", "request a new link",
                   "this link has expired"]
LOGIN_MARKERS = ["sign in to netflix", "log in to netflix", "sign in", "password"]


def classify_page_outcome(page_text: str, clicked: bool) -> str:
    """Pure classifier for the page Netflix left us on — testable without a browser.

    'confirmed'      — Netflix explicitly says the household updated
    'expired'        — the secure link timed out; a fresh email is needed
    'login_required' — hit a login wall; automation can't proceed without a session
    'clicked'        — we clicked the button but saw no explicit confirmation
    'visited'        — no button found, no confirmation (magic-link visit MAY suffice)
    """
    text = (page_text or "").lower()
    if any(m in text for m in SUCCESS_MARKERS):
        return "confirmed"
    if any(m in text for m in EXPIRED_MARKERS):
        return "expired"
    if not clicked and any(m in text for m in LOGIN_MARKERS):
        return "login_required"
    return "clicked" if clicked else "visited"


def click_netflix_update(url: str) -> str:
    """Visit the secure link, click the update button if present, and VERIFY by
    reading the resulting page state. Returns a classify_page_outcome() status,
    or 'failed' if the browser automation itself errored.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            
            print(f"Spinning up Browser Subagent to navigate to Netflix link...")
            page.goto(url, wait_until='networkidle')
            
            print("Searching DOM for confirmation button...")
            selectors = [
                "button[data-uia='action-update-household']",
                # Confirmed by the user 2026-06-10: the live page's button says "Confirm update"
                "button:has-text('Confirm update')",
                "button:has-text('Update Netflix Household')",
                "button:has-text('Update Household')",
                ".btn-update"
            ]
            
            clicked = False
            for selector in selectors:
                try:
                    element = page.locator(selector).first
                    if element.is_visible(timeout=2000):
                        element.click()
                        print(f"Successfully simulated browser click on: {selector}")
                        clicked = True
                        break
                except Exception:
                    continue
            
            page.wait_for_timeout(3000)

            # VERIFY: read what Netflix actually says now, don't assume.
            try:
                page_text = page.inner_text('body')
            except Exception:
                page_text = ""
            try:
                page.screenshot(  # evidence for debugging, overwritten each run
                    path=os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                      'logs', 'netflix_last_visit.png'))
            except Exception:
                pass

            outcome = classify_page_outcome(page_text, clicked)
            print(f"Post-visit page classified as: {outcome}")
            browser.close()
            return outcome

    except Exception as e:
        print(f"Playwright automation failed: {e}")
        return "failed"

@tool
def update_netflix_household() -> str:
    """Use this tool when the user asks you to update their Netflix Household.
    It will autonomously authenticate the secondary Netflix email, fetch the latest household update link, and use a headless browser to physically click the confirmation button on Netflix's website.
    """
    print("Initiating Netflix Household Update Sequence...")
    
    service = get_netflix_gmail_service()
    if not service:
        return "Failed to authenticate with Netflix-tied Gmail account. Missing token."
        
    print("Fetching recent Netflix emails...")
    link = extract_netflix_link(service)

    if not link:
        return ("Couldn't find a household-update email — or a usable update link inside "
                "one — in the secondary inbox.")

    print(f"Found Secure Link: {link[:60]}...")

    outcome = click_netflix_update(link)
    if outcome == "confirmed":
        return ("✅ Verified: Netflix's page confirmed the household update. "
                "The device should work now.")
    if outcome == "clicked":
        return ("Clicked the confirmation button, but Netflix didn't show an explicit "
                "confirmation message — it most likely worked; worth a glance at the device.")
    if outcome == "visited":
        return ("I opened the secure link but found no confirmation button and no "
                "confirmation message. The visit alone may have sufficed (magic link), "
                "but I can't verify it — please check whether the device can play now.")
    if outcome == "login_required":
        return ("The link led to a Netflix login wall, so I can't complete this one — "
                "the update needs to be confirmed from a device that's signed in "
                "(tap the email link on the account owner's phone).")
    if outcome == "expired":
        return ("That update link has expired — they're time-limited. Trigger a fresh one "
                "from the TV and I'll catch the new email automatically.")
    return "Failed to complete the browser automation step."
