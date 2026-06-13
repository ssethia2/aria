"""Email skill — the shared Gmail gateway for the whole project.

Owns OAuth/token handling (get_gmail_service), restricted scopes, the allowlist-enforced
send_email (see docs/adr/0003), and run_email_summary (fetch + LLM-classify + label junk).
Other skills import get_gmail_service from here rather than re-authenticating.
"""
import os
import base64
from email.message import EmailMessage
from google.cloud import storage
import google.auth.transport.requests
class ForceHTTP(google.auth.transport.requests.Request):
    def __call__(self, url, method="GET", body=None, headers=None, **kwargs):
        if url and "metadata.google.internal" in url:
            url = url.replace("https://", "http://")
        return super().__call__(url, method=method, body=body, headers=headers, **kwargs)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from langchain_core.prompts import PromptTemplate
import json
# Security: Restricted scopes.
# gmail.modify allows reading, labeling, and moving, but NOT permanent deletion.
# gmail.send restricts sending securely.
# calendar.events + calendar.readonly (ADR 0006): event CRUD and calendar listing,
# but NOT calendar sharing/ACL changes or calendar deletion.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.readonly',
]
# Cache for the allowlist so we don't query GCS every time
_cached_allowlist = None
def _load_local_allowlist():
    """The send allowlist for self-hosters: allow.json if present, else [USER_EMAIL]
    from .env (so a separate file isn't required). Returns None if neither exists."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "allow.json")
    try:
        with open(path) as f:
            return json.load(f).get("allowed_emails", [])
    except Exception:
        pass
    user_email = os.getenv("USER_EMAIL")
    return [user_email] if user_email else None


def get_allowed_recipients():
    """Fetches the send allowlist: GCS bucket if configured, else local allow.json.

    On GCS failure, falls back to the local allow.json (laptop hosting — see ADR 0005);
    if that's also unavailable, fail-safes to an empty list so nothing sends.
    """
    global _cached_allowlist
    if _cached_allowlist is not None:
        return _cached_allowlist

    bucket_name = os.getenv("ALLOWLIST_BUCKET_NAME")
    if not bucket_name:
        local = _load_local_allowlist()
        if local is not None:
            print("🔒 Using local allow.json send allowlist.")
            _cached_allowlist = local
            return _cached_allowlist
        print("⚠️ No ALLOWLIST_BUCKET_NAME and no allow.json — failing safe to empty allowlist.")
        return []

    try:
        import google.auth
        credentials, project_id = google.auth.default()
        
        auth_request = ForceHTTP()
        credentials.refresh(auth_request)
        
        print(f"🔒 Fetching secure allowlist from GCS bucket: {bucket_name} (Project: {project_id})...")
        client = storage.Client(credentials=credentials, project=project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob("allowlist.json")
        
        content = blob.download_as_text()
        data = json.loads(content)
        _cached_allowlist = data.get("allowed_emails", [])
        return _cached_allowlist
    except Exception as e:
        print(f"❌ Failed to fetch allowlist from GCS: {e}")
        local = _load_local_allowlist()
        if local is not None:
            print("🔒 Falling back to local allow.json send allowlist (ADR 0005).")
            _cached_allowlist = local
            return _cached_allowlist
        # Fail safe to an empty list so no emails can be sent if security checks fail
        return []
def get_gmail_service():
    """Shows basic usage of the Gmail API."""
    creds = None
    # The file token.json stores the user's access and refresh tokens
    token_path = os.path.join(os.path.dirname(__file__), '..', 'token.json')
    creds_path = os.path.join(os.path.dirname(__file__), '..', 'credentials.json')
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            import google.auth.transport.requests
            creds.refresh(google.auth.transport.requests.Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except HttpError as error:
        print(f'An error occurred initializing Gmail: {error}')
        return None
def fetch_recent_emails(service, max_results=10):
    """Fetch the latest emails from INBOX (via IMAP if app-password mode, else Gmail API)."""
    import email_backend
    if email_backend.using_app_password():
        print(f"Fetching last {max_results} emails via IMAP...")
        return email_backend.imap_fetch_recent(max_results)
    print(f"Fetching last {max_results} emails from INBOX...")
    try:
        results = service.users().messages().list(userId='me', labelIds=['INBOX'], maxResults=max_results).execute()
        messages = results.get('messages', [])
        if not messages:
            print('No new messages.')
            return []
        
        email_data = []
        for message in messages:
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            
            # Extract basic info
            headers = msg['payload'].get('headers', [])
            subject = next((header['value'] for header in headers if header['name'] == 'Subject'), 'No Subject')
            sender = next((header['value'] for header in headers if header['name'] == 'From'), 'Unknown Sender')
            snippet = msg.get('snippet', '')

            email_data.append({
                'id': message['id'],
                'subject': subject,
                'sender': sender,
                'snippet': snippet,
                'list_unsubscribe': next(
                    (h['value'] for h in headers if h['name'].lower() == 'list-unsubscribe'), ''),
            })
            
        return email_data
    except HttpError as error:
        print(f'An error occurred fetching emails: {error}')
        return []
def get_or_create_label(service, label_name="To Be Deleted"):
    """Gets the ID of a label, or creates it if it doesn't exist."""
    results = service.users().labels().list(userId='me').execute()
    labels = results.get('labels', [])
    for label in labels:
        if label['name'] == label_name:
            return label['id']
            
    # Create it
    label_object = {
        'messageListVisibility': 'show',
        'name': label_name,
        'labelListVisibility': 'labelShow'
    }
    created_label = service.users().labels().create(userId='me', body=label_object).execute()
    return created_label['id']
