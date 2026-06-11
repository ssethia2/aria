"""One-shot: register Gmail push notifications to a Pub/Sub topic.

Tells Gmail to push INBOX changes for the Netflix account to your Pub/Sub topic so
aria_server.py receives instant webhooks. Edit PROJECT_ID / TOPIC_NAME below before
running, and grant gmail-api-push@system.gserviceaccount.com the Pub/Sub Publisher role.
"""
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def setup_gmail_push(project_id: str, topic_name: str):
    """Tells the Gmail API to start pushing notifications to a specific Pub/Sub Topic."""
    token_path = os.path.join(os.path.dirname(__file__), 'token_netflix.json')
    if not os.path.exists(token_path):
        print("Error: token_netflix.json not found! Authenticate first.")
        return

    creds = Credentials.from_authorized_user_file(token_path)
    service = build('gmail', 'v1', credentials=creds)

    topic_path = f"projects/{project_id}/topics/{topic_name}"
    
    # We use labelIds=['INBOX'] to filter to just the Inbox
    request_body = {
        "labelIds": ["INBOX"],
        "topicName": topic_path
    }

    try:
        print(f"Instructing Gmail to push updates to {topic_path}...")
        response = service.users().watch(userId='me', body=request_body).execute()
        print("✅ Success! Gmail is now watching the inbox.")
        print(f"History ID: {response.get('historyId')}")
    except Exception as e:
        print(f"❌ Error setting up Gmail watch: {e}")
        print("\nMake sure you granted 'gmail-api-push@system.gserviceaccount.com' the 'Pub/Sub Publisher' role on your Topic!")

if __name__ == "__main__":
    # REPLACE THESE WITH YOUR ACTUAL CLOUD PROJECT ID AND TOPIC NAME
    PROJECT_ID = "YOUR_GOOGLE_CLOUD_PROJECT_ID"
    TOPIC_NAME = "aria-gmail-updates"
    
    if PROJECT_ID == "YOUR_GOOGLE_CLOUD_PROJECT_ID":
        print("Please edit setup_gmail_watch.py and put your actual Project ID in!")
    else:
        setup_gmail_push(PROJECT_ID, TOPIC_NAME)
