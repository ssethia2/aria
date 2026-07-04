"""Deterministic pre-filter — discard obvious bulk mail before the LLM screens it.

The email-digest LLM ran on EVERY new message every ~15 min. But a large share of inbox
volume is bulk (newsletters, marketing, social notifications) that is never a reply owed
or worth flagging. Bulk mail carries the RFC-standard List-Unsubscribe header;
transactional mail that DOES matter (bills, security alerts, travel) does not. So one
deterministic header check cuts the most frequent LLM call with high precision — and zero
quality cost, since nothing flag-worthy is dropped.

Conservative by design: only List-Unsubscribe triggers a discard. Sender-pattern guessing
(no-reply@, notifications@) is deliberately avoided — important transactional mail uses
those too.
"""


def is_bulk(email: dict) -> bool:
    """True if the email is bulk mail safe to skip LLM screening on."""
    return bool((email.get('list_unsubscribe') or '').strip())


import re

_NOREPLY_RE = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?@|alerts?@|automated|mailer-daemon|"
    r"noreply|updates@|info@|support@.*\.zendesk\.)", re.IGNORECASE)


def is_noreply_sender(from_header: str) -> bool:
    """True when the From address is an automated/no-reply sender — mail that can still be
    WORTH FLAGGING (bank alert, booking change) but can never be a reply the user OWES.
    Used only to gate reply_owed tracking, never digest screening (see module docstring:
    important transactional mail legitimately uses these addresses)."""
    return bool(_NOREPLY_RE.search(from_header or ""))