def apply_label_to_email(service, email_id, label_id):
    """Applies a label to an email (and theoretically removes INBOX to archive)."""
    body = {
        'addLabelIds': [label_id],
        'removeLabelIds': ['INBOX']
    }
    service.users().messages().modify(userId='me', id=email_id, body=body).execute()
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory import load_profile, update_profile
from langchain_core.tools import tool
@tool
def update_memory(key: str, value: str) -> str:
    """Use this tool to save a new preference or fact about the user.
    For example, if the user mentions they hate newsletters, save key='hates_newsletters', value='true'.
    """
    update_profile(key, value)
    return "Memory successfully updated."
def send_email(service, to_email, subject, body_text):
    """Sends an email using the Gmail API, strictly enforcing the allowlist."""
    allowed_recipients = get_allowed_recipients()
    
    if to_email not in allowed_recipients:
        print(f"❌ SECURITY BLOCK: Cannot send email to {to_email}. Not in GCS allowlist.")
        return None

    import email_backend
    if email_backend.using_app_password():
        print(f"Sending email to {to_email} via SMTP...")
        try:
            email_backend.smtp_send(to_email, subject, body_text)
            print("✅ Email sent (SMTP).")
            return {'id': 'smtp-sent'}
        except Exception as e:
            print(f"❌ SMTP send failed: {e}")
            return None

    print(f"Sending email to {to_email}...")
    message = EmailMessage()
    message.set_content(body_text)
    message['To'] = to_email
    message['From'] = 'me'
    message['Subject'] = subject
    # base64url encode the message bytes
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    create_message = {'raw': encoded_message}
    try:
        sent_message = service.users().messages().send(userId="me", body=create_message).execute()
        print(f"✅ Email sent successfully! (Message ID: {sent_message['id']})")
        return sent_message
    except HttpError as error:
        print(f"❌ An error occurred sending the email: {error}")
        return None
@tool
def draft_email_reply(search_query: str, body: str) -> str:
    """Write a reply to an email as a DRAFT in the user's Gmail — nothing is sent;
    he reviews and hits send himself. Use when he asks you to draft/answer/respond
    to an email (often one you flagged as a reply owed).

    Args:
        search_query: Gmail search to find the email being replied to
            (e.g. 'from:rohan trip plans', 'subject:"apartment listings"').
        body: The reply text, written in the user's voice — concise, no signature.
    """
    import email_backend
    if email_backend.using_app_password():
        # IMAP mode: search the inbox loosely and draft against the best match.
        matches = [e for e in email_backend.imap_fetch_recent(30)
                   if any(t.lower() in (e['subject'] + e['sender']).lower()
                          for t in search_query.split())]
        if not matches:
            return f"Couldn't find an email matching {search_query!r}."
        m = matches[0]
        subj = m['subject'] if m['subject'].lower().startswith('re:') else f"Re: {m['subject']}"
        ok = email_backend.imap_create_draft(m['sender'], subj, body,
                                             in_reply_to=m.get('message_id'))
        return (f"Draft reply to {m['sender']} saved to your Drafts folder for review."
                if ok else "Couldn't save the draft to your Drafts folder.")

    service = get_gmail_service()
    if not service:
        return "Gmail isn't available right now."

    found = service.users().messages().list(
        userId='me', q=search_query, maxResults=1).execute().get('messages', [])
    if not found:
        return f"Couldn't find an email matching {search_query!r}."

    msg = service.users().messages().get(
        userId='me', id=found[0]['id'], format='metadata',
        metadataHeaders=['From', 'Subject', 'Message-ID']).execute()
    headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
    sender = headers.get('From', '')
    subject = headers.get('Subject', '')
    message_id = headers.get('Message-ID', '')

    reply = EmailMessage()
    reply.set_content(body)
    reply['To'] = sender
    reply['Subject'] = subject if subject.lower().startswith('re:') else f"Re: {subject}"
    if message_id:  # keeps Gmail threading the reply correctly
        reply['In-Reply-To'] = message_id
        reply['References'] = message_id

    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    service.users().drafts().create(
        userId='me',
        body={'message': {'raw': raw, 'threadId': msg.get('threadId')}}).execute()
    return (f"Draft reply to {sender} created — it's sitting in Gmail Drafts "
            f"for review. Nothing was sent.")


