"""Package tracking — keyless, off the Gmail inbox.

No carrier API needed: shipping/delivery notifications already land in the inbox.
This surfaces them (and pulls out trackable numbers + links where it can); Aria
summarizes. Reuses the primary Gmail service.
"""
import re

from langchain_core.tools import tool

from skills.email_manager import get_gmail_service

QUERY = ('newer_than:21d (subject:shipped OR subject:"out for delivery" OR '
         'subject:delivered OR subject:tracking OR subject:"on its way" OR '
         'from:ups OR from:fedex OR from:usps OR from:shipment-tracking@amazon.com)')

# Conservative carrier patterns → tracking URL. Only high-confidence shapes.
_CARRIERS = [
    ('UPS',   re.compile(r'\b1Z[0-9A-Z]{16}\b'),
     'https://www.ups.com/track?tracknum={}'),
    ('USPS',  re.compile(r'\b9[2-5]\d{20,24}\b'),
     'https://tools.usps.com/go/TrackConfirmAction?tLabels={}'),
    ('FedEx', re.compile(r'\b\d{12}(?:\d{3})?(?:\d{5})?\b'),  # 12/15/20 digit
     'https://www.fedex.com/fedextrack/?trknbr={}'),
]


def _extract_tracking(text: str):
    for carrier, pat, url in _CARRIERS:
        m = pat.search(text or '')
        if m:
            return carrier, m.group(0), url.format(m.group(0))
    return None


@tool
def check_packages(max_results: int = 15) -> str:
    """Find the user's recent package shipments and deliveries from his inbox
    (last 3 weeks). Use when he asks about packages, deliveries, or "where's my X".
    Returns the relevant emails with any tracking number/link found — summarize
    them for him (in transit vs delivered)."""
    service = get_gmail_service()
    if not service:
        return "Gmail isn't available right now."

    found = service.users().messages().list(
        userId='me', q=QUERY, maxResults=max_results).execute().get('messages', [])
    if not found:
        return "No shipping or delivery emails in the last few weeks."

    out = []
    for m in found:
        msg = service.users().messages().get(
            userId='me', id=m['id'], format='metadata',
            metadataHeaders=['From', 'Subject', 'Date']).execute()
        h = {x['name']: x['value'] for x in msg['payload'].get('headers', [])}
        snippet = msg.get('snippet', '')
        line = f"- {h.get('Date', '')[:16]} | {h.get('From', '?')}: {h.get('Subject', '')}"
        track = _extract_tracking(f"{h.get('Subject','')} {snippet}")
        if track:
            line += f"\n  {track[0]} {track[1]} → {track[2]}"
        out.append(line)
    return "Recent shipping/delivery emails:\n" + "\n".join(out)
