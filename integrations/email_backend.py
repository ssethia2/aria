"""Provider-agnostic email via IMAP/SMTP — so self-hosters need NO Google Cloud project.

The Gmail *API* requires an OAuth app (a GCP project, restricted-scope verification,
7-day token expiry in testing) — a real barrier to "anyone can run this." This backend
sidesteps all of it: the user enables 2FA and pastes an app password into .env. Works
with Gmail, Fastmail, iCloud, any IMAP/SMTP host.

Active only when EMAIL_APP_PASSWORD + USER_EMAIL are set; otherwise the code keeps using
the Gmail API path unchanged (so existing OAuth installs are untouched). Covers the CORE
loop — read inbox, send, draft. Richer features (newsletter queries, carrier package
search, Netflix automation) still want the Gmail API; documented as an upgrade.
"""
import email
import imaplib
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import parsedate_to_datetime


def using_app_password() -> bool:
    return bool(os.getenv('EMAIL_APP_PASSWORD') and os.getenv('USER_EMAIL'))


def _env(key, default):
    """os.getenv but a blank value (common in a half-filled .env) falls to default."""
    return os.getenv(key) or default


def _cfg():
    return {
        'user': os.getenv('USER_EMAIL'),
        'password': os.getenv('EMAIL_APP_PASSWORD'),
        'imap_host': _env('IMAP_HOST', 'imap.gmail.com'),
        'smtp_host': _env('SMTP_HOST', 'smtp.gmail.com'),
        'smtp_port': int(_env('SMTP_PORT', '587')),
    }


def _imap():
    c = _cfg()
    m = imaplib.IMAP4_SSL(c['imap_host'])
    m.login(c['user'], c['password'])
    return m


def _parse(raw_bytes) -> dict:
    msg = email.message_from_bytes(raw_bytes)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                try:
                    body = part.get_payload(decode=True).decode(errors='ignore')
                    break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(errors='ignore')
        except Exception:
            body = ""
    return {
        'id': msg.get('Message-ID', '').strip() or str(time.time()),
        'subject': msg.get('Subject', 'No Subject'),
        'sender': msg.get('From', 'Unknown Sender'),
        'snippet': ' '.join(body.split())[:200],
        'message_id': msg.get('Message-ID', '').strip(),
        'list_unsubscribe': msg.get('List-Unsubscribe', ''),  # bulk-mail marker
    }


def _fetch_ids(m, ids):
    out = []
    for i in ids:
        typ, data = m.fetch(i, '(RFC822)')
        if typ == 'OK' and data and data[0]:
            out.append(_parse(data[0][1]))
    return out


def imap_fetch_recent(max_results=10) -> list:
    """The most recent INBOX messages, newest first."""
    m = _imap()
    try:
        m.select('INBOX')
        typ, data = m.search(None, 'ALL')
        ids = data[0].split()[-max_results:][::-1]
        return _fetch_ids(m, ids)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def imap_fetch_since(since_ts: float) -> list:
    """INBOX messages since the given unix time (day-granular SINCE; caller dedups)."""
    m = _imap()
    try:
        m.select('INBOX')
        since = time.strftime('%d-%b-%Y', time.localtime(since_ts))
        typ, data = m.search(None, f'(SINCE {since})')
        ids = data[0].split()[-30:][::-1]
        return _fetch_ids(m, ids)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def smtp_send(to_email, subject, body, in_reply_to=None) -> bool:
    c = _cfg()
    msg = EmailMessage()
    msg['From'] = c['user']
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.set_content(body)
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to
    ctx = ssl.create_default_context()
    with smtplib.SMTP(c['smtp_host'], c['smtp_port']) as s:
        s.starttls(context=ctx)
        s.login(c['user'], c['password'])
        s.send_message(msg)
    return True


def imap_create_draft(to_email, subject, body, in_reply_to=None) -> bool:
    c = _cfg()
    msg = EmailMessage()
    msg['From'] = c['user']
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.set_content(body)
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to
    m = _imap()
    try:
        for folder in ('[Gmail]/Drafts', 'Drafts', 'INBOX.Drafts'):
            try:
                m.append(folder, '\\Draft', imaplib.Time2Internaldate(time.time()),
                         msg.as_bytes())
                return True
            except Exception:
                continue
        return False
    finally:
        try:
            m.logout()
        except Exception:
            pass


def check_login() -> tuple:
    """(ok, detail) — used by healthcheck for the app-password path."""
    try:
        m = _imap()
        m.logout()
        return True, f"IMAP login OK ({_cfg()['imap_host']})"
    except Exception as e:
        return False, f"IMAP login failed: {e}"