def user_has_replied(subject, since_date=None):
    """Gmail-API only: did the user SEND mail on this subject's thread (optionally since
    since_date YYYY-MM-DD)? Used to auto-resolve reply_owed commitments. Returns False on
    the IMAP/app-password path (no easy sent-thread search) or if Gmail is unavailable."""
    import email_backend
    if email_backend.using_app_password():
        return False
    service = get_gmail_service()
    if not service:
        return False
    core = subject.split(': ', 1)[-1].strip()
    if not core:
        return False
    query = f'in:sent subject:"{core}"'
    if since_date:
        query += f' after:{since_date.replace("-", "/")}'
    try:
        res = service.users().messages().list(userId='me', q=query, maxResults=1).execute()
        return bool(res.get('messages'))
    except Exception as e:
        print(f"user_has_replied check failed: {e}")
        return False


def run_email_summary():
    """Fetches and triages recent emails (Gmail API, or IMAP in app-password mode)."""
    print("Running Pilot Skill: Email Summary...")
    import email_backend
    app_pw = email_backend.using_app_password()
    service = get_gmail_service()
    if not service and not app_pw:
        print("Failed to start Gmail service. Check credentials.json.")
        return None, None

    emails = fetch_recent_emails(service)
    if not emails:
        return [], []
    # Same deterministic pre-filter as the engine digest: don't pay the LLM to
    # re-classify bulk mail (newsletters/marketing) — it's deterministically "cleaned up".
    from email_filter import is_bulk
    bulk = [e for e in emails if is_bulk(e)]
    emails = [e for e in emails if not is_bulk(e)]
    if bulk:
        print(f"Pre-filtered {len(bulk)} bulk email(s) before LLM triage.")
    if not emails:
        return [], []
    # Get the LLM from our unified router
    print("Initializing LLM Router...")
    from llm_router import get_llm
    try:
        llm = get_llm(temperature=0)
    except Exception as e:
        print(f"Failed to intialize LLM. Error: {e}")
        return [], []
        
    # Load User Profile (Memory Layer 1)
    profile_data = load_profile()
    profile_text = json.dumps(profile_data, indent=2) if profile_data else "No specific personal context available."
    prompt = PromptTemplate(
        input_variables=["email_data", "profile_data"],
        template='''You are an intelligent email assistant. Analyze the following emails.
        For each email, decide if it is "IMPORTANT", "NEWS", or "JUNK/PROMOTION".
        
        Here is what you know about the user:
        {profile_data}
        
        Use this knowledge to determine what is truly important to them.
        
        Emails:
        {email_data}
        
        Return ONLY a raw JSON array of objects with the following keys:
        - "id": the email id
        - "category": the category you chose
        - "summary": a 1-sentence summary of the email (if important/news. If junk, leave blank)
        - "action": "KEEP" or "DELETE"
        
        Do not output markdown code blocks. Just the raw JSON array.
        '''
    )
    formatted_emails = json.dumps(emails, indent=2)
    chain = prompt | llm
    
    response = chain.invoke({
        "email_data": formatted_emails,
        "profile_data": profile_text
    })
    
    try:
        # Clean potential markdown from output
        cleaned_response = response.content.strip().strip('```json').strip('```')
        classifications = json.loads(cleaned_response)

        # Auto-labelling needs the Gmail API; in IMAP/app-password mode we triage
        # and report but don't move messages (no label step).
        label_id = get_or_create_label(service, "To Be Deleted") if service else None

        print("\n--- Daily Email Summary ---\n")

        for item in classifications:
            email_info = next((e for e in emails if e['id'] == item['id']), None)
            if not email_info: continue

            if item['action'] == 'DELETE':
                print(f"[TRASHING] {email_info['subject']}")
                if label_id:
                    apply_label_to_email(service, item['id'], label_id)
            else:
                print(f"[{item['category']}] {email_info['sender']}")
                print(f"Subject: {email_info['subject']}")
                print(f"Summary: {item['summary']}\n")
                
        return classifications, emails
                
    except json.JSONDecodeError:
        print("Failed to parse LLM response. Raw output:")
        print(response.content)
        return [], []

